"""把长期记忆 provenance 绑定到当前认知上下文。

绑定只修改本轮 body 的内部 overlay/evidence，并过滤已被认知状态撤回的
record。它不读取存储、不推进 generation，也不负责提交；records 的读取和
认知状态快照由上层上下文服务提供。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.memory_architecture import MemoryRecord


@dataclass(frozen=True)
class CognitiveMemoryBindingPorts:
    """当前认知状态的窄读取端口。"""

    current_state: Callable[[Mapping[str, Any]], dict[str, Any]]


class CognitiveMemoryBindingService:
    """拥有 memory records 到认知 beliefs/evidence 的本轮投影。"""

    def __init__(self, ports: CognitiveMemoryBindingPorts) -> None:
        self.ports = ports

    def bind(
        self,
        body: dict[str, Any],
        memories: list[str],
        records: list[MemoryRecord],
    ) -> tuple[list[str], list[MemoryRecord]]:
        """Expose stored provenance and exclude superseded record identities."""

        current = self.ports.current_state(body)
        if not current:
            return memories, records
        state = deepcopy(current)
        beliefs = state.setdefault("beliefs", [])
        by_memory = {
            str(item.get("memoryId")): item
            for item in beliefs
            if isinstance(item, Mapping) and item.get("memoryId")
        }
        blocked = {
            mid for mid, item in by_memory.items()
            if item.get("status") != "active"
        }
        for record in records:
            if record.memory_id not in by_memory and record.active and record.source_span:
                beliefs.append({
                    "beliefId": "memory:" + record.memory_id,
                    "memoryId": record.memory_id,
                    "domain": "user_fact",
                    "authority": "USER_STATEMENT",
                    "status": "active",
                    "claim": record.content,
                    "sourceSpan": record.source_span,
                    "sourceTurnIds": list(record.source_turn_ids),
                    "subject": record.subject,
                    "predicate": record.predicate,
                    "object": record.object_value,
                })
        if isinstance(body.get("_cognitiveOverlay"), dict):
            body["_cognitiveOverlay"]["state"] = state
        else:
            body["_cognitiveBase"] = state
        rejected_text = {
            record.content for record in records
            if record.memory_id in blocked
        }
        selected_contents = set(memories) - rejected_text
        memory_evidence: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.active and record.source_span and record.content in selected_contents:
                ref = "memory:" + record.memory_id
                entry = {
                    "ok": True,
                    "sourceType": "user_memory",
                    "authority": "USER_STATEMENT",
                    "body": record.content,
                    "sourceSpan": record.source_span,
                    "memoryId": record.memory_id,
                    "historical": True,
                    "observedAt": record.last_confirmed_at,
                    "sourceTurnIds": list(record.source_turn_ids),
                }
                memory_evidence[ref] = entry
                memory_evidence[record.memory_id] = entry
        body["_cognitiveEvidence"] = {
            **(body.get("_cognitiveEvidence") or {}),
            **memory_evidence,
        }
        return (
            [item for item in memories if item not in rejected_text],
            [record for record in records if record.memory_id not in blocked],
        )
