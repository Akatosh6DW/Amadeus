"""模型 JSON 容器解析及截断字段的纯恢复。"""
from __future__ import annotations
import json
import re
from typing import Any


def parse_json_object(text: str) -> dict[str, Any] | None:
    candidate = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.S | re.I)
    if fenced:
        candidate = fenced.group(1).strip()
    for _ in range(3):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip() and value.strip() != candidate:
            candidate = value.strip()
            continue
        return None
    match = re.search(r"\{.*\}", candidate, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def looks_like_incomplete_structured_reply(text: Any) -> bool:
    """Distinguish broken response transport from an intentional plain-text answer."""

    candidate = str(text or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
    if not candidate.startswith(("{", "[")):
        return False
    return bool(re.search(
        r'["\'](?:reply|spokenTextJa|emotion|gesture|toolRequests|knowledgeConfidence)["\']\s*:',
        candidate,
        flags=re.I,
    ))


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
