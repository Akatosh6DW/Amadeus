"""Small, timezone-aware recurrence helpers used by the Todo store.

The Todo database keeps the user's rule and a separate anchor.  Keeping the
anchor outside the rule matters for rules such as ``FREQ=MONTHLY``: the 31st
of the month is meaningful even though the current occurrence may have been
advanced after the process was offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from dateutil.rrule import rrulestr


_ALIASES = {
    "daily": "daily",
    "day": "daily",
    "every day": "daily",
    "weekly": "weekly",
    "week": "weekly",
    "every week": "weekly",
    "weekdays": "weekdays",
    "weekday": "weekdays",
    "workdays": "weekdays",
    "working days": "weekdays",
    "monthly": "monthly",
    "month": "monthly",
    "every month": "monthly",
}

_ALIAS_RRULES = {
    "daily": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "monthly": "FREQ=MONTHLY",
}


def _clean_rule(value: Any) -> str:
    text = str(value or "").strip()
    # A model occasionally emits an RRULE on a separate line after a DTSTART.
    # DTSTART belongs to the persisted anchor, so retain only the RRULE line.
    lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
    if len(lines) > 1:
        lines = [line for line in lines if not line.upper().startswith("DTSTART")]
    text = " ".join(lines).strip()
    if text.upper().startswith("RRULE:"):
        text = text.split(":", 1)[1].strip()
    return text


def normalize_recurrence_rule(value: Any) -> str:
    """Validate and normalize a Todo recurrence rule.

    The four friendly aliases remain aliases in storage so existing clients
    that read ``recurrenceRule == \"daily\"`` keep working.  RFC5545 rules are
    upper-cased and whitespace is removed, while ``RRULE:`` is accepted as a
    convenience prefix.  Invalid rules raise ``ValueError`` before a task is
    written.
    """

    text = _clean_rule(value)
    if not text:
        return ""
    alias = _ALIASES.get(text.casefold())
    if alias:
        return alias
    compact = re.sub(r"\s+", "", text).upper()
    if not compact.startswith("FREQ="):
        raise ValueError("recurrence_rule_must_start_with_freq")
    try:
        # Try an aware DTSTART first so RFC5545's UTC ``UNTIL=...Z`` form is
        # accepted, then fall back to a naive DTSTART for local UNTIL values.
        try:
            rrulestr(compact, dtstart=datetime(2000, 1, 1, tzinfo=timezone.utc))
        except (TypeError, ValueError, OverflowError):
            rrulestr(compact, dtstart=datetime(2000, 1, 1))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_recurrence_rule") from exc
    return compact


def recurrence_rrule(value: Any) -> str:
    """Return a dateutil rule string for an alias or an RFC5545 rule."""

    normalized = normalize_recurrence_rule(value)
    return _ALIAS_RRULES.get(normalized, normalized)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def next_occurrence(
    rule: Any,
    *,
    anchor: datetime,
    after: datetime,
) -> datetime | None:
    """Return the first occurrence strictly after ``after``.

    ``anchor`` is supplied separately from ``rule`` and is therefore stable
    when a task is advanced across a restart or when several occurrences were
    missed while the process was offline.  The first result after ``after`` is
    intentionally returned, coalescing missed occurrences into one run.
    """

    normalized = normalize_recurrence_rule(rule)
    if not normalized:
        return None
    anchor = _aware(anchor)
    after = _aware(after)
    rule_text = recurrence_rrule(normalized)
    try:
        schedule = rrulestr(rule_text, dtstart=anchor)
        result = schedule.after(after, inc=False)
    except (TypeError, ValueError, OverflowError):
        # RFC5545 UNTIL values without a timezone are common in hand-written
        # rules.  Treat them as local/naive values when an aware anchor makes
        # dateutil reject the mixed form, then restore the anchor's timezone.
        naive_anchor = anchor.replace(tzinfo=None)
        naive_after = after.astimezone(anchor.tzinfo).replace(tzinfo=None)
        schedule = rrulestr(rule_text, dtstart=naive_anchor)
        result = schedule.after(naive_after, inc=False)
        if result is not None:
            result = result.replace(tzinfo=anchor.tzinfo)
    return result


__all__ = [
    "next_occurrence",
    "normalize_recurrence_rule",
    "recurrence_rrule",
]
