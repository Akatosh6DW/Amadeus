"""Durable, runtime-learned website names and aliases.

The curated catalog in ``tools.web_destinations`` remains an immutable seed.
This store records destinations that the user actually opened or bookmarked,
without turning a browser receipt into a claim that the URL is an official
website.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any, Iterable, Mapping
import uuid

from tools.web_destinations import normalize_destination_alias


WEB_DESTINATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS learned_web_destinations (
    destination_id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL DEFAULT 'observed_usage',
    source_turn_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learned_web_destination_aliases (
    alias_key TEXT PRIMARY KEY,
    alias_text TEXT NOT NULL,
    destination_id TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'observed_usage',
    source_turn_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(destination_id) REFERENCES learned_web_destinations(destination_id)
);
CREATE INDEX IF NOT EXISTS idx_learned_web_alias_destination
ON learned_web_destination_aliases(destination_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_web_destination_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(WEB_DESTINATION_SCHEMA_SQL)


def _public(
    destination: sqlite3.Row | Mapping[str, Any] | None,
    *,
    matched_alias: str = "",
) -> dict[str, Any]:
    if destination is None:
        return {}
    row = dict(destination)
    return {
        "id": str(row.get("destination_id") or ""),
        "title": str(row.get("canonical_name") or ""),
        "url": str(row.get("canonical_url") or ""),
        "matchedAlias": str(matched_alias or ""),
        "source": "learned_web_destinations",
        "sourceKind": str(row.get("source_kind") or ""),
        "sourceTurnId": str(row.get("source_turn_id") or ""),
        "createdAt": str(row.get("created_at") or ""),
        "updatedAt": str(row.get("updated_at") or ""),
    }


def resolve_learned_web_destination(
    connection: sqlite3.Connection,
    alias: str,
) -> dict[str, Any]:
    key = normalize_destination_alias(alias)
    if not key:
        return {}
    row = connection.execute(
        "SELECT d.*, a.alias_text AS matched_alias "
        "FROM learned_web_destination_aliases AS a "
        "JOIN learned_web_destinations AS d "
        "ON d.destination_id = a.destination_id "
        "WHERE a.alias_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return {}
    return _public(row, matched_alias=str(row["matched_alias"] or ""))


def learn_web_destination(
    connection: sqlite3.Connection,
    *,
    canonical_name: str,
    canonical_url: str,
    aliases: Iterable[str] = (),
    source_kind: str = "observed_usage",
    source_turn_id: str = "",
) -> dict[str, Any]:
    """Idempotently remember a used URL and attach unclaimed exact aliases.

    An existing alias is never silently moved to another URL.  Conflicts are
    returned to the caller so one mistaken open cannot corrupt prior memory.
    """

    name = " ".join(str(canonical_name or "").split()).strip()[:240]
    url = str(canonical_url or "").strip()[:4096]
    if not name or not url:
        raise ValueError("web_destination_name_and_url_required")
    timestamp = _now_iso()
    row = connection.execute(
        "SELECT * FROM learned_web_destinations WHERE canonical_url = ?",
        (url,),
    ).fetchone()
    created = row is None
    if row is None:
        destination_id = f"destination-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO learned_web_destinations(" 
            "destination_id, canonical_name, canonical_url, source_kind, "
            "source_turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                destination_id, name, url, str(source_kind or "observed_usage"),
                str(source_turn_id or ""), timestamp, timestamp,
            ),
        )
    else:
        destination_id = str(row["destination_id"])
        connection.execute(
            "UPDATE learned_web_destinations SET updated_at = ?, "
            "source_turn_id = CASE WHEN ? <> '' THEN ? ELSE source_turn_id END "
            "WHERE destination_id = ?",
            (timestamp, str(source_turn_id or ""), str(source_turn_id or ""), destination_id),
        )

    learned_aliases: list[str] = []
    conflicts: list[dict[str, str]] = []
    alias_values = [name, *(str(item or "") for item in aliases)]
    seen_keys: set[str] = set()
    for alias_text in alias_values:
        display = " ".join(alias_text.split()).strip()[:240]
        key = normalize_destination_alias(display)
        if not display or not key or key in seen_keys:
            continue
        seen_keys.add(key)
        existing = connection.execute(
            "SELECT a.destination_id, d.canonical_url FROM learned_web_destination_aliases a "
            "JOIN learned_web_destinations d ON d.destination_id = a.destination_id "
            "WHERE a.alias_key = ?",
            (key,),
        ).fetchone()
        if existing is not None and str(existing["destination_id"]) != destination_id:
            conflicts.append({
                "alias": display,
                "existingUrl": str(existing["canonical_url"] or ""),
            })
            continue
        connection.execute(
            "INSERT INTO learned_web_destination_aliases(" 
            "alias_key, alias_text, destination_id, source_kind, source_turn_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(alias_key) DO UPDATE SET "
            "alias_text=excluded.alias_text, updated_at=excluded.updated_at, "
            "source_turn_id=CASE WHEN excluded.source_turn_id <> '' "
            "THEN excluded.source_turn_id ELSE learned_web_destination_aliases.source_turn_id END",
            (
                key, display, destination_id, str(source_kind or "observed_usage"),
                str(source_turn_id or ""), timestamp, timestamp,
            ),
        )
        learned_aliases.append(display)

    destination = connection.execute(
        "SELECT * FROM learned_web_destinations WHERE destination_id = ?",
        (destination_id,),
    ).fetchone()
    return {
        **_public(destination, matched_alias=learned_aliases[0] if learned_aliases else ""),
        "created": created,
        "learnedAliases": learned_aliases,
        "aliasConflicts": conflicts,
    }


def list_learned_web_destinations(
    connection: sqlite3.Connection,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM learned_web_destinations ORDER BY updated_at DESC LIMIT ?",
        (max(1, min(500, int(limit))),),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        aliases = connection.execute(
            "SELECT alias_text FROM learned_web_destination_aliases "
            "WHERE destination_id = ? ORDER BY created_at, alias_text",
            (str(row["destination_id"]),),
        ).fetchall()
        result.append({
            **_public(row),
            "aliases": [str(alias["alias_text"] or "") for alias in aliases],
        })
    return result
