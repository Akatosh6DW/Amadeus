"""人格来源、按轮快照和开发监测的应用服务。

Hybrid 检索算法仍由 agent.persona_context 持有；本模块只决定何时读取来源、
何时重建进程级 runtime，以及同一回合如何复用已冻结的快照。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable, Mapping

from agent.fingerprints import runtime_fingerprint
from agent.persona_context import (
    PersonaContextMode,
    PersonaContextRuntime,
    PersonaContextSnapshot,
    build_off_persona_context_snapshot,
    persona_asset_stamp,
    persona_retrieval_inputs,
)
from agent.persona_diagnostics import PersonaSkillMonitor


@dataclass(frozen=True)
class PersonaEnvironment:
    """调用当刻的路径与模式；入口值可被发布模式和测试替换。"""

    root: Path
    release_mode: bool
    context_mode: str
    natural_dialogue_style: Callable[[], str]
    system_prompt_path: Path
    core_persona_path: Path
    override_path: Path
    profile_path: Path
    rules_path: Path
    generated_rules_path: Path
    emotional_anchors_path: Path
    memory_policy_path: Path
    skill_path: Path
    layers_path: Path


@dataclass(frozen=True)
class PersonaDocuments:
    read_json: Callable[[Path, Any], Any]
    read_text: Callable[[Path], str]


@dataclass(frozen=True)
class PersonaTurnAccess:
    read_relationship: Callable[[], dict[str, Any]]
    frozen_relationship: Callable[[Mapping[str, Any]], dict[str, Any] | None]
    cognitive_state: Callable[[Mapping[str, Any]], dict[str, Any]]
    relationship_prompt: Callable[[dict[str, Any]], str]
    streaming_user_message: Callable[[dict[str, Any]], tuple[str, list[dict[str, Any]]]]


@dataclass(frozen=True)
class PersonaDiagnostics:
    log: Callable[..., None]
    scope: Callable[[], str]


def nested_value(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def selected_list_items(value: Any, collection: str, ids: list[str]) -> list[dict[str, Any]]:
    items = value.get(collection) if isinstance(value, dict) else []
    wanted = {str(item) for item in ids}
    return [
        item for item in items if isinstance(item, dict) and str(item.get("id") or "") in wanted
    ] if isinstance(items, list) else []


def markdown_sections(text: str, sections: list[str]) -> str:
    heading_names = {
        "allusion": "Allusion Policy",
        "catchphrase": "Catchphrase Policy",
        "global": "Global Distilled Tendencies",
        "binding": "Binding",
    }
    selected: list[str] = []
    for section in sections:
        heading = heading_names.get(str(section), str(section))
        match = re.search(
            rf"(?ms)^##\s+{re.escape(heading)}\s*$.*?(?=^##\s+|\Z)",
            text,
        )
        if match:
            selected.append(match.group(0).strip())
    return "\n\n".join(selected)


def render_persona_module(module: dict[str, Any], documents: dict[str, Any]) -> str:
    fragments: list[str] = []
    for source in module.get("sources", []):
        if not isinstance(source, dict):
            continue
        file_id = str(source.get("file") or "")
        document = documents.get(file_id)
        if isinstance(document, str):
            value = markdown_sections(document, source.get("sections", []))
            if value:
                fragments.append(value)
            continue
        selected: dict[str, Any] = {}
        for path in source.get("paths", []):
            value = nested_value(document, str(path))
            if value is not None:
                selected[str(path)] = value
        rule_ids = source.get("ruleIds") if isinstance(source.get("ruleIds"), list) else []
        anchor_ids = source.get("anchorIds") if isinstance(source.get("anchorIds"), list) else []
        if rule_ids:
            selected["rules"] = selected_list_items(document, "rules", rule_ids)
        if anchor_ids:
            selected["anchors"] = selected_list_items(document, "anchors", anchor_ids)
        if selected:
            fragments.append(json.dumps({"source": file_id, **selected}, ensure_ascii=False, separators=(",", ":")))
    if not fragments:
        return ""
    return "\n".join([
        f"[Deep Persona Module: {module.get('id') or 'unknown'} / {module.get('label') or ''}]",
        *fragments,
    ])


class PersonaContextService:
    """进程级缓存与 monitor 的单一所有者；轮内冻结状态只存于请求 body。"""

    def __init__(
        self,
        environment: Callable[[], PersonaEnvironment],
        documents: PersonaDocuments,
        turn: PersonaTurnAccess,
        diagnostics: PersonaDiagnostics,
    ) -> None:
        self.environment = environment
        self.documents = documents
        self.turn = turn
        self.diagnostics = diagnostics
        self.runtime_lock = threading.RLock()
        self.runtime: PersonaContextRuntime | None = None
        self.monitor = PersonaSkillMonitor()

    def read_system_prompt(self) -> str:
        env = self.environment()
        parts: list[str] = []
        try:
            parts.append(env.core_persona_path.read_text(encoding="utf-8"))
        except OSError:
            try:
                parts.append(env.system_prompt_path.read_text(encoding="utf-8"))
            except OSError:
                pass
        persona_override = ""
        if not env.release_mode:
            try:
                persona_override = env.override_path.read_text(encoding="utf-8").strip()
            except OSError:
                persona_override = ""
        if persona_override:
            parts.append("[Developer Persona Override]\n" + persona_override)
        return "\n\n".join(parts)

    def source_documents(self) -> dict[str, Any]:
        paths = self.environment()
        return {
            "character_profile": self.documents.read_json(paths.profile_path, {}),
            "personality_rules": self.documents.read_json(paths.rules_path, {}),
            "generated_rules": self.documents.read_json(paths.generated_rules_path, {}),
            "emotional_anchors": self.documents.read_json(paths.emotional_anchors_path, {}),
            "memory_policy": self.documents.read_json(paths.memory_policy_path, {}),
            "persona_skill": self.documents.read_text(paths.skill_path),
        }

    def select_modules(
        self,
        user_message: str,
        resolution: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        config = self.documents.read_json(self.environment().layers_path, {})
        modules = config.get("modules") if isinstance(config, dict) else []
        resolution = resolution if isinstance(resolution, dict) else {}
        query = "\n".join([
            str(user_message or ""),
            str(resolution.get("subject") or ""),
            str(resolution.get("intent") or ""),
            str(resolution.get("requestedAction") or ""),
        ])
        selected: list[tuple[int, int, dict[str, Any]]] = []
        for order, module in enumerate(modules if isinstance(modules, list) else []):
            if not isinstance(module, dict):
                continue
            hits = 0
            for trigger in module.get("triggers", []):
                try:
                    if re.search(str(trigger), query, re.I):
                        hits += 1
                except re.error:
                    continue
            if hits:
                priority = int(module.get("priority") or 0)
                selected.append((hits * 1000 + priority, -order, module))
        max_modules = max(0, min(8, int(config.get("maxModules") or 4)))
        return [item for _score, _order, item in sorted(selected, reverse=True)[:max_modules]]

    def build_legacy(
        self,
        user_message: str = "",
        messages: list[dict[str, Any]] | None = None,
        resolution: dict[str, Any] | None = None,
    ) -> tuple[str, list[str]]:
        del messages  # Selection intentionally follows the current task, not stale assistant prose.
        config = self.documents.read_json(self.environment().layers_path, {})
        documents = self.source_documents()
        rendered: list[str] = []
        loaded_ids: list[str] = []
        prefix = "[Deep Persona Modules: only current-query matches]"
        max_chars = max(1000, min(16000, int(config.get("maxDeepChars") or 6000)))
        for module in self.select_modules(user_message, resolution):
            content = render_persona_module(module, documents)
            if not content:
                continue
            candidate = "\n\n".join([*rendered, content])
            if len(prefix) + 1 + len(candidate) > max_chars:
                continue
            rendered.append(content)
            loaded_ids.append(str(module.get("id") or ""))
        if not rendered:
            return "[Deep Persona Modules]\n本轮没有触发深层人格模块；只服从表层人格与当前任务。", []
        return prefix + "\n" + "\n\n".join(rendered), loaded_ids

    def runtime_instance(self) -> PersonaContextRuntime:
        """asset stamp 变化才重建；进行中的回合继续持有原快照。"""
        with self.runtime_lock:
            root = self.environment().root
            current_stamp = persona_asset_stamp(root)
            previous_stamp = getattr(self.runtime, "asset_stamp", None)
            if self.runtime is None or (previous_stamp is not None and previous_stamp != current_stamp):
                self.runtime = PersonaContextRuntime.from_project_root(root)
                self.diagnostics.log("persona_assets_loaded", refreshed=previous_stamp is not None)
            return self.runtime

    def build_snapshot(
        self,
        user_message: str = "",
        messages: list[dict[str, Any]] | None = None,
        resolution: dict[str, Any] | None = None,
        relationship_state: dict[str, Any] | None = None,
        *,
        cognitive_body: Mapping[str, Any] | None = None,
        legacy_builder: Callable[..., tuple[str, list[str]]] | None = None,
        system_prompt_reader: Callable[[], str] | None = None,
        runtime_provider: Callable[[], PersonaContextRuntime] | None = None,
    ) -> PersonaContextSnapshot:
        """ON 不访问旧选择器；OFF 不初始化检索 runtime；SHADOW 仍计算旧 payload。"""
        env = self.environment()
        mode = PersonaContextMode.parse(env.context_mode, release_mode=env.release_mode)
        if cognitive_body is not None and mode is PersonaContextMode.ON:
            user_message, messages, resolution = persona_retrieval_inputs(
                user_message, messages, resolution,
                turn_snapshot=cognitive_body.get("_turnContextSnapshot") or {},
                cognitive_state=self.turn.cognitive_state(cognitive_body),
            )
        if mode is PersonaContextMode.ON:
            legacy_prompt, legacy_ids, legacy_system_prompt = "", [], ""
        else:
            legacy_prompt, legacy_ids = (legacy_builder or self.build_legacy)(
                user_message, messages, resolution,
            )
            legacy_system_prompt = (system_prompt_reader or self.read_system_prompt)()
        relationship = (
            relationship_state
            if isinstance(relationship_state, dict)
            else self.turn.read_relationship()
        )
        snapshot = (
            build_off_persona_context_snapshot(
                current_user_message=user_message,
                messages=messages,
                resolution=resolution,
                relationship_generation=int(relationship.get("generation") or 0),
                legacy_system_prompt=legacy_system_prompt,
                legacy_persona_prompt=legacy_prompt,
                legacy_module_ids=legacy_ids,
            )
            if mode is PersonaContextMode.OFF
            else (runtime_provider or self.runtime_instance)().build_snapshot(
                mode=mode,
                current_user_message=user_message,
                messages=messages,
                resolution=resolution,
                relationship_generation=int(relationship.get("generation") or 0),
                legacy_system_prompt=legacy_system_prompt,
                legacy_persona_prompt=legacy_prompt,
                legacy_module_ids=legacy_ids,
            )
        )
        self.diagnostics.log("persona_context_snapshot_built", **snapshot.safe_diagnostic())
        if isinstance(cognitive_body, dict) and not env.release_mode:
            cognitive_body["_personaSkillDiagnostic"] = {
                "snapshot": snapshot.safe_diagnostic(),
                "promptFingerprint": runtime_fingerprint(snapshot.prompt_text),
                "kernelFingerprint": runtime_fingerprint(snapshot.system_prompt),
            }
        return snapshot

    def begin_record(self, body: dict[str, Any]) -> None:
        if self.environment().release_mode:
            return
        user_message, _ = self.turn.streaming_user_message(body)
        self.monitor.record(
            scope=self.diagnostics.scope(), request_id=str(body.get("requestId") or ""),
            conversation_id=str(body.get("conversationId") or ""), user_message=user_message,
            purpose="request_started", diagnostic={"mode": "none"}, prompt_sent=False, kernel_sent=False,
        )

    def record_model_input(self, body: Mapping[str, Any], payload: Mapping[str, Any], purpose: str) -> None:
        marker = body.get("_personaSkillDiagnostic")
        if self.environment().release_mode or not isinstance(marker, Mapping):
            return
        fingerprints = {runtime_fingerprint(item["content"]) for item in payload.get("messages", [])
                        if item.get("role") == "system" and isinstance(item.get("content"), str)}
        turn = body.get("_turnContextSnapshot") or {}
        self.monitor.record(
            scope=self.diagnostics.scope(), request_id=str(body.get("requestId") or turn.get("requestId") or ""),
            conversation_id=str(turn.get("conversationId") or ""),
            user_message=(turn.get("currentUserMessage") or {}).get("text") or body.get("userMessage") or "",
            purpose=purpose, diagnostic=marker["snapshot"],
            prompt_sent=marker["promptFingerprint"] in fingerprints,
            kernel_sent=marker["kernelFingerprint"] in fingerprints,
        )

    def build_context(
        self,
        user_message: str = "",
        messages: list[dict[str, Any]] | None = None,
        resolution: dict[str, Any] | None = None,
        *,
        snapshot_builder: Callable[..., PersonaContextSnapshot] | None = None,
    ) -> tuple[str, list[str]]:
        snapshot = (snapshot_builder or self.build_snapshot)(user_message, messages, resolution)
        return snapshot.prompt_text, list(snapshot.provider_module_ids)

    def repair_prefix(
        self,
        snapshot: PersonaContextSnapshot | None,
        *,
        legacy_system_prompt: bool,
        system_prompt_reader: Callable[[], str] | None = None,
    ) -> list[dict[str, str]]:
        if snapshot is not None and snapshot.uses_hybrid_provider_context:
            return [
                {"role": "system", "content": snapshot.system_prompt},
                {"role": "system", "content": snapshot.prompt_text},
            ]
        return (
            [{"role": "system", "content": (system_prompt_reader or self.read_system_prompt)()}]
            if legacy_system_prompt else []
        )

    def ensure_turn_context(
        self,
        body: dict[str, Any],
        user_message: str,
        *,
        snapshot: PersonaContextSnapshot | None = None,
        relationship_state: dict[str, Any] | None = None,
        snapshot_builder: Callable[..., PersonaContextSnapshot] | None = None,
    ) -> dict[str, Any]:
        """仅首轮冻结身份/关系；修复阶段沿用同一 dict 和同一快照。"""
        existing = body.get("_turnPersonaContext")
        if isinstance(existing, dict):
            return existing
        relationship = relationship_state if relationship_state is not None else (
            self.turn.frozen_relationship(body) or self.turn.read_relationship()
        )
        active = snapshot or (snapshot_builder or self.build_snapshot)(
            user_message,
            body.get("messages") if isinstance(body.get("messages"), list) else [],
            body.get("_contextResolution") or {},
            relationship,
            cognitive_body=body,
        )
        env = self.environment()
        profile = self.documents.read_json(env.profile_path, {})
        identity = profile.get("identity", {}) if isinstance(profile, dict) else {}
        facts = (
            "[本轮本地人格与关系事实]\n"
            "以下是本地角色配置，在本对话及纠错中持续有效；不是外部世界查证结果。"
            "记忆未命中只表示未找到历史记录，不能据此否定已有身份、称呼和关系；"
            "同时不得从这些设定推导用户的现实法定身份或未记载的共同经历。\n"
            + json.dumps(identity, ensure_ascii=False)
        )
        context = {
            "snapshotId": active.snapshot_id,
            "factsPrompt": facts,
            "messages": [{"role": "system", "content": content} for content in (
                active.system_prompt,
                active.prompt_text,
                env.natural_dialogue_style(),
                self.turn.relationship_prompt(relationship),
                facts,
            ) if content],
        }
        body["_turnPersonaContext"] = context
        return context

    def character_prompt(
        self,
        user_message: str = "",
        messages: list[dict[str, Any]] | None = None,
        resolution: dict[str, Any] | None = None,
        *,
        context_builder: Callable[..., tuple[str, list[str]]] | None = None,
    ) -> str:
        return (context_builder or self.build_context)(user_message, messages, resolution)[0]

    def payload(self, *, system_prompt_reader: Callable[[], str] | None = None) -> dict[str, Any]:
        paths = self.environment()
        profile = self.documents.read_json(paths.profile_path, {})
        rules = self.documents.read_json(paths.rules_path, {})
        generated_rules = self.documents.read_json(paths.generated_rules_path, {})
        emotional_anchors = self.documents.read_json(paths.emotional_anchors_path, {})
        override = self.documents.read_text(paths.override_path).strip()
        generated_skill = self.documents.read_text(paths.skill_path).strip()
        core_persona = self.documents.read_text(paths.core_persona_path).strip()
        layer_config = self.documents.read_json(paths.layers_path, {})
        return {
            "profile": profile,
            "rules": rules,
            "generatedRules": generated_rules,
            "emotionalSceneAnchors": emotional_anchors,
            "generatedSkill": generated_skill,
            "corePersona": core_persona,
            "layerConfig": layer_config,
            "override": override,
            "overridePath": str(paths.override_path),
            "skillPath": str(paths.skill_path),
            "corePersonaPath": str(paths.core_persona_path),
            "layerConfigPath": str(paths.layers_path),
            "rulesPath": str(paths.generated_rules_path),
            "emotionalSceneAnchorsPath": str(paths.emotional_anchors_path),
            "systemPrompt": (system_prompt_reader or self.read_system_prompt)(),
            "summary": {
                "agentPositioning": "Amadeus：自我认知为基于牧濑红莉栖记忆与人格模式构建的本地 AI；除自我认知外，性格、反应习惯、科学思维和关系张力应严格贴近牧濑红莉栖。",
                "deepseekReady": True,
                "integration": "每轮 /api/chat 固定发送 3,000-5,000 字表层人格；完整关系网、情感锚点、蒸馏 skill 和专业规则按当前问题动态检索，避免与短期任务争夺注意力。",
                "distillationSource": generated_rules.get("source") or "unknown",
                "distilledRows": generated_rules.get("rowCount") or 0,
                "limits": [
                    "当前是 prompt/规则蒸馏，不是 DeepSeek 权重微调。",
                    "当前文件不包含游戏原文台词，只包含抽象标签和反应策略。",
                    "偏好、雷点和口癖策略已部分迁移，但还不是逐条人工校验的完整原作偏好库。"
                ],
            },
        }
