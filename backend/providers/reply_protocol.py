"""结构化模型回复的有界解析。

回复正文可以讨论协议字段、展示 JSON 或包含代码围栏。因而这里不把字段名
当成泄漏信号；只有完整 JSON 文档、或能由 JSON 解码器确认的嵌套 envelope
才会被当作协议。解析失败时只对看起来确实是协议对象的输入报告截断，普通
技术正文仍可作为文本候选交给上层的语义恢复。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
import json
from typing import Any


# These names describe the application response contract.  They are used only
# after structural JSON parsing (or while identifying a truncated object), never
# as a substring blacklist over visible prose.
VISIBLE_REPLY_FIELDS = frozenset({
    "reply",
    "textZh",
    "text_zh",
    "zh",
    "text",
    "spokenTextJa",
    "spoken_text_ja",
    "spokenJa",
    "ja",
})

PROTOCOL_FIELDS = frozenset({
    *VISIBLE_REPLY_FIELDS,
    "payload",
    "kind",
    "type",
    "semanticKind",
    "speechUnits",
    "utterances",
    "emotion",
    "emotionIntensity",
    "gesture",
    "voiceStyle",
    "voiceTone",
    "voiceIntensity",
    "speechPace",
    "speechSpeed",
    "deliveryMode",
    "memoryUpdates",
    "memoryAction",
    "relationEvent",
    "requiresUserResponse",
    "expectedResponseType",
    "completedActs",
    "turnComplete",
    "turnEnd",
    "streamProtocol",
    "cognitive",
    "calls",
    "toolRequests",
    "scheduleIntent",
    "sources",
})

_MAX_UNWRAP_DEPTH = 3


def _strip_whole_code_fence(value: str) -> tuple[str, bool]:
    """Return a complete fenced body, without treating inline fences as syntax."""

    candidate = value.strip()
    if not candidate.startswith("```"):
        return candidate, False
    first_newline = candidate.find("\n")
    if first_newline < 0 or not candidate.endswith("```"):
        return candidate, False
    body = candidate[first_newline + 1 : -3].strip()
    return body, True


def parse_json_document(value: Any, *, allow_code_fence: bool = True) -> Any | None:
    """Decode one complete JSON document.

    ``json.JSONDecoder.raw_decode`` is deliberately used with an end-position
    check.  This prevents a valid object embedded before trailing garbage from
    being mistaken for the complete provider response.
    """

    if not isinstance(value, str):
        return value
    candidate = value.strip()
    if allow_code_fence:
        candidate, _ = _strip_whole_code_fence(candidate)
    if not candidate:
        return None
    decoder = json.JSONDecoder()
    try:
        parsed, end = decoder.raw_decode(candidate)
    except json.JSONDecodeError:
        return None
    if candidate[end:].strip():
        return None
    return parsed


def parse_json_mapping(value: Any, *, max_string_layers: int = 3) -> Mapping[str, Any] | None:
    """Decode a mapping, including a bounded number of JSON-encoded strings."""

    candidate: Any = value
    for _ in range(max(1, int(max_string_layers))):
        parsed = parse_json_document(candidate)
        if not isinstance(parsed, str) or parsed == candidate:
            candidate = parsed
            break
        candidate = parsed
    return candidate if isinstance(candidate, Mapping) else None


def iter_json_values(text: str) -> Iterator[Any]:
    """Yield complete JSON values embedded in text in source order.

    This is used only by compatibility callers that historically accepted a
    prose prefix around a JSON object.  It never fabricates closing braces and
    therefore cannot turn a truncated object into a deliverable value.
    """

    candidate = str(text or "")
    decoder = json.JSONDecoder()
    index = 0
    while index < len(candidate):
        next_open = min(
            (position for position in (candidate.find("{", index), candidate.find("[", index)) if position >= 0),
            default=-1,
        )
        if next_open < 0:
            return
        try:
            parsed, end = decoder.raw_decode(candidate[next_open:])
        except json.JSONDecodeError:
            index = next_open + 1
            continue
        yield parsed
        index = next_open + max(1, end)


def _mapping_keys(value: Mapping[str, Any]) -> set[str]:
    return {str(key) for key in value.keys()}


def is_reply_envelope(value: Any) -> bool:
    """Whether a parsed mapping has the shape of an application reply envelope."""

    if not isinstance(value, Mapping):
        return False
    keys = _mapping_keys(value)
    # V3 cognitive turns may carry visible text only inside ordered speech
    # units.  Their field names are protocol-specific even when no legacy
    # scalar ``reply`` is present, so they must enter the envelope path too.
    if keys.intersection({"speechUnits", "utterances"}):
        return True
    if not keys.intersection(VISIBLE_REPLY_FIELDS):
        return False
    overlap = keys.intersection(PROTOCOL_FIELDS)
    # A one-field ``{"reply": ...}`` object is unambiguously an outer
    # envelope.  Other one-field JSON examples (for example ``{"text":
    # "..."}``) need a second contract field before they enter the protocol
    # path; this keeps visible examples from being decoded as transport.
    return keys == {"reply"} or len(overlap) >= 2


def _is_nested_envelope(value: Any) -> bool:
    """Require two contract fields before decoding an inner JSON example."""

    if not isinstance(value, Mapping) or not is_reply_envelope(value):
        return False
    return len(_mapping_keys(value).intersection(PROTOCOL_FIELDS)) >= 2


def is_nested_reply_envelope(value: Any) -> bool:
    """Recognize an inner envelope only when it carries two schema fields."""

    return _is_nested_envelope(value)


def _nested_envelope(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Find one structurally complete nested envelope, if present."""

    for key in ("payload", "reply", "textZh", "text_zh", "text"):
        nested = value.get(key)
        if isinstance(nested, Mapping) and _is_nested_envelope(nested):
            return nested
        if isinstance(nested, str):
            parsed = parse_json_mapping(nested)
            if parsed is not None and _is_nested_envelope(parsed):
                return parsed
    return None


def unwrap_reply_envelope(value: Mapping[str, Any], *, max_depth: int = _MAX_UNWRAP_DEPTH) -> dict[str, Any]:
    """Deterministically merge nested response envelopes.

    Outer metadata is retained while inner protocol fields win.  A bounded depth
    keeps malformed provider output from becoming an unbounded recursion path.
    """

    current = dict(value)
    for _ in range(max(0, int(max_depth))):
        nested = _nested_envelope(current)
        if nested is None or nested == current:
            break
        current = {**current, **dict(nested)}
    return current


def visible_reply(value: Mapping[str, Any]) -> str:
    """Extract the first visible reply field from a parsed envelope."""

    normalized = unwrap_reply_envelope(value)
    for key in ("reply", "textZh", "text_zh", "zh", "text"):
        candidate = normalized.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _truncated_object_keys(text: str) -> set[str]:
    """Collect top-level JSON object keys even when the value is truncated."""

    candidate = str(text or "").lstrip()
    if not candidate.startswith(("{", "[")):
        return set()
    decoder = json.JSONDecoder()
    keys: set[str] = set()
    object_depth = 0
    quoted = False
    escaped = False
    index = 0
    while index < len(candidate):
        char = candidate[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            if object_depth == 1:
                try:
                    key, end = decoder.raw_decode(candidate[index:])
                except json.JSONDecodeError:
                    break
                if isinstance(key, str):
                    after = index + end
                    while after < len(candidate) and candidate[after].isspace():
                        after += 1
                    if after < len(candidate) and candidate[after] == ":":
                        keys.add(key)
                # Skip the complete key token; nested content is handled by the
                # normal loop after the colon.
                index += max(1, end)
                continue
            quoted = True
        elif char == "{":
            object_depth += 1
        elif char == "}":
            object_depth = max(0, object_depth - 1)
        index += 1
    return keys


def looks_like_truncated_reply_envelope(text: Any) -> bool:
    """Return true only for malformed JSON with contract-shaped top-level keys."""

    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        if first_newline < 0:
            return False
        # An opening fence without its closing fence is itself an incomplete
        # transport wrapper.  Inspect the body as JSON, but keep the missing
        # fence visible to the caller as a protocol-invalid response.
        body = candidate[first_newline + 1 :].lstrip()
        if body:
            parsed_body = parse_json_document(body, allow_code_fence=False)
            if isinstance(parsed_body, Mapping) and is_reply_envelope(parsed_body):
                return not candidate.endswith("```")
            candidate = body
    if not candidate.startswith(("{", "[")):
        return False
    if parse_json_document(candidate) is not None:
        return False
    keys = _truncated_object_keys(candidate)
    return bool(keys.intersection(PROTOCOL_FIELDS))


__all__ = [
    "PROTOCOL_FIELDS",
    "VISIBLE_REPLY_FIELDS",
    "is_reply_envelope",
    "is_nested_reply_envelope",
    "iter_json_values",
    "looks_like_truncated_reply_envelope",
    "parse_json_document",
    "parse_json_mapping",
    "unwrap_reply_envelope",
    "visible_reply",
]
