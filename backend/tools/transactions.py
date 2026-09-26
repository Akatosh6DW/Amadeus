"""待办批量写入的单连接事务与回滚结果。"""

from __future__ import annotations

from contextlib import AbstractContextManager
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from agent.turn_runtime import ToolResultEnvelope, utc_now_iso

from .validation import AUDIT_EXECUTOR_ID


class TodoBatchAbort(RuntimeError):
    def __init__(self, index: int, result: Mapping[str, Any]) -> None:
        super().__init__(str(result.get("error") or "todo_batch_failed"))
        self.index = index
        self.result = dict(result)


def todo_batch_result_envelope(
    result: Mapping[str, Any],
    operation: Mapping[str, Any],
    *,
    request_id: str,
    started_at: str,
) -> dict[str, Any]:
    normalized = dict(result)
    normalized.setdefault("name", "todo_manage")
    normalized.setdefault("toolCallId", str(operation.get("toolCallId") or ""))
    normalized["_toolResultEnvelope"] = ToolResultEnvelope.from_legacy_result(
        normalized,
        turn_id=f"{request_id}:turn" if request_id else "",
        started_at=started_at,
        provenance={"executor": AUDIT_EXECUTOR_ID, "transaction": "schedule_batch"},
    ).to_dict()
    return normalized


def execute_todo_batch(
    operations: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    *,
    request_id: str,
    state_database: Callable[[], AbstractContextManager[sqlite3.Connection]],
    todo_manage: Callable[..., dict[str, Any]],
    current_pending_action: Callable[[], dict[str, Any]],
    clear_pending_action: Callable[[], Any],
    make_envelope: Callable[..., dict[str, Any]],
    write_log: Callable[..., Any],
) -> list[dict[str, Any]]:
    """全部待办及收据共用连接；失败在 with 内抛出才能回滚。"""

    started_at = utc_now_iso()
    partial: list[dict[str, Any]] = []
    try:
        with state_database() as connection:
            for index, operation in enumerate(operations):
                result = todo_manage(
                    dict(settings),
                    dict(operation.get("args") or {}),
                    source_turn_id=request_id,
                    _connection=connection,
                    _defer_pending=True,
                    _time_anchor=operation.get("timeAnchor"),
                )
                partial.append(dict(result))
                if (
                    not result.get("ok")
                    or not isinstance(result.get("actionReceipt"), Mapping)
                    or result["actionReceipt"].get("status") != "accepted"
                ):
                    raise TodoBatchAbort(index, result)
    except TodoBatchAbort as abort:
        rolled_back: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            if index < abort.index:
                value = {
                    "name": "todo_manage", "ok": False, "error": "todo_batch_rolled_back",
                    "failedCommandIndex": abort.index, "batchAtomic": True,
                }
            elif index == abort.index:
                value = {
                    **abort.result, "ok": False,
                    "batchAtomic": True, "batchRolledBack": True,
                }
                value.pop("actionReceipt", None)
            else:
                value = {
                    "name": "todo_manage", "ok": False, "error": "todo_batch_not_executed",
                    "attempted": False, "failedCommandIndex": abort.index, "batchAtomic": True,
                }
            rolled_back.append(make_envelope(
                value, operation, request_id=request_id, started_at=started_at,
            ))
        write_log(
            "todo_batch_rolled_back",
            requestId=request_id,
            commandCount=len(operations),
            failedCommandIndex=abort.index,
            error=str(abort.result.get("error") or "todo_batch_failed")[:160],
        )
        return rolled_back

    # 连接离开 with 后已经提交，只有此时才能清除待办 pending action。
    pending = current_pending_action()
    if pending.get("type") == "todo_mutation":
        clear_pending_action()
    accepted = [
        make_envelope(
            {**result, "batchAtomic": True}, operation,
            request_id=request_id, started_at=started_at,
        )
        for result, operation in zip(partial, operations)
    ]
    write_log(
        "todo_batch_committed",
        requestId=request_id,
        commandCount=len(accepted),
        actionIds=[
            str((item.get("actionReceipt") or {}).get("actionId") or "")
            for item in accepted
        ],
    )
    return accepted
