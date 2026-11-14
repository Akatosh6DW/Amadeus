"""Small, account-scoped bridge between autonomous life and QQ Space.

The QQ Space worker deliberately runs with an isolated data directory.  This
module is the only persistence boundary it needs for sharing a bounded set of
life material and verified Space results.  The existing life tables remain
owned by :mod:`backend.life.store`; this module owns only
``life_qzone_events``.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


_MAX_INTEREST = 1200
_MAX_ACTIVITY = 8
_MAX_ACTIVITY_SUMMARY = 500
_MAX_POSTS = 8
_MAX_POST = 1000
_MAX_EVENTS = 20
_MAX_EVENT_SUMMARY = 800
_MAX_ACTIONS = 16
_MAX_EXCERPTS = 8
_MAX_EXCERPT = 360
_MAX_URL = 2048


def _account(value: object) -> str:
    text = str(value or "").strip()
    return text if text.isascii() and text.isdigit() and 5 <= len(text) <= 20 else ""


def _text(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _safe_url(value: object) -> str:
    text = _text(value, _MAX_URL)
    return text if text.startswith(("http://", "https://")) else ""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path).expanduser().resolve()
    if read_only:
        # A missing life database is the normal state before autonomous life is
        # configured.  Do not create or mutate it merely to export context.
        if not path.is_file():
            raise FileNotFoundError(path)
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _life_account(connection: sqlite3.Connection) -> str:
    try:
        row = connection.execute(
            "SELECT value FROM life_state WHERE key='config'"
        ).fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    try:
        config = json.loads(row[0])
    except (TypeError, ValueError):
        return ""
    return _account(config.get("publishAccount")) if isinstance(config, dict) else ""


def _account_matches(connection: sqlite3.Connection, account: str) -> bool:
    return bool(account and _life_account(connection) == account)


def _recent_life_material(connection: sqlite3.Connection, account: str) -> dict[str, Any]:
    """Read only bounded, non-private material from the existing life tables."""

    if not _account_matches(connection, account):
        return {}

    try:
        note_row = connection.execute(
            "SELECT value FROM life_state WHERE key='note'"
        ).fetchone()
        note = ""
        if note_row is not None:
            try:
                value = json.loads(note_row[0])
                note = _text(value, _MAX_INTEREST)
            except (TypeError, ValueError):
                note = ""

        activities = []
        for row in connection.execute(
            "SELECT started_at, status, summary FROM life_runs "
            "ORDER BY started_at DESC LIMIT ?", (_MAX_ACTIVITY,)
        ):
            activities.append({
                "at": _text(row[0], 64),
                "status": _text(row[1], 32),
                "summary": _text(row[2], _MAX_ACTIVITY_SUMMARY),
            })

        published = []
        for row in connection.execute(
            "SELECT created_at, visibility, content FROM life_drafts "
            "WHERE account=? AND state='sent' "
            "ORDER BY created_at DESC LIMIT ?", (account, _MAX_POSTS),
        ):
            published.append({
                "at": _text(row[0], 64),
                "visibility": _text(row[1], 16),
                "content": _text(row[2], _MAX_POST),
            })
    except sqlite3.Error:
        # Treat an old or partially initialized life database as no bridge
        # context.  The independent QQ Space feature must keep working.
        return {}

    result: dict[str, Any] = {"account": account}
    if note:
        result["interest"] = note
    if activities:
        result["recentActivities"] = activities
    if published:
        result["publishedPosts"] = published
    return result if len(result) > 1 else {}


def export_qzone_context(path: Path, account: str) -> dict[str, Any]:
    """Export bounded life material for one exact, configured QQ account.

    An absent life database, malformed configuration, or account mismatch
    returns ``{}`` so an unconfigured independent QQ Space visit retains its
    previous behavior.  This function never creates or writes the database.
    """

    account = _account(account)
    if not account:
        return {}
    try:
        with closing(_connect(Path(path), read_only=True)) as connection:
            return _recent_life_material(connection, account)
    except (FileNotFoundError, sqlite3.Error):
        return {}


def _ensure_events_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS life_qzone_events (
            account TEXT NOT NULL,
            run_id TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(account, run_id)
        )"""
    )


def _safe_receipt(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("state", "platformId", "requestKey", "deliveryState"):
        text = _text(value.get(key), 256)
        if text:
            result[key] = text
    return result


def _safe_action(value: object, account: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    action = _text(value.get("action"), 32)
    if action not in {"status", "feed", "posts", "publish", "like", "comment"}:
        return None
    row: dict[str, Any] = {
        "action": action,
        "ok": value.get("ok") is True,
    }
    if _text(value.get("account"), 20) == account:
        row["account"] = account
    if isinstance(value.get("count"), int) and not isinstance(value.get("count"), bool):
        row["count"] = max(0, min(20, value["count"]))
    for key in ("sent", "attempted"):
        if isinstance(value.get(key), bool):
            row[key] = value[key]
    receipt = _safe_receipt(value.get("receipt"))
    if receipt:
        row["receipt"] = receipt
    delivery_state = _text(value.get("deliveryState"), 32)
    if delivery_state:
        row["deliveryState"] = delivery_state

    # Read excerpts are deliberately small and exclude opaque post references,
    # authors, cookies and other provider identifiers.  They are material for
    # later natural conversation, not instructions or permission evidence.
    excerpts = []
    items = value.get("items")
    if not isinstance(items, list) and isinstance(value.get("excerpts"), list):
        # Worker results already contain sanitized excerpts.  Keep accepting
        # raw service-style ``items`` for direct callers and tests.
        items = value.get("excerpts")
    if isinstance(items, list):
        for item in items[:_MAX_EXCERPTS]:
            if not isinstance(item, dict):
                continue
            excerpt = _text(item.get("content") or item.get("excerpt"), _MAX_EXCERPT)
            if not excerpt:
                continue
            item_row = {"excerpt": excerpt}
            created = _text(item.get("createdAt"), 64)
            if created:
                item_row["at"] = created
            url = _safe_url(item.get("url"))
            if url:
                item_row["url"] = url
            excerpts.append(item_row)
    if excerpts:
        row["excerpts"] = excerpts

    # A publish body is copied only from the actual tool arguments/result and
    # only as bounded text.  Consumers must require sent=True before treating
    # it as a published post.
    content = _text(value.get("content"), _MAX_POST)
    if content and action == "publish":
        row["content"] = content
    return row


def _safe_event(account: str, run_id: str, result: object) -> dict[str, Any]:
    source = result if isinstance(result, dict) else {}
    event: dict[str, Any] = {
        "account": account,
        "runId": run_id,
        "ok": source.get("ok") is True,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
    }
    request_id = _text(source.get("request_id"), 256)
    if request_id:
        event["requestId"] = request_id
    summary = _text(source.get("reply"), _MAX_EVENT_SUMMARY)
    if summary:
        event["summary"] = summary
    actions = []
    raw_actions = source.get("qzoneActions")
    if isinstance(raw_actions, list):
        for raw in raw_actions[:_MAX_ACTIONS]:
            action = _safe_action(raw, account)
            if action is not None:
                actions.append(action)
    if actions:
        event["qzoneActions"] = actions
    return event


def record_qzone_event(path: Path, account: str, run_id: str, result: object) -> dict[str, Any]:
    """Record one worker result idempotently for the exact life account.

    Returns the stored event plus ``duplicate``.  Account mismatch and an
    unavailable/unconfigured life database are reported as ``stored=False``;
    they never block the independent Space worker.
    """

    account = _account(account)
    run_id = _text(run_id, 256)
    if not account or not run_id:
        return {"stored": False, "reason": "invalid_scope"}
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        return {"stored": False, "reason": "life_store_unavailable"}
    event = _safe_event(account, run_id, result)
    try:
        with closing(_connect(path, read_only=False)) as connection:
            if not _account_matches(connection, account):
                return {"stored": False, "reason": "account_mismatch"}
            _ensure_events_table(connection)
            inserted = connection.execute(
                "INSERT OR IGNORE INTO life_qzone_events(account,run_id,result,created_at) "
                "VALUES (?,?,?,?)",
                (account, run_id, _json(event), event["recordedAt"]),
            ).rowcount == 1
            connection.commit()
            stored = connection.execute(
                "SELECT result FROM life_qzone_events WHERE account=? AND run_id=?",
                (account, run_id),
            ).fetchone()
            if stored is None:
                return {"stored": False, "reason": "life_store_unavailable"}
            return {"stored": True, "duplicate": not inserted,
                    "event": json.loads(stored[0])}
    except (FileNotFoundError, sqlite3.Error, TypeError, ValueError):
        return {"stored": False, "reason": "life_store_unavailable"}


def read_qzone_events(path: Path, account: str) -> list[dict[str, Any]]:
    """Return bounded, account-matched worker events for the life loop."""

    account = _account(account)
    if not account:
        return []
    try:
        with closing(_connect(Path(path), read_only=True)) as connection:
            if not _account_matches(connection, account):
                return []
            try:
                rows = connection.execute(
                    "SELECT result FROM life_qzone_events WHERE account=? "
                    "ORDER BY created_at DESC LIMIT ?", (account, _MAX_EVENTS)
                ).fetchall()
            except sqlite3.Error:
                return []
            events = []
            for row in rows:
                try:
                    value = json.loads(row[0])
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict) and value.get("account") == account:
                    events.append(value)
            return events
    except (FileNotFoundError, sqlite3.Error):
        return []


__all__ = ["export_qzone_context", "record_qzone_event", "read_qzone_events"]
