"""待办续接的授权状态与澄清提示边界。

本模块只维护 pending Todo continuation 的模型决定、清空和等待
状态编排；真正的动作、版本检查和回执仍归 ``todo_actions``，
通过窄端口接入，不把待续状态误当成已完成写入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping
import json


TODO_CONTINUATION_DECISIONS = {
    "execute",
    "needs_input",
    "unrelated",
    "cancel",
}


def normalize_todo_continuation_decision(value: Any) -> str:
    decision = str(value or "").strip().lower()
    return decision if decision in TODO_CONTINUATION_DECISIONS else ""


@dataclass(frozen=True)
class TodoContinuationPorts:
    """续接状态和日志的窄端口。"""

    current_pending_action: Callable[[], dict[str, Any]]
    clear_pending_action: Callable[[str], Any]
    save_pending_action: Callable[..., dict[str, Any]]
    write_log: Callable[..., Any]


class TodoContinuationService:
    """Owns pending Todo continuation decisions without executing Todo writes."""

    def __init__(self, ports: TodoContinuationPorts) -> None:
        self.ports = ports


    def pending_todo_action_prompt(self) -> str:
        pending = self.ports.current_pending_action()
        if pending.get("type") != "todo_mutation":
            return ""
        return "\n".join([
            "[待续接的 Todo 操作：这是已获用户授权的旧请求，不是新任务推销]",
            json.dumps({
                "status": pending.get("status"),
                "error": pending.get("error"),
                "originalUserRequest": pending.get("originalUserRequest"),
                "intendedCommand": pending.get("intendedCommand"),
                "candidateTasks": pending.get("candidateTasks"),
            }, ensure_ascii=False, separators=(",", ":")),
            "用户当前消息可能是在补充任务内容、时间、候选选择或其他缺失条件。必须按完整语义判断，而不是靠某个词触发。",
            "若它与 originalUserRequest 强相关，就继续同一次已授权操作并输出 todo_manage；不要要求用户重复说‘加入日程’。",
            "候选序号按 candidateTasks 的 1-based 顺序；只能选择这里的真实 taskId/version，不得编造。",
            "若用户取消、否定或转向无关主题，不执行写操作；仍无法唯一确定时继续提出最小澄清。",
        ])

    def apply_todo_continuation_transition(
        self,
        transition: Mapping[str, Any] | None,
        *,
        request_id: str,
        user_message: str,
    ) -> dict[str, Any]:
        """Persist a model-decided continuation without claiming a Todo write.

        A pending continuation records authorization plus missing information.  It
        is not an action receipt and cannot make a failed/omitted tool call look
        successful.
        """

        payload = transition if isinstance(transition, Mapping) else {}
        decision = normalize_todo_continuation_decision(payload.get("decision"))
        existing = self.ports.current_pending_action()
        existing = existing if existing.get("type") == "todo_mutation" else {}
        if decision in {"unrelated", "cancel"}:
            if existing:
                action_id = str(existing.get("actionId") or "")
                self.ports.clear_pending_action(action_id)
                self.ports.write_log(
                    "todo_continuation_cleared",
                    requestId=str(request_id)[:120],
                    actionId=action_id,
                    decision=decision,
                )
            return {}
        if decision != "needs_input":
            return existing
        pending_command = (
            dict(payload.get("pendingCommand"))
            if isinstance(payload.get("pendingCommand"), Mapping) else {}
        )
        pending = self.ports.save_pending_action(
            request_id=request_id,
            command=pending_command,
            candidates=[],
            status="needs_details",
            error="todo_details_required",
            action_id=str(existing.get("actionId") or ""),
            original_user_request=(
                str(existing.get("originalUserRequest") or "")
                or str(user_message or "")
            ),
        )
        self.ports.write_log(
            "todo_continuation_waiting_for_user",
            requestId=str(request_id)[:120],
            actionId=str(pending.get("actionId") or ""),
            carried=bool(existing),
            originalRequestId=str(pending.get("originRequestId") or ""),
        )
        return pending
