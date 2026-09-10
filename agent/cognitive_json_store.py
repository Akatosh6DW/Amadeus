"""Lossless SQLite storage for large cognitive JSON, independent of prompting.

Small documents keep the legacy inline format. Large documents use immutable,
content-addressed sections in the same transaction. Readers reconstruct the
original object; this layer never summarizes or trims conversation content.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from typing import Any


INLINE_JSON_BYTES = 128_000
MAX_RECORD_JSON_BYTES = 512_000
STORAGE_FORMAT = "amadeus-cognitive-json-v1"


class JsonStorageError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def ensure_json_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS cognitive_json_sections (
        section_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL
    )""")


def store_json(connection: sqlite3.Connection, value: Any, *, encoded: str) -> str:
    """Return inline JSON or a small reference. Never commit the connection."""
    if len(encoded.encode("utf-8")) <= INLINE_JSON_BYTES:
        return encoded

    def store_node(item: Any) -> str:
        text = _json(item)
        if len(text.encode("utf-8")) <= INLINE_JSON_BYTES:
            node = {"kind": "value", "value": item}
        elif isinstance(item, dict):
            node = {"kind": "object", "items": {key: store_node(val) for key, val in item.items()}}
        elif isinstance(item, list):
            node = {"kind": "array", "items": [store_node(val) for val in item]}
        elif isinstance(item, str):
            # Six bytes also covers JSON-escaped control characters.
            width = INLINE_JSON_BYTES // 6
            node = {"kind": "string", "items": [store_node(item[i:i + width])
                                                  for i in range(0, len(item), width)]}
        else:
            raise JsonStorageError("unsupported oversized cognitive JSON value")
        payload = _json(node)
        size = len(payload.encode("utf-8"))
        if size > MAX_RECORD_JSON_BYTES:
            raise JsonStorageError(f"cognitive JSON section too large: bytes={size}, limit={MAX_RECORD_JSON_BYTES}")
        section_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        connection.execute("INSERT OR IGNORE INTO cognitive_json_sections(section_id, payload_json) VALUES (?, ?)",
                           (section_id, payload))
        return section_id

    return _json({"storageFormat": STORAGE_FORMAT, "root": store_node(value)})


def load_json(connection: sqlite3.Connection, encoded: str) -> Any:
    """Read both legacy inline rows and new section references."""
    value = json.loads(encoded)
    if (not isinstance(value, dict) or set(value) != {"storageFormat", "root"}
            or value.get("storageFormat") != STORAGE_FORMAT):
        return value
    cache: dict[str, Any] = {}

    def read_node(section_id: str) -> Any:
        if section_id in cache:
            return deepcopy(cache[section_id])
        row = connection.execute("SELECT payload_json FROM cognitive_json_sections WHERE section_id=?",
                                 (section_id,)).fetchone()
        if row is None:
            raise JsonStorageError(f"missing cognitive JSON section: {section_id}")
        if hashlib.sha256(row[0].encode("utf-8")).hexdigest() != section_id:
            raise JsonStorageError(f"corrupt cognitive JSON section: {section_id}")
        node = json.loads(row[0])
        kind = node.get("kind")
        if kind == "value":
            result = node["value"]
        elif kind == "object":
            result = {key: read_node(ref) for key, ref in node["items"].items()}
        elif kind in {"array", "string"}:
            result = [read_node(ref) for ref in node["items"]]
            if kind == "string":
                result = "".join(result)
        else:
            raise JsonStorageError(f"invalid cognitive JSON section kind: {kind}")
        cache[section_id] = result
        return result

    return read_node(value["root"])
