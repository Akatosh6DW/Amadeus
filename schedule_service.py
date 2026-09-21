"""Durable schedule workflow state for the AMADEUS Todo subsystem.

This service persists model-decided schedule activity independently from chat
history.  Todo execution remains a separate capability, while this module
ensures unrelated conversation, memory clearing, and UI refreshes cannot erase
an unfinished schedule workflow accidentally.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping
import uuid

from agent.schedule_architecture import ScheduleIntent, SchedulePolicyDecision


SCHEDULE_WORKFLOW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schedule_workflows (
    workflow_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    original_user_request TEXT NOT NULL DEFAULT '',
    origin_request_id TEXT NOT NULL DEFAULT '',
    updated_request_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    execution_context_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_schedule_workflows_status_time
ON schedule_workflows(status, updated_at);
"""

ACTIVE_WORKFLOW_STATUSES = frozenset({
    "collecting_details", "awaiting_confirmation", "ready", "executing", "suspended",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schedule_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEDULE_WORKFLOW_SCHEMA_SQL)
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(schedule_workflows)")
    }
    if "execution_context_json" not in columns:
        connection.execute(
            "ALTER TABLE schedule_workflows ADD COLUMN "
            "execution_context_json TEXT NOT NULL DEFAULT '{}'"
        )


def _row_to_workflow(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    value = dict(row)
    try:
        intent = json.loads(str(value.get("intent_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        intent = {}
    try:
        policy = json.loads(str(value.get("policy_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        policy = {}
    try:
        execution_context = json.loads(
            str(value.get("execution_context_json") or "{}")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        execution_context = {}
    return {
        "workflowId": str(value.get("workflow_id") or ""),
        "status": str(value.get("status") or ""),
        "intent": intent if isinstance(intent, dict) else {},
        "policy": policy if isinstance(policy, dict) else {},
        "originalUserRequest": str(value.get("original_user_request") or ""),
        "originRequestId": str(value.get("origin_request_id") or ""),
        "updatedRequestId": str(value.get("updated_request_id") or ""),
        "createdAt": str(value.get("created_at") or ""),
        "updatedAt": str(value.get("updated_at") or ""),
        "completedAt": str(value.get("completed_at") or ""),
        "error": str(value.get("error") or ""),
        "executionContext": (
            execution_context if isinstance(execution_context, dict) else {}
        ),
    }


def current_schedule_workflow(connection: sqlite3.Connection) -> dict[str, Any]:
    ensure_schedule_schema(connection)
    placeholders = ",".join("?" for _ in ACTIVE_WORKFLOW_STATUSES)
    row = connection.execute(
        f"SELECT * FROM schedule_workflows WHERE status IN ({placeholders}) "
        "ORDER BY datetime(updated_at) DESC, rowid DESC LIMIT 1",
        tuple(sorted(ACTIVE_WORKFLOW_STATUSES)),
    ).fetchone()
    return _row_to_workflow(row)


def read_schedule_workflow(
    connection: sqlite3.Connection,
    workflow_id: str,
) -> dict[str, Any]:
    ensure_schedule_schema(connection)
    row = connection.execute(
        "SELECT * FROM schedule_workflows WHERE workflow_id = ?",
        (str(workflow_id or ""),),
    ).fetchone()
    return _row_to_workflow(row)


def _status_for_policy(policy: SchedulePolicyDecision) -> str:
    return {
        "collect_details": "collecting_details",
        "confirm": "awaiting_confirmation",
        "execute": "ready",
        "cancel": "cancelled",
        "reject": "failed",
        "read": "completed",
        "none": "suspended",
    }[policy.action]


# 只保存日程对话的状态迁移；实际创建或修改待办仍交给独立的执行能力。
# 无关话题使当前流程挂起，继续原任务时复用标识和原始请求，避免丢失上下文。
def apply_schedule_transition(
    connection: sqlite3.Connection,
    intent: ScheduleIntent,
    policy: SchedulePolicyDecision,
    *,
    request_id: str,
    user_message: str,
) -> dict[str, Any]:
    """Persist one explicit state transition; never execute a Todo write."""

    ensure_schedule_schema(connection)
    current = current_schedule_workflow(connection)
    timestamp = _now_iso()
    if policy.action == "none":
        if current and current.get("status") != "suspended":
            connection.execute(
                "UPDATE schedule_workflows SET status='suspended', updated_request_id=?, "
                "updated_at=? WHERE workflow_id=?",
                (str(request_id)[:120], timestamp, current["workflowId"]),
            )
            return read_schedule_workflow(connection, current["workflowId"])
        return current

    if policy.action == "cancel":
        if not current:
            return {}
        connection.execute(
            "UPDATE schedule_workflows SET status='cancelled', updated_request_id=?, "
            "updated_at=?, completed_at=? WHERE workflow_id=?",
            (str(request_id)[:120], timestamp, timestamp, current["workflowId"]),
        )
        return read_schedule_workflow(connection, current["workflowId"])

    reuse_current = bool(
        current
        and intent.relation == "continue"
        and current.get("status") in ACTIVE_WORKFLOW_STATUSES
    )
    workflow_id = (
        str(current.get("workflowId") or "")
        if reuse_current else f"schedule-{uuid.uuid4().hex}"
    )
    created_at = str(current.get("createdAt") or timestamp) if reuse_current else timestamp
    original_request = (
        str(current.get("originalUserRequest") or "")
        if reuse_current else " ".join(str(user_message or "").split()).strip()[:1000]
    )
    origin_request_id = (
        str(current.get("originRequestId") or "")
        if reuse_current else str(request_id)[:120]
    )
    status = _status_for_policy(policy)
    completed_at = timestamp if status in {"completed", "cancelled", "failed"} else ""
    connection.execute(
        """
        INSERT INTO schedule_workflows(
            workflow_id, status, intent_json, policy_json, original_user_request,
            origin_request_id, updated_request_id, created_at, updated_at,
            completed_at, error, execution_context_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workflow_id) DO UPDATE SET
            status=excluded.status,
            intent_json=excluded.intent_json,
            policy_json=excluded.policy_json,
            original_user_request=excluded.original_user_request,
            updated_request_id=excluded.updated_request_id,
            updated_at=excluded.updated_at,
            completed_at=excluded.completed_at,
            error=excluded.error,
            execution_context_json=excluded.execution_context_json
        """,
        (
            workflow_id,
            status,
            json.dumps(intent.to_dict(), ensure_ascii=False, separators=(",", ":")),
            json.dumps(policy.to_dict(), ensure_ascii=False, separators=(",", ":")),
            original_request,
            origin_request_id,
            str(request_id)[:120],
            created_at,
            timestamp,
            completed_at,
            "" if policy.allowed else policy.reason[:160],
            "{}",
        ),
    )
    return read_schedule_workflow(connection, workflow_id)


def mark_schedule_execution(
    connection: sqlite3.Connection,
    workflow_id: str,
    *,
    status: str,
    request_id: str,
    error: str = "",
    execution_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"collecting_details", "executing", "completed", "failed"}:
        raise ValueError("invalid_schedule_execution_status")
    ensure_schedule_schema(connection)
    timestamp = _now_iso()
    connection.execute(
        "UPDATE schedule_workflows SET status=?, updated_request_id=?, updated_at=?, "
        "completed_at=?, error=?, execution_context_json=? WHERE workflow_id=?",
        (
            status,
            str(request_id)[:120],
            timestamp,
            timestamp if status in {"completed", "failed"} else "",
            str(error or "")[:160],
            json.dumps(
                dict(execution_context or {}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            str(workflow_id or ""),
        ),
    )
    return read_schedule_workflow(connection, workflow_id)


def schedule_workflow_prompt(workflow: Mapping[str, Any] | None) -> str:
    value = workflow if isinstance(workflow, Mapping) else {}
    if not value or str(value.get("status") or "") not in ACTIVE_WORKFLOW_STATUSES:
        return ""
    return "\n".join((
        "[持久化日程工作流：只能由统一 scheduleIntent 续接]",
        json.dumps({
            "workflowId": value.get("workflowId"),
            "status": value.get("status"),
            "originalUserRequest": value.get("originalUserRequest"),
            "intent": value.get("intent"),
            "policy": value.get("policy"),
            "executionContext": value.get("executionContext"),
        }, ensure_ascii=False, separators=(",", ":")),
        "结合当前消息的完整语义输出 relation=continue、unrelated 或 cancel。",
        "无关消息会暂存工作流而不是删除；只有明确取消才取消。"
        "续接时必须使用 authorization=continued，不得要求用户重复授权。",
        "若当前消息已补齐 intent.missingFields 且仍是执行请求，直接输出 mutation 和完整工具调用；"
        "不要把已经明确授权的续接降成确认问题。只有仍缺少真正必要的信息时才继续 clarification。",
    ))
