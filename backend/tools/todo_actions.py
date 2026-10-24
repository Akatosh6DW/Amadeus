"""待办动作的应用编排与可审计回执。

这个模块拥有待办写操作的连续责任：解析命令、在同一 SQLite 连接中
完成状态变更与实际读回核验、生成回执，以及在目标不明确时保存待续动作。
数据库、待续记录、日志和时间由入口端口提供；这样 HTTP/模型入口不会成为
第二个状态所有者，也不会在批处理时偷偷重新打开连接。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence
from contextlib import nullcontext
import json
import re
import sqlite3
import uuid

from agent.cognitive_tools import canonical_tool_arguments
from agent.fingerprints import legacy_persistent_fingerprint
from agent.schedule_time import resolve_time_spec, schedule_time_facts
from agent.todo_architecture import TodoCommand
from todo_store import (
    TodoResultMismatch,
    TodoVersionConflict,
    create_todo,
    read_todo,
    reactivate_daily_all_day_todos,
    resolve_task_reference,
    set_todo_status,
    snooze_todo,
    update_todo,
    verify_todo_state,
)


@dataclass(frozen=True)
class TodoActionPorts:
    """动态入口端口；端口保持测试替换和批量连接语义。"""

    database_context: Callable[[], Any]
    list_response: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    current_pending_action: Callable[[], dict[str, Any]]
    write_pending_action: Callable[[dict[str, Any]], dict[str, Any]]
    clear_pending_action: Callable[[], Any]
    write_action_receipt: Callable[..., dict[str, Any]]
    now_iso: Callable[[], str]
    now_local: Callable[[], datetime]
    now_utc: Callable[[], datetime]
    write_log: Callable[..., Any]


def public_todo_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "taskId": str(value.get("taskId") or "")[:100],
        "title": str(value.get("title") or "")[:240],
        "details": str(value.get("details") or "")[:500],
        "onDue": str(value.get("onDue") or "")[:4000],
        "status": str(value.get("status") or "")[:40],
        "dueAt": str(value.get("dueAt") or "")[:80],
        "dueDate": str(value.get("dueDate") or "")[:10],
        "allDay": not bool(value.get("dueAt")),
        "timezone": str(value.get("timezone") or "")[:80],
        "recurrenceRule": str(value.get("recurrenceRule") or ""),
        "version": max(0, int(value.get("version") or 0)),
        "matchType": str(value.get("matchType") or "")[:40],
    }


def todo_time_receipt(task: Mapping[str, Any], anchor: datetime | None) -> dict[str, Any]:
    """Derive reply facts from persisted state, without risking an accepted write."""
    if anchor is None:
        return {}
    try:
        return schedule_time_facts(task, anchor=anchor)
    except ValueError:
        # Older records may contain platform timezone labels rather than IANA.
        # Keep successful mutations successful and mark the display fallback.
        facts = schedule_time_facts({**task, "timezone": ""}, anchor=anchor)
        return {**facts, "timezoneFallback": True}

def resolve_todo_command_time(
    command: TodoCommand, anchor: datetime | None, existing: Mapping[str, Any] | None = None,
) -> TodoCommand:
    if not command.time_spec:
        return command
    if anchor is None:
        raise ValueError("todo_time_clock_permission_required")
    zone = command.timezone_name or str((existing or {}).get("timezone") or "")
    due_at = resolve_time_spec(command.time_spec, anchor=anchor, timezone_name=zone, existing=existing)
    if command.due_at and command.due_at != due_at:
        raise ValueError("todo_time_spec_conflict: expected dueAt=" + due_at)
    return replace(command, due_at=due_at, due_date="", timezone_name=zone,
                   provided_fields=tuple(dict.fromkeys((*command.provided_fields, "dueAt", "dueDate", "timezone"))))

class TodoActionService:
    """在一个事务内执行单个待办动作，并绑定真实存储状态。"""

    def __init__(self, ports: TodoActionPorts) -> None:
        self.ports = ports

    def save_pending(
            self,
            *,
            request_id: str,
            command: Mapping[str, Any],
            candidates: Sequence[Mapping[str, Any]],
            status: str,
            error: str,
            action_id: str = "",
            original_user_request: str = "",
        ) -> dict[str, Any]:
        existing = self.ports.current_pending_action()
        existing = (
            existing
            if isinstance(existing, dict)
            and existing.get("type") == "todo_mutation"
            and bool(action_id)
            and str(existing.get("actionId") or "") == str(action_id)
            else {}
        )
        pending = {
            "actionId": str(
                action_id
                or existing.get("actionId")
                or f"todo-action-{uuid.uuid4().hex}"
            )[:100],
            "type": "todo_mutation",
            "status": str(status)[:40],
            "error": str(error)[:80],
            "intendedCommand": (
                dict(command)
                if command else
                dict(existing.get("intendedCommand") or {})
            ),
            "candidateTasks": [
                public_todo_candidate(item)
                for item in (
                    list(candidates)
                    if candidates else list(existing.get("candidateTasks") or [])
                )[:8]
            ],
            "originalUserRequest": re.sub(
                r"\s+",
                " ",
                str(
                    original_user_request
                    or existing.get("originalUserRequest")
                    or ""
                ),
            ).strip()[:1000],
            "originRequestId": str(existing.get("originRequestId") or request_id)[:120],
            "updatedRequestId": str(request_id)[:120],
            "createdAt": str(existing.get("createdAt") or self.ports.now_iso())[:80],
            "updatedAt": self.ports.now_iso(),
        }
        self.ports.write_pending_action(pending)
        self.ports.write_log(
            "pending_todo_action_saved",
            requestId=str(request_id)[:120],
            actionId=pending["actionId"],
            status=pending["status"],
            error=pending["error"],
            candidateCount=len(pending["candidateTasks"]),
        )
        return pending

    def manage(
            self,
            settings: dict[str, Any],
            args: dict[str, Any] | None = None,
            *,
            source_turn_id: str = "",
            _connection: sqlite3.Connection | None = None,
            _defer_pending: bool = False,
            _time_anchor: datetime | None = None,
        ) -> dict[str, Any]:
        """Apply one schema-validated Todo mutation and issue an auditable receipt."""

        anchor = (_time_anchor or self.ports.now_local()) if settings.get("permissionReadSystemTime") else None
        raw = dict(args) if isinstance(args, dict) else {}
        try:
            command = TodoCommand.from_mapping(raw)
        except ValueError as error:
            return {"name": "todo_manage", "ok": False, "error": str(error)}
        if command.action == "list":
            return self.ports.list_response({}, {"scope": command.scope})

        action_material = json.dumps(
            canonical_tool_arguments("todo_manage", command.to_dict()),
            ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        action_id = (
            "todo-action-" + legacy_persistent_fingerprint(
                f"{source_turn_id}\0{action_material}", length=32
            )
            if source_turn_id else f"todo-action-{uuid.uuid4().hex}"
        )

        task: dict[str, Any] = {}
        before_task: dict[str, Any] = {}
        resolution: dict[str, Any] = {}
        receipt: dict[str, Any] = {}
        resolved_task_id = command.task_id
        expected_state: dict[str, Any] = {}
        try:
            database_scope = (
                nullcontext(_connection) if _connection is not None else self.ports.database_context()
            )
            with database_scope as connection:
                reactivate_daily_all_day_todos(connection)
                replay_row = connection.execute(
                    "SELECT payload_json FROM action_receipts WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                if replay_row is not None:
                    try:
                        replay_receipt = json.loads(str(replay_row["payload_json"] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        replay_receipt = {}
                    if (
                        isinstance(replay_receipt, dict)
                        and replay_receipt.get("status") == "accepted"
                    ):
                        replay_task = read_todo(
                            connection,
                            str(replay_receipt.get("taskId") or replay_receipt.get("resolvedTarget") or ""),
                        )
                        # A historical success is not proof that the current row
                        # still satisfies the request. Never recreate on replay.
                        replay_expected = replay_receipt.get("expectedState")
                        if not isinstance(replay_expected, Mapping) or not replay_expected:
                            replay_expected = {"version": replay_receipt.get("taskVersionAfter")}
                        verification = verify_todo_state(replay_task, replay_expected,
                            task_id=str(replay_receipt.get("taskId") or replay_receipt.get("resolvedTarget") or ""))
                        if verification["status"] != "verified":
                            return {"name": "todo_manage", "ok": False, "error": "todo_result_changed",
                                    "action": command.action, "currentTask": replay_task,
                                    "previousActionReceipt": replay_receipt, "stateVerification": verification,
                                    "requiresRefresh": True, "replayed": True, "source": "local_sqlite"}
                        return {
                            "name": "todo_manage",
                            "ok": True,
                            "action": str(replay_receipt.get("action") or command.action),
                            "task": replay_task,
                            "actionReceipt": replay_receipt,
                            "timeFacts": todo_time_receipt(replay_task, anchor),
                            "replayed": True,
                            "stateVerification": verification,
                            "source": "local_sqlite",
                        }
                if command.action == "create":
                    command = resolve_todo_command_time(command, anchor)
                    expected_state = {"title": command.title, "details": command.details, "onDue": command.on_due,
                        "dueAt": command.due_at, "dueDate": command.due_date if not command.due_at else "",
                        "timezone": command.timezone_name, "remindBeforeMinutes": command.remind_before_minutes,
                        "priority": command.priority, "recurrenceRule": command.recurrence_rule, "status": "pending"}
                    task = create_todo(
                        connection,
                        title=command.title,
                        details=command.details,
                        on_due=command.on_due,
                        due_at=command.due_at,
                        due_date=command.due_date,
                        timezone_name=command.timezone_name,
                        remind_before_minutes=command.remind_before_minutes,
                        priority=command.priority,
                        recurrence_rule=command.recurrence_rule,
                        source_turn_id=source_turn_id,
                    )
                else:
                    target = command.target_ref.to_dict()
                    if resolved_task_id:
                        target["taskId"] = resolved_task_id
                    raw_target = raw.get("targetRef") or raw.get("target_ref") or {}
                    target_status_explicit = isinstance(raw_target, Mapping) and any(
                        key in raw_target for key in ("status", "scope")
                    )
                    if not resolved_task_id:
                        # TodoCommand.to_dict() always carries the target's
                        # default status ("active").  Treat that default as
                        # implicit for resume so a paused task remains
                        # discoverable after request constraint canonicalization.
                        if command.action == "resume" and (
                            not target_status_explicit or target.get("status") == "active"
                        ):
                            target["status"] = "paused"
                        elif not target_status_explicit:
                            target["status"] = (
                                "completed" if command.action == "reopen" else "active"
                            )
                    resolution = resolve_task_reference(connection, target)
                    if resolution.get("status") == "resolved" and isinstance(
                        resolution.get("task"), Mapping
                    ):
                        before_task = dict(resolution["task"])
                        resolved_task_id = str(before_task.get("taskId") or "")

                    if resolution.get("status") == "resolved" and command.action == "update":
                        command = resolve_todo_command_time(command, anchor, before_task)
                        if command.time_spec:
                            raw.update(dueAt=command.due_at, dueDate="", timezone=command.timezone_name)
                        changes: dict[str, Any] = {}
                        key_map = {
                            "title": ("title", command.title),
                            "details": ("details", command.details),
                            "onDue": ("onDue", command.on_due),
                            "dueAt": ("dueAt", command.due_at),
                            "due_at": ("dueAt", command.due_at),
                            "dueDate": ("dueDate", command.due_date),
                            "due_date": ("dueDate", command.due_date),
                            "timezone": ("timezone", command.timezone_name),
                            "timezoneName": ("timezone", command.timezone_name),
                            "remindBeforeMinutes": (
                                "remindBeforeMinutes", command.remind_before_minutes
                            ),
                            "remind_before_minutes": (
                                "remindBeforeMinutes", command.remind_before_minutes
                            ),
                            "priority": ("priority", command.priority),
                            "recurrenceRule": (
                                "recurrenceRule", command.recurrence_rule
                            ),
                            "recurrence_rule": (
                                "recurrenceRule", command.recurrence_rule
                            ),
                        }
                        for incoming, (public_name, value) in key_map.items():
                            if incoming in raw:
                                changes[public_name] = value
                        if "title" in changes and not str(changes["title"]).strip():
                            return {
                                "name": "todo_manage", "ok": False,
                                "error": "todo_title_required",
                            }
                        if not changes:
                            return {
                                "name": "todo_manage", "ok": False,
                                "error": "todo_no_changes",
                            }
                        expected_state = dict(changes)
                        if changes.get("dueAt"):
                            expected_state["dueDate"] = ""
                        elif changes.get("dueDate"):
                            expected_state["dueAt"] = ""
                        expected_state.update(status=before_task.get("status"), snoozedUntil="")
                        if command.expected_version and command.expected_version != before_task.get("version"):
                            raise TodoVersionConflict(before_task, command.expected_version)
                        if verify_todo_state(before_task, expected_state, task_id=resolved_task_id)["status"] == "verified":
                            task = before_task
                        else:
                            task = update_todo(
                                connection,
                                resolved_task_id,
                                changes,
                                expected_version=command.expected_version,
                            )
                    elif resolution.get("status") == "resolved" and command.action in {
                        "complete", "reopen", "delete", "pause", "resume",
                    }:
                        status = {
                            "complete": "completed",
                            "reopen": "pending",
                            "delete": "deleted",
                            "pause": "paused",
                            "resume": "pending",
                        }[command.action]
                        task = set_todo_status(
                            connection,
                            resolved_task_id,
                            status,
                            expected_version=command.expected_version,
                        )
                    elif resolution.get("status") == "resolved" and command.action == "snooze":
                        snooze_until = command.snooze_until
                        if not snooze_until and command.snooze_minutes:
                            snooze_until = (
                                self.ports.now_utc()
                                + timedelta(minutes=command.snooze_minutes)
                            ).isoformat(timespec="seconds")
                        if not snooze_until:
                            return {
                                "name": "todo_manage", "ok": False,
                                "error": "todo_snooze_time_required",
                            }
                        task = snooze_todo(
                            connection,
                            resolved_task_id,
                            snooze_until=snooze_until,
                            expected_version=command.expected_version,
                        )
                if task:
                    # Store functions return rows, but success must be tied to a
                    # read of the selected record and the resolved requested state.
                    result_task_id = str(task.get("taskId") or "")
                    task = read_todo(connection, resolved_task_id or result_task_id)
                    if expected_state:
                        verification = verify_todo_state(task, expected_state,
                            task_id=resolved_task_id or result_task_id)
                        if verification["status"] != "verified":
                            raise TodoResultMismatch(task, verification)
                    else:
                        # Status/snooze/recurrence have their own store transition
                        # semantics; bind replay to the exact resulting version.
                        expected_state = {"version": task.get("version")}
                    receipt = {
                        "actionId": action_id,
                        "tool": "todo_manage",
                        "status": "accepted",
                        "resolvedTarget": str(task.get("taskId") or ""),
                        "action": command.action,
                        "taskId": str(task.get("taskId") or ""),
                        "requestedTarget": (
                            command.target_ref.title
                            or command.target_ref.query
                            or command.task_id
                        ),
                        "taskVersionBefore": int(before_task.get("version") or 0),
                        "taskVersionAfter": int(task.get("version") or 0),
                        "timestamp": self.ports.now_iso(),
                        "timeFacts": todo_time_receipt(task, anchor),
                        "expectedState": expected_state,
                        "stateVerification": verify_todo_state(task, expected_state,
                            task_id=resolved_task_id or result_task_id),
                    }
                    if command.time_spec:
                        receipt["timeSpec"] = dict(command.time_spec)
                    self.ports.write_action_receipt(receipt, connection=connection)
        except TodoResultMismatch as mismatch:
            self.ports.write_log("todo_result_verification_failed", requestId=source_turn_id,
                             action=command.action, verification=mismatch.verification)
            return {"name": "todo_manage", "ok": False, "error": "todo_result_mismatch",
                    "stateVerification": mismatch.verification, "requiresRefresh": True,
                    "source": "local_sqlite"}
        except TodoVersionConflict as conflict:
            return {
                "name": "todo_manage",
                "ok": False,
                "error": "todo_version_conflict",
                "expectedVersion": conflict.expected_version,
                "currentTask": public_todo_candidate(conflict.task),
                "requiresRefresh": True,
            }
        except ValueError as error:
            self.ports.write_log("schedule_time_resolution_rejected", requestId=source_turn_id,
                             timeSpec=command.time_spec, error=str(error))
            return {"name": "todo_manage", "ok": False, "error": str(error), "attempted": False,
                    "requiresClarification": str(error) in {"todo_time_ambiguous", "todo_time_nonexistent"},
                    "reason": "Resolve the timeSpec ambiguity or conflict using the same user intent; no reminder was written."}
        except sqlite3.Error as error:
            self.ports.write_log(
                "todo_transaction_failed",
                requestId=str(source_turn_id)[:120],
                action=command.action,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
            return {
                "name": "todo_manage",
                "ok": False,
                "error": "todo_transaction_failed",
            }

        if command.action != "create" and resolution.get("status") != "resolved":
            resolution_status = str(resolution.get("status") or "not_found")
            candidates = [
                public_todo_candidate(item)
                for item in resolution.get("candidates", [])
                if isinstance(item, Mapping)
            ]
            error = (
                "todo_target_ambiguous"
                if resolution_status == "ambiguous"
                else str(resolution.get("error") or "todo_target_not_found")
            )
            pending = (
                {}
                if _defer_pending
                else self.save_pending(
                    request_id=source_turn_id,
                    command={**command.to_dict(), "taskId": ""},
                    candidates=candidates,
                    status="needs_selection" if candidates else "needs_reference",
                    error=error,
                )
            )
            return {
                "name": "todo_manage",
                "ok": False,
                "error": error,
                "requiresClarification": True,
                "candidates": candidates,
                "pendingActionId": pending.get("actionId"),
                "source": "local_sqlite",
            }

        if not task:
            return {"name": "todo_manage", "ok": False, "error": "todo_not_found"}
        self.ports.write_log(
            "action_receipt",
            actionId=str(receipt.get("actionId") or "")[:100],
            tool="todo_manage",
            status=str(receipt.get("status") or "")[:40],
            resolvedTarget=str(receipt.get("resolvedTarget") or "")[:2000],
            error="",
        )
        if command.time_spec:
            self.ports.write_log("schedule_time_resolved", requestId=source_turn_id,
                             timeSpec=command.time_spec, timeFacts=receipt.get("timeFacts", {}))
        if not _defer_pending:
            pending = self.ports.current_pending_action()
            if pending.get("type") == "todo_mutation":
                self.ports.clear_pending_action()
        return {
            "name": "todo_manage",
            "ok": True,
            "action": command.action,
            "task": task,
            "actionReceipt": receipt,
            "timeFacts": receipt.get("timeFacts", {}),
            "source": "local_sqlite",
        }
