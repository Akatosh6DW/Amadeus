"""模型 JSON 容器解析及截断字段的纯恢复。"""
from __future__ import annotations
import json
import re
from typing import Any

from backend.providers.reply_protocol import (
    iter_json_values,
    looks_like_truncated_reply_envelope,
    parse_json_document,
    parse_json_mapping,
)


def parse_json_object(text: str) -> dict[str, Any] | None:
    candidate = str(text or "").strip()
    value = parse_json_mapping(candidate)
    if value is not None:
        return dict(value)
    # A response that starts as a JSON container but fails the whole-document
    # check is malformed/truncated.  Do not recover a later inner object or the
    # first of several adjacent documents from it.
    if candidate.startswith(("{", "[")):
        return None
    # Compatibility callers historically accepted a prose prefix around one
    # complete object.  Iterate decoder-confirmed values instead of using a
    # greedy ``{.*}`` expression that could cross unrelated objects.
    for embedded in iter_json_values(candidate):
        if isinstance(embedded, dict):
            return embedded
    return None


def looks_like_incomplete_structured_reply(text: Any) -> bool:
    """Distinguish broken response transport from an intentional plain-text answer."""

    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        newline = candidate.find("\n")
        if newline >= 0:
            candidate = candidate[newline + 1 :].lstrip()
    return looks_like_truncated_reply_envelope(candidate)


def extract_complete_json_string_field(text: Any, field: str) -> str:
    """Recover one complete JSON string field without displaying adjacent fields."""

    candidate = str(text or "")
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"', candidate)
    if not match:
        return ""
    start = match.end()
    escaped = False
    for index in range(start, len(candidate)):
        char = candidate[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char != '"':
            continue
        fragment = candidate[start:index]
        try:
            decoded = json.loads(f'"{fragment}"')
        except json.JSONDecodeError:
            return ""
        return decoded if isinstance(decoded, str) else ""
    return ""
