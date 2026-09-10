"""SQLite persistence for user-defined web bookmarks.

Bookmark names are normalized only for exact identity matching.  The store
does not infer intent and never performs fuzzy matching, so a similar name
cannot silently resolve to the wrong website.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any, Mapping
import uuid

from tools.web_destinations import normalize_destination_alias


BOOKMARK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS web_bookmarks (
    bookmark_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    alias_key TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    source_turn_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_web_bookmarks_updated
ON web_bookmarks(deleted_at, updated_at DESC);
"""


class BookmarkVersionConflict(RuntimeError):
    def __init__(self, bookmark: Mapping[str, Any], expected_version: int) -> None:
        super().__init__("bookmark_version_conflict")
        self.bookmark = dict(bookmark)
        self.expected_version = int(expected_version)


class BookmarkAliasConflict(RuntimeError):
    def __init__(self, bookmark: Mapping[str, Any]) -> None:
        super().__init__("bookmark_alias_conflict")
        self.bookmark = dict(bookmark)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_bookmark_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(BOOKMARK_SCHEMA_SQL)


def bookmark_alias_key(value: Any) -> str:
    return normalize_destination_alias(str(value or ""))


def _public(row: sqlite3.Row | Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    value = dict(row)
    return {
        "bookmarkId": str(value.get("bookmark_id") or ""),
        "name": str(value.get("display_name") or ""),
        "url": str(value.get("canonical_url") or ""),
        "version": int(value.get("version") or 1),
        "sourceTurnId": str(value.get("source_turn_id") or ""),
        "createdAt": str(value.get("created_at") or ""),
        "updatedAt": str(value.get("updated_at") or ""),
        "deletedAt": str(value.get("deleted_at") or ""),
    }


def read_bookmark(connection: sqlite3.Connection, bookmark_id: str) -> dict[str, Any]:
    return _public(connection.execute(
        "SELECT * FROM web_bookmarks WHERE bookmark_id = ?",
        (str(bookmark_id or ""),),
    ).fetchone())


def resolve_bookmark(connection: sqlite3.Connection, name: str) -> dict[str, Any]:
    key = bookmark_alias_key(name)
    if not key:
        return {}
    return _public(connection.execute(
        "SELECT * FROM web_bookmarks WHERE alias_key = ? AND deleted_at = ''",
        (key,),
    ).fetchone())


def list_bookmarks(
    connection: sqlite3.Connection,
    *,
    query: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    key = bookmark_alias_key(query)
    rows = connection.execute(
        "SELECT * FROM web_bookmarks WHERE deleted_at = '' "
        "ORDER BY updated_at DESC LIMIT ?",
        (max(1, min(500, int(limit))),),
    ).fetchall()
    items = [_public(row) for row in rows]
    if key:
        items = [
            item for item in items
            if key in bookmark_alias_key(item.get("name"))
            or key in str(item.get("url") or "").casefold()
        ]
    return items


def create_bookmark(
    connection: sqlite3.Connection,
    *,
    name: str,
    canonical_url: str,
    source_turn_id: str = "",
) -> dict[str, Any]:
    display_name = " ".join(str(name or "").split()).strip()
    key = bookmark_alias_key(display_name)
    if not display_name or not key:
        raise ValueError("bookmark_name_required")
    existing = connection.execute(
        "SELECT * FROM web_bookmarks WHERE alias_key = ?",
        (key,),
    ).fetchone()
    if existing is not None:
        raise BookmarkAliasConflict(_public(existing))
    timestamp = _now_iso()
    bookmark_id = f"bookmark-{uuid.uuid4().hex}"
    connection.execute(
        "INSERT INTO web_bookmarks(bookmark_id, display_name, alias_key, canonical_url, "
        "version, source_turn_id, created_at, updated_at, deleted_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, '')",
        (
            bookmark_id, display_name, key, str(canonical_url or "").strip(),
            str(source_turn_id or ""), timestamp, timestamp,
        ),
    )
    return read_bookmark(connection, bookmark_id)


def update_bookmark(
    connection: sqlite3.Connection,
    bookmark_id: str,
    changes: Mapping[str, Any],
    *,
    expected_version: int = 0,
) -> dict[str, Any]:
    current = read_bookmark(connection, bookmark_id)
    if not current or current.get("deletedAt"):
        return {}
    if expected_version and int(current.get("version") or 0) != int(expected_version):
        raise BookmarkVersionConflict(current, expected_version)
    display_name = (
        " ".join(str(changes.get("name") or "").split()).strip()
        if "name" in changes else str(current.get("name") or "")
    )
    key = bookmark_alias_key(display_name)
    if not display_name or not key:
        raise ValueError("bookmark_name_required")
    collision = connection.execute(
        "SELECT * FROM web_bookmarks WHERE alias_key = ? AND bookmark_id <> ?",
        (key, str(bookmark_id or "")),
    ).fetchone()
    if collision is not None:
        raise BookmarkAliasConflict(_public(collision))
    canonical_url = (
        str(changes.get("url") or "").strip()
        if "url" in changes else str(current.get("url") or "")
    )
    connection.execute(
        "UPDATE web_bookmarks SET display_name = ?, alias_key = ?, canonical_url = ?, "
        "version = version + 1, updated_at = ? WHERE bookmark_id = ?",
        (display_name, key, canonical_url, _now_iso(), str(bookmark_id or "")),
    )
    return read_bookmark(connection, bookmark_id)


def delete_bookmark(
    connection: sqlite3.Connection,
    bookmark_id: str,
    *,
    expected_version: int = 0,
) -> dict[str, Any]:
    current = read_bookmark(connection, bookmark_id)
    if not current or current.get("deletedAt"):
        return {}
    if expected_version and int(current.get("version") or 0) != int(expected_version):
        raise BookmarkVersionConflict(current, expected_version)
    deleted = dict(current)
    deleted["version"] = int(current.get("version") or 0) + 1
    deleted["deletedAt"] = _now_iso()
    connection.execute(
        "DELETE FROM web_bookmarks WHERE bookmark_id = ?",
        (str(bookmark_id or ""),),
    )
    return deleted
