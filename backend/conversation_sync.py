"""Small in-process revision bus for local UI state changes.

The QQ owner bridge and the desktop UI share one local backend process.  A
bounded revision map lets the UI notice a completed remote owner turn without
polling the full conversation history.  The persisted conversation remains the
source of truth; this module only carries invalidation metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
from typing import Any


@dataclass(frozen=True)
class _Change:
    revision: int
    source: str


class ConversationSync:
    """Thread-safe, bounded invalidation state for one backend process."""

    def __init__(self, *, max_conversations: int = 128, max_requests: int = 512) -> None:
        self._lock = threading.RLock()
        self._epoch = secrets.token_hex(12)
        self._revision = 0
        self._changes: dict[str, _Change] = {}
        self._request_revisions: dict[str, int] = {}
        self._max_conversations = max(8, int(max_conversations))
        self._max_requests = max(32, int(max_requests))

    def publish(self, conversation_id: Any, *, request_id: Any = "", source: str = "") -> dict[str, Any]:
        """Record one completed local turn and return the new snapshot.

        An atomic owner request may be observed twice when the normal request
        deduplication path returns a cached result.  Request IDs therefore act
        as an idempotency key for the invalidation itself.
        """
        conversation = str(conversation_id or "").strip()[:180]
        request = str(request_id or "").strip()[:180]
        if not conversation:
            return self.snapshot()
        with self._lock:
            if request and request in self._request_revisions:
                return self._snapshot_locked()
            self._revision += 1
            self._changes[conversation] = _Change(
                revision=self._revision,
                source=str(source or "local")[:80],
            )
            if request:
                self._request_revisions[request] = self._revision
                while len(self._request_revisions) > self._max_requests:
                    self._request_revisions.pop(next(iter(self._request_revisions)))
            while len(self._changes) > self._max_conversations:
                self._changes.pop(next(iter(self._changes)))
            return self._snapshot_locked()

    def snapshot(self, conversation_id: Any | None = None) -> dict[str, Any]:
        with self._lock:
            conversation = str(conversation_id or "").strip()[:180]
            if conversation:
                change = self._changes.get(conversation)
                return {
                    "ok": True,
                    "epoch": self._epoch,
                    "revision": self._revision,
                    "conversationId": conversation,
                    "change": ({"revision": change.revision, "source": change.source}
                                if change is not None else None),
                }
            return self._snapshot_locked()

    def reset(self) -> None:
        """Clear process state for isolated tests; never used by production UI."""
        with self._lock:
            self._revision = 0
            self._changes.clear()
            self._request_revisions.clear()

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "ok": True,
            "epoch": self._epoch,
            "revision": self._revision,
            "conversations": {
                conversation: {"revision": change.revision, "source": change.source}
                for conversation, change in self._changes.items()
            },
        }


LOCAL_CONVERSATION_SYNC = ConversationSync()


def publish_conversation_sync(conversation_id: Any, *, request_id: Any = "", source: str = "") -> dict[str, Any]:
    return LOCAL_CONVERSATION_SYNC.publish(conversation_id, request_id=request_id, source=source)


def conversation_sync_snapshot(conversation_id: Any | None = None) -> dict[str, Any]:
    return LOCAL_CONVERSATION_SYNC.snapshot(conversation_id)
