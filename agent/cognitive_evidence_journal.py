"""Durable execution evidence independent of acceptance of a model answer.

Only the backend executor writes this journal. A rejected model draft must
not erase observations or imply that a completed external action never ran.
It also must not commit the rejected draft's beliefs or task revisions.
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from agent.cognitive_state import ensure_schema as ensure_cognitive_schema
from agent.cognitive_tools import project_durable_tool_progress


def append_failed_request(connection: sqlite3.Connection, *, conversation_id: str,
                          turn_id: str, root_snapshot_id: str, user_request: str) -> None:
    """Retain the real request, never the rejected answer or its state patches."""
    if not all((conversation_id, turn_id, root_snapshot_id, user_request)):
        return
    connection.execute("""CREATE TABLE IF NOT EXISTS cognitive_failed_requests (
        conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
        root_snapshot_id TEXT NOT NULL, user_request TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(conversation_id, turn_id))""")
    connection.execute("""INSERT OR IGNORE INTO cognitive_failed_requests
        VALUES (?, ?, ?, ?, ?)""", (conversation_id, turn_id, root_snapshot_id,
        user_request, datetime.now(timezone.utc).isoformat(timespec="milliseconds")))


def read_failed_requests(connection: sqlite3.Connection, conversation_id: str) -> list[dict]:
    """Only recent failures after the latest accepted turn can be resumed implicitly."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_failed_requests'").fetchone():
        return []
    ensure_schema(connection)
    rows = connection.execute("""SELECT f.turn_id, f.user_request, f.created_at
        FROM cognitive_failed_requests f WHERE f.conversation_id=?
        AND NOT EXISTS (SELECT 1 FROM cognitive_traces t
            WHERE t.conversation_id=f.conversation_id AND
            (t.turn_id=f.turn_id OR julianday(t.created_at)>=julianday(f.created_at)))
        AND julianday(f.created_at)>=julianday('now', '-30 minutes')
        ORDER BY f.created_at DESC LIMIT 3""", (conversation_id,)).fetchall()
    return [{"turnId": turn, "userRequest": request, "createdAt": at,
             "answerCommitted": False, "failureStage": "cognitive_gate",
             "role": "failed_request_history_only", "continuationRequiresCurrentUserIntent": True}
            for turn, request, at in reversed(rows)]


def ensure_schema(connection: sqlite3.Connection) -> None:
    ensure_cognitive_schema(connection)
    connection.execute("""CREATE TABLE IF NOT EXISTS cognitive_execution_evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        root_snapshot_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        user_request TEXT NOT NULL,
        receipt_json TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        UNIQUE(conversation_id, turn_id, tool_call_id)
    )""")


def append_receipts(
    connection: sqlite3.Connection, *, conversation_id: str, turn_id: str,
    root_snapshot_id: str, user_request: str, receipts: Sequence[Mapping],
) -> None:
    if not all(isinstance(value, str) and value.strip()
               for value in (conversation_id, turn_id, root_snapshot_id)):
        raise ValueError("execution evidence requires backend conversation, turn and snapshot identities")
    ensure_schema(connection)
    for receipt in receipts:
        call_id = str(receipt.get("toolCallId") or "")
        if not call_id:
            raise ValueError("execution evidence requires an executed toolCallId")
        encoded = json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        existing = connection.execute("""SELECT receipt_json FROM cognitive_execution_evidence
            WHERE conversation_id=? AND turn_id=? AND tool_call_id=?""",
            (conversation_id, turn_id, call_id)).fetchone()
        if existing:
            if existing[0] != encoded:
                raise ValueError("execution receipt identity conflict")
            continue
        connection.execute("""INSERT INTO cognitive_execution_evidence
            (conversation_id, turn_id, root_snapshot_id, tool_call_id, user_request, receipt_json, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", (
                conversation_id, turn_id, root_snapshot_id, call_id, user_request, encoded,
                datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            ))


def read_receipts(
    connection: sqlite3.Connection, conversation_id: str, *,
    turn_ids: Sequence[str] | None = None, recent_turn_limit: int = 3,
) -> list[dict]:
    """Return backend records for one conversation; compact before prompting."""
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise ValueError("conversation_id required")
    ensure_schema(connection)
    if turn_ids is not None:
        if isinstance(turn_ids, (str, bytes)) or not isinstance(turn_ids, Sequence):
            raise ValueError("turn_ids must be a list")
        if len(turn_ids) > 100 or any(not isinstance(t, str) or not t.strip() for t in turn_ids):
            raise ValueError("invalid turn_ids")
        turns = list(dict.fromkeys(turn_ids))
    else:
        if not 1 <= recent_turn_limit <= 10:
            raise ValueError("recent_turn_limit must be between 1 and 10")
        turns = [row[0] for row in connection.execute("""SELECT turn_id
            FROM cognitive_execution_evidence WHERE conversation_id=? GROUP BY turn_id
            ORDER BY MAX(evidence_id) DESC LIMIT ?""", (conversation_id, recent_turn_limit))]
    if not turns:
        return []
    placeholders = ",".join("?" for _ in turns)
    rows = connection.execute(f"""SELECT e.turn_id, e.root_snapshot_id, e.user_request,
        e.receipt_json, e.observed_at,
        EXISTS(SELECT 1 FROM cognitive_traces t
               WHERE t.conversation_id=e.conversation_id AND t.turn_id=e.turn_id)
        FROM cognitive_execution_evidence e WHERE e.conversation_id=? AND e.turn_id IN ({placeholders})
        ORDER BY e.evidence_id""", (conversation_id, *turns)).fetchall()
    grouped: dict[str, dict] = {}
    for turn, root, user_request, encoded, observed_at, answer_committed in rows:
        item = grouped.setdefault(turn, {
            "turnId": turn, "rootSnapshotId": root, "userRequest": user_request,
            "answerCommitted": bool(answer_committed), "createdAt": observed_at, "toolReceipts": [],
        })
        item["toolReceipts"].append(json.loads(encoded))
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_execution_plans'").fetchone():
        for turn, task_json in connection.execute(f"""SELECT turn_id, task_json FROM cognitive_execution_plans
            WHERE conversation_id=? AND turn_id IN ({placeholders}) ORDER BY plan_id""", (conversation_id, *turns)):
            if turn in grouped:
                progressed = project_durable_tool_progress(connection, {
                    "conversationId": conversation_id, "task": json.loads(task_json)}, turn_id=turn)
                grouped[turn].setdefault("executionPlans", []).append(progressed["task"])
    return list(grouped.values())


def append_execution_plan(connection: sqlite3.Connection, *, conversation_id: str,
                          turn_id: str, task: Mapping) -> None:
    """Keep the executed plan as history, without accepting a draft's cognition."""
    if not task.get("taskId") or not task.get("plan"):
        return
    snapshot = {key: deepcopy(task[key]) for key in
                ("taskId", "goal", "target", "status", "planRevision", "plan") if key in task}
    connection.execute("""CREATE TABLE IF NOT EXISTS cognitive_execution_plans (
        plan_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
        turn_id TEXT NOT NULL, task_id TEXT NOT NULL, plan_revision INTEGER NOT NULL,
        task_json TEXT NOT NULL, UNIQUE(conversation_id, turn_id, task_id, plan_revision))""")
    connection.execute("""INSERT OR IGNORE INTO cognitive_execution_plans
        (conversation_id, turn_id, task_id, plan_revision, task_json) VALUES (?, ?, ?, ?, ?)""",
        (conversation_id, turn_id, task["taskId"], task.get("planRevision", 0),
         json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))


def append_reused_step_binding(connection: sqlite3.Connection, *, conversation_id: str,
                               turn_id: str, binding: Mapping) -> None:
    """Store consumption separately: reusing a result is not another execution."""
    ensure_schema(connection)
    receipt_ids = set(binding.get("receiptIds") or [])
    known = {row[0] for row in connection.execute("""SELECT tool_call_id FROM cognitive_execution_evidence
        WHERE conversation_id=? AND turn_id=?""", (conversation_id, turn_id))}
    if not receipt_ids or not receipt_ids <= known:
        raise ValueError("step reuse requires existing execution receipts")
    connection.execute("""CREATE TABLE IF NOT EXISTS cognitive_reused_step_bindings (
        binding_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
        turn_id TEXT NOT NULL, task_id TEXT NOT NULL, plan_revision INTEGER NOT NULL,
        binding_json TEXT NOT NULL, UNIQUE(conversation_id, task_id, plan_revision, binding_json))""")
    connection.execute("""INSERT OR IGNORE INTO cognitive_reused_step_bindings
        (conversation_id, turn_id, task_id, plan_revision, binding_json) VALUES (?, ?, ?, ?, ?)""",
        (conversation_id, turn_id, binding["taskId"], binding["planRevision"],
         json.dumps(dict(binding), ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
