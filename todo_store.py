"""SQLite persistence and reminder leasing for the AMADEUS Todo domain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import json
import re
import sqlite3
from typing import Any, Mapping, Sequence
import unicodedata
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.fingerprints import legacy_persistent_fingerprint
from agent.todo_architecture import recent_todo_task_ids


TODO_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS todo_items (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'normal',
    due_at TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    timezone_name TEXT NOT NULL DEFAULT '',
    remind_before_minutes INTEGER NOT NULL DEFAULT 0,
    recurrence_rule TEXT NOT NULL DEFAULT '',
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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _row_dict(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    value = dict(row)
    return {
        "taskId": str(value.get("task_id") or ""),
        "title": str(value.get("title") or ""),
        "details": str(value.get("details") or ""),
        "status": str(value.get("status") or "pending"),
        "priority": str(value.get("priority") or "normal"),
        "dueAt": str(value.get("due_at") or ""),
        "dueDate": str(value.get("due_date") or ""),
        "allDay": not bool(value.get("due_at")),
        "timezone": str(value.get("timezone_name") or ""),
        "remindBeforeMinutes": int(value.get("remind_before_minutes") or 0),
        "recurrenceRule": str(value.get("recurrence_rule") or ""),
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
    scope = status if status in {"active", "pending", "completed", "deleted", "all"} else "active"
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
            "details": str(item.get("details") or "")[:500],
            "status": str(item.get("status") or "pending"),
            "dueAt": str(item.get("dueAt") or ""),
            "dueDate": str(item.get("dueDate") or ""),
            "allDay": not bool(item.get("dueAt")),
            "timezone": str(item.get("timezone") or ""),
            "recurrenceRule": str(item.get("recurrenceRule") or ""),
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
        "WHERE task_id=? AND status IN ('pending','claimed')",
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


def create_todo(
    connection: sqlite3.Connection,
    *,
    title: str,
    details: str = "",
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
    connection.execute(
        """
        INSERT INTO todo_items(
            task_id, title, details, status, priority, due_at, due_date, timezone_name,
            remind_before_minutes, recurrence_rule, snoozed_until, version, source_turn_id,
            created_at, updated_at, completed_at, deleted_at
        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, '', 1, ?, ?, ?, '', '')
        """,
        (
            task_id,
            str(title).strip(),
            str(details or "").strip(),
            priority,
            due_at,
            due_date if not due_at else "",
            timezone_name,
            max(0, int(remind_before_minutes)),
            str(recurrence_rule or ""),
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
    allowed = {
        "title": "title",
        "details": "details",
        "priority": "priority",
        "dueAt": "due_at",
        "dueDate": "due_date",
        "timezone": "timezone_name",
        "remindBeforeMinutes": "remind_before_minutes",
        "recurrenceRule": "recurrence_rule",
    }
    changes = dict(changes)
    if changes.get("dueAt"):
        changes["dueDate"] = ""
    elif changes.get("dueDate"):
        changes["dueAt"] = ""
    assignments: list[str] = []
    values: list[Any] = []
    for public_name, column in allowed.items():
        if public_name not in changes:
            continue
        assignments.append(f"{column} = ?")
        values.append(changes[public_name])
    if not assignments:
        return current
    active_now = now or utc_now()
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
    """Return the next daily wall-clock occurrence strictly after ``after``."""

    if str(task.get("recurrenceRule") or "") != "daily":
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
    local_due = due.astimezone(local_zone)
    local_after = active_after.astimezone(local_zone)
    next_local = local_due
    for _ in range(3700):
        next_local = next_local + timedelta(days=1)
        if next_local > local_after:
            return iso_utc(next_local)
    return ""


def advance_recurring_todo(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    now: datetime | None = None,
    event_type: str = "recurrence_advanced",
) -> dict[str, Any]:
    active_now = now or utc_now()
    current = read_todo(connection, task_id)
    next_due = next_recurring_due_at(current, after=active_now)
    if not current or not next_due:
        return {}
    connection.execute(
        """
        UPDATE todo_items
        SET due_at=?, status='pending', completed_at='', snoozed_until='',
            version=version+1, updated_at=?
        WHERE task_id=? AND status IN ('pending','in_progress')
        """,
        (next_due, iso_utc(active_now), task_id),
    )
    task = read_todo(connection, task_id)
    _event(
        connection,
        task_id,
        event_type,
        {"previousDueAt": current.get("dueAt"), "nextDueAt": next_due},
        now=active_now,
    )
    rebuild_todo_reminders(connection, task, now=active_now)
    return task


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
    if (status == "completed" and str(current.get("recurrenceRule") or "") == "daily"
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
    if task.get("recurrenceRule") == "daily":
        # Every day at/after the start date has an occurrence in the range.
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
        "AND recurrence_rule='daily' AND due_at='' AND completed_at<>''"
    ).fetchall()
    for row in rows:
        task = _row_dict(row)
        completed = parse_iso(task["completedAt"])
        if completed is None or (
            _task_local_now(task, completed).date() >= _task_local_now(task, active_now).date()
        ):
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
    row = connection.execute(
        """
        SELECT r.*, i.*
        FROM todo_reminders r
        JOIN todo_items i ON i.task_id = r.task_id
        WHERE r.status='pending'
          AND r.available_at <= ?
          AND r.scheduled_at <= ?
          AND i.status IN ('pending','in_progress')
          AND i.version = r.task_version
        ORDER BY CASE r.reminder_kind WHEN 'all_day' THEN 1 ELSE 0 END,
                 r.scheduled_at ASC, r.created_at ASC
        LIMIT 1
        """,
        (timestamp, timestamp),
    ).fetchone()
    if row is None:
        return {}
    reminder_id = str(row["reminder_id"])
    updated = connection.execute(
        """
        UPDATE todo_reminders
        SET status='claimed', claimed_at=?, attempt_count=attempt_count+1, updated_at=?
        WHERE reminder_id=? AND status='pending'
        """,
        (timestamp, timestamp, reminder_id),
    )
    if updated.rowcount != 1:
        return {}
    task = read_todo(connection, str(row["task_id"]))
    return {
        "reminderId": reminder_id,
        "kind": str(row["reminder_kind"]),
        "scheduledAt": str(row["scheduled_at"]),
        "attemptCount": int(row["attempt_count"] or 0) + 1,
        "task": task,
    }


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
    if str(row["reminder_kind"]) == "all_day" and str(row["status"]) != "claimed":
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
        and str(row["reminder_kind"] or "") == "due"
    ):
        task = read_todo(connection, str(row["task_id"] or ""))
        if (
            str(task.get("recurrenceRule") or "") == "daily"
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
    return result


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
