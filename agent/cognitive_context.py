"""Pure helpers for applying staged cognitive context controls.

The cognitive state is an overlay for one turn.  This module deliberately
does not read storage, inspect user prose, or import the server.  Callers pass
the frozen state and the candidate objects they already trust.  That keeps
scope decisions explicit and makes it possible to use the same filtering
rules for memory ranking and conversation prompt construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_RECALL_SCOPE = "conversation"
RECALL_SCOPES = frozenset({"conversation", "archive", "long_term"})
MAX_RECALL_QUERY_LENGTH = 500
MAX_CONTEXT_CONTROL_IDS = 128

# These are wire-format aliases retained for clients that already persisted a
# more descriptive value.  They are exact values, never inferred from the
# current user message.  Canonical values are what gets written back to state.
RECALL_SCOPE_ALIASES = {
    "current_conversation": "conversation",
    "session": "conversation",
    "memory": "long_term",
    "user": "long_term",
    "user_memory": "long_term",
}


def normalize_recall_scope(value: Any, *, default: str | None = DEFAULT_RECALL_SCOPE) -> str:
    """Validate one explicit scope and return its canonical wire value.

    ``default=None`` is useful at the patch validation boundary: malformed or
    unknown values must be rejected there rather than silently widening a
    retrieval scope.  The non-strict projection used by prompt code falls
    back to the safe conversation scope for stale/untrusted overlays.
    """

    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError("recall scope must be a non-empty string")
    raw = value.strip().casefold()
    canonical = RECALL_SCOPE_ALIASES.get(raw, raw)
    if canonical not in RECALL_SCOPES:
        raise ValueError(f"unsupported recall scope: {value.strip()}")
    return canonical


def _state_context(value: Any) -> Mapping[str, Any]:
    """Extract ``state.context`` from a state, overlay, or direct context."""

    if not isinstance(value, Mapping):
        return {}
    candidate: Any = value
    nested_state = candidate.get("state")
    if isinstance(nested_state, Mapping):
        candidate = nested_state
    nested_context = candidate.get("context")
    if isinstance(nested_context, Mapping):
        return nested_context
    # This branch makes the helper convenient for tests and for callers that
    # have already projected the state to its context object.
    if any(key in candidate for key in ("preferredIds", "suppressedIds", "recallQuery", "recallScope")):
        return candidate
    return {}


def _normalise_ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        ident = str(item or "").strip()
        if not ident or ident in seen:
            continue
        seen.add(ident)
        result.append(ident)
        if len(result) >= MAX_CONTEXT_CONTROL_IDS:
            break
    return result


def effective_context_controls(value: Any) -> dict[str, Any]:
    """Project only the usable context controls from a staged state.

    Prompt paths may receive a legacy or partially persisted overlay.  Unknown
    scope values therefore fail closed to ``conversation`` here; the mutation
    boundary uses :func:`normalize_recall_scope` strictly and rejects them.
    """

    context = _state_context(value)
    raw_query = context.get("recallQuery")
    query = raw_query.strip()[:MAX_RECALL_QUERY_LENGTH] if isinstance(raw_query, str) else ""
    try:
        scope = normalize_recall_scope(context.get("recallScope"), default=DEFAULT_RECALL_SCOPE)
    except ValueError:
        scope = DEFAULT_RECALL_SCOPE
    return {
        "preferredIds": _normalise_ids(context.get("preferredIds")),
        "suppressedIds": _normalise_ids(context.get("suppressedIds")),
        "recallQuery": query,
        "recallScope": scope,
    }


def effective_retrieval_query(user_message: Any, value: Any = None) -> str:
    """Return the explicit rewrite query, or the ordinary current query."""

    controls = effective_context_controls(value)
    query = str(controls.get("recallQuery") or "").strip()
    return query or str(user_message or "").strip()


def effective_memory_retrieval_query(
    user_message: Any,
    messages: Sequence[Any] = (),
    value: Any = None,
    context_state: Any = None,
) -> str:
    """Build the memory query under the explicitly selected recall scope.

    The default conversation scope keeps the existing current-plus-recent
    user query.  ``archive`` adds user text from the already frozen,
    same-conversation context snapshot; ``long_term`` uses only the current
    request so old conversational wording cannot steer user-memory ranking.
    An explicit rewrite always replaces all derived text.
    """

    controls = effective_context_controls(value)
    explicit = str(controls.get("recallQuery") or "").strip()
    if explicit:
        return explicit
    current = str(user_message or "").strip()
    if controls["recallScope"] == "long_term":
        return current
    recent_user = [
        str(item.get("content") or "").strip()
        for item in list(messages or [])[-8:]
        if isinstance(item, Mapping)
        and str(item.get("role") or "") == "user"
        and str(item.get("content") or "").strip()
    ]
    if controls["recallScope"] == "archive":
        context = context_state if isinstance(context_state, Mapping) else {}
        active_id = str(context.get("conversationId") or "").strip()
        active_epoch = context.get("conversationEpoch")
        archived_user = [
            str(item.get("user") or "").strip()
            for item in context.get("recentTurns", [])
            if isinstance(item, Mapping)
            and str(item.get("user") or "").strip()
            and _turn_belongs_to_context(item, active_id, active_epoch)
            and not candidate_is_suppressed(item, value)
        ]
        recent_user = archived_user + recent_user
    return "\n".join([current, *recent_user]).strip()


def _string_value(value: Any) -> str:
    return str(value or "").strip()


def _add_id(result: set[str], value: Any) -> None:
    ident = _string_value(value)
    if ident:
        result.add(ident)


def _add_values(result: set[str], value: Any) -> None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _add_id(result, item)
    else:
        _add_id(result, value)


def _turn_belongs_to_context(
    candidate: Any,
    active_conversation_id: str = "",
    active_epoch: Any = None,
) -> bool:
    """Accept only the active identity/generation when it is available."""

    if not isinstance(candidate, Mapping):
        return False
    active_id = str(active_conversation_id or "").strip()
    if active_id:
        candidate_id = str(candidate.get("conversationId") or "").strip()
        if candidate_id and candidate_id != active_id:
            return False
    if active_epoch is not None:
        try:
            expected = max(0, int(active_epoch))
        except (TypeError, ValueError):
            expected = 0
        candidate_epoch_value = candidate.get("conversationEpoch", candidate.get("contextEpoch"))
        if candidate_epoch_value is not None:
            try:
                if max(0, int(candidate_epoch_value)) != expected:
                    return False
            except (TypeError, ValueError):
                return False
        elif expected > 0 and not str(candidate.get("conversationId") or "").strip():
            return False
    return True


def candidate_context_ids(candidate: Any) -> frozenset[str]:
    """Collect exact persisted IDs used to bind a candidate to context.

    The function is intentionally schema-tolerant for ``dict`` snapshots and
    ``MemoryRecord`` objects, but it never treats candidate text as an ID.  A
    preference/suppression can therefore affect an item only when the staged
    operation named one of that item's persisted IDs.
    """

    result: set[str] = set()
    mapping = candidate if isinstance(candidate, Mapping) else None
    fields = (
        "id",
        "contextId",
        "context_id",
        "memoryId",
        "memory_id",
        "turnId",
        "turn_id",
        "messageId",
        "message_id",
        "userMessageId",
        "user_message_id",
        "assistantMessageId",
        "assistant_message_id",
        "sourceTurnId",
        "source_turn_id",
        "referenceId",
        "reference_id",
        "claimId",
        "claim_id",
        "originRef",
        "origin_ref",
    )
    collection_fields = (
        "sourceTurnIds",
        "source_turn_ids",
        "sourceMessageIds",
        "source_message_ids",
        "evidenceIds",
        "evidence_ids",
    )
    for field in fields:
        value = mapping.get(field) if mapping is not None else getattr(candidate, field, None)
        _add_id(result, value)
    for field in collection_fields:
        value = mapping.get(field) if mapping is not None else getattr(candidate, field, None)
        _add_values(result, value)
    return frozenset(result)


def candidate_is_suppressed(candidate: Any, value: Any = None) -> bool:
    controls = effective_context_controls(value)
    return bool(candidate_context_ids(candidate) & set(controls["suppressedIds"]))


def candidate_is_preferred(candidate: Any, value: Any = None) -> bool:
    controls = effective_context_controls(value)
    return bool(candidate_context_ids(candidate) & set(controls["preferredIds"]))


def prioritize_context_candidates(
    candidates: Sequence[Any],
    value: Any = None,
    *,
    limit: int | None = None,
) -> list[Any]:
    """Drop suppressed items and reserve output capacity for preferred ones."""

    controls = effective_context_controls(value)
    suppressed = set(controls["suppressedIds"])
    preferred = set(controls["preferredIds"])
    allowed = [
        item for item in candidates
        if not (candidate_context_ids(item) & suppressed)
    ]
    preferred_items = [
        item for item in allowed
        if candidate_context_ids(item) & preferred
    ]
    ordinary_items = [
        item for item in allowed
        if not (candidate_context_ids(item) & preferred)
    ]
    if limit is None:
        return allowed
    capacity = max(0, int(limit))
    preferred_items = preferred_items[-capacity:] if capacity else []
    remaining = capacity - len(preferred_items)
    selected = preferred_items + (ordinary_items[-remaining:] if remaining else [])
    # Candidate order is chronological. Reserve capacity without changing
    # chronology or replacing the latest ordinary turns with the oldest ones.
    identities = {id(item) for item in selected}
    return [item for item in allowed if id(item) in identities]


def scope_allows_archive(value: Any = None) -> bool:
    """Whether the explicit overlay permits durable same-conversation turns."""

    return effective_context_controls(value)["recallScope"] == "archive"


def scope_is_long_term(value: Any = None) -> bool:
    """Whether the explicit overlay selects user-scoped long-term recall."""

    return effective_context_controls(value)["recallScope"] == "long_term"
