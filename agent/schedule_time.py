"""Pure, typed time semantics for Todo schedule commands.

The model chooses the semantic relation (the next occurrence, a calendar
date, a duration, or the date already attached to a task).  This module owns
the small deterministic boundary that turns that choice into a UTC instant
and produces facts from the persisted task.  It deliberately has no access to
files, databases, network state, or the process clock.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


__all__ = [
    "normalize_time_spec",
    "resolve_time_spec",
    "schedule_time_facts",
]


_KINDS = frozenset({"next_clock", "calendar", "after", "existing_date"})
_COMMON_FIELDS = frozenset({"kind", "reason"})
_CLOCK_RE = re.compile(r"^(?P<hour>[0-9]|[01]\d|2[0-3]):(?P<minute>[0-5]\d)(?::(?P<second>[0-5]\d))?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<hour>\d{2})(?::?(?P<minute>\d{2}))$")


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
    must use one of the four known kinds and only fields allowed by that kind.
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
        "kind", "clock", "date", "dayOffset", "seconds", "reason"
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
        "next_clock": _COMMON_FIELDS | {"clock"},
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

    if kind == "calendar":
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


def _parse_existing_due_at(existing: Mapping[str, Any]) -> datetime:
    value = _mapping_value(existing, "dueAt", "due_at")
    if value in (None, ""):
        _raise("todo_existing_due_at_required")
    return _parse_datetime(value, error="todo_existing_due_at_invalid")


def _clock_time(clock: str) -> time:
    match = _CLOCK_RE.fullmatch(clock)
    assert match is not None
    return time(
        int(match.group("hour")),
        int(match.group("minute")),
        int(match.group("second") or 0),
    )


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
        target_date = _parse_existing_due_at(existing).astimezone(zone).date()
    elif normalized["kind"] == "calendar":
        if "date" in normalized:
            target_date = date.fromisoformat(normalized["date"])
        else:
            target_date = local_anchor.date() + timedelta(days=normalized["dayOffset"])
    else:
        target_date = local_anchor.date()

    local_naive = datetime.combine(target_date, _clock_time(normalized["clock"]))
    candidate = _localize(local_naive, zone)
    if normalized["kind"] == "next_clock" and candidate.astimezone(timezone.utc) <= anchor_utc:
        next_date = target_date + timedelta(days=1)
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
