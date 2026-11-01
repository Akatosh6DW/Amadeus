"""Explicit prompt ownership and content-free, per-request accounting.

Only application-owned modules with the same ID and version may replace one
another. User messages, tool receipts, attachments and unlabelled instructions
are never deduplicated by their wording.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from agent.context_pressure import estimate_tokens

MODULE_KEY = "_amadeusPromptModule"


def system_message(module_id: str, content: str, *, source: str,
                   version: str = "1", reason: str = "entry", category: str = "engineering") -> dict[str, Any]:
    return {"role": "system", "content": content, MODULE_KEY: {
        "id": module_id, "version": version, "source": source,
        "reason": reason, "category": category,
    }}


def assemble_modules(messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """The latest projection of a declared module owns its slot in this request."""
    latest: dict[tuple[str, str], int] = {}
    for index, message in enumerate(messages):
        module = message.get(MODULE_KEY)
        if message.get("role") == "system" and isinstance(module, Mapping) and module.get("id"):
            latest[(str(module["id"]), str(module.get("version", "1")))] = index
    active, removed = [], []
    for index, message in enumerate(messages):
        module = message.get(MODULE_KEY)
        if message.get("role") == "system" and isinstance(module, Mapping) and module.get("id"):
            key = (str(module["id"]), str(module.get("version", "1")))
            if latest[key] != index:
                removed.append({"id": key[0], "version": key[1], "source": str(module.get("source", ""))})
                continue
        active.append(dict(message))
    return active, removed


def prompt_manifest(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Never log text, names, URLs, account IDs, credentials or tool arguments."""
    blocks = []
    for index, message in enumerate(messages):
        content = message.get("content", "")
        serialized = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        role = str(message.get("role") or "")
        module = message.get(MODULE_KEY)
        metadata = dict(module) if isinstance(module, Mapping) else {}
        blocks.append({
            "index": index, "role": role,
            "id": metadata.get("id", f"unlabelled:{role}:{index}"),
            "version": metadata.get("version"), "source": metadata.get("source", "unlabelled"),
            "reason": metadata.get("reason", "existing_message"),
            "category": metadata.get("category", "unclassified"),
            "characters": len(serialized), "estimatedTokens": estimate_tokens(content),
            "sha256": sha256(serialized.encode("utf-8")).hexdigest(),
        })
    return {"protocolVersion": "semantic-v3", "tokenMeasurement": "estimate", "blocks": blocks,
            "estimatedContentTokens": sum(block["estimatedTokens"] for block in blocks)}


def wire_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in message.items() if key != MODULE_KEY} for message in messages]
