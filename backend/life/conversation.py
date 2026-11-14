"""Bridge verified conversation turns into bounded daily-life continuity.

This module records conversation material as a continuity event.  It never
turns the stored user or assistant text into a new instruction, and it never
reads history or performs a delivery operation.  QQ delivery is advanced only
from an injected, host-owned receipt reader.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from backend.life.continuity import ContinuityStore, MAX_SUMMARY_CHARS
from backend.life.owner_scope import owner_scope_key


_EVENT_KIND = "daily_conversation"
_DESKTOP_SCOPE = "desktop"
_QQ_PENDING_STATES = frozenset(("prepared", "accepted", "unknown"))
_QQ_STATES = frozenset(("prepared", "accepted", "sent", "unknown", "failed"))
_QQ_RECORDED_STATES = frozenset(("prepared", "accepted", "unknown", "failed", "sent"))
_MAX_USER_MESSAGE = 1600
_MAX_ASSISTANT_MESSAGE = 2200


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bounded(value: str, limit: int) -> str:
    value = _text(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _message_content(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("content", "text", "message", "reply"):
            content = _message_content(value.get(key))
            if content:
                return content
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_message_content(item) for item in value]
        return "\n".join(part for part in parts if part)
    return _text(value)


def _turn_message(turn: Mapping[str, Any], role: str) -> str:
    direct_keys = {
        "user": ("user", "userMessage", "userText", "user_text"),
        "assistant": ("assistant", "assistantReply", "assistantText", "assistantMessage",
                      "assistant_reply", "reply"),
    }[role]
    for key in direct_keys:
        content = _message_content(turn.get(key))
        if content:
            return content
    messages = turn.get("messages")
    if isinstance(messages, (list, tuple)):
        matches = []
        for item in messages:
            if not isinstance(item, Mapping):
                continue
            item_role = _text(item.get("role")).lower()
            if item_role == role:
                content = _message_content(item.get("content") or item.get("text"))
                if content:
                    matches.append(content)
        if matches:
            return "\n".join(matches)
    if _text(turn.get("role")).lower() == role:
        return _message_content(turn.get("content") or turn.get("text"))
    return ""


def _identifier(turn: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = turn.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
    return ""


def _owner_values(selected_owner: tuple[str, str]) -> tuple[str, str] | None:
    try:
        account, owner = selected_owner
    except (TypeError, ValueError):
        return None
    account = _text(str(account))
    owner = _text(str(owner))
    return (account, owner) if account and owner else None


def _verified_channel(
    owner_context: Mapping[str, Any] | None,
    selected_owner: tuple[str, str],
) -> tuple[str, str, str, str, str, str] | None:
    """Return channel/scope/identity from an authenticated host context."""

    # Desktop turns are intentionally independent from QQ binding.  An
    # unconfigured installation may pass an empty selected-owner tuple while
    # still having a valid local conversation.
    if owner_context is None:
        return ("desktop", _DESKTOP_SCOPE, "", "", "", "")
    selected = _owner_values(selected_owner)
    if selected is None:
        return None
    selected_account, selected_owner_id = selected
    if not isinstance(owner_context, Mapping):
        return None
    account = _text(str(owner_context.get("account") or ""))
    owner = _text(str(owner_context.get("owner") or ""))
    kind = _text(str(owner_context.get("kind") or ""))
    target = _text(str(owner_context.get("target") or ""))
    if (account != selected_account or owner != selected_owner_id
            or kind != "private" or target != owner):
        return None
    return (
        "qq",
        owner_scope_key(account, owner),
        account,
        owner,
        _identifier(owner_context, "requestId"),
        _identifier(owner_context, "messageId"),
    )


def _qq_status(turn: Mapping[str, Any], assistant: str) -> str:
    for key in ("assistantState", "assistantStatus", "responseState"):
        state = _text(turn.get(key)).lower()
        if state in ("prepared", "accepted"):
            return state
    delivery = turn.get("delivery")
    if isinstance(delivery, Mapping):
        state = _text(delivery.get("state")).lower()
        if state in ("prepared", "accepted"):
            return state
    # A generated answer is accepted by the desktop owner endpoint, while a
    # missing answer remains a prepared turn.  ``ok`` never implies delivery.
    return "accepted" if assistant else "prepared"


def _summary(user: str, assistant: str) -> str:
    parts = []
    if user:
        parts.append("user material:\n" + _bounded(user, _MAX_USER_MESSAGE))
    if assistant:
        parts.append("assistant material:\n" + _bounded(assistant, _MAX_ASSISTANT_MESSAGE))
    return "\n".join(parts)[:MAX_SUMMARY_CHARS]


def _source_ref(*, channel: str, account: str, owner: str, request_id: str,
                message_id: str, status: str) -> dict[str, str]:
    reference: dict[str, str] = {
        "sourceType": "daily_conversation",
        "channel": channel,
        "requestId": request_id,
        "originalMessageId": message_id,
        # Keep the original gateway/user message ID under its short name too;
        # delivery IDs are stored separately by the receipt refresh below.
        "messageId": message_id,
        "deliveryState": status,
    }
    if channel == "qq":
        reference.update({
            "account": account,
            "owner": owner,
            "kind": "private",
            "target": owner,
        })
    return reference


def record_daily_turn(
    store: ContinuityStore,
    turn: Mapping[str, Any],
    *,
    selected_owner: tuple[str, str],
    owner_context: dict[str, Any] | None,
    delegated: bool,
    now: datetime,
) -> dict[str, Any] | None:
    """Record one local or authenticated-owner daily conversation turn.

    ``owner_context`` is the host-authenticated scope, never a nickname or
    message-body claim.  Delegated turns, group contexts, and other owners are
    ignored before any store operation.  The event ID is deterministic so a
    retry updates one event instead of inserting another one.
    """

    if delegated or not isinstance(turn, Mapping):
        return None
    channel_data = _verified_channel(owner_context, selected_owner)
    if channel_data is None:
        return None
    channel, scope, account, owner, context_request_id, context_message_id = channel_data
    user = _turn_message(turn, "user")
    assistant = _turn_message(turn, "assistant")
    summary = _summary(user, assistant)
    if not summary:
        return None

    request_id = context_request_id or _identifier(turn, "requestId", "request_id", "turnId", "turn_id")
    if not request_id:
        return None
    message_id = context_message_id or _identifier(
        turn, "messageId", "message_id", "userMessageId", "user_message_id", "originalMessageId"
    )
    status = "delivered" if channel == "desktop" else _qq_status(turn, assistant)
    event_id = "conversation:" + request_id
    source_refs = [_source_ref(
        channel=channel, account=account, owner=owner, request_id=request_id,
        message_id=message_id, status=status,
    )]
    if channel == "qq":
        # A retry of one owner request must not turn a previously confirmed
        # delivery back into a merely accepted/generated answer.  The store
        # reads the exact stable event, including one outside the prompt window.
        existing = store.event(event_id, scope=scope)
        if isinstance(existing, Mapping) and existing.get("status") in _QQ_RECORDED_STATES:
            status = str(existing["status"])
            existing_refs = existing.get("sourceRefs")
            if isinstance(existing_refs, list):
                source_refs = existing_refs
    return store.record_event(
        event_id,
        scope=scope,
        channel=channel,
        kind=_EVENT_KIND,
        summary=summary,
        status=status,
        source_refs=source_refs,
        now=now,
    )


def _source_ref_for_owner(event: Mapping[str, Any]) -> tuple[int, dict[str, Any]] | None:
    refs = event.get("sourceRefs")
    if not isinstance(refs, list):
        return None
    for index, item in enumerate(refs):
        if not isinstance(item, Mapping):
            continue
        account = _text(str(item.get("account") or ""))
        owner = _text(str(item.get("owner") or ""))
        request_id = _text(str(item.get("requestId") or ""))
        if (item.get("channel") == "qq" and item.get("kind") == "private"
                and item.get("target") == owner and account and owner and request_id):
            return index, dict(item)
    return None


def _receipt_message_ids(receipt: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[Any] = []
    raw_many = receipt.get("messageIds")
    if isinstance(raw_many, (list, tuple)):
        values.extend(raw_many)
    raw_one = receipt.get("messageId")
    if raw_one not in (None, ""):
        values.append(raw_one)
    result = []
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _delivery_ids(ref: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[Any] = []
    raw_many = ref.get("deliveryMessageIds")
    if isinstance(raw_many, (list, tuple)):
        values.extend(raw_many)
    raw_one = ref.get("deliveryMessageId")
    if raw_one not in (None, ""):
        values.append(raw_one)
    result = []
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _with_receipt(refs: list[Any], index: int, ref: dict[str, Any],
                  state: str, message_ids: tuple[str, ...]) -> list[Any]:
    updated = [dict(item) if isinstance(item, Mapping) else item for item in refs]
    ref["deliveryState"] = state
    ref.pop("deliveryMessageId", None)
    ref.pop("deliveryMessageIds", None)
    if len(message_ids) == 1:
        ref["deliveryMessageId"] = message_ids[0]
    elif message_ids:
        ref["deliveryMessageIds"] = list(message_ids)
    updated[index] = ref
    return updated


def refresh_owner_turn_receipts(
    store: ContinuityStore,
    snapshot: Mapping[str, Any],
    *,
    read_receipt: Callable[..., dict[str, Any]],
    now: datetime,
) -> bool:
    """Refresh pending QQ conversation events from an injected receipt reader."""

    experiences = snapshot.get("experiences") if isinstance(snapshot, Mapping) else None
    if not isinstance(experiences, list):
        return False
    changed = False
    seen: set[str] = set()
    for raw_event in experiences:
        if not isinstance(raw_event, Mapping):
            continue
        event_id = _text(str(raw_event.get("id") or ""))
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        if (raw_event.get("kind") != _EVENT_KIND
                or raw_event.get("channel") != "qq"
                or raw_event.get("status") not in _QQ_PENDING_STATES):
            continue
        source_data = _source_ref_for_owner(raw_event)
        if source_data is None:
            continue
        source_index, source_ref = source_data
        account = _text(str(source_ref.get("account") or ""))
        owner = _text(str(source_ref.get("owner") or ""))
        request_id = _text(str(source_ref.get("requestId") or ""))
        try:
            receipt = read_receipt(account=account, owner=owner, request_id=request_id)
        except Exception:
            continue
        if not isinstance(receipt, Mapping):
            continue
        state = _text(receipt.get("state")).lower()
        if state not in _QQ_STATES:
            continue
        message_ids = _receipt_message_ids(receipt)
        # A bare ``ok`` or a sent state without platform IDs is not evidence of
        # delivery.  Keep the accepted/prepared event pending for a later read.
        if state == "sent" and not message_ids:
            continue
        if (state == raw_event.get("status")
                and source_ref.get("deliveryState") == state
                and _delivery_ids(source_ref) == message_ids):
            continue
        refs = raw_event.get("sourceRefs")
        if not isinstance(refs, list):
            continue
        updated_refs = _with_receipt(refs, source_index, source_ref, state, message_ids)
        store.record_event(
            event_id,
            scope=_text(str(raw_event.get("scope") or "")),
            channel="qq",
            kind=_EVENT_KIND,
            summary=_text(str(raw_event.get("summary") or "")),
            status=state,
            source_refs=updated_refs,
            now=now,
        )
        changed = True
    return changed


__all__ = ["record_daily_turn", "refresh_owner_turn_receipts"]
