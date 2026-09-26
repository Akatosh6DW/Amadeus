"""认知工具执行状态与真实回执日志，保证回答失败前已保存副作用证据。

复用必须匹配原请求与回执身份；本模块不执行工具，也不伪造新的成功记录。
"""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Callable, Mapping, Sequence
from agent.cognitive_tools import project_tool_plan_receipts, tool_step_binding, receipt_matches_request, apply_reused_step_binding
from agent.cognitive_evidence_journal import append_receipts as journal_cognitive_receipts, append_reused_step_binding, append_execution_plan
from backend.context.turn_snapshot import cognitive_turn_state
from backend.chat.upstream_pipeline import tool_result_was_attempted

def cognitive_execution_state(body: Mapping[str, Any]) -> dict[str, Any]:
    state = project_tool_plan_receipts(cognitive_turn_state(body), body.get("_cognitiveEvidence") or {})
    if not state:
        return {}
    return {
        **state,
        "_executionTurnId": str((body.get("_turnContextSnapshot") or {}).get("currentTurnId") or body.get("requestId") or ""),
        "_executionRootSnapshotId": str((body.get("_turnContextSnapshot") or {}).get("rootSnapshotId") or ""),
        "_executionUserRequest": str(body.get("userMessage") or ""),
        "_invalidatedPendingActionIds": list((body.get("_cognitiveOverlay") or {}).get("invalidatedPendingActionIds", [])),
    }


@dataclass(frozen=True)
class ExecutionJournalPorts:
    database: Callable[..., Any]
    log: Callable[..., Any]

class ExecutionJournal:
    def __init__(self, ports: ExecutionJournalPorts) -> None:
        self.ports = ports

    def record_cognitive_execution_result(
        self,
        item: dict[str, Any], cognitive_state: Mapping[str, Any] | None, *,
        request_id: str = "", user_question: str = "",
    ) -> dict[str, Any]:
        """Persist an executor receipt before answer generation can fail."""
        if cognitive_state and cognitive_state.get("_executionRootSnapshotId"):
            with self.ports.database() as connection:
                journal_cognitive_receipts(
                    connection, conversation_id=str(cognitive_state.get("conversationId") or ""),
                    turn_id=str(cognitive_state.get("_executionTurnId") or request_id),
                    root_snapshot_id=str(cognitive_state["_executionRootSnapshotId"]),
                    user_request=str(cognitive_state.get("_executionUserRequest") or user_question),
                    receipts=[item],
                )
                if item.get("_cognitiveStepBinding") and tool_result_was_attempted(item):
                    append_execution_plan(connection, conversation_id=str(cognitive_state.get("conversationId") or ""),
                        turn_id=str(cognitive_state.get("_executionTurnId") or request_id),
                        task=cognitive_state.get("task") or {})
        return item


    def bind_reused_tool_steps(self, body: Mapping[str, Any], state: dict[str, Any],
                              requests: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]]) -> None:
        """Update step progress from the exact receipts used by a reuse branch."""
        snapshot = body.get("_turnContextSnapshot") or {}
        for request in requests:
            binding = tool_step_binding(state.get("task") or {}, request)
            matching = [item for item in results if item.get("toolCallId") and receipt_matches_request(item, request)]
            if not binding or not matching:
                continue
            binding.update(request=deepcopy(dict(request)), receiptIds=list(dict.fromkeys(item["toolCallId"] for item in matching)))
            projected = apply_reused_step_binding(state, binding, matching)
            if projected.get("task") == state.get("task"):
                continue
            if snapshot.get("rootSnapshotId") and state.get("conversationId"):
                with self.ports.database() as connection:
                    journal_cognitive_receipts(connection, conversation_id=state["conversationId"],
                        turn_id=str(snapshot.get("currentTurnId") or body.get("requestId") or ""),
                        root_snapshot_id=snapshot["rootSnapshotId"], user_request=str(body.get("userMessage") or ""), receipts=matching)
                    append_reused_step_binding(connection, conversation_id=state["conversationId"],
                        turn_id=str(snapshot.get("currentTurnId") or body.get("requestId") or ""), binding=binding)
                    append_execution_plan(connection, conversation_id=state["conversationId"],
                        turn_id=str(snapshot.get("currentTurnId") or body.get("requestId") or ""), task=state["task"])
            state["task"] = projected["task"]
            self.ports.log("cognitive_reused_step_synchronized", requestId=body.get("requestId"),
                             taskId=binding["taskId"], stepId=binding["stepId"], receiptIds=binding["receiptIds"])
