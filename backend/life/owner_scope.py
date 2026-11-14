"""Authenticated owner context for shared daily-life projections.

The scope is installed by the verified QQ owner bridge around the existing
desktop atomic request.  It is deliberately independent from the request
body and from the existing ``owner_input_scope`` trust context.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_OWNER_LIFE_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar(
    "amadeus_owner_life_context", default=None
)


def owner_scope_key(account: str, owner: str) -> str:
    """Return the stable shared-life key for one authenticated QQ owner."""

    return f"qq-owner:{str(account)}:{str(owner)}"


@contextmanager
def owner_life_scope(*, account: str, owner: str, kind: str, target: str,
                     message_id: str, request_id: str) -> Iterator[None]:
    """Expose one verified owner event to code running in the atomic pipeline.

    A fresh dictionary is stored for each request and the previous context is
    restored even when the atomic handler raises.  The camel-case field names
    match the host delivery envelope consumed by the life projection.
    """

    context = {
        "account": str(account),
        "owner": str(owner),
        "kind": str(kind),
        "target": str(target),
        "messageId": str(message_id),
        "requestId": str(request_id),
    }
    token = _OWNER_LIFE_CONTEXT.set(context)
    try:
        yield
    finally:
        _OWNER_LIFE_CONTEXT.reset(token)


def current_owner_life_context() -> dict[str, str] | None:
    """Return a copy of the verified owner context for the current task."""

    context = _OWNER_LIFE_CONTEXT.get()
    return dict(context) if context is not None else None


__all__ = [
    "current_owner_life_context",
    "owner_life_scope",
    "owner_scope_key",
]
