"""Per-turn Persona Kernel + Context Skill snapshot construction.

The snapshot is a value object: it freezes every selected capsule body and its
identity, and can be passed explicitly to primary generation and later repair
calls.  No context variable or mutable "current snapshot" exists here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping, Sequence

from agent.skill_retrieval import (
    AssetValidationError,
    Embedder,
    HybridSkillRetriever,
    LoadedSkill,
    load_shared_bge_embedder,
    RegistryStore,
    RetrievalPolicy,
    RetrievalResult,
    SkillCandidate,
    SkillRegistry,
    load_runtime_registry,
)
from agent.fingerprints import runtime_fingerprint
from agent.cognitive_context import candidate_context_ids, candidate_is_suppressed, effective_context_controls
from agent.turn_context import turn_messages


SNAPSHOT_SCHEMA_VERSION = 1


def persona_asset_stamp(root: Path) -> tuple:
    """Cheap change detection; full hashes are checked when a generation loads."""
    paths = [root / "prompts" / "persona-kernel.zh.txt", root / "configs" / "context_skill_policy.json",
             root / "configs" / "context_skill_retrieval_cases.jsonl"]
    paths.extend((root / "assets" / "context-skills").glob("*"))
    paths.extend(path for path in (root / "skills" / "persona").rglob("*")
                 if path.name in {"manifest.json", "SKILL.txt"})
    stamp = []
    for path in sorted(paths):
        try:
            stat = path.stat()
            stamp.append((str(path), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            stamp.append((str(path), None, None))
    return tuple(stamp)


class PersonaContextMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"

    @classmethod
    def parse(cls, value: str | None, *, release_mode: bool = False) -> "PersonaContextMode":
        # Launchers opt into ON or SHADOW explicitly.  An unspecified mode must
        # never load the embedding runtime merely to discard its result.
        default = cls.OFF
        normalized = str(value or "").strip().lower()
        try:
            return cls(normalized) if normalized else default
        except ValueError:
            return default


@dataclass(frozen=True)
class PersonaContextSnapshot:
    schema_version: int
    snapshot_id: str
    registry_generation: int
    relationship_generation: int
    query_hash: str
    mode: str
    selected_skills: tuple[LoadedSkill, ...]
    candidates: tuple[SkillCandidate, ...]
    system_prompt: str
    prompt_text: str
    hybrid_prompt_text: str
    kernel_fingerprint: str
    fallback_reason: str | None
    legacy_module_ids: tuple[str, ...]
    retrieval_context_source: str = ""

    @property
    def provider_module_ids(self) -> tuple[str, ...]:
        if self.mode == PersonaContextMode.ON.value:
            return tuple(item.skill_id for item in self.selected_skills)
        return self.legacy_module_ids

    @property
    def uses_hybrid_provider_context(self) -> bool:
        return self.mode == PersonaContextMode.ON.value

    def safe_diagnostic(self) -> dict[str, Any]:
        """Return telemetry with no raw query, system prompt, or capsule body."""

        return {
            "schemaVersion": self.schema_version,
            "snapshotId": self.snapshot_id,
            "registryGeneration": self.registry_generation,
            "relationshipGeneration": self.relationship_generation,
            "queryHash": self.query_hash,
            "mode": self.mode,
            "selectedSkills": [item.identity_dict() for item in self.selected_skills],
            "candidates": [item.diagnostic_dict() for item in self.candidates],
            "kernelFingerprint": self.kernel_fingerprint,
            "fallbackReason": self.fallback_reason or "",
            "legacyModuleIds": list(self.legacy_module_ids),
            "providerContext": "kernel_hybrid" if self.uses_hybrid_provider_context else "legacy",
            "retrievalContextSource": self.retrieval_context_source,
        }


def _normalized_excerpt(value: Any, max_chars: int = 600) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def persona_retrieval_inputs(
    current_user_message: str, messages: Sequence[Mapping[str, Any]] | None,
    resolution: Mapping[str, Any] | None, *, turn_snapshot: Mapping[str, Any],
    cognitive_state: Mapping[str, Any],
) -> tuple[str, Sequence[Mapping[str, Any]], dict[str, Any]]:
    """Adapt explicit reply/model selections without reviving lexical routing."""
    adapted = dict(resolution or {})
    anchor = turn_snapshot.get("resolvedAnchor") or {}
    selected = [dict(item) for item in anchor.get("messages", []) if isinstance(item, Mapping)]
    if selected:
        if anchor.get("selectedText"):
            target_ids = set(anchor.get("targetMessageIds") or [])
            selected = [{**item, "text": anchor["selectedText"]} for item in selected
                        if item.get("messageId") in target_ids]
        normalized = [{**item, "content": str(item.get("text") or item.get("content") or "")}
                      for item in selected]
        return current_user_message, normalized, {"isContinuation": True, "_personaContextSource": "explicit_reply"}

    controls = effective_context_controls(cognitive_state)
    if controls["recallQuery"]:
        return controls["recallQuery"], (), {"isContinuation": False, "_personaContextSource": "model_rewrite"}
    preferred = set(controls["preferredIds"])
    context = turn_snapshot.get("_contextState") or {}
    selected = []
    for turn in context.get("recentTurns", []):
        if not isinstance(turn, Mapping) or candidate_is_suppressed(turn, cognitive_state):
            continue
        if turn.get("conversationId") and turn["conversationId"] != turn_snapshot.get("conversationId"):
            continue
        for item in turn_messages(turn):
            if preferred.intersection(candidate_context_ids(item)) and not candidate_is_suppressed(item, cognitive_state):
                selected.append({**item, "content": item["text"]})
    if selected:
        return current_user_message, selected, {"isContinuation": True, "_personaContextSource": "model_selected"}
    # Visible preceding speech is a candidate for the conversation model, not
    # proof that an independent new user message continues that old scene.
    filtered = [item for item in (messages or []) if not candidate_is_suppressed(item, cognitive_state)]
    return current_user_message, filtered, {**adapted, "_personaContextSource": "current_message"}


def build_retrieval_query(
    current_user_message: str,
    messages: Sequence[Mapping[str, Any]] | None = None,
    resolution: Mapping[str, Any] | None = None,
) -> str:
    """Build a current-turn-first semantic query.

    Recent dialogue is included only when the shared context resolver has
    established that the message is a continuation.  A complete short social
    turn must not inherit a previous technical topic and select the wrong
    Persona Skill merely because it arrived soon afterwards.
    """

    current = _normalized_excerpt(current_user_message, 900)
    active_resolution = resolution if isinstance(resolution, Mapping) else {}
    is_continuation = bool(active_resolution.get("isContinuation"))
    if not is_continuation:
        return "current_user: " + current if current else ""

    recent_users: list[str] = []
    recent_assistant = ""
    for item in reversed(tuple(messages or ())):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "")
        content = _normalized_excerpt(item.get("content"), 500)
        if not content or content == current:
            continue
        if role == "user" and len(recent_users) < 2:
            recent_users.append(content)
        elif role == "assistant" and not recent_assistant:
            recent_assistant = content
        if len(recent_users) >= 2 and recent_assistant:
            break
    parts: list[str] = []
    for prior in reversed(recent_users):
        parts.append("previous_user: " + prior)
    if recent_assistant:
        parts.append("assistant_context: " + recent_assistant)
    resolved_question = _normalized_excerpt(active_resolution.get("resolvedQuestion"), 700)
    subject = _normalized_excerpt(active_resolution.get("subject"), 240)
    if resolved_question and resolved_question != current:
        parts.append("resolved_question: " + resolved_question)
    elif subject and subject not in current:
        parts.append("resolved_subject: " + subject)
    if current:
        parts.append("current_user: " + current)
    return "\n".join(parts)


def _hybrid_prompt(selected: Sequence[LoadedSkill]) -> str:
    if not selected:
        return (
            "[Persona Context Skills]\n"
            "本轮没有通过高置信本地检索门槛的人格 Skill；只依据 Persona Kernel、"
            "当前对话、关系上下文与任务合同回答。"
        )
    parts = ["[Persona Context Skill: frozen for this turn]"]
    if len(selected) > 1:
        parts.append(
            "以下场景指引可以共同使用，各自只作用于本轮符合其适用条件的部分。"
            "结合当前诉求自然组织成一次回复，不逐份表演或重复反应。"
            "若指引之间有张力，依据当前情境与用户明确要求决定表达轻重，"
            "并始终保持基础人格、身份事实和关系边界。"
        )
    parts.extend(item.body.strip() for item in selected)
    return "\n\n".join(parts)


def _snapshot_id(
    *,
    registry_generation: int,
    relationship_generation: int,
    query_hash: str,
    mode: str,
    kernel_fingerprint: str,
    selected: Sequence[LoadedSkill],
    legacy_module_ids: Sequence[str],
    system_prompt: str,
    prompt_text: str,
) -> str:
    identity = {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "registryGeneration": int(registry_generation),
        "relationshipGeneration": int(relationship_generation),
        "queryHash": query_hash,
        "mode": mode,
        "kernelFingerprint": kernel_fingerprint,
        "selected": [
            {
                "id": item.skill_id,
                "version": item.version,
                "sha256": item.capsule_sha256,
            }
            for item in selected
        ],
        "legacyModuleIds": list(legacy_module_ids),
        "systemPromptFingerprint": runtime_fingerprint(system_prompt, length=32),
        "promptTextFingerprint": runtime_fingerprint(prompt_text, length=32),
    }
    serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "pcs_" + runtime_fingerprint(serialized, length=24)


def build_off_persona_context_snapshot(
    *,
    current_user_message: str,
    messages: Sequence[Mapping[str, Any]] | None,
    resolution: Mapping[str, Any] | None,
    relationship_generation: int,
    legacy_system_prompt: str,
    legacy_persona_prompt: str,
    legacy_module_ids: Sequence[str],
) -> PersonaContextSnapshot:
    """Build the legacy/off snapshot without touching Kernel or ONNX assets."""

    query = build_retrieval_query(current_user_message, messages, resolution)
    query_hash = runtime_fingerprint(query, length=32)
    system_prompt = str(legacy_system_prompt or "")
    prompt_text = str(legacy_persona_prompt or "")
    legacy_ids = tuple(str(item) for item in legacy_module_ids if str(item))
    snapshot_id = _snapshot_id(
        registry_generation=0,
        relationship_generation=int(relationship_generation),
        query_hash=query_hash,
        mode=PersonaContextMode.OFF.value,
        kernel_fingerprint="",
        selected=(),
        legacy_module_ids=legacy_ids,
        system_prompt=system_prompt,
        prompt_text=prompt_text,
    )
    return PersonaContextSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=snapshot_id,
        registry_generation=0,
        relationship_generation=int(relationship_generation),
        query_hash=query_hash,
        mode=PersonaContextMode.OFF.value,
        selected_skills=(),
        candidates=(),
        system_prompt=system_prompt,
        prompt_text=prompt_text,
        hybrid_prompt_text="",
        kernel_fingerprint="",
        fallback_reason=None,
        legacy_module_ids=legacy_ids,
    )


class PersonaContextRuntime:
    """Immutable-registry runtime; each call produces a standalone snapshot."""

    def __init__(
        self,
        *,
        kernel_body: str,
        registry_store: RegistryStore | None = None,
        policy: RetrievalPolicy | None = None,
        embedder: Embedder | None = None,
        asset_failure: str | None = None,
    ) -> None:
        body = str(kernel_body or "").strip()
        if not body:
            raise AssetValidationError("persona_kernel_missing")
        self._kernel_body = body
        self._kernel_fingerprint = runtime_fingerprint(body, length=32)
        self._registry_store = registry_store or RegistryStore()
        self._policy = policy
        self._retriever = HybridSkillRetriever(embedder)
        self._asset_failure = str(asset_failure or "") or None

    @property
    def kernel_fingerprint(self) -> str:
        return self._kernel_fingerprint

    @classmethod
    def from_project_root(cls, root: Path) -> "PersonaContextRuntime":
        kernel_path = root / "prompts" / "persona-kernel.zh.txt"
        try:
            kernel_body = kernel_path.read_text(encoding="utf-8")
        except OSError as error:
            raise AssetValidationError("persona_kernel_missing") from error
        policy: RetrievalPolicy | None = None
        embedder: Embedder | None = None
        registry: SkillRegistry | None = None
        failure: str | None = None
        try:
            policy_path = root / "configs" / "context_skill_policy.json"
            raw_policy = json.loads(policy_path.read_text(encoding="utf-8"))
            if not isinstance(raw_policy, dict):
                raise AssetValidationError("policy_invalid_shape")
            policy = RetrievalPolicy.from_dict(raw_policy)
            assets = raw_policy.get("assets") if isinstance(raw_policy.get("assets"), dict) else {}

            def local_path(name: str, default: str) -> Path:
                raw = str(assets.get(name) or default)
                candidate = (root / raw).resolve()
                try:
                    candidate.relative_to(root.resolve())
                except ValueError as error:
                    raise AssetValidationError(f"asset_path_outside_project:{name}") from error
                return candidate

            embedding_root = local_path(
                "embeddingRoot",
                "assets/context-embedding/bge-small-zh-v1.5",
            )
            embedder = load_shared_bge_embedder(embedding_root)
            registry_paths = {
                "skills_root": (root / "skills" / "persona").resolve(),
                "index_path": local_path("index", "assets/context-skills/persona-index.v2.json"),
                "embeddings_path": local_path("embeddings", "assets/context-skills/persona-embeddings.v2.npy"),
            }
            try:
                registry = load_runtime_registry(**registry_paths, policy=policy,
                    model_artifact_digest=embedder.artifact_digest)
            except AssetValidationError as error:
                if str(error) not in {"index_source_digest_mismatch", "policy_source_digest_mismatch",
                                      "policy_index_build_mismatch"}:
                    raise
                # Source edits need refreshed assets, not permanent loss of all
                # skills. This is local maintenance, never an extra chat call.
                from tools.build_context_skill_index import build_calibrated_index
                build_calibrated_index(root)
                policy = RetrievalPolicy.from_dict(json.loads(policy_path.read_text(encoding="utf-8")))
                registry = load_runtime_registry(**registry_paths, policy=policy,
                    model_artifact_digest=embedder.artifact_digest)
        except (
            OSError,
            UnicodeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            AssetValidationError,
        ) as error:
            failure = str(error) or type(error).__name__
        store = RegistryStore(registry)
        runtime = cls(
            kernel_body=kernel_body,
            registry_store=store,
            policy=policy,
            embedder=embedder,
            asset_failure=failure,
        )
        runtime.asset_stamp = persona_asset_stamp(root)
        return runtime
    def build_snapshot(
        self,
        *,
        mode: PersonaContextMode | str,
        current_user_message: str,
        messages: Sequence[Mapping[str, Any]] | None,
        resolution: Mapping[str, Any] | None,
        relationship_generation: int,
        legacy_system_prompt: str,
        legacy_persona_prompt: str,
        legacy_module_ids: Sequence[str],
        system_prompt_suffix: str = "",
    ) -> PersonaContextSnapshot:
        active_mode = mode if isinstance(mode, PersonaContextMode) else PersonaContextMode.parse(str(mode))
        query = build_retrieval_query(current_user_message, messages, resolution)
        query_hash = runtime_fingerprint(query, length=32)
        registry = self._registry_store.snapshot()
        registry_generation = int(registry.generation) if registry is not None else 0
        retrieval = RetrievalResult((), (), self._asset_failure or "retrieval_not_run")
        if active_mode is not PersonaContextMode.OFF:
            if self._asset_failure:
                retrieval = RetrievalResult((), (), self._asset_failure)
            else:
                retrieval = self._retriever.retrieve(query, registry, self._policy)
        hybrid_prompt = _hybrid_prompt(retrieval.selected)
        if active_mode is PersonaContextMode.ON:
            system_prompt = self._kernel_body
            if system_prompt_suffix.strip():
                system_prompt += "\n\n" + system_prompt_suffix.strip()
            prompt_text = hybrid_prompt
        else:
            # Shadow is provider-payload identical to off by construction.
            system_prompt = str(legacy_system_prompt or "")
            prompt_text = str(legacy_persona_prompt or "")
        fallback_reason = (
            None if active_mode is PersonaContextMode.OFF else retrieval.fallback_reason
        )
        legacy_ids = tuple(str(item) for item in legacy_module_ids if str(item))
        kernel_fingerprint = (
            runtime_fingerprint(system_prompt, length=32)
            if active_mode is PersonaContextMode.ON else self._kernel_fingerprint
        )
        snapshot_id = _snapshot_id(
            registry_generation=registry_generation,
            relationship_generation=int(relationship_generation),
            query_hash=query_hash,
            mode=active_mode.value,
            kernel_fingerprint=kernel_fingerprint,
            selected=retrieval.selected,
            legacy_module_ids=legacy_ids,
            system_prompt=system_prompt,
            prompt_text=prompt_text,
        )
        return PersonaContextSnapshot(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            registry_generation=registry_generation,
            relationship_generation=int(relationship_generation),
            query_hash=query_hash,
            mode=active_mode.value,
            selected_skills=tuple(retrieval.selected),
            candidates=tuple(retrieval.candidates),
            system_prompt=system_prompt,
            prompt_text=prompt_text,
            hybrid_prompt_text=hybrid_prompt,
            kernel_fingerprint=kernel_fingerprint,
            fallback_reason=fallback_reason,
            legacy_module_ids=legacy_ids,
            retrieval_context_source=str((resolution or {}).get("_personaContextSource") or ""),
        )
