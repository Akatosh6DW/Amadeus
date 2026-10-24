"""SQLite persistence and reminder leasing for the Amadeus Todo domain."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import json
import re
import sqlite3
from typing import Any, Callable, Mapping, Sequence
import unicodedata
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.fingerprints import legacy_persistent_fingerprint
from agent.task_recurrence import next_occurrence, normalize_recurrence_rule
from agent.todo_architecture import recent_todo_task_ids


TODO_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS todo_items (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    on_due TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'normal',
    due_at TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    timezone_name TEXT NOT NULL DEFAULT '',
    remind_before_minutes INTEGER NOT NULL DEFAULT 0,
    recurrence_rule TEXT NOT NULL DEFAULT '',
    recurrence_anchor_at TEXT NOT NULL DEFAULT '',
    snoozed_until TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    source_turn_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_todo_items_status_due
ON todo_items(status, due_at, updated_at);

CREATE TABLE IF NOT EXISTS todo_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todo_events_task_time
ON todo_events(task_id, created_at);

CREATE TABLE IF NOT EXISTS todo_reminders (
    reminder_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    reminder_kind TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todo_reminders_due
ON todo_reminders(status, available_at, scheduled_at);

CREATE TABLE IF NOT EXISTS todo_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    occurrence_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    claimed_at TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    receipt_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '',
    next_occurrence_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, occurrence_at)
);
CREATE INDEX IF NOT EXISTS idx_todo_runs_task_time
ON todo_runs(task_id, occurrence_at, created_at);
CREATE INDEX IF NOT EXISTS idx_todo_runs_status
ON todo_runs(status, occurrence_at);

CREATE TABLE IF NOT EXISTS todo_daily_openings (
    timezone_name TEXT NOT NULL,
    local_date TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    PRIMARY KEY(timezone_name, local_date)
);
"""


class TodoVersionConflict(RuntimeError):
    def __init__(self, task: Mapping[str, Any], expected_version: int) -> None:
        super().__init__("todo_version_conflict")
        self.task = dict(task)
        self.expected_version = int(expected_version)


class TodoResultMismatch(RuntimeError):
    def __init__(self, task: Mapping[str, Any], verification: Mapping[str, Any]) -> None:
        super().__init__("todo_result_mismatch")
        self.task = dict(task)
        self.verification = dict(verification)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _local_timezone() -> Any:
    """Return the host's local timezone for tasks without an explicit zone."""

    return datetime.now().astimezone().tzinfo or timezone.utc


def iso_utc(value: datetime | None = None) -> str:
    active = value or utc_now()
    if active.tzinfo is None:
        active = active.replace(tzinfo=timezone.utc)
    return active.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def ensure_todo_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(TODO_SCHEMA_SQL)
    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in connection.execute("PRAGMA table_info(todo_items)").fetchall()
    }
    if "recurrence_rule" not in columns:
        connection.execute(
            "ALTER TABLE todo_items ADD COLUMN recurrence_rule TEXT NOT NULL DEFAULT ''"
        )
    if "due_date" not in columns:
        connection.execute(
            "ALTER TABLE todo_items ADD COLUMN due_date TEXT NOT NULL DEFAULT ''"
        )
    if "on_due" not in columns:
        connection.execute("ALTER TABLE todo_items ADD COLUMN on_due TEXT NOT NULL DEFAULT ''")
    if "recurrence_anchor_at" not in columns:
        connection.execute(
            "ALTER TABLE todo_items ADD COLUMN recurrence_anchor_at TEXT NOT NULL DEFAULT ''"
        )


def _row_dict(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    value = dict(row)
    return {
        "taskId": str(value.get("task_id") or ""),
        "title": str(value.get("title") or ""),
        "details": str(value.get("details") or ""),
        "onDue": str(value.get("on_due") or ""),
        "status": str(value.get("status") or "pending"),
        "priority": str(value.get("priority") or "normal"),
        "dueAt": str(value.get("due_at") or ""),
        "dueDate": str(value.get("due_date") or ""),
        "allDay": not bool(value.get("due_at")),
        "timezone": str(value.get("timezone_name") or ""),
        "remindBeforeMinutes": int(value.get("remind_before_minutes") or 0),
        "recurrenceRule": str(value.get("recurrence_rule") or ""),
        "recurrenceAnchorAt": str(value.get("recurrence_anchor_at") or ""),
        "snoozedUntil": str(value.get("snoozed_until") or ""),
        "version": int(value.get("version") or 1),
        "sourceTurnId": str(value.get("source_turn_id") or ""),
        "createdAt": str(value.get("created_at") or ""),
        "updatedAt": str(value.get("updated_at") or ""),
        "completedAt": str(value.get("completed_at") or ""),
        "deletedAt": str(value.get("deleted_at") or ""),
    }


def read_todo(connection: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM todo_items WHERE task_id = ?",
        (str(task_id or ""),),
    ).fetchone()
    return _row_dict(row)


def verify_todo_state(task: Mapping[str, Any], expected: Mapping[str, Any], *, task_id: str) -> dict[str, Any]:
    """Compare a resolved requested state with a database read, not two commands."""
    mismatched = []
    if not task or task.get("taskId") != task_id:
        mismatched.append("taskId")
    for key, wanted in expected.items():
        actual = task.get(key)
        if key in {"dueAt", "snoozedUntil"} and wanted and actual:
            matches = parse_iso(wanted) is not None and parse_iso(wanted) == parse_iso(actual)
        else:
            matches = actual == wanted
        if not matches:
            mismatched.append(key)
    return {"status": "verified" if not mismatched else "mismatch",
            "source": "local_sqlite", "taskId": task_id,
            "observedVersion": task.get("version"), "mismatchedFields": mismatched}


def list_todos(
    connection: sqlite3.Connection,
    *,
    scope: str = "active",
    limit: int = 200,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    reactivate_daily_all_day_todos(connection, now=now)
    normalized = str(scope or "active").strip().lower()
    where = {
        "active": "status IN ('pending','in_progress')",
        "pending": "status IN ('pending','in_progress')",
        "paused": "status = 'paused'",
        "completed": "status = 'completed'",
        "deleted": "status = 'deleted'",
        "all": "status <> 'deleted'",
    }.get(normalized, "status IN ('pending','in_progress')")
    rows = connection.execute(
        f"""
        SELECT * FROM todo_items
        WHERE {where}
        ORDER BY
            CASE status WHEN 'in_progress' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
            CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
            CASE WHEN due_at = '' THEN 1 ELSE 0 END,
            due_at ASC,
            due_date ASC,
            updated_at DESC
        LIMIT ?
        """,
        (max(1, min(500, int(limit))),),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def todo_change_summary(
    connection: sqlite3.Connection,
    *,
    since_revision: int = 0,
) -> dict[str, int]:
    """Return the authoritative Todo event revision and unseen change count.

    SQLite ``rowid`` is monotonic for the lifetime of this event log and gives
    the UI a cheap invalidation cursor.  If the database was replaced and a
    client presents a cursor newer than the new log, count the replacement
    log as unseen instead of leaving that client permanently stuck.
    """

    try:
        seen_revision = max(0, int(since_revision))
    except (TypeError, ValueError):
        seen_revision = 0
    row = connection.execute(
        "SELECT COALESCE(MAX(rowid), 0) AS revision, COUNT(*) AS event_count "
        "FROM todo_events"
    ).fetchone()
    revision = int(row["revision"] or 0) if row is not None else 0
    event_count = int(row["event_count"] or 0) if row is not None else 0
    if revision <= 0 or seen_revision == revision:
        unseen_count = 0
    elif seen_revision > revision:
        unseen_count = event_count
    else:
        unseen = connection.execute(
            "SELECT COUNT(*) AS event_count FROM todo_events WHERE rowid > ?",
            (seen_revision,),
        ).fetchone()
        unseen_count = int(unseen["event_count"] or 0) if unseen is not None else 0
    return {
        "revision": revision,
        "unseenCount": max(0, unseen_count),
    }


def _normalized_search_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)


def search_todos(
    connection: sqlite3.Connection,
    target_ref: Mapping[str, Any] | None = None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return deterministic candidates for a model-produced task reference."""

    target = dict(target_ref or {})
    reactivate_daily_all_day_todos(connection)
    task_id = str(target.get("taskId") or target.get("task_id") or "").strip()
    if task_id:
        exact = read_todo(connection, task_id)
        return [{**exact, "matchType": "task_id", "matchScore": 1000}] if exact else []

    status = str(target.get("status") or target.get("scope") or "active").strip().lower()
    scope = status if status in {"active", "pending", "paused", "completed", "deleted", "all"} else "active"
    items = list_todos(connection, scope=scope, limit=500)
    if status == "in_progress":
        items = [item for item in items if item.get("status") == "in_progress"]

    due_after = parse_iso(target.get("dueAfter") or target.get("due_after"))
    due_before = parse_iso(target.get("dueBefore") or target.get("due_before"))
    if due_after or due_before:
        bounded: list[dict[str, Any]] = []
        for item in items:
            due_at = parse_iso(item.get("dueAt"))
            if due_at is None:
                if _all_day_overlaps_range(item, due_after, due_before):
                    bounded.append(item)
                continue
            if due_after and due_at < due_after:
                continue
            if due_before and due_at > due_before:
                continue
            bounded.append(item)
        items = bounded

    recent_raw = target.get("recentTaskIds") or target.get("recent_task_ids") or []
    recent_ids = [
        str(item).strip() for item in recent_raw
        if str(item).strip()
    ] if isinstance(recent_raw, (list, tuple)) else []
    relative = str(target.get("relative") or target.get("relativeReference") or "").strip()
    if relative and recent_ids:
        by_id = {str(item.get("taskId") or ""): item for item in items}
        recent_matches = [by_id[task_id] for task_id in recent_ids if task_id in by_id]
        if recent_matches:
            items = recent_matches

    title_query = str(target.get("title") or "").strip()
    general_query = str(target.get("query") or target.get("text") or "").strip()
    needle = _normalized_search_text(title_query or general_query)
    ranked: list[dict[str, Any]] = []
    for position, item in enumerate(items):
        title = _normalized_search_text(item.get("title"))
        details = _normalized_search_text(item.get("details"))
        match_type = "filter"
        score = max(1, 100 - position)
        if needle:
            if needle == title:
                match_type, score = "exact_title", 900
            elif needle and needle in title:
                match_type, score = "title_contains", 760
            elif title and title in needle:
                match_type, score = "query_contains_title", 700
            elif needle and needle in details:
                match_type, score = "details_contains", 520
            else:
                similarity = SequenceMatcher(None, needle, title).ratio() if title else 0.0
                if similarity < 0.55:
                    continue
                match_type, score = "similar_title", int(400 + similarity * 200)
        if str(item.get("taskId") or "") in recent_ids:
            score += max(0, 40 - recent_ids.index(str(item.get("taskId") or "")) * 4)
        ranked.append({**item, "matchType": match_type, "matchScore": score})
    ranked.sort(key=lambda item: (-int(item.get("matchScore") or 0), str(item.get("dueAt") or "")))
    return ranked[:max(1, min(100, int(limit)))]


def match_active_todos_to_dialogue(
    connection: sqlite3.Connection,
    current_text: str,
    recent_messages: Sequence[Mapping[str, Any]] | None = None,
    *,
    limit: int = 4,
) -> dict[str, Any]:
    """Read bounded Todo context for the model; never classify intent or force tools.

    Text search only ranks rows when there are many tasks. A missing title or
    lifecycle keyword must not hide all tasks from the conversational model.
    """

    active_items = list_todos(connection, scope="active", limit=500)
    compact_current = " ".join(str(current_text or "").split()).strip()[:500]
    result: dict[str, Any] = {
        "active": bool(active_items),
        "activeTaskCount": len(active_items),
        "strongMatch": False,
        "requiresModelDecision": False,
        "matchSource": "none",
        "candidates": [],
        "recentDialogue": [],
        "truncated": False,
    }
    if not active_items or not compact_current:
        return result

    active_ids = {str(item.get("taskId") or "") for item in active_items}

    def ranked_for(text: str) -> list[dict[str, Any]]:
        return [
            item for item in search_todos(
                connection,
                {"query": text, "status": "active"},
                limit=max(8, limit * 2),
            )
            if str(item.get("taskId") or "") in active_ids
        ]

    direct = ranked_for(compact_current)
    source = "current_utterance"
    threshold = 500
    ranked = direct

    recent_dialogue = [
        {"role": str(message["role"]), "content": str(message.get("content") or "")[:300]}
        for message in tuple(recent_messages or ())[-4:]
        if isinstance(message, Mapping) and message.get("role") in {"user", "assistant"}
        and str(message.get("content") or "").strip() != compact_current
    ]

    best_score = int(ranked[0].get("matchScore") or 0) if ranked else 0
    strong_match = best_score >= threshold
    if strong_match:
        ranked = [
            item for item in ranked
            if int(item.get("matchScore") or 0) >= threshold
            and best_score - int(item.get("matchScore") or 0) <= 80
        ]
    else:
        # These are context candidates, not a tool obligation. Do not make a
        # regex the gatekeeper for understanding natural progress reports.
        source = "active_inventory"
        ranked = active_items

    for message in reversed(tuple(recent_messages or ())):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        referenced_ids = list(recent_todo_task_ids([message]))
        for container in (message, message.get("meta"), message.get("metadata")):
            if not isinstance(container, Mapping):
                continue
            reminder_id = str(container.get("proactiveEventId") or "")
            if container.get("proactiveKind") == "todo_reminder" and reminder_id:
                # Older delivered reminders have an event ID but no todoContext.
                row = connection.execute(
                    "SELECT task_id, task_version, scheduled_at, reminder_kind FROM todo_reminders WHERE reminder_id = ?",
                    (reminder_id,),
                ).fetchone()
                if row is not None:
                    task_id = str(row["task_id"])
                    current = next((item for item in active_items if item["taskId"] == task_id), {})
                    if str(row["reminder_kind"]) == "all_day":
                        scheduled = parse_iso(row["scheduled_at"])
                        advanced = bool(current.get("recurrenceRule") and scheduled and
                            _task_local_now(current, utc_now()).date() > _task_local_now(current, scheduled).date())
                    else:
                        advanced = bool(
                            current.get("recurrenceRule")
                            and int(current.get("version") or 0) != int(row["task_version"])
                            and (parse_iso(current.get("dueAt")) or utc_now()) > (parse_iso(row["scheduled_at"]) or utc_now())
                        )
                    if advanced:
                        # A daily reminder may already have advanced on delivery.
                        # Its old occurrence cannot identify tomorrow's task.
                        referenced_ids = [item for item in referenced_ids if item != task_id]
                        ranked = [item for item in ranked if item["taskId"] != task_id]
                        result["previousReminder"] = {
                            "taskId": task_id, "scheduledAt": str(row["scheduled_at"]),
                            "taskVersion": int(row["task_version"]), "recurrenceAdvanced": True,
                            "nextDueAt": current.get("dueAt"),
                        }
                    else:
                        referenced_ids.append(task_id)
        by_id = {str(item.get("taskId") or ""): item for item in active_items}
        bound = [by_id[task_id] for task_id in dict.fromkeys(referenced_ids) if task_id in by_id]
        if bound:
            if not strong_match:
                ranked = [{**item, "matchType": "recent_task_reference"} for item in bound]
                source = "recent_task_reference"
            recent_dialogue = [{"role": "assistant", "content": str(message.get("content") or "")[:500]}]
        # Never silently bind an older topic past the nearest assistant turn.
        break

    candidates = [
        {
            "taskId": str(item.get("taskId") or ""),
            "title": str(item.get("title") or ""),
            "details": str(item.get("details") or "")[:4000],
            "onDue": str(item.get("onDue") or "")[:4000],
            "status": str(item.get("status") or "pending"),
            "priority": str(item.get("priority") or "normal"),
            "dueAt": str(item.get("dueAt") or ""),
            "dueDate": str(item.get("dueDate") or ""),
            "allDay": not bool(item.get("dueAt")),
            "timezone": str(item.get("timezone") or ""),
            "remindBeforeMinutes": int(item.get("remindBeforeMinutes") or 0),
            "recurrenceRule": str(item.get("recurrenceRule") or ""),
            "snoozedUntil": str(item.get("snoozedUntil") or ""),
            "version": int(item.get("version") or 0),
            "matchType": str(item.get("matchType") or ""),
            "matchScore": int(item.get("matchScore") or 0),
        }
        for item in ranked
    ][:max(1, min(8, int(limit)))]
    result.update({
        "strongMatch": False,  # Compatibility diagnostic; never a tool trigger.
        "requiresModelDecision": bool(candidates),
        "matchSource": source if candidates else "none",
        "candidates": candidates,
        "recentDialogue": recent_dialogue,
        "truncated": len(candidates) < len(ranked),
    })
    return result


def resolve_task_reference(
    connection: sqlite3.Connection,
    target_ref: Mapping[str, Any] | None,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    """Resolve to zero/one/many candidates without allowing the model to invent IDs."""

    target = dict(target_ref or {})
    candidates = search_todos(connection, target, limit=max(limit, 20))
    if not candidates:
        return {"status": "not_found", "task": None, "candidates": []}
    if str(target.get("taskId") or target.get("task_id") or "").strip():
        return {
            "status": "resolved", "task": candidates[0],
            "candidates": candidates[:1], "matchType": "task_id",
        }
    try:
        ordinal = int(target.get("ordinal") or target.get("index") or 0)
    except (TypeError, ValueError):
        ordinal = 0
    if ordinal:
        if 1 <= ordinal <= len(candidates):
            selected = candidates[ordinal - 1]
            return {
                "status": "resolved", "task": selected,
                "candidates": candidates[:limit], "matchType": "ordinal",
            }
        return {
            "status": "not_found", "task": None,
            "candidates": candidates[:limit], "error": "todo_ordinal_out_of_range",
        }
    if len(candidates) == 1:
        return {
            "status": "resolved", "task": candidates[0],
            "candidates": candidates, "matchType": candidates[0].get("matchType"),
        }
    exact = [item for item in candidates if item.get("matchType") in {"task_id", "exact_title"}]
    if len(exact) == 1:
        return {
            "status": "resolved", "task": exact[0],
            "candidates": candidates[:limit], "matchType": exact[0].get("matchType"),
        }
    return {
        "status": "ambiguous", "task": None,
        "candidates": candidates[:limit],
    }


def _check_expected_version(current: Mapping[str, Any], expected_version: int) -> None:
    if expected_version and int(current.get("version") or 0) != int(expected_version):
        raise TodoVersionConflict(current, expected_version)


def _recurrence_anchor(
    recurrence_rule: Any,
    *,
    due_at: Any = "",
    due_date: Any = "",
    timezone_name: Any = "",
    fallback: datetime | None = None,
) -> str:
    """Choose the stable DTSTART persisted with a recurring task."""

    if not normalize_recurrence_rule(recurrence_rule):
        return ""
    parsed_due = parse_iso(due_at)
    if parsed_due is not None:
        return iso_utc(parsed_due)
    date_text = str(due_date or "").strip()
    if date_text:
        try:
            day = datetime.fromisoformat(date_text).date()
            zone_name = str(timezone_name or "").strip()
            zone = ZoneInfo(zone_name) if zone_name else _local_timezone()
            return iso_utc(datetime(day.year, day.month, day.day, tzinfo=zone))
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            pass
    return iso_utc(fallback or utc_now())


def _task_recurrence_anchor(task: Mapping[str, Any]) -> datetime | None:
    anchor = parse_iso(task.get("recurrenceAnchorAt"))
    if anchor is not None:
        return anchor
    return parse_iso(task.get("dueAt")) or parse_iso(task.get("createdAt"))


def _event(
    connection: sqlite3.Connection,
    task_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> None:
    connection.execute(
        "INSERT INTO todo_events(event_id, task_id, event_type, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            f"todo-event-{uuid.uuid4().hex}",
            task_id,
            event_type,
            json.dumps(dict(payload or {}), ensure_ascii=False, separators=(",", ":")),
            iso_utc(now),
        ),
    )


def _reminder_id(task_id: str, version: int, kind: str, scheduled_at: str) -> str:
    digest = legacy_persistent_fingerprint(
        f"{task_id}\x1f{version}\x1f{kind}\x1f{scheduled_at}",
        length=24,
    )
    return f"todo-reminder-{digest}"


def rebuild_todo_reminders(
    connection: sqlite3.Connection,
    task: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    active_now = now or utc_now()
    task_id = str(task.get("taskId") or "")
    version = int(task.get("version") or 1)
    connection.execute(
        "UPDATE todo_reminders SET status='cancelled', updated_at=? "
        "WHERE task_id=? AND reminder_kind <> 'execution' "
        "AND status IN ('pending','claimed')",
        (iso_utc(active_now), task_id),
    )
    if str(task.get("status") or "pending") not in {"pending", "in_progress"}:
        return

    schedules: list[tuple[str, datetime]] = []
    snoozed = parse_iso(task.get("snoozedUntil"))
    due = parse_iso(task.get("dueAt"))
    remind_before = max(0, int(task.get("remindBeforeMinutes") or 0))
    if snoozed is not None and snoozed > active_now:
        schedules.append(("snoozed", snoozed))
    elif due is not None:
        if remind_before > 0:
            schedules.append(("upcoming", due - timedelta(minutes=remind_before)))
        schedules.append(("due", due))

    timestamp = iso_utc(active_now)
    for kind, scheduled in schedules:
        scheduled_at = iso_utc(scheduled)
        reminder_id = _reminder_id(task_id, version, kind, scheduled_at)
        connection.execute(
            """
            INSERT OR REPLACE INTO todo_reminders(
                reminder_id, task_id, task_version, reminder_kind,
                scheduled_at, available_at, status, attempt_count,
                claimed_at, delivered_at, last_error, payload_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, '', '', '', '{}', ?, ?)
            """,
            (
                reminder_id,
                task_id,
                version,
                kind,
                scheduled_at,
                scheduled_at,
                timestamp,
                timestamp,
            ),
        )


# 同时写入任务、创建事件并重建提醒；有具体时刻时不再保留全天日期字段。
def create_todo(
    connection: sqlite3.Connection,
    *,
    title: str,
    details: str = "",
    on_due: str = "",
    due_at: str = "",
    due_date: str = "",
    timezone_name: str = "",
    remind_before_minutes: int = 0,
    priority: str = "normal",
    recurrence_rule: str = "",
    source_turn_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    active_now = now or utc_now()
    timestamp = iso_utc(active_now)
    task_id = f"todo-{uuid.uuid4().hex}"
    normalized_recurrence = normalize_recurrence_rule(recurrence_rule)
    stored_due_at = str(due_at or "").strip()
    stored_due_date = str(due_date or "").strip() if not stored_due_at else ""
    recurrence_anchor = _recurrence_anchor(
        normalized_recurrence,
        due_at=stored_due_at,
        due_date=stored_due_date,
        timezone_name=timezone_name,
        fallback=active_now,
    )
    connection.execute(
        """
        INSERT INTO todo_items(
            task_id, title, details, on_due, status, priority, due_at, due_date, timezone_name,
            remind_before_minutes, recurrence_rule, recurrence_anchor_at, snoozed_until,
            version, source_turn_id, created_at, updated_at, completed_at, deleted_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, '', 1, ?, ?, ?, '', '')
        """,
        (
            task_id,
            str(title).strip(),
            str(details or "").strip(),
            str(on_due or "").strip(),
            priority,
            stored_due_at,
            stored_due_date,
            timezone_name,
            max(0, int(remind_before_minutes)),
            normalized_recurrence,
            recurrence_anchor,
            str(source_turn_id or "")[:100],
            timestamp,
            timestamp,
        ),
    )
    task = read_todo(connection, task_id)
    _event(connection, task_id, "created", task, now=active_now)
    rebuild_todo_reminders(connection, task, now=active_now)
    return task


def update_todo(
    connection: sqlite3.Connection,
    task_id: str,
    changes: Mapping[str, Any],
    *,
    now: datetime | None = None,
    expected_version: int = 0,
) -> dict[str, Any]:
    current = read_todo(connection, task_id)
    if not current or current.get("status") == "deleted":
        return {}
    _check_expected_version(current, expected_version)
    active_now = now or utc_now()
    allowed = {
        "title": "title",
        "details": "details",
        "onDue": "on_due",
        "priority": "priority",
        "dueAt": "due_at",
        "dueDate": "due_date",
        "timezone": "timezone_name",
        "remindBeforeMinutes": "remind_before_minutes",
        "recurrenceRule": "recurrence_rule",
    }
    changes = dict(changes)
    if "recurrenceRule" in changes:
        changes["recurrenceRule"] = normalize_recurrence_rule(changes.get("recurrenceRule"))
    if changes.get("dueAt"):
        changes["dueDate"] = ""
    elif "dueDate" in changes and changes.get("dueDate"):
        changes["dueAt"] = ""
    assignments: list[str] = []
    values: list[Any] = []
    for public_name, column in allowed.items():
        if public_name not in changes:
            continue
        assignments.append(f"{column} = ?")
        values.append(changes[public_name])
    if any(name in changes for name in ("recurrenceRule", "dueAt", "dueDate", "timezone")):
        next_rule = changes.get("recurrenceRule", current.get("recurrenceRule"))
        next_due_at = changes.get("dueAt", current.get("dueAt"))
        next_due_date = changes.get("dueDate", current.get("dueDate"))
        next_timezone = changes.get("timezone", current.get("timezone"))
        assignments.append("recurrence_anchor_at = ?")
        if (
            normalize_recurrence_rule(next_rule)
            and "dueAt" not in changes
            and "dueDate" not in changes
            and str(current.get("recurrenceAnchorAt") or "").strip()
        ):
            # Editing the rule changes its future pattern; it must not move
            # DTSTART to the already-advanced current occurrence.
            values.append(str(current.get("recurrenceAnchorAt") or ""))
        else:
            values.append(_recurrence_anchor(
                next_rule,
                due_at=next_due_at,
                due_date=next_due_date,
                timezone_name=next_timezone,
                fallback=parse_iso(current.get("recurrenceAnchorAt")) or active_now,
            ))
    if not assignments:
        return current
    assignments.extend(("snoozed_until = ''", "version = version + 1", "updated_at = ?"))
    values.extend((iso_utc(active_now), task_id))
    where = "task_id = ?"
    if expected_version:
        where += " AND version = ?"
        values.append(int(expected_version))
    cursor = connection.execute(
        f"UPDATE todo_items SET {', '.join(assignments)} WHERE {where}",
        values,
    )
    if expected_version and cursor.rowcount != 1:
        latest = read_todo(connection, task_id)
        if latest:
            raise TodoVersionConflict(latest, expected_version)
        return {}
    task = read_todo(connection, task_id)
    _event(connection, task_id, "updated", dict(changes), now=active_now)
    rebuild_todo_reminders(connection, task, now=active_now)
    return task


def next_recurring_due_at(
    task: Mapping[str, Any],
    *,
    after: datetime | None = None,
) -> str:
    """Return the next occurrence, coalescing occurrences missed while offline."""

    if not normalize_recurrence_rule(task.get("recurrenceRule")):
        return ""
    due = parse_iso(task.get("dueAt"))
    if due is None:
        return ""
    active_after = after or utc_now()
    if active_after.tzinfo is None:
        active_after = active_after.replace(tzinfo=timezone.utc)
    timezone_name = str(task.get("timezone") or "").strip()
    try:
        local_zone = ZoneInfo(timezone_name) if timezone_name else timezone.utc
    except ZoneInfoNotFoundError:
        local_zone = timezone.utc
    anchor = _task_recurrence_anchor(task) or due
    next_local = next_occurrence(
        task.get("recurrenceRule"),
        anchor=anchor.astimezone(local_zone),
        after=active_after.astimezone(local_zone),
    )
    return iso_utc(next_local) if next_local is not None else ""


def _advance_task_occurrence(
    connection: sqlite3.Connection,
    current: Mapping[str, Any],
    *,
    now: datetime,
    event_type: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Advance one current timed occurrence under an optional version guard."""

    task_id = str(current.get("taskId") or "")
    if not task_id or not current.get("dueAt"):
        return {}
    next_due = next_recurring_due_at(current, after=now)
    version = int(current.get("version") or 0)
    where = "task_id=? AND status IN ('pending','in_progress')"
    parameters: list[Any] = [iso_utc(now), task_id]
    if expected_version is not None:
        where += " AND version=?"
        parameters.append(int(expected_version))
    if not next_due:
        # COUNT/UNTIL exhaustion is a completed schedule, rather than a task
        # that remains permanently due at its last occurrence.
        cursor = connection.execute(
            f"UPDATE todo_items SET status='completed', completed_at=?, "
            f"snoozed_until='', version=version+1, updated_at=? WHERE {where}",
            [iso_utc(now), iso_utc(now), *parameters[1:]],
        )
        if cursor.rowcount != 1:
            return {}
        task = read_todo(connection, task_id)
        _event(connection, task_id, "recurrence_finished", {
            "previousDueAt": current.get("dueAt"),
            "previousVersion": version,
        }, now=now)
        rebuild_todo_reminders(connection, task, now=now)
        return task
    cursor = connection.execute(
        f"""
        UPDATE todo_items
        SET due_at=?, status='pending', completed_at='', snoozed_until='',
            version=version+1, updated_at=?
        WHERE {where}
        """,
        [next_due, iso_utc(now), *parameters[1:]],
    )
    if cursor.rowcount != 1:
        return {}
    task = read_todo(connection, task_id)
    _event(
        connection,
        task_id,
        event_type,
        {"previousDueAt": current.get("dueAt"), "nextDueAt": next_due},
        now=now,
    )
    rebuild_todo_reminders(connection, task, now=now)
    return task


def advance_recurring_todo(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    now: datetime | None = None,
    event_type: str = "recurrence_advanced",
) -> dict[str, Any]:
    active_now = now or utc_now()
    current = read_todo(connection, task_id)
    if not current:
        return {}
    return _advance_task_occurrence(
        connection,
        current,
        now=active_now,
        event_type=event_type,
    )


def _run_json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"value": value}
        return dict(parsed) if isinstance(parsed, Mapping) else {"value": parsed}
    return {}


def _run_dict(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    value = dict(row)
    return {
        "runId": str(value.get("run_id") or ""),
        "taskId": str(value.get("task_id") or ""),
        "taskVersion": int(value.get("task_version") or 0),
        "occurrenceAt": str(value.get("occurrence_at") or ""),
        "status": str(value.get("status") or ""),
        "attemptCount": int(value.get("attempt_count") or 0),
        "claimedAt": str(value.get("claimed_at") or ""),
        "startedAt": str(value.get("started_at") or ""),
        "finishedAt": str(value.get("finished_at") or ""),
        "result": _run_json(value.get("result_json")),
        "receipt": _run_json(value.get("receipt_json")),
        "lastError": str(value.get("last_error") or ""),
        "nextOccurrenceAt": str(value.get("next_occurrence_at") or ""),
        "createdAt": str(value.get("created_at") or ""),
        "updatedAt": str(value.get("updated_at") or ""),
    }


def claim_due_todo_run(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    task_id: str = "",
) -> dict[str, Any]:
    """Claim one due onDue occurrence exactly once.

    A run is keyed by the task and its persisted occurrence time.  Terminal
    rows and an unresolved ``started`` row are both returned as unavailable;
    callers can inspect them through ``read_todo_run`` instead of rerunning an
    operation whose external side effect is unknown.
    """

    active_now = now or utc_now()
    # A user may have completed an all-day recurring task through the UI.
    # Background execution has no client poll to perform the usual daily
    # reactivation, so do that small durable transition before selecting a
    # due occurrence.
    reactivate_daily_all_day_todos(connection, now=active_now)
    timestamp = iso_utc(active_now)
    where = [
        "i.status IN ('pending','in_progress')",
        "i.on_due <> ''",
        "(i.due_at = '' OR i.due_at <= ?)",
        "(i.snoozed_until = '' OR i.snoozed_until <= ?)",
    ]
    parameters: list[Any] = [timestamp, timestamp]
    if str(task_id or "").strip():
        where.append("i.task_id = ?")
        parameters.append(str(task_id).strip())
    rows = connection.execute(
        "SELECT i.* FROM todo_items i WHERE " + " AND ".join(where) +
        " ORDER BY CASE WHEN i.due_at='' THEN 1 ELSE 0 END, "
        "i.due_at ASC, i.due_date ASC, i.created_at ASC",
        parameters,
    )
    for row in rows:
        task = _row_dict(row)
        try:
            occurrence = _task_occurrence_key(task, active_now)
        except (TypeError, ValueError):
            continue
        if not occurrence:
            continue
        # A task may be edited while an earlier external action is still
        # unresolved.  Do not start a second action for that task even if the
        # edited due time has a different occurrence key.
        started = connection.execute(
            "SELECT 1 FROM todo_runs WHERE task_id=? AND status='started' LIMIT 1",
            (task["taskId"],),
        ).fetchone()
        if started is not None:
            continue
        existing = connection.execute(
            "SELECT * FROM todo_runs WHERE task_id=? AND occurrence_at=?",
            (task["taskId"], occurrence),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["status"] or "") in {"completed", "failed", "unknown", "cancelled"}
                and normalize_recurrence_rule(task.get("recurrenceRule"))
            ):
                if task.get("dueAt"):
                    _advance_task_occurrence(
                        connection,
                        task,
                        now=active_now,
                        event_type="scheduled_run_reconciled",
                        expected_version=int(task["version"]),
                    )
                else:
                    _advance_all_day_task_occurrence(
                        connection,
                        task,
                        occurrence_at=occurrence,
                        now=active_now,
                        event_type="scheduled_run_reconciled",
                        expected_version=int(task["version"]),
                    )
            continue
        if _migrate_legacy_execution(
            connection, task, now=active_now, occurrence_at=occurrence
        ):
            continue
        run_id = f"todo-run-{uuid.uuid4().hex}"
        receipt = json.dumps({"task": task}, ensure_ascii=False, separators=(",", ":"))
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO todo_runs(
                run_id, task_id, task_version, occurrence_at, status, attempt_count,
                claimed_at, started_at, receipt_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'started', 1, ?, ?, ?, ?, ?)
            """,
            (run_id, task["taskId"], task["version"], occurrence,
             timestamp, timestamp, receipt, timestamp, timestamp),
        )
        if inserted.rowcount != 1:
            continue
        run = read_todo_run(connection, run_id)
        return {
            "runId": run_id,
            "task": task,
            "taskId": task["taskId"],
            "taskVersion": int(task["version"]),
            "occurrenceAt": occurrence,
            "scheduledAt": occurrence,
            "status": "started",
            "attemptCount": 1,
            "run": run,
        }
    return {}


def read_todo_run(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM todo_runs WHERE run_id=?", (str(run_id or ""),)
    ).fetchone()
    return _run_dict(row)


def list_todo_runs(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM todo_runs WHERE task_id=? "
        "ORDER BY started_at DESC, rowid DESC LIMIT ?",
        (str(task_id or ""), max(1, min(500, int(limit)))),
    ).fetchall()
    return [_run_dict(row) for row in rows]


def _migrate_legacy_execution(
    connection: sqlite3.Connection,
    task: Mapping[str, Any],
    *,
    now: datetime,
    occurrence_at: str = "",
) -> bool:
    """Import the old reminder payload marker before a headless claim.

    Older clients stored only ``todo_reminders.payload_json.execution``.  A
    started marker is intentionally imported as an unresolved run; a finished
    result is finalized through the new idempotent path and therefore advances
    a recurring task without waiting for a browser acknowledgement.
    """

    task_id = str(task.get("taskId") or "")
    occurrence = str(occurrence_at or _task_occurrence_key(task, now) or "")
    if not task_id or not occurrence:
        return False
    rows = connection.execute(
        "SELECT * FROM todo_reminders WHERE task_id=? AND task_version=? "
        "AND status IN ('pending','claimed') "
        "AND reminder_kind IN ('due','snoozed','all_day') "
        "ORDER BY created_at DESC",
        (task_id, int(task.get("version") or 0)),
    ).fetchall()
    for row in rows:
        payload = _run_json(row["payload_json"])
        execution = payload.get("execution")
        if not isinstance(execution, Mapping):
            continue
        if not execution.get("result") and str(execution.get("status") or "") != "started":
            continue
        run_id = f"todo-run-legacy-{row['reminder_id']}"
        timestamp = iso_utc(now)
        receipt = json.dumps({"task": task}, ensure_ascii=False, separators=(",", ":"))
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO todo_runs(
                run_id, task_id, task_version, occurrence_at, status, attempt_count,
                claimed_at, started_at, receipt_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'started', 1, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, int(task.get("version") or 0), occurrence,
             timestamp, timestamp, receipt, timestamp, timestamp),
        )
        if inserted.rowcount != 1:
            return True
        result = execution.get("result")
        if isinstance(result, Mapping) and result:
            status = str(result.get("scheduledExecutionStatus") or "unknown")
            if status.strip().lower() not in {
                "completed", "failed", "unknown", "cancelled", "succeeded", "success", "done",
            }:
                status = "unknown"
            finish_todo_run(
                connection,
                run_id,
                status=status,
                result=result,
                receipt={"legacyReminderId": str(row["reminder_id"]), "task": task},
                now=now,
            )
        else:
            # A legacy started marker can be discovered after the new startup
            # recovery pass (for example, on the first poll after an upgrade).
            # The old marker cannot be distinguished from an interrupted
            # process claim, so resolve it once as unknown instead of leaving
            # the task permanently stuck in ``started``.
            finish_todo_run(
                connection,
                run_id,
                status="unknown",
                result={
                    "reply": "定时任务已经开始执行，但应用在结果确认前重启了；我不会重复执行。",
                    "spokenTextJa": "予約タスクは実行を開始したけれど、結果の確認前に再起動したわ。重複実行はしない。",
                    "scheduledExecutionStatus": "unknown",
                    "scheduledRunId": run_id,
                    "recovered": True,
                },
                receipt={
                    "legacyReminderId": str(row["reminder_id"]),
                    "task": task,
                    "recovered": True,
                },
                error="legacy_execution_started_without_terminal_receipt",
                now=now,
            )
        return True
    return False


def finish_todo_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    result: Mapping[str, Any] | None = None,
    receipt: Mapping[str, Any] | None = None,
    error: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a terminal outcome and advance the task in one store action."""

    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"succeeded", "success", "done"}:
        normalized_status = "completed"
    if normalized_status not in {"completed", "failed", "unknown", "cancelled"}:
        raise ValueError("invalid_todo_run_status")
    active_now = now or utc_now()
    timestamp = iso_utc(active_now)
    existing = connection.execute(
        "SELECT * FROM todo_runs WHERE run_id=?", (str(run_id or ""),)
    ).fetchone()
    if existing is None:
        return {}
    previous = _run_dict(existing)
    if previous["status"] in {"completed", "failed", "unknown", "cancelled"}:
        task = read_todo(connection, previous["taskId"])
        return {"run": previous, "task": task, "recurrenceAdvanced": False,
                "nextDueAt": task.get("dueAt", "") if task else "",
                "deliveryReminderId": f"todo-execution-{str(run_id or '')}"}
    result_payload = _run_json(result)
    receipt_payload = _run_json(receipt)
    # Recovery and legacy migration add metadata after the claim.  Preserve
    # the original task snapshot captured at claim time even when their
    # receipt omits it, so history remains useful after edits or deletion.
    previous_receipt = previous.get("receipt")
    if "task" not in receipt_payload and isinstance(previous_receipt, Mapping):
        previous_task = previous_receipt.get("task")
        if isinstance(previous_task, Mapping):
            receipt_payload["task"] = dict(previous_task)
    updated = connection.execute(
        """
        UPDATE todo_runs
        SET status=?, finished_at=?, result_json=?, receipt_json=?, last_error=?, updated_at=?
        WHERE run_id=? AND status NOT IN ('completed','failed','unknown','cancelled')
        """,
        (
            normalized_status,
            timestamp,
            json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")),
            json.dumps(receipt_payload, ensure_ascii=False, separators=(",", ":")),
            str(error or "")[:1000],
            timestamp,
            str(run_id or ""),
        ),
    )
    if updated.rowcount != 1:
        final = read_todo_run(connection, run_id)
        task = read_todo(connection, final.get("taskId", "")) if final else {}
        return {"run": final, "task": task, "recurrenceAdvanced": False,
                "nextDueAt": task.get("dueAt", "") if task else "",
                "deliveryReminderId": f"todo-execution-{str(run_id or '')}"}

    current = read_todo(connection, previous["taskId"])
    advanced = {}
    current_due = parse_iso(current.get("dueAt")) if current else None
    occurrence_matches = bool(
        current
        and (
            (current_due is not None and iso_utc(current_due) == previous["occurrenceAt"])
            or (not current.get("dueAt") and previous["occurrenceAt"])
        )
    )
    if (
        current
        and current.get("status") in {"pending", "in_progress"}
        and int(current.get("version") or 0) == int(previous["taskVersion"])
        and occurrence_matches
        and normalize_recurrence_rule(current.get("recurrenceRule"))
    ):
        if current.get("dueAt"):
            advanced = _advance_task_occurrence(
                connection,
                current,
                now=active_now,
                event_type="scheduled_run_finished",
                expected_version=previous["taskVersion"],
            )
        else:
            advanced = _advance_all_day_task_occurrence(
                connection,
                current,
                occurrence_at=previous["occurrenceAt"],
                now=active_now,
                event_type="scheduled_run_finished",
                expected_version=previous["taskVersion"],
            )
    elif (
        current
        and normalized_status == "completed"
        and not normalize_recurrence_rule(current.get("recurrenceRule"))
        and current.get("onDue")
        and current.get("status") in {"pending", "in_progress"}
        and int(current.get("version") or 0) == int(previous["taskVersion"])
    ):
        advanced = set_todo_status(
            connection,
            current["taskId"],
            "completed",
            now=active_now,
            expected_version=current["version"],
        )
    next_due = str(advanced.get("dueAt") or "") if advanced else ""
    next_due_date = str(advanced.get("dueDate") or "") if advanced else ""
    next_occurrence = next_due or next_due_date
    connection.execute(
        "UPDATE todo_runs SET next_occurrence_at=?, updated_at=? WHERE run_id=?",
        (next_occurrence, timestamp, str(run_id or "")),
    )
    delivery_task = advanced or read_todo(connection, previous["taskId"])
    receipt_task = receipt_payload.get("task")
    # The execution receipt is the authoritative instruction snapshot.  Keep
    # it in the delivery envelope even when recurrence advancement edits the
    # live task or a user deletes it after the worker finished.
    occurrence_task = (
        dict(receipt_task) if isinstance(receipt_task, Mapping) else delivery_task
    )
    execution_payload = {
        "runId": str(run_id or ""),
        "occurrenceAt": previous["occurrenceAt"],
        "executionStatus": normalized_status,
        "executionResult": result_payload,
        "receipt": receipt_payload,
        "taskSnapshot": occurrence_task,
        "nextTaskSnapshot": delivery_task,
    }
    # A legacy due reminder may still be pending/claimed when a headless run
    # finishes.  Its browser acknowledgement must not become a second state
    # transition; the execution delivery below is the sole result envelope.
    connection.execute(
        "UPDATE todo_reminders SET status='cancelled', updated_at=? "
        "WHERE task_id=? AND reminder_kind IN ('due','snoozed','all_day') "
        "AND task_version=? AND status IN ('pending','claimed')",
        (timestamp, previous["taskId"], int(previous["taskVersion"])),
    )
    delivery_id = f"todo-execution-{str(run_id or '')}"
    connection.execute(
        """
        INSERT OR IGNORE INTO todo_reminders(
            reminder_id, task_id, task_version, reminder_kind,
            scheduled_at, available_at, status, attempt_count,
            claimed_at, delivered_at, last_error, payload_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'execution', ?, ?, 'pending', 0, '', '', '', ?, ?, ?)
        """,
        (
            delivery_id,
            previous["taskId"],
            int(previous["taskVersion"]),
            timestamp,
            timestamp,
            json.dumps(execution_payload, ensure_ascii=False, separators=(",", ":")),
            timestamp,
            timestamp,
        ),
    )
    _event(connection, previous["taskId"], "todo_run_finished", {
        "runId": str(run_id or ""),
        "occurrenceAt": previous["occurrenceAt"],
        "status": normalized_status,
        "nextOccurrenceAt": next_occurrence,
        "error": str(error or "")[:1000],
    }, now=active_now)
    final = read_todo_run(connection, run_id)
    task = advanced or read_todo(connection, previous["taskId"])
    return {
        "run": final,
        "task": task,
        "recurrenceAdvanced": bool(advanced),
        "nextDueAt": next_due,
        "nextDueDate": next_due_date,
        "deliveryReminderId": delivery_id,
    }


def recover_started_todo_runs(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 0,
) -> list[dict[str, Any]]:
    """Resolve abandoned started runs as unknown after a restart.

    This never retries the external action.  It turns unresolved claims into
    terminal unknown receipts, allowing a recurring task to move to its next
    occurrence and making the result deliverable from the normal execution
    reminder queue.  Call this at startup with the default zero threshold
    while holding the process execution lock.  A positive threshold remains
    accepted for compatibility with older callers, but recovery should not be
    scheduled periodically because a slow live worker cannot be distinguished
    reliably from an abandoned one.
    """

    active_now = now or utc_now()
    threshold = int(stale_after_seconds)
    if threshold <= 0:
        rows = connection.execute(
            "SELECT run_id FROM todo_runs WHERE status='started' ORDER BY started_at ASC"
        ).fetchall()
    else:
        stale_before = iso_utc(active_now - timedelta(seconds=threshold))
        rows = connection.execute(
            "SELECT run_id FROM todo_runs WHERE status='started' AND started_at <= ? "
            "ORDER BY started_at ASC",
            (stale_before,),
        ).fetchall()
    recovered: list[dict[str, Any]] = []
    for row in rows:
        result = finish_todo_run(
            connection,
            str(row["run_id"]),
            status="unknown",
            result={
                "reply": "定时任务已经开始执行，但应用在结果确认前重启了；我不会重复执行。",
                "spokenTextJa": "予約タスクは実行を開始したけれど、結果の確認前に再起動したわ。重複実行はしない。",
                "scheduledExecutionStatus": "unknown",
                "scheduledRunId": str(row["run_id"]),
                "recovered": True,
            },
            receipt={"recovered": True},
            error="execution_started_without_terminal_receipt",
            now=active_now,
        )
        if result:
            recovered.append(result)
    return recovered


def set_todo_status(
    connection: sqlite3.Connection,
    task_id: str,
    status: str,
    *,
    now: datetime | None = None,
    expected_version: int = 0,
) -> dict[str, Any]:
    current = read_todo(connection, task_id)
    if not current:
        return {}
    if current.get("status") == "deleted":
        return current if status == "deleted" else {}
    if current.get("status") == status:
        return current
    _check_expected_version(current, expected_version)
    active_now = now or utc_now()
    if (status == "completed" and normalize_recurrence_rule(current.get("recurrenceRule"))
            and current.get("dueAt")):
        return advance_recurring_todo(
            connection,
            task_id,
            now=active_now,
            event_type="recurring_occurrence_completed",
        )
    timestamp = iso_utc(active_now)
    completed_at = timestamp if status == "completed" else ""
    deleted_at = timestamp if status == "deleted" else ""
    version_clause = " AND version=?" if expected_version else ""
    parameters: tuple[Any, ...] = (
        status, completed_at, deleted_at, timestamp, task_id,
        *((int(expected_version),) if expected_version else ()),
    )
    cursor = connection.execute(
        f"""
        UPDATE todo_items
        SET status=?, completed_at=?, deleted_at=?, snoozed_until='',
            version=version+1, updated_at=?
        WHERE task_id=?{version_clause}
        """,
        parameters,
    )
    if expected_version and cursor.rowcount != 1:
        latest = read_todo(connection, task_id)
        if latest:
            raise TodoVersionConflict(latest, expected_version)
        return {}
    task = read_todo(connection, task_id)
    _event(connection, task_id, status, task, now=active_now)
    rebuild_todo_reminders(connection, task, now=active_now)
    return task


def _task_local_now(task: Mapping[str, Any], now: datetime) -> datetime:
    name = str(task.get("timezone") or "")
    try:
        return now.astimezone(ZoneInfo(name)) if name else now.astimezone()
    except ZoneInfoNotFoundError:
        return now.astimezone()


def _all_day_zone(task: Mapping[str, Any]) -> ZoneInfo | timezone:
    name = str(task.get("timezone") or "").strip()
    try:
        return ZoneInfo(name) if name else _local_timezone()
    except ZoneInfoNotFoundError:
        return timezone.utc


def _all_day_occurs_on(task: Mapping[str, Any], day_text: str) -> bool:
    """Return whether a recurring all-day task has an occurrence on a date."""

    rule = normalize_recurrence_rule(task.get("recurrenceRule"))
    if not rule:
        return True
    try:
        day = datetime.fromisoformat(str(day_text)).date()
    except (TypeError, ValueError):
        return False
    zone = _all_day_zone(task)
    anchor = _task_recurrence_anchor(task)
    if anchor is None:
        return False
    local_anchor = anchor.astimezone(zone)
    local_target = datetime(day.year, day.month, day.day, tzinfo=zone)
    occurrence = next_occurrence(
        rule,
        anchor=local_anchor,
        after=local_target - timedelta(microseconds=1),
    )
    return occurrence is not None and occurrence.astimezone(zone).date() == day


def _next_all_day_occurrence_after(
    task: Mapping[str, Any], completed_at: datetime,
) -> datetime | None:
    rule = normalize_recurrence_rule(task.get("recurrenceRule"))
    anchor = _task_recurrence_anchor(task)
    if not rule or anchor is None:
        return None
    zone = _all_day_zone(task)
    local_completed = completed_at.astimezone(zone)
    after = datetime(
        local_completed.year, local_completed.month, local_completed.day,
        23, 59, 59, tzinfo=zone,
    )
    return next_occurrence(rule, anchor=anchor.astimezone(zone), after=after)


def _all_day_due_date(task: Mapping[str, Any], now: datetime) -> str:
    """Return the effective local date for an all-day occurrence.

    Recurring all-day tasks coalesce a missed calendar occurrence onto today;
    one-off tasks retain their original due date so a failed run cannot create
    a new occurrence every morning.
    """

    zone = _all_day_zone(task)
    local_now = now.astimezone(zone)
    today = local_now.date().isoformat()
    due_date = str(task.get("dueDate") or "").strip()
    if due_date and due_date > today:
        return ""
    rule = normalize_recurrence_rule(task.get("recurrenceRule"))
    if not rule:
        if due_date:
            return due_date
        created_at = parse_iso(task.get("createdAt"))
        return created_at.astimezone(zone).date().isoformat() if created_at else today
    anchor = _task_recurrence_anchor(task)
    if anchor is None:
        return ""
    local_anchor = anchor.astimezone(zone)
    first = next_occurrence(
        rule,
        anchor=local_anchor,
        after=local_anchor - timedelta(microseconds=1),
    )
    if first is None or first.astimezone(zone).date().isoformat() > today:
        return ""
    return today


def _task_occurrence_key(task: Mapping[str, Any], now: datetime) -> str:
    """Stable run key for the task's current timed or all-day occurrence."""

    due = parse_iso(task.get("dueAt"))
    if due is not None:
        return iso_utc(due)
    effective_date = _all_day_due_date(task, now)
    if not effective_date:
        return ""
    zone = _all_day_zone(task)
    day = datetime.fromisoformat(effective_date).date()
    return iso_utc(datetime(day.year, day.month, day.day, tzinfo=zone))


def _advance_all_day_task_occurrence(
    connection: sqlite3.Connection,
    current: Mapping[str, Any],
    *,
    occurrence_at: str,
    now: datetime,
    event_type: str,
    expected_version: int,
) -> dict[str, Any]:
    """Advance an all-day recurring occurrence without inventing a clock time."""

    task_id = str(current.get("taskId") or "")
    rule = normalize_recurrence_rule(current.get("recurrenceRule"))
    anchor = _task_recurrence_anchor(current)
    if not task_id or not rule or anchor is None:
        return {}
    zone = _all_day_zone(current)
    occurrence = parse_iso(occurrence_at) or now
    local_occurrence = occurrence.astimezone(zone)
    after = datetime(
        local_occurrence.year, local_occurrence.month, local_occurrence.day,
        23, 59, 59, tzinfo=zone,
    )
    next_at = next_occurrence(rule, anchor=anchor.astimezone(zone), after=after)
    if next_at is None:
        cursor = connection.execute(
            """
            UPDATE todo_items
            SET status='completed', completed_at=?, snoozed_until='',
                version=version+1, updated_at=?
            WHERE task_id=? AND status IN ('pending','in_progress') AND version=?
            """,
            (iso_utc(now), iso_utc(now), task_id, int(expected_version)),
        )
        if cursor.rowcount != 1:
            return {}
        task = read_todo(connection, task_id)
        _event(connection, task_id, "recurrence_finished", {
            "previousDueDate": current.get("dueDate"),
            "occurrenceAt": occurrence_at,
        }, now=now)
        rebuild_todo_reminders(connection, task, now=now)
        return task
    next_date = next_at.astimezone(zone).date().isoformat()
    cursor = connection.execute(
        """
        UPDATE todo_items
        SET due_date=?, status='pending', completed_at='', snoozed_until='',
            version=version+1, updated_at=?
        WHERE task_id=? AND status IN ('pending','in_progress') AND version=?
        """,
        (next_date, iso_utc(now), task_id, int(expected_version)),
    )
    if cursor.rowcount != 1:
        return {}
    task = read_todo(connection, task_id)
    _event(connection, task_id, event_type, {
        "previousDueDate": current.get("dueDate"),
        "nextDueDate": next_date,
        "occurrenceAt": occurrence_at,
    }, now=now)
    rebuild_todo_reminders(connection, task, now=now)
    return task


def _all_day_overlaps_range(
    task: Mapping[str, Any], after: datetime | None, before: datetime | None,
) -> bool:
    """Match local calendar days without assigning a clock time to the task."""
    local_now = _task_local_now(task, utc_now())
    date_text = str(task.get("dueDate") or "")
    if date_text:
        day = datetime.fromisoformat(date_text).date()
    elif task.get("recurrenceRule") == "daily":
        day = _task_local_now(task, parse_iso(task.get("createdAt")) or utc_now()).date()
    else:
        # Unscheduled one-off work belongs to today's outstanding list.
        day = local_now.date()
    start = local_now.replace(year=day.year, month=day.month, day=day.day,
                              hour=0, minute=0, second=0, microsecond=0)
    if before is not None and start > before:
        return False
    if task.get("recurrenceRule"):
        # A recurring all-day task is represented by the date of its next
        # outstanding occurrence.  The scheduler itself coalesces late timed
        # occurrences; calendar filtering only needs a deterministic overlap.
        return after is None or before is None or after <= before
    end = start + timedelta(days=1)
    return after is None or end > after


def reactivate_daily_all_day_todos(
    connection: sqlite3.Connection, *, now: datetime | None = None,
) -> None:
    """Keep today's completion visible; reopen the same task on its next local day."""
    active_now = now or utc_now()
    rows = connection.execute(
        "SELECT * FROM todo_items WHERE status='completed' "
        "AND recurrence_rule<>'' AND due_at='' AND completed_at<>''"
    ).fetchall()
    for row in rows:
        task = _row_dict(row)
        completed = parse_iso(task["completedAt"])
        if completed is None:
            continue
        completed_local = _task_local_now(task, completed)
        active_local = _task_local_now(task, active_now)
        if completed_local.date() >= active_local.date():
            continue
        try:
            next_occurrence_at = _next_all_day_occurrence_after(task, completed)
        except ValueError:
            continue
        if next_occurrence_at is None or next_occurrence_at.astimezone(
            _all_day_zone(task)
        ).date() > active_local.date():
            continue
        cursor = connection.execute(
            "UPDATE todo_items SET status='pending', completed_at='', snoozed_until='', "
            "version=version+1, updated_at=? WHERE task_id=? AND version=? AND status='completed'",
            (iso_utc(active_now), task["taskId"], task["version"]),
        )
        if cursor.rowcount:
            _event(connection, task["taskId"], "recurring_all_day_reactivated",
                   {"completedAt": task["completedAt"]}, now=active_now)


def prepare_all_day_reminders(
    connection: sqlite3.Connection, *, now: datetime | None = None,
) -> None:
    """Queue all-day work at the first active client poll of each local day.

    The opening marker and queue are committed together by the caller. Reopening
    the app, multiple windows and later polls cannot requeue that day's tasks.
    """
    active_now = now or utc_now()
    timestamp = iso_utc(active_now)
    reactivate_daily_all_day_todos(connection, now=active_now)
    rows = connection.execute(
        "SELECT * FROM todo_items WHERE due_at='' AND status IN ('pending','in_progress')"
    ).fetchall()
    groups: dict[str, list[dict[str, Any]]] = {"": []}
    for row in rows:
        task = _row_dict(row)
        groups.setdefault(task["timezone"], []).append(task)
    for zone, tasks in groups.items():
        local_now = _task_local_now({"timezone": zone}, active_now)
        today = local_now.date().isoformat()
        day_start = iso_utc(local_now.replace(hour=0, minute=0, second=0, microsecond=0))
        connection.execute(
            "UPDATE todo_reminders SET status='cancelled', updated_at=? "
            "WHERE reminder_kind='all_day' AND status IN ('pending','claimed') "
            "AND scheduled_at<? AND task_id IN (SELECT task_id FROM todo_items WHERE timezone_name=?)",
            (timestamp, day_start, zone),
        )
        opening = connection.execute(
            "INSERT OR IGNORE INTO todo_daily_openings(timezone_name, local_date, opened_at) VALUES (?,?,?)",
            (zone, today, timestamp),
        )
        if opening.rowcount != 1:
            continue
        for task in tasks:
            if task["dueDate"] and task["dueDate"] > today:
                continue
            if task["recurrenceRule"]:
                try:
                    occurs_today = _all_day_occurs_on(task, today)
                except ValueError:
                    occurs_today = False
                if not occurs_today:
                    continue
            snoozed = parse_iso(task.get("snoozedUntil"))
            if snoozed and snoozed > active_now:
                continue
            reminder_id = _reminder_id(task["taskId"], 0, "all_day", today)
            connection.execute(
                "INSERT OR IGNORE INTO todo_reminders("
                "reminder_id,task_id,task_version,reminder_kind,scheduled_at,available_at,created_at,updated_at"
                ") VALUES (?,?,?,'all_day',?,?,?,?)",
                (reminder_id, task["taskId"], task["version"], day_start, timestamp, timestamp, timestamp),
            )


def snooze_todo(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    snooze_until: str,
    now: datetime | None = None,
    expected_version: int = 0,
) -> dict[str, Any]:
    current = read_todo(connection, task_id)
    if not current or current.get("status") not in {"pending", "in_progress"}:
        return {}
    _check_expected_version(current, expected_version)
    active_now = now or utc_now()
    version_clause = " AND version=?" if expected_version else ""
    parameters: tuple[Any, ...] = (
        snooze_until, iso_utc(active_now), task_id,
        *((int(expected_version),) if expected_version else ()),
    )
    cursor = connection.execute(
        f"""
        UPDATE todo_items
        SET snoozed_until=?, version=version+1, updated_at=?
        WHERE task_id=?{version_clause}
        """,
        parameters,
    )
    if expected_version and cursor.rowcount != 1:
        latest = read_todo(connection, task_id)
        if latest:
            raise TodoVersionConflict(latest, expected_version)
        return {}
    task = read_todo(connection, task_id)
    _event(connection, task_id, "snoozed", {"until": snooze_until}, now=active_now)
    rebuild_todo_reminders(connection, task, now=active_now)
    return task


# 回收超时的提醒领取记录，再以任务版本过滤失效提醒。
# 条件更新确保同一条 pending 记录只被一个调用方成功领取；返回空字典表示未领取。
def claim_due_reminder(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = 180,
) -> dict[str, Any]:
    active_now = now or utc_now()
    timestamp = iso_utc(active_now)
    stale_before = iso_utc(active_now - timedelta(seconds=max(30, stale_after_seconds)))
    connection.execute(
        """
        UPDATE todo_reminders
        SET status='pending', claimed_at='', available_at=?, updated_at=?
        WHERE status='claimed' AND claimed_at <> '' AND claimed_at <= ?
        """,
        (timestamp, timestamp, stale_before),
    )
    rows = connection.execute(
        """
        SELECT r.*, i.*
        FROM todo_reminders r
        LEFT JOIN todo_items i ON i.task_id = r.task_id
        WHERE r.status='pending'
          AND r.available_at <= ?
          AND r.scheduled_at <= ?
          AND (
                r.reminder_kind='execution'
                OR (
                    i.status IN ('pending','in_progress')
                    AND i.version = r.task_version
                    AND (i.on_due='' OR r.reminder_kind='upcoming')
                )
          )
        ORDER BY CASE r.reminder_kind WHEN 'execution' THEN 0 WHEN 'all_day' THEN 2 ELSE 1 END,
                 r.scheduled_at ASC, r.created_at ASC
        """,
        (timestamp, timestamp),
    )
    row = None
    for candidate in rows:
        reminder_kind = str(candidate["reminder_kind"] or "")
        if reminder_kind in {"due", "snoozed", "all_day"}:
            task = read_todo(connection, str(candidate["task_id"] or ""))
            if task:
                try:
                    occurrence = _task_occurrence_key(task, active_now)
                except (TypeError, ValueError):
                    occurrence = ""
                if occurrence and connection.execute(
                    "SELECT 1 FROM todo_runs WHERE task_id=? AND occurrence_at=? LIMIT 1",
                    (str(candidate["task_id"] or ""), occurrence),
                ).fetchone() is not None:
                    continue
        reminder_id = str(candidate["reminder_id"])
        updated = connection.execute(
            """
            UPDATE todo_reminders
            SET status='claimed', claimed_at=?, attempt_count=attempt_count+1, updated_at=?
            WHERE reminder_id=? AND status='pending'
            """,
            (timestamp, timestamp, reminder_id),
        )
        if updated.rowcount == 1:
            row = candidate
            break
    if row is None:
        return {}
    task = read_todo(connection, str(row["task_id"]))
    payload = _run_json(row["payload_json"])
    if not task and str(row["reminder_kind"] or "") == "execution":
        snapshot = payload.get("taskSnapshot")
        task = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    result = {
        "reminderId": reminder_id,
        "kind": str(row["reminder_kind"]),
        "scheduledAt": str(row["scheduled_at"]),
        "attemptCount": int(row["attempt_count"] or 0) + 1,
        "task": task,
    }
    if str(row["reminder_kind"] or "") == "execution":
        snapshot = payload.get("taskSnapshot")
        if isinstance(snapshot, Mapping):
            task = dict(snapshot)
            result["task"] = task
        execution_result = payload.get("executionResult")
        if not isinstance(execution_result, Mapping):
            execution_result = (payload.get("execution") or {}).get("result") or {}
        result.update({
            "runId": str(payload.get("runId") or ""),
            "occurrenceAt": str(payload.get("occurrenceAt") or ""),
            "executionResult": dict(execution_result) if isinstance(execution_result, Mapping) else {},
            "taskSnapshot": snapshot or task,
            "nextTaskSnapshot": payload.get("nextTaskSnapshot") or {},
        })
    return result


# 领取成功不等于送达：收到结果后才更新 delivered，失败则延后重试并限制次数。
def acknowledge_reminder(
    connection: sqlite3.Connection,
    reminder_id: str,
    *,
    delivered: bool,
    error: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    active_now = now or utc_now()
    timestamp = iso_utc(active_now)
    row = connection.execute(
        "SELECT * FROM todo_reminders WHERE reminder_id=?",
        (str(reminder_id or ""),),
    ).fetchone()
    if row is None:
        return {}
    if str(row["status"]) != "claimed":
        return {"reminderId": str(row["reminder_id"]), "taskId": str(row["task_id"]),
                "status": str(row["status"]), "ignored": True}
    attempts = int(row["attempt_count"] or 0)
    if delivered:
        status = "delivered"
        available_at = str(row["available_at"])
        delivered_at = timestamp
    elif attempts >= 3:
        status = "failed"
        available_at = str(row["available_at"])
        delivered_at = ""
    else:
        status = "pending"
        available_at = iso_utc(active_now + timedelta(seconds=min(300, 30 * (2 ** attempts))))
        delivered_at = ""
    connection.execute(
        """
        UPDATE todo_reminders
        SET status=?, available_at=?, delivered_at=?, last_error=?, updated_at=?
        WHERE reminder_id=?
        """,
        (status, available_at, delivered_at, str(error or "")[:500], timestamp, reminder_id),
    )
    _event(
        connection,
        str(row["task_id"]),
        "reminder_delivered" if delivered else "reminder_failed",
        {"reminderId": reminder_id, "error": str(error or "")[:500]},
        now=active_now,
    )
    recurring_task: dict[str, Any] = {}
    if (
        delivered
        and str(row["status"] or "") == "claimed"
        and str(row["reminder_kind"] or "") in {"due", "snoozed"}
    ):
        task = read_todo(connection, str(row["task_id"] or ""))
        if (
            normalize_recurrence_rule(task.get("recurrenceRule"))
            and int(task.get("version") or 0) == int(row["task_version"] or 0)
        ):
            recurring_task = advance_recurring_todo(
                connection,
                str(row["task_id"] or ""),
                now=active_now,
                event_type="recurring_reminder_delivered",
            )
    result = {
        "reminderId": reminder_id,
        "taskId": str(row["task_id"]),
        "status": status,
        "deliveredAt": delivered_at,
        "availableAt": available_at,
    }
    if recurring_task:
        result["recurrenceAdvanced"] = True
        result["nextDueAt"] = recurring_task.get("dueAt")
        result["task"] = recurring_task
    elif delivered and str(row["status"]) == "claimed" and str(row["reminder_kind"]) not in {"upcoming", "execution"}:
        task = read_todo(connection, str(row["task_id"]))
        execution_result = (_run_json(row["payload_json"]).get("execution") or {}).get("result") or {}
        if (task.get("onDue") and not task.get("recurrenceRule")
                and task.get("version") == int(row["task_version"])
                and execution_result.get("scheduledExecutionStatus") == "completed"):
            result["task"] = set_todo_status(connection, task["taskId"], "completed",
                                            expected_version=task["version"], now=active_now)
    return result


def acknowledge_reminder_in_transaction(
    reminder_id: str,
    *,
    delivered: bool,
    error: str = "",
    state_database: Callable[[], AbstractContextManager[sqlite3.Connection]],
) -> dict[str, Any]:
    """在待办仓储事务内确认提醒；原状态机异常继续交给调用方处理。"""

    with state_database() as connection:
        return acknowledge_reminder(
            connection,
            reminder_id,
            delivered=delivered,
            error=error,
        )


def release_claimed_reminder(
    connection: sqlite3.Connection,
    reminder_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Return an undelivered lease without counting a paused system as a failure."""

    active_now = now or utc_now()
    timestamp = iso_utc(active_now)
    updated = connection.execute(
        """
        UPDATE todo_reminders
        SET status='pending', claimed_at='', available_at=?,
            attempt_count=MAX(0, attempt_count-1), updated_at=?
        WHERE reminder_id=? AND status='claimed'
        """,
        (timestamp, timestamp, str(reminder_id or "")),
    )
    return updated.rowcount == 1
