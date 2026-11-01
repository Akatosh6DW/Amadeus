"""Pure, typed time semantics for Todo schedule commands.

The model chooses the semantic relation (the next occurrence, a calendar
date, a duration, or the date already attached to a task).  This module owns
the small deterministic boundary that turns that choice into a UTC instant
and produces facts from the persisted task.  It deliberately has no access to
files, databases, network state, or the process clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


__all__ = [
    "normalize_time_spec",
    "ParsedClock",
    "parse_clock_text",
    "parse_time_quote",
    "validate_time_source_evidence",
    "validate_schedule_time_evidence",
    "resolve_time_spec",
    "schedule_time_facts",
]


_KINDS = frozenset({
    "next_clock",
    "next_clock_12h",
    "next_weekday",
    "calendar",
    "after",
    "existing_date",
})
_COMMON_FIELDS = frozenset({"kind", "reason"})
_UNSPECIFIED_MERIDIEM = "unspecified"
_CLOCK_RE = re.compile(r"^(?P<hour>[0-9]|[01]\d|2[0-3]):(?P<minute>[0-5]\d)(?::(?P<second>[0-5]\d))?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<hour>\d{2})(?::?(?P<minute>\d{2}))$")

# regex-purpose: format; bounded clock syntax, validated numeric ranges below.
_CLOCK_TEXT_RE = re.compile(
    r"^(?P<hour>\d{1,2})(?:\s*:\s*(?P<minute>\d{1,2})(?:\s*:\s*(?P<second>\d{1,2}))?)?$",
    re.I,
)
# regex-purpose: format; explicit meridiem suffix, not inferred user intent.
_AMPM_SUFFIX_RE = re.compile(
    r"^(?P<clock>\d{1,2}(?:\s*:\s*\d{1,2})?(?:\s*:\s*\d{1,2})?)\s*"
    r"(?P<meridiem>a\.?m\.?|p\.?m\.?)$",
    re.I,
)
# regex-purpose: format; explicit meridiem prefix, not inferred user intent.
_AMPM_PREFIX_RE = re.compile(
    r"^(?P<meridiem>a\.?m\.?|p\.?m\.?)\s*"
    r"(?P<clock>\d{1,2}(?:\s*:\s*\d{1,2})?(?:\s*:\s*\d{1,2})?)$",
    re.I,
)
# regex-purpose: format; explicit Chinese clock tokens with numeric validation.
_CHINESE_CLOCK_RE = re.compile(
    r"^(?:(?P<meridiem>今晚|傍晚|早晨|清晨|凌晨|早上|上午|中午|下午|晚上|晚|夜里)\s*)?"
    r"(?P<hour>\d{1,2}|[零〇一二两三四五六七八九十百]+)\s*"
    r"(?:(?P<colon>[:：])\s*(?P<cminute>\d{1,2}|[零〇一二两三四五六七八九十百]+)|"
    r"(?P<unit>[点时])\s*(?:(?P<half>半)|"
    r"(?P<quarter>一刻|三刻)|"
    r"(?P<after>过\s*(?P<after_minute>\d{1,2}|[零〇一二两三四五六七八九十百]+)\s*分?)|"
    r"(?P<minute>\d{1,2}|[零〇一二两三四五六七八九十百]+)\s*分?)?)?$",
    re.I,
)
_BOUNDARY_LEFT = frozenset(".:")
_SOURCE_ID_FIELDS = (
    "sourceMessageId", "source_message_id", "messageId", "message_id",
    "sourceTurnId", "source_turn_id", "turnId", "turn_id", "id",
    "sourceId", "source_id", "messageRef", "message_ref",
)
_SOURCE_QUOTE_FIELDS = (
    "quote", "sourceQuote", "source_quote", "sourceSpan", "source_span",
)
_SOURCE_CLOCK_FIELDS = (
    "parsedClock", "parsed_clock", "clock", "timeValue", "time_value",
    "parsedTime", "parsed_time", "value", "time",
)
_SOURCE_ROLE_FIELDS = (
    "sourceRole", "source_role", "role", "speaker", "sourceSpeaker",
    "source_speaker",
)
_SOURCE_ORIGIN_FIELDS = (
    "sourceType", "source_type", "origin", "conversationOrigin",
    "conversation_origin", "contextSource", "context_source",
)
_TOOL_SOURCE_ALIASES = frozenset({
    "tool", "tool_result", "tool_evidence", "toolresult", "toolresults",
    "web", "web_evidence", "research", "ocr", "visual", "attachment",
})


@dataclass(frozen=True)
class ParsedClock:
    """A clock parsed from one complete source quote."""

    clock: str
    quote: str

    def to_dict(self) -> dict[str, str]:
        return {"clock": self.clock, "quote": self.quote}


def _nfkc(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _chinese_number(value: str) -> int | None:
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    text = str(value or "")
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        if left and left not in digits:
            return None
        if right and right not in digits:
            return None
        return tens * 10 + ones
    if all(char in digits for char in text):
        result = 0
        for char in text:
            result = result * 10 + digits[char]
        return result
    return None


def _format_clock(hour: int, minute: int, second: int = 0) -> str | None:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        return None
    return f"{hour:02d}:{minute:02d}" + (f":{second:02d}" if second else "")


def _parse_numeric_clock(value: str) -> str | None:
    match = _CLOCK_TEXT_RE.fullmatch(value)
    if match is None:
        return None
    return _format_clock(
        int(match.group("hour")),
        int(match.group("minute") or 0),
        int(match.group("second") or 0),
    )


def parse_clock_text(value: Any) -> str | None:
    """Parse one complete human clock expression into ``HH:MM[:SS]``.

    The parser intentionally accepts only a complete expression.  It never
    searches arbitrary prose, so callers must separately prove that a quote
    is present in an identified source message and is not a token fragment.
    """

    text = _nfkc(value)
    if not text:
        return None
    aliases = {
        "正午": "12:00",
        "中午十二点": "12:00",
        "午夜": "00:00",
        "零点": "00:00",
    }
    if text in aliases:
        return aliases[text]

    match = _AMPM_SUFFIX_RE.fullmatch(text) or _AMPM_PREFIX_RE.fullmatch(text)
    if match is not None:
        clock = _parse_numeric_clock(_nfkc(match.group("clock")))
        if clock is None:
            return None
        hour = int(clock[:2])
        suffix = match.group("meridiem").lower().replace(".", "")
        if hour > 12 or hour == 0:
            return None
        if suffix == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        second = int(clock[6:8]) if len(clock) > 5 else 0
        return _format_clock(hour, int(clock[3:5]), second)

    # A bare 24-hour clock is unambiguous, while a bare ``9`` is not a clock
    # source and is therefore intentionally rejected.
    bare = _parse_numeric_clock(text)
    if bare is not None and ":" in text:
        return bare

    match = _CHINESE_CLOCK_RE.fullmatch(text)
    if match is None or not match.group("unit"):
        return None
    hour = _chinese_number(match.group("hour"))
    if hour is None:
        return None
    if match.group("colon"):
        minute = _chinese_number(match.group("cminute"))
    elif match.group("half"):
        minute = 30
    elif match.group("quarter"):
        minute = 15 if match.group("quarter") == "一刻" else 45
    elif match.group("after"):
        minute = _chinese_number(match.group("after_minute"))
    elif match.group("minute"):
        minute = _chinese_number(match.group("minute"))
    else:
        minute = 0
    if minute is None:
        return None
    meridiem = match.group("meridiem") or ""
    if meridiem in {"今晚", "傍晚", "下午", "晚上", "晚", "夜里", "中午"} and hour < 12:
        hour += 12
    elif meridiem in {"凌晨", "早晨", "清晨", "早上", "上午"} and hour == 12:
        hour = 0
    return _format_clock(hour, minute)


def parse_time_quote(value: Any) -> str | None:
    """Compatibility alias for the complete-quote clock parser."""

    return parse_clock_text(value)


def _first_field(mapping: Mapping[str, Any], fields: Iterable[str]) -> Any:
    for field in fields:
        value = mapping.get(field)
        if value not in (None, ""):
            return value
    return None


def _source_id(message: Mapping[str, Any], fallback: Any = None) -> str:
    value = _first_field(message, _SOURCE_ID_FIELDS)
    return str(value if value not in (None, "") else fallback or "").strip()[:180]


def _source_role(message: Mapping[str, Any]) -> str:
    value = _first_field(message, _SOURCE_ROLE_FIELDS)
    role = str(value or "").strip().lower()
    if role:
        return role
    if message.get("user") not in (None, ""):
        return "user"
    if message.get("assistant") not in (None, ""):
        return "assistant"
    return ""


def _source_origin(message: Mapping[str, Any]) -> str:
    value = _first_field(message, _SOURCE_ORIGIN_FIELDS)
    return _nfkc(value).casefold().replace("-", "_").replace(" ", "_")


def _source_content(message: Mapping[str, Any]) -> str:
    for key in ("content", "text", "user", "assistant", "message"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            parts: list[str] = []
            for item in value:
                if isinstance(item, Mapping):
                    text = item.get("text") or item.get("content") or item.get("value")
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
                elif isinstance(item, str) and item.strip():
                    parts.append(item)
            if parts:
                return " ".join(parts)
    return ""


def _source_messages(messages: Any) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    if isinstance(messages, Mapping):
        nested_messages = messages.get("messages") or messages.get("turns")
        if isinstance(nested_messages, Sequence) and not isinstance(nested_messages, (str, bytes)):
            messages = nested_messages
        else:
            nested_by_id = messages.get("sourceMessages") or messages.get("source_messages")
            if isinstance(nested_by_id, (Mapping, Sequence)) and not isinstance(nested_by_id, (str, bytes)):
                messages = nested_by_id
    if isinstance(messages, Mapping):
        if any(key in messages for key in ("role", "content", "text", "user", "assistant", "message")):
            ident = _source_id(messages)
            return ((ident, messages),) if ident else (("", messages),)
        result: list[tuple[str, Mapping[str, Any]]] = []
        for key, value in messages.items():
            if isinstance(value, Mapping):
                result.append((_source_id(value, key), value))
        return tuple(result)
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        result = []
        for item in messages:
            if isinstance(item, Mapping):
                result.append((_source_id(item), item))
        return tuple(result)
    return ()


def _evidence_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    for key in (
        "timeSource", "time_source", "timeEvidence", "time_evidence",
        "sourceEvidence", "source_evidence",
    ):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested
    nested_source = value.get("source")
    if isinstance(nested_source, Mapping):
        merged = dict(value)
        merged.update({key: nested for key, nested in nested_source.items() if key not in merged})
        return merged
    return value


def _matching_quote_indexes(source: str, quote: str) -> tuple[int, ...]:
    source_nfkc = _nfkc(source)
    quote_nfkc = _nfkc(quote)
    if not source_nfkc or not quote_nfkc:
        return ()
    indexes: list[int] = []
    start = 0
    while True:
        found = source_nfkc.find(quote_nfkc, start)
        if found < 0:
            break
        indexes.append(found)
        start = found + max(1, len(quote_nfkc))
    return tuple(indexes)


def _quote_is_complete_token(source: str, quote: str, index: int) -> bool:
    """Return whether ``quote`` is not a suffix of a larger clock token.

    The model supplies the quote; this check only establishes that the quote
    occurs as a complete source span.  In particular, ``4:30`` in ``24:30``
    and ``12时`` in ``3.12时`` must not become time evidence merely because
    their suffixes parse successfully.
    """

    source_text = _nfkc(source)
    quote_text = _nfkc(quote)
    left = source_text[index - 1] if index > 0 else ""
    end = index + len(quote_text)
    right = source_text[end] if end < len(source_text) else ""
    return (
        left not in _BOUNDARY_LEFT
        and not left.isdigit()
        and not right.isdigit()
    )


def validate_time_source_evidence(
    evidence: Any,
    messages: Any = (),
    *,
    current_message: Any = None,
) -> dict[str, Any]:
    """Validate a model-provided clock quote against identified source data.

    This is deliberately a narrow evidence check.  It does not decide whether
    a phrase means a schedule time and it does not search the whole transcript
    for a clock.  The caller supplies a source id, quote, and parsed value;
    prior turns are valid sources when present in ``messages``.
    """

    raw = _evidence_mapping(evidence)
    quote_value = _first_field(raw, _SOURCE_QUOTE_FIELDS)
    quote = _nfkc(quote_value)
    source_id = str(_first_field(raw, _SOURCE_ID_FIELDS) or "").strip()
    declared_value = _first_field(raw, _SOURCE_CLOCK_FIELDS)
    declared_clock = parse_clock_text(declared_value)
    source_rows = _source_messages(messages)
    if current_message is not None and isinstance(current_message, Mapping):
        current_id = _source_id(current_message)
        if current_id and not any(row_id == current_id for row_id, _row in source_rows):
            source_rows = (*source_rows, (current_id, current_message))

    result: dict[str, Any] = {
        "valid": False,
        "status": "missing_source_evidence",
        "sourceMessageId": source_id,
        "quote": quote,
        "parsedClock": declared_clock or "",
    }
    if not quote or not source_id or declared_clock is None:
        if not quote:
            result["status"] = "missing_quote"
        elif not source_id:
            result["status"] = "missing_source_message"
        else:
            result["status"] = "invalid_parsed_clock"
        return result

    source_message = next(
        (message for row_id, message in source_rows if row_id == source_id),
        None,
    )
    if source_message is None:
        result["status"] = "source_message_not_found"
        return result

    source_role = _source_role(source_message)
    source_type = _source_origin(source_message)
    declared_role = _first_field(raw, _SOURCE_ROLE_FIELDS)
    declared_origin = _first_field(raw, _SOURCE_ORIGIN_FIELDS)
    result["sourceRole"] = source_role
    result["sourceType"] = source_type
    if declared_role not in (None, "") and str(declared_role).strip().lower() != source_role:
        result["status"] = "source_role_mismatch"
        return result
    if declared_origin not in (None, ""):
        normalized_declared_origin = _nfkc(declared_origin).casefold().replace("-", "_").replace(" ", "_")
        if source_type and normalized_declared_origin != source_type:
            result["status"] = "source_type_mismatch"
            return result
        source_type = normalized_declared_origin
        result["sourceType"] = source_type
    if source_role in {"tool", "function", "developer"} or source_type in _TOOL_SOURCE_ALIASES:
        result["status"] = "non_conversational_source"
        return result

    source_text = _source_content(source_message)
    matching_indexes = _matching_quote_indexes(source_text, quote)
    if not matching_indexes:
        result["status"] = "quote_not_in_source"
        return result
    if not any(_quote_is_complete_token(source_text, quote, index) for index in matching_indexes):
        result["status"] = "fragment_of_other_token"
        return result

    # The quote is retained for provenance diagnostics.  Structured schedule
    # grounding validates the model's dueAt/timeSpec separately and must not
    # make execution depend on locally parsing this verbatim fragment.
    parsed_quote = parse_clock_text(quote)
    if parsed_quote is None:
        result["status"] = "quote_not_a_complete_clock"
        return result
    if parsed_quote != declared_clock:
        result["status"] = "value_mismatch"
        result["quoteParsedClock"] = parsed_quote
        return result
    result["quoteParsedClock"] = parsed_quote
    result["valid"] = True
    result["status"] = "accepted"
    return result


def validate_schedule_time_evidence(
    evidence: Any,
    messages: Any = (),
    *,
    current_message: Any = None,
) -> dict[str, Any]:
    """Compatibility name used by schedule callers."""

    return validate_time_source_evidence(
        evidence,
        messages,
        current_message=current_message,
    )


def _raise(code: str) -> None:
    raise ValueError(code)


def _blank_optional(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _compact_reason(value: Any) -> str:
    if not isinstance(value, str):
        _raise("todo_time_invalid_reason")
    result = " ".join(value.split()).strip()
    # A reason is an audit hint, not a second free-form prompt.  Rejecting a
    # long value keeps the structured boundary bounded and avoids truncating
    # evidence silently.
    if len(result) > 240:
        _raise("todo_time_reason_too_long")
    return result


def _clock(value: Any) -> str:
    if not isinstance(value, str):
        _raise("todo_time_clock_required")
    result = value.strip()
    match = _CLOCK_RE.fullmatch(result)
    if match is None:
        _raise("todo_time_invalid_clock")
    normalized = f"{int(match.group('hour')):02d}:{match.group('minute')}"
    if match.group("second") is not None:
        normalized += f":{match.group('second')}"
    return normalized


def _meridiem(value: Any) -> str:
    """Validate the explicit marker for an unqualified 12-hour clock.

    Explicit morning/evening wording is already represented by the canonical
    24-hour ``clock`` value.  ``unspecified`` is reserved for a bare clock such
    as ``八点四十`` whose AM/PM meaning must be selected against the frozen
    anchor rather than guessed from prose.
    """

    if not isinstance(value, str) or value.strip().lower() != _UNSPECIFIED_MERIDIEM:
        _raise("todo_time_invalid_meridiem")
    return _UNSPECIFIED_MERIDIEM


def _validate_unspecified_clock(clock: str) -> None:
    hour = int(clock[:2])
    if not 1 <= hour <= 12:
        _raise("todo_time_12h_clock_invalid")


def _weekday(value: Any) -> int:
    """Validate an ISO weekday without accepting bool or numeric strings."""

    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 7:
        _raise("todo_time_invalid_weekday")
    return value


def _calendar_date(value: Any) -> str:
    if not isinstance(value, str) or _DATE_RE.fullmatch(value.strip()) is None:
        _raise("todo_time_invalid_date")
    result = value.strip()
    try:
        parsed = date.fromisoformat(result)
    except ValueError:
        _raise("todo_time_invalid_date")
    return parsed.isoformat()


def _nonnegative_integer(value: Any) -> int:
    # ``bool`` is an ``int`` subclass, but accepting True as dayOffset is an
    # ambiguous model payload and violates the wire contract.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _raise("todo_time_invalid_day_offset")
    return value


def _positive_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _raise("todo_time_invalid_seconds")
    return value


def normalize_time_spec(value: Any) -> dict[str, Any]:
    """Validate and canonicalize one model-produced time specification.

    ``None`` and an empty mapping mean that the command intentionally carries
    no clock specification and normalize to ``{}``.  Every non-empty mapping
    must use one of the six known kinds and only fields allowed by that kind.
    The returned mapping is a fresh plain dictionary suitable for logging and
    for passing to :func:`resolve_time_spec`.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _raise("todo_time_spec_invalid")
    if not value:
        return {}

    unknown = set(value) - {
        "kind", "clock", "meridiem", "weekday", "date", "dayOffset", "seconds", "reason"
    }
    if unknown:
        _raise("todo_time_unknown_field")

    raw_kind = value.get("kind")
    if not isinstance(raw_kind, str) or not raw_kind.strip():
        _raise("todo_time_kind_required")
    kind = raw_kind.strip().lower()
    if kind not in _KINDS:
        _raise("todo_time_invalid_kind")

    fields_by_kind = {
        "next_clock": _COMMON_FIELDS | {"clock", "meridiem"},
        "next_clock_12h": _COMMON_FIELDS | {"clock", "meridiem"},
        "next_weekday": _COMMON_FIELDS | {"weekday", "clock"},
        "calendar": _COMMON_FIELDS | {"clock", "date", "dayOffset"},
        "after": _COMMON_FIELDS | {"seconds"},
        "existing_date": _COMMON_FIELDS | {"clock"},
    }
    unexpected = {
        key for key in set(value) - fields_by_kind[kind]
        if not _blank_optional(value[key])
    }
    if unexpected:
        _raise("todo_time_field_not_allowed")

    normalized: dict[str, Any] = {"kind": kind}
    if kind != "after":
        if "clock" not in value or _blank_optional(value["clock"]):
            _raise("todo_time_clock_required")
        normalized["clock"] = _clock(value["clock"])
        if kind == "next_clock_12h":
            normalized["meridiem"] = _meridiem(
                value.get("meridiem", _UNSPECIFIED_MERIDIEM)
            )
            _validate_unspecified_clock(normalized["clock"])
        elif "meridiem" in value and not _blank_optional(value["meridiem"]):
            normalized["meridiem"] = _meridiem(value["meridiem"])
            _validate_unspecified_clock(normalized["clock"])

    if kind == "next_weekday":
        if "weekday" not in value or _blank_optional(value["weekday"]):
            _raise("todo_time_weekday_required")
        normalized["weekday"] = _weekday(value["weekday"])
    elif kind == "calendar":
        has_date = "date" in value and not _blank_optional(value["date"])
        has_offset = "dayOffset" in value and not _blank_optional(value["dayOffset"])
        if has_date == has_offset:
            _raise("todo_time_calendar_date_ambiguous")
        if has_date:
            normalized["date"] = _calendar_date(value["date"])
        else:
            normalized["dayOffset"] = _nonnegative_integer(value["dayOffset"])
    elif kind == "after":
        if "seconds" not in value or _blank_optional(value["seconds"]):
            _raise("todo_time_seconds_required")
        normalized["seconds"] = _positive_integer(value["seconds"])

    if "reason" in value and not _blank_optional(value["reason"]):
        reason = _compact_reason(value["reason"])
        if reason:
            normalized["reason"] = reason
    return normalized


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _parse_timezone(value: Any) -> tuple[tzinfo, str]:
    if not isinstance(value, str):
        _raise("todo_timezone_invalid")
    text = value.strip()
    if not text:
        _raise("todo_timezone_invalid")
    if text.upper() == "UTC":
        return timezone.utc, "UTC"

    offset = _OFFSET_RE.fullmatch(text)
    if offset is not None:
        hours = int(offset.group("hour"))
        minutes = int(offset.group("minute"))
        if hours > 23 or minutes > 59:
            _raise("todo_timezone_invalid")
        delta = timedelta(hours=hours, minutes=minutes)
        if offset.group("sign") == "-":
            delta = -delta
        label = f"{'-' if delta < timedelta(0) else '+'}{abs(int(delta.total_seconds())) // 3600:02d}:{abs(int(delta.total_seconds())) // 60 % 60:02d}"
        if delta == timedelta(0):
            return timezone.utc, "+00:00"
        return timezone(delta), label

    try:
        zone = ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        _raise("todo_timezone_invalid")
    return zone, str(getattr(zone, "key", text))


def _timezone_label(value: tzinfo) -> str:
    key = getattr(value, "key", None)
    if isinstance(key, str) and key:
        return key
    offset = value.utcoffset(None)
    if offset is None or offset == timedelta(0):
        return "UTC"
    seconds = int(offset.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    absolute = abs(seconds)
    return f"{sign}{absolute // 3600:02d}:{absolute // 60 % 60:02d}"


def _mapping_timezone(mapping: Mapping[str, Any] | None) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for key in ("timezone", "timezoneName", "timezone_name"):
        if key in mapping:
            return mapping[key]
    return None


def _select_timezone(
    timezone_name: Any,
    *,
    anchor: datetime,
    existing: Mapping[str, Any] | None = None,
) -> tuple[tzinfo, str]:
    if timezone_name is None:
        requested = ""
    elif isinstance(timezone_name, str):
        requested = timezone_name.strip()
    else:
        _raise("todo_timezone_invalid")
    if requested:
        return _parse_timezone(requested)

    inherited = _mapping_timezone(existing)
    if inherited not in (None, ""):
        return _parse_timezone(inherited)

    # The caller has already established that anchor is aware.  Keep its
    # actual tzinfo, including a ZoneInfo object or a fixed offset, so date
    # arithmetic remains local to the caller's frozen time snapshot.
    assert anchor.tzinfo is not None
    return anchor.tzinfo, _timezone_label(anchor.tzinfo)


def _mapping_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _parse_datetime(value: Any, *, error: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            _raise(error)
        candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            _raise(error)
    else:
        _raise(error)
    if not _is_aware(parsed):
        _raise(error)
    return parsed


def _parse_existing_local_date(existing: Mapping[str, Any], *, zone: tzinfo) -> date:
    """Read the date that an ``existing_date`` update must preserve.

    Timed tasks carry the date in ``dueAt`` and all-day tasks carry it in
    ``dueDate``.  Both are authoritative for this mode; falling back to the
    frozen anchor would silently turn an update into a new next-occurrence
    calculation.
    """

    due_at = _mapping_value(existing, "dueAt", "due_at")
    if due_at not in (None, ""):
        return _parse_datetime(due_at, error="todo_existing_due_at_invalid").astimezone(zone).date()
    due_date = _mapping_value(existing, "dueDate", "due_date")
    if due_date not in (None, ""):
        try:
            return date.fromisoformat(_calendar_date(due_date))
        except ValueError:
            _raise("todo_existing_due_date_invalid")
    _raise("todo_existing_date_required")


def _clock_time(clock: str) -> time:
    match = _CLOCK_RE.fullmatch(clock)
    assert match is not None
    return time(
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second") or 0),
    )


def _resolve_unspecified_12h_clock(
    clock: str,
    *,
    local_anchor: datetime,
    anchor_utc: datetime,
    zone: tzinfo,
) -> str:
    """Choose the next 08:xx/20:xx occurrence without reading user prose."""

    base_time = _clock_time(clock)
    hours = (0, 12) if base_time.hour == 12 else (base_time.hour, base_time.hour + 12)
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        target_date = local_anchor.date() + timedelta(days=day_offset)
        for hour in hours:
            candidate = _localize(
                datetime.combine(target_date, base_time.replace(hour=hour)), zone
            )
            if candidate.astimezone(timezone.utc) > anchor_utc:
                candidates.append(candidate)
    if not candidates:
        _raise("todo_time_candidate_unavailable")
    return _utc_iso(min(candidates, key=lambda value: value.astimezone(timezone.utc)))


def _localize(naive: datetime, zone: tzinfo) -> datetime:
    """Attach a zone only when round-trip validation proves one instant.

    ``zoneinfo`` intentionally lets callers construct both sides of a DST
    transition.  Round-tripping each fold through UTC distinguishes a missing
    local wall time from an ambiguous repeated one without guessing.
    """

    candidates: list[tuple[datetime, datetime]] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        try:
            candidate_utc = candidate.astimezone(timezone.utc)
            round_trip = candidate_utc.astimezone(zone).replace(tzinfo=None)
        except (OverflowError, ValueError):
            continue
        if round_trip == naive:
            candidates.append((candidate_utc, candidate))

    unique: dict[datetime, datetime] = {}
    for candidate_utc, candidate in candidates:
        unique[candidate_utc] = candidate
    if not unique:
        _raise("todo_time_nonexistent")
    if len(unique) > 1:
        _raise("todo_time_ambiguous")
    return next(iter(unique.values()))


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# 使用调用方给定的带时区时间作为锚点；同一输入可重复得到相同的 UTC 时刻。
# next_clock and next_weekday advance only when the local candidate is equal to
# or before the frozen anchor; explicitly specified calendar dates never move.
def resolve_time_spec(
    spec: Mapping[str, Any],
    *,
    anchor: datetime,
    timezone_name: str = "",
    existing: Mapping[str, Any] | None = None,
) -> str:
    """Resolve a normalized or raw time specification to a UTC ISO instant."""

    if not isinstance(anchor, datetime) or not _is_aware(anchor):
        _raise("todo_anchor_naive")
    if existing is not None and not isinstance(existing, Mapping):
        _raise("todo_existing_invalid")
    normalized = normalize_time_spec(spec)
    if not normalized:
        _raise("todo_time_spec_required")
    zone, _ = _select_timezone(timezone_name, anchor=anchor, existing=existing)
    anchor_utc = anchor.astimezone(timezone.utc)

    if normalized["kind"] == "after":
        return _utc_iso(anchor_utc + timedelta(seconds=normalized["seconds"]))

    local_anchor = anchor.astimezone(zone)
    if normalized["kind"] == "existing_date":
        if existing is None:
            _raise("todo_time_existing_task_required")
        target_date = _parse_existing_local_date(existing, zone=zone)
    elif normalized["kind"] == "next_weekday":
        days_ahead = (normalized["weekday"] - local_anchor.isoweekday()) % 7
        target_date = local_anchor.date() + timedelta(days=days_ahead)
    elif normalized["kind"] == "calendar":
        if "date" in normalized:
            target_date = date.fromisoformat(normalized["date"])
        else:
            target_date = local_anchor.date() + timedelta(days=normalized["dayOffset"])
    else:
        target_date = local_anchor.date()

    if normalized["kind"] == "next_clock_12h" or normalized.get("meridiem") == _UNSPECIFIED_MERIDIEM:
        return _resolve_unspecified_12h_clock(
            normalized["clock"],
            local_anchor=local_anchor,
            anchor_utc=anchor_utc,
            zone=zone,
        )

    local_naive = datetime.combine(target_date, _clock_time(normalized["clock"]))
    candidate = _localize(local_naive, zone)
    if normalized["kind"] in {"next_clock", "next_weekday"} and candidate.astimezone(timezone.utc) <= anchor_utc:
        increment = 7 if normalized["kind"] == "next_weekday" else 1
        next_date = target_date + timedelta(days=increment)
        candidate = _localize(
            datetime.combine(next_date, _clock_time(normalized["clock"])), zone
        )
    return _utc_iso(candidate)


def _clock_text(value: datetime) -> str:
    if value.microsecond:
        return value.strftime("%H:%M:%S.%f")
    if value.second:
        return value.strftime("%H:%M:%S")
    return value.strftime("%H:%M")


def _number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def schedule_time_facts(
    task: Mapping[str, Any],
    *,
    anchor: datetime,
    timezone_name: str = "",
) -> dict[str, Any]:
    """Return deterministic model-facing facts from one saved Todo mapping.

    A non-empty ``snoozedUntil`` is the current effective instant.  Otherwise
    ``dueAt`` is used.  When neither exists, ``dueDate`` is retained as an
    all-day local date and no clock or duration is invented.
    """

    if not isinstance(task, Mapping):
        _raise("todo_task_invalid")
    if not isinstance(anchor, datetime) or not _is_aware(anchor):
        _raise("todo_anchor_naive")
    zone, label = _select_timezone(timezone_name, anchor=anchor, existing=task)
    anchor_utc = anchor.astimezone(timezone.utc)
    anchor_local = anchor.astimezone(zone)

    due_raw = _mapping_value(task, "dueAt", "due_at")
    snooze_raw = _mapping_value(task, "snoozedUntil", "snoozed_until")
    due_date_raw = _mapping_value(task, "dueDate", "due_date")
    due_date = ""
    if due_date_raw not in (None, ""):
        due_date = _calendar_date(due_date_raw)

    effective_raw = snooze_raw if snooze_raw not in (None, "") else due_raw
    facts: dict[str, Any] = {
        "dueAt": "",
        "localDate": "",
        "localClock": "",
        "localIso": "",
        "timezone": label,
        "anchorIso": _utc_iso(anchor_utc),
        "dayOffset": None,
        "secondsUntilDue": None,
        "hoursUntilDue": None,
        "allDay": False,
    }

    if effective_raw not in (None, ""):
        due = _parse_datetime(effective_raw, error="todo_invalid_due_at")
        due_utc = due.astimezone(timezone.utc)
        due_local = due.astimezone(zone)
        seconds = (due_utc - anchor_utc).total_seconds()
        facts.update({
            "dueAt": _utc_iso(due),
            "localDate": due_local.date().isoformat(),
            "localClock": _clock_text(due_local),
            "localIso": due_local.isoformat(),
            "dayOffset": (due_local.date() - anchor_local.date()).days,
            "secondsUntilDue": _number(seconds),
            "hoursUntilDue": seconds / 3600,
            "allDay": False,
        })
        return facts

    # A saved task with no effective instant is all-day, whether or not it has
    # a due date.  ``None`` duration values prevent a model from mistaking an
    # all-day task for something due at the anchor or at local midnight.
    facts["allDay"] = True
    if due_date:
        target_date = date.fromisoformat(due_date)
        facts["localDate"] = due_date
        facts["dayOffset"] = (target_date - anchor_local.date()).days
    return facts
