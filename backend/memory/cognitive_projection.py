"""认知提交中的长期记忆投影。

认知 outbox 已验证的操作在这里转换成 memory records。服务只拥有这一段
memory-side projection 编排，使用 storage 的同一把可重入锁，并在读取
records 前检查 clearGeneration；认知状态本身和 outbox 事务仍归
``agent.cognitive_commit``，不会在此复制。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from agent.cognitive_commit import CognitiveCommitError
from agent.memory_architecture import new_memory_record, version_memory_record


@dataclass(frozen=True)
class MemoryCognitiveProjectionPorts:
    """投影需要的持久化、代际和 typed update 窄端口。"""

    persistence_lock: Any
    clear_generation: Callable[[dict[str, Any] | None], int]
    read_memory: Callable[[dict[str, Any] | None], list[str]]
    read_records: Callable[[dict[str, Any] | None], list[Any]]
    write_records: Callable[..., list[str]]
    merge_typed: Callable[..., list[str]]


class MemoryCognitiveProjectionService:
    """把 validated memory operations 投影为可审计的版本化 records。"""

    def __init__(self, ports: MemoryCognitiveProjectionPorts) -> None:
        self.ports = ports

    def project_memory_records(
        self,
        payload: Mapping[str, Any],
        *,
        settings: dict[str, Any],
    ) -> list[str]:
        """Project validated user-memory corrections as versioned records.

        A correction names the old ``MemoryRecord``. The old row remains as an
        audit predecessor and a replacement points back to it; a retry keyed by
        the cognitive commit does not create another version.
        """

        source_turn_id = str(
            payload.get("sourceTurnId") or payload.get("commitId") or ""
        ).strip()[:180]
        operations = payload.get("operations")
        operations = operations if isinstance(operations, list) else []
        updates = payload.get("updates")
        updates = updates if isinstance(updates, list) else []
        with self.ports.persistence_lock:
            expected_clear_generation = payload.get("memoryClearGeneration")
            if (
                expected_clear_generation is not None
                and self.ports.clear_generation(settings) != expected_clear_generation
            ):
                return self.ports.read_memory(settings)
            # Read under the same lock as the eventual replacement. A
            # projector retry must never base a new version on a stale snapshot.
            records = list(self.ports.read_records(settings))
            changed = False
            structured_additions: list[dict[str, Any]] = []
            for raw_operation in operations:
                if not isinstance(raw_operation, Mapping):
                    continue
                operation = str(raw_operation.get("operation") or "").upper()
                if operation not in {
                    "ADD_USER_BELIEF",
                    "SUPERSEDE_USER_BELIEF",
                    "PROPOSE_MEMORY_CORRECTION",
                }:
                    continue
                if operation == "PROPOSE_MEMORY_CORRECTION":
                    # A proposal belongs in cognitive_state as ``proposed``.
                    # It reaches long-term memory only when explicit
                    # user-facing authorization is carried by the operation.
                    if not bool(raw_operation.get("authorized")):
                        continue
                    domain = str(raw_operation.get("domain") or "").strip().lower()
                    if domain not in {
                        "user", "user_fact", "user_preference", "user_intent",
                        "user_statement",
                    }:
                        continue
                content = str(raw_operation.get("content") or "").strip()
                source_span = str(raw_operation.get("sourceSpan") or "").strip()[:600]
                if operation == "ADD_USER_BELIEF":
                    if raw_operation.get("scope", "session") != "user":
                        continue
                    if not content or not source_span:
                        raise CognitiveCommitError(
                            "cognitive user belief requires claim and sourceSpan"
                        )
                    memory_id = str(raw_operation.get("memoryId") or "")
                    if memory_id:
                        if not any(item.memory_id == memory_id for item in records):
                            records.append(replace(new_memory_record(
                                content,
                                source_turn_ids=[source_turn_id],
                                source_span=source_span,
                                subject=str(raw_operation.get("subject") or "") or None,
                                predicate=str(raw_operation.get("predicate") or "") or None,
                                object_value=str(raw_operation.get("object") or "") or None,
                            ), memory_id=memory_id))
                            changed = True
                        continue
                    structured_additions.append({
                        "content": content,
                        "type": "user_memory",
                        "sourceSpan": source_span,
                        "sourceTurnIds": [source_turn_id] if source_turn_id else [],
                        "subject": str(raw_operation.get("subject") or "").strip(),
                        "predicate": str(raw_operation.get("predicate") or "").strip(),
                        "object": str(raw_operation.get("object") or "").strip(),
                        "polarity": str(raw_operation.get("polarity") or "positive").strip(),
                        "temporal": str(raw_operation.get("temporal") or "current").strip(),
                        "userPinned": bool(raw_operation.get("userPinned")),
                    })
                    continue
                target_id = str(raw_operation.get("targetMemoryId") or "").strip()
                if not target_id:
                    if (
                        operation == "SUPERSEDE_USER_BELIEF"
                        and raw_operation.get("scope", "session") == "session"
                    ):
                        continue
                    if operation == "PROPOSE_MEMORY_CORRECTION" and content and source_span:
                        structured_additions.append({
                            "content": content,
                            "type": "correction",
                            "modality": "corrected",
                            "sourceSpan": source_span,
                            "sourceTurnIds": [source_turn_id] if source_turn_id else [],
                            "userPinned": True,
                        })
                        continue
                    raise CognitiveCommitError(
                        "cognitive memory correction requires targetMemoryId"
                    )
                if not content:
                    raise CognitiveCommitError(
                        "cognitive memory correction requires content"
                    )
                lookup_target_id = (
                    target_id[len("memory:"):]
                    if target_id.startswith("memory:") else target_id
                )
                already_versioned = any(
                    item.previous_version_id == lookup_target_id
                    and source_turn_id
                    and source_turn_id in item.source_turn_ids
                    for item in records
                )
                if already_versioned:
                    continue
                existing = next(
                    (
                        item for item in records
                        if item.memory_id == lookup_target_id and item.active
                    ),
                    None,
                )
                if existing is None:
                    raise CognitiveCommitError(
                        f"cognitive memory correction target is not active: {target_id}"
                    )
                versioned = list(version_memory_record(
                    records,
                    lookup_target_id,
                    content,
                    source_turn_id=source_turn_id,
                ))
                replacement_index = next(
                    (
                        index for index, item in enumerate(versioned)
                        if item.previous_version_id == lookup_target_id
                    ),
                    None,
                )
                if replacement_index is not None and source_span:
                    replacement = versioned[replacement_index]
                    versioned[replacement_index] = replace(
                        replacement,
                        memory_id=str(raw_operation.get("memoryId") or replacement.memory_id),
                        source_span=source_span,
                        modality="corrected",
                    )
                records = versioned
                changed = True
            if changed:
                self.ports.write_records(records, settings=settings)
            all_updates = [*updates, *structured_additions]
            if all_updates:
                # This path retains typed candidates and their source spans.
                # The commit id is the source key, so replays confirm an
                # existing row instead of adding an unlinked ordinary string.
                return self.ports.merge_typed(
                    all_updates,
                    user_message=str(payload.get("userMessage") or ""),
                    settings=settings,
                    source_turn_id=source_turn_id,
                    update_types=(
                        payload.get("updateTypes")
                        if isinstance(payload.get("updateTypes"), Mapping) else {}
                    ),
                )
        return [item.content for item in records if item.active and item.content.strip()]
