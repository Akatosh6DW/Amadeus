"""Small durable store for role experiences and ongoing thoughts.

Continuity is deliberately separate from conversation memory.  It stores only
role-owned observations and explicitly authored thoughts, with source
references carried through as data.  No method in this module performs a
network action or writes user facts.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable, Iterator


MAX_EVENTS = 500
MAX_SUMMARY_CHARS = 4000
MAX_CONTENT_CHARS = 4000
MAX_ID_CHARS = 512
MAX_SCOPE_CHARS = 512
MAX_SOURCE_REFS = 64
MAX_SOURCE_REFS_JSON_CHARS = 16000
MAX_THOUGHTS_IN_CONTEXT = 40
RECENT_CLOSED_THOUGHTS = 8

_THOUGHT_STATUSES = frozenset(("open", "paused", "done", "dismissed", "expired"))
_THOUGHT_STATUS_ALIASES = {"completed": "done"}
_ACTIVE_THOUGHT_STATUSES = frozenset(("open", "paused"))
_CLOSED_THOUGHT_STATUSES = frozenset(("done", "dismissed", "expired"))
_SHARE_STATES = frozenset((
    "", "draft", "sending", "sent", "unknown", "failed", "not_attempted", "cancelled",
))
_CLAIMING_SHARE_STATES = frozenset(("draft", "sending", "unknown", "sent"))
_RELEASED_SHARE_STATES = frozenset(("failed", "not_attempted", "cancelled"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_events (
    event_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_continuity_events_recent
    ON continuity_events(scope, updated_at DESC, event_id DESC);

CREATE TABLE IF NOT EXISTS continuity_thoughts (
    thought_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    source_refs_json TEXT NOT NULL,
    due_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    last_shared_at TEXT NOT NULL DEFAULT '',
    sharing_state TEXT NOT NULL DEFAULT '',
    delivery_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_continuity_thoughts_scope_status
    ON continuity_thoughts(scope, status, updated_at DESC, thought_id DESC);
CREATE INDEX IF NOT EXISTS idx_continuity_thoughts_delivery
    ON continuity_thoughts(delivery_id, sharing_state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_continuity_thoughts_natural_key
    ON continuity_thoughts(scope, kind, content);
"""


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime, *, field: str = "time") -> str:
    return _aware_utc(value, field=field).isoformat()


def _optional_time(value: datetime | str | None, *, field: str) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return _stamp(value, field=field)
    if not isinstance(value, str):
        raise TypeError(f"{field} must be an aware datetime or ISO string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO datetime") from error
    return _stamp(parsed, field=field)


def _text(value: Any, *, field: str, maximum: int, nonblank: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    if nonblank and not value.strip():
        raise ValueError(f"{field} must not be blank")
    if len(value) > maximum:
        raise ValueError(f"{field} is too long")
    return value


def _scope(value: Any) -> str:
    return _text(value, field="scope", maximum=MAX_SCOPE_CHARS)


def _identifier(value: Any, *, field: str) -> str:
    return _text(value, field=field, maximum=MAX_ID_CHARS)


def _scopes(values: Iterable[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    try:
        result = tuple(dict.fromkeys(_scope(item) for item in values))
    except TypeError as error:
        raise TypeError("scopes must be an iterable of text scopes") from error
    if len(result) > 256:
        raise ValueError("too many scopes")
    return result


def _source_refs(value: Iterable[Any] | None) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray, dict)):
        raise TypeError("source_refs must be an iterable of references")
    try:
        result = tuple(value)
    except TypeError as error:
        raise TypeError("source_refs must be an iterable of references") from error
    if len(result) > MAX_SOURCE_REFS:
        raise ValueError("too many source references")
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("source_refs must be JSON-compatible") from error
    if len(encoded) > MAX_SOURCE_REFS_JSON_CHARS:
        raise ValueError("source_refs are too large")
    return result


def _encode_refs(value: Iterable[Any]) -> str:
    return json.dumps(tuple(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _decode_refs(value: str) -> list[Any]:
    try:
        result = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("stored source_refs are invalid") from error
    if not isinstance(result, list):
        raise ValueError("stored source_refs are invalid")
    return result


def _thought_status(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("status must be text")
    normalized = _THOUGHT_STATUS_ALIASES.get(value, value)
    if normalized not in _THOUGHT_STATUSES:
        raise ValueError("unsupported thought status")
    return normalized


def _sharing_state(value: Any) -> str:
    if not isinstance(value, str) or value not in _SHARE_STATES:
        raise ValueError("unsupported sharing state")
    return value


class ContinuityStore:
    """SQLite-backed, scope-isolated continuity state.

    Each public operation gets a fresh connection and transaction.  This keeps
    separate store instances safe to use from different worker threads or
    processes, while the per-instance lock avoids needless contention between
    callers sharing one object.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        with self._db():
            pass

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(_SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["event_id"],
            "scope": row["scope"],
            "channel": row["channel"],
            "kind": row["kind"],
            "status": row["status"],
            "summary": row["summary"],
            "sourceRefs": _decode_refs(row["source_refs_json"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _thought_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["thought_id"],
            "scope": row["scope"],
            "content": row["content"],
            "kind": row["kind"],
            "status": row["status"],
            "revision": int(row["revision"]),
            "sourceRefs": _decode_refs(row["source_refs_json"]),
            "dueAt": row["due_at"],
            "expiresAt": row["expires_at"],
            "lastSharedAt": row["last_shared_at"],
            "sharingState": row["sharing_state"],
            "deliveryId": row["delivery_id"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _stable_thought_id(scope: str, kind: str, content: str) -> str:
        token = "\x1f".join((scope, kind, content)).encode("utf-8")
        return "thought-" + hashlib.sha256(token).hexdigest()[:32]

    @staticmethod
    def _expire_thoughts(db: sqlite3.Connection, scopes: tuple[str, ...], now: str) -> None:
        if not scopes:
            return
        placeholders = ",".join("?" for _ in scopes)
        db.execute(
            f"""UPDATE continuity_thoughts
                SET status='expired', updated_at=?
                WHERE scope IN ({placeholders})
                  AND status IN ('open','paused')
                  AND expires_at != '' AND expires_at <= ?""",
            (now, *scopes, now),
        )

    def record_event(
        self,
        event_id: str,
        *,
        scope: str,
        channel: str,
        kind: str,
        summary: str,
        now: datetime,
        status: str = "observed",
        source_refs: Iterable[Any] = (),
    ) -> dict[str, Any]:
        event_id = _identifier(event_id, field="event_id")
        scope = _scope(scope)
        channel = _text(channel, field="channel", maximum=MAX_ID_CHARS)
        kind = _text(kind, field="kind", maximum=MAX_ID_CHARS)
        summary = _text(summary, field="summary", maximum=MAX_SUMMARY_CHARS)
        status = _text(status, field="status", maximum=MAX_ID_CHARS)
        refs = _source_refs(source_refs)
        now_value = _stamp(now, field="now")
        refs_json = _encode_refs(refs)
        with self._db() as db:
            existing = db.execute(
                "SELECT * FROM continuity_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None and existing["scope"] != scope:
                raise ValueError("event_id already belongs to another scope")
            if existing is not None and all(existing[key] == value for key, value in (
                ("channel", channel), ("kind", kind), ("summary", summary),
                ("status", status), ("source_refs_json", refs_json),
            )):
                return self._event_payload(existing)
            if existing is None:
                db.execute(
                    """INSERT INTO continuity_events
                       (event_id,scope,channel,kind,summary,status,source_refs_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (event_id, scope, channel, kind, summary, status, refs_json, now_value, now_value),
                )
            else:
                db.execute(
                    """UPDATE continuity_events
                       SET channel=?, kind=?, summary=?, status=?, source_refs_json=?, updated_at=?
                       WHERE event_id=?""",
                    (channel, kind, summary, status, refs_json, now_value, event_id),
                )
            db.execute(
                """DELETE FROM continuity_events
                   WHERE event_id NOT IN (
                       SELECT event_id FROM continuity_events
                       ORDER BY updated_at DESC, event_id DESC LIMIT ?
                   )""",
                (MAX_EVENTS,),
            )
            row = db.execute("SELECT * FROM continuity_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise RuntimeError("event disappeared during retention")
            return self._event_payload(row)

    def context(
        self,
        scopes: Iterable[str] | str,
        now: datetime,
        *,
        limit: int = 18,
    ) -> dict[str, list[dict[str, Any]]]:
        scopes = _scopes(scopes)
        if type(limit) is not int or not 0 <= limit <= MAX_EVENTS:
            raise ValueError("limit must be between 0 and 500")
        now_value = _stamp(now, field="now")
        if not scopes:
            return {"experiences": [], "thoughts": []}
        placeholders = ",".join("?" for _ in scopes)
        with self._db() as db:
            self._expire_thoughts(db, scopes, now_value)
            experiences = db.execute(
                f"""SELECT * FROM continuity_events
                    WHERE scope IN ({placeholders})
                    ORDER BY updated_at DESC, event_id DESC LIMIT ?""",
                (*scopes, limit),
            ).fetchall()
            active = db.execute(
                f"""SELECT * FROM continuity_thoughts
                    WHERE scope IN ({placeholders}) AND status IN ('open','paused')
                    ORDER BY CASE WHEN status='open' AND due_at!='' AND due_at<=?
                                  AND sharing_state NOT IN ('sending','draft','unknown','sent')
                                  THEN 0 ELSE 1 END, updated_at DESC, thought_id DESC LIMIT ?""",
                (*scopes, now_value, MAX_THOUGHTS_IN_CONTEXT),
            ).fetchall()
            remaining = max(0, MAX_THOUGHTS_IN_CONTEXT - len(active))
            closed = ()
            if remaining:
                closed = db.execute(
                    f"""SELECT * FROM continuity_thoughts
                        WHERE scope IN ({placeholders})
                          AND status IN ('done','dismissed','expired')
                        ORDER BY updated_at DESC, thought_id DESC LIMIT ?""",
                    (*scopes, min(RECENT_CLOSED_THOUGHTS, remaining)),
                ).fetchall()
            return {
                "experiences": [self._event_payload(row) for row in experiences],
                "thoughts": [self._thought_payload(row) for row in (*active, *closed)],
            }

    def event(self, event_id: str, *, scope: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM continuity_events WHERE event_id=? AND scope=?",
                             (event_id, scope)).fetchone()
            return self._event_payload(row) if row else None

    def thought(self, thought_id: str, *, scopes, now: datetime):
        scopes = _scopes(scopes)
        if not scopes:
            return None
        with self._db() as db:
            self._expire_thoughts(db, scopes, _stamp(now, field="now"))
            row = db.execute(f"SELECT * FROM continuity_thoughts WHERE thought_id=? AND scope IN ({','.join('?' for _ in scopes)})",
                             (thought_id, *scopes)).fetchone()
            return self._thought_payload(row) if row else None

    def has_due_thought(self, scopes, now: datetime, *, since: datetime | None = None) -> bool:
        scopes = _scopes(scopes)
        if not scopes:
            return False
        now_value = _stamp(now, field="now")
        since_value = _stamp(since, field="since") if since else ""
        placeholders = ",".join("?" for _ in scopes)
        with self._db() as db:
            return db.execute(f"""SELECT 1 FROM continuity_thoughts
                WHERE scope IN ({placeholders}) AND status='open' AND due_at!='' AND due_at<=?
                  AND (expires_at='' OR expires_at>?)
                  AND sharing_state NOT IN ('sending','draft','unknown','sent')
                  AND (due_at>? OR updated_at>?) LIMIT 1""",
                (*scopes, now_value, now_value, since_value, since_value)).fetchone() is not None

    def recover_pending_shares(self, delivery_states: dict[str, str], *, now: datetime):
        """Called once under the app process lock, before life scheduling starts.

        A life dispatch always creates its chat row before calling QQ. A claim
        without that row therefore never reached the sender and can be released.
        A persisted chat with a lost receipt stays unknown until reconciliation.
        """
        with self._db() as db:
            ids = [row[0] for row in db.execute(
                "SELECT DISTINCT delivery_id FROM continuity_thoughts WHERE sharing_state='sending'")]
        for delivery_id in ids:
            if delivery_id.startswith("life-chat-"):
                state = delivery_states.get(delivery_id, "not_attempted")
                self.finish_share(delivery_id, state="unknown" if state == "sending" else state, now=now)

    def remember_thought(
        self,
        content: str,
        *,
        scope: str,
        now: datetime,
        kind: str = "interest",
        source_refs: Iterable[Any] = (),
        thought_id: str = "",
        status: str = "open",
        due_at: datetime | str | None = "",
        expires_at: datetime | str | None = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        content = _text(content, field="content", maximum=MAX_CONTENT_CHARS)
        scope = _scope(scope)
        kind = _text(kind, field="kind", maximum=MAX_ID_CHARS)
        if kind not in ("interest", "topic"):
            raise ValueError("thought kind must be interest or topic")
        refs = _source_refs(source_refs)
        status = _thought_status(status)
        due_value = _optional_time(due_at, field="due_at")
        expires_value = _optional_time(expires_at, field="expires_at")
        now_value = _stamp(now, field="now")
        if expires_value and status in _ACTIVE_THOUGHT_STATUSES and expires_value <= now_value:
            status = "expired"
        if thought_id:
            thought_id = _identifier(thought_id, field="thought_id")
        elif expected_revision is not None:
            raise ValueError("expected_revision requires an existing thought_id")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("expected_revision must be a positive integer")
        refs_json = _encode_refs(refs)
        with self._db() as db:
            row = None
            if thought_id:
                row = db.execute(
                    "SELECT * FROM continuity_thoughts WHERE thought_id=?", (thought_id,)
                ).fetchone()
                if row is not None and row["scope"] != scope:
                    raise ValueError("thought_id already belongs to another scope")
            else:
                row = db.execute(
                    "SELECT * FROM continuity_thoughts WHERE scope=? AND kind=? AND content=?",
                    (scope, kind, content),
                ).fetchone()
                if row is not None:
                    return self._thought_payload(row)
                thought_id = self._stable_thought_id(scope, kind, content)
            if row is None:
                if expected_revision is not None:
                    raise ValueError("expected_revision does not match a new thought")
                db.execute(
                    """INSERT INTO continuity_thoughts
                       (thought_id,scope,content,kind,status,revision,source_refs_json,due_at,expires_at,
                        last_shared_at,sharing_state,delivery_id,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (thought_id, scope, content, kind, status, 1, refs_json, due_value, expires_value,
                     "", "", "", now_value, now_value),
                )
            else:
                if expected_revision is None or expected_revision != int(row["revision"]):
                    raise ValueError("expected_revision does not match the current thought")
                reopen = row["status"] in _CLOSED_THOUGHT_STATUSES and status in _ACTIVE_THOUGHT_STATUSES
                values_changed = any((
                    row["content"] != content,
                    row["kind"] != kind,
                    row["status"] != status,
                    row["source_refs_json"] != refs_json,
                    row["due_at"] != due_value,
                    row["expires_at"] != expires_value,
                ))
                if not values_changed:
                    return self._thought_payload(row)
                conflict = db.execute(
                    """SELECT thought_id FROM continuity_thoughts
                       WHERE scope=? AND kind=? AND content=? AND thought_id != ?""",
                    (scope, kind, content, thought_id),
                ).fetchone()
                if conflict is not None:
                    raise ValueError("a thought with the same scope, kind and content already exists")
                sharing_state = "" if reopen else row["sharing_state"]
                delivery_id = "" if reopen else row["delivery_id"]
                db.execute(
                    """UPDATE continuity_thoughts SET content=?, kind=?, status=?, revision=?,
                       source_refs_json=?, due_at=?, expires_at=?, sharing_state=?, delivery_id=?, updated_at=?
                       WHERE thought_id=?""",
                    (content, kind, status, int(row["revision"]) + 1, refs_json, due_value, expires_value,
                     sharing_state, delivery_id, now_value, thought_id),
                )
            result = db.execute("SELECT * FROM continuity_thoughts WHERE thought_id=?", (thought_id,)).fetchone()
            if result is None:
                raise RuntimeError("thought disappeared during update")
            return self._thought_payload(result)

    def reserve_share(
        self,
        thought_ids: Iterable[str],
        *,
        scopes: Iterable[str] | str,
        delivery_id: str,
        now: datetime,
    ) -> bool:
        if isinstance(thought_ids, str):
            thought_ids = (thought_ids,)
        try:
            thought_ids = tuple(dict.fromkeys(_identifier(item, field="thought_id") for item in thought_ids))
        except TypeError as error:
            raise TypeError("thought_ids must be an iterable of IDs") from error
        if len(thought_ids) > MAX_THOUGHTS_IN_CONTEXT:
            raise ValueError("too many thoughts to reserve")
        if not thought_ids:
            return True
        scopes = _scopes(scopes)
        delivery_id = _identifier(delivery_id, field="delivery_id")
        now_value = _stamp(now, field="now")
        placeholders = ",".join("?" for _ in thought_ids)
        scope_set = set(scopes)
        with self._db() as db:
            rows = db.execute(
                f"SELECT * FROM continuity_thoughts WHERE thought_id IN ({placeholders})",
                thought_ids,
            ).fetchall()
            if len(rows) != len(thought_ids):
                return False
            existing_delivery = db.execute(
                "SELECT thought_id, sharing_state FROM continuity_thoughts WHERE delivery_id=?",
                (delivery_id,),
            ).fetchall()
            requested = {row["thought_id"] for row in rows}
            if existing_delivery and any(row["thought_id"] not in requested for row in existing_delivery):
                return False
            already_claimed = all(
                row["delivery_id"] == delivery_id and row["sharing_state"] in ("draft", "sending")
                for row in rows
            )
            if already_claimed:
                return True
            for row in rows:
                if (row["scope"] not in scope_set or row["status"] != "open"
                        or (row["due_at"] and row["due_at"] > now_value)
                        or (row["expires_at"] and row["expires_at"] <= now_value)
                        or row["sharing_state"] in _CLAIMING_SHARE_STATES):
                    return False
            db.execute(
                f"""UPDATE continuity_thoughts
                    SET sharing_state='sending', delivery_id=?, updated_at=?
                    WHERE thought_id IN ({placeholders})""",
                (delivery_id, now_value, *thought_ids),
            )
            return True

    def finish_share(self, delivery_id: str, *, state: str, now: datetime) -> bool:
        delivery_id = _identifier(delivery_id, field="delivery_id")
        state = _sharing_state(state)
        if not state:
            raise ValueError("finish_share state must be non-empty")
        now_value = _stamp(now, field="now")
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM continuity_thoughts WHERE delivery_id=?", (delivery_id,)
            ).fetchall()
            if not rows:
                return False
            for row in rows:
                current = row["sharing_state"]
                if current == state:
                    continue
                if current == "sent" and state != "sent":
                    continue
                if current == "sent" and state == "sent":
                    continue
                if state == "sent":
                    db.execute(
                        """UPDATE continuity_thoughts
                           SET sharing_state='sent', last_shared_at=CASE WHEN last_shared_at='' THEN ? ELSE last_shared_at END,
                               updated_at=? WHERE thought_id=?""",
                        (now_value, now_value, row["thought_id"]),
                    )
                elif state in ("draft", "sending", "unknown"):
                    db.execute(
                        "UPDATE continuity_thoughts SET sharing_state=?, updated_at=? WHERE thought_id=?",
                        (state, now_value, row["thought_id"]),
                    )
                elif state in _RELEASED_SHARE_STATES:
                    db.execute(
                        """UPDATE continuity_thoughts SET sharing_state=?, delivery_id='', updated_at=?
                           WHERE thought_id=?""",
                        (state, now_value, row["thought_id"]),
                    )
            return True


__all__ = ["ContinuityStore"]
