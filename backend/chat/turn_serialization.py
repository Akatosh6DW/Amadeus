"""按会话串行化完整聊天轮次。

模型和工具调用发生在会话快照与认知提交之间；只锁最终写入仍会让两个
请求基于同一个认知 revision 并发生成。这里提供一个独立于
``CONVERSATION_LOCK`` 的按 ``conversationId`` FIFO scope，调用方可以在
快照读取前进入，并在提交后退出。等待期间轮次可被用户取消，异常路径由
context manager 统一释放 owner。
"""

from __future__ import annotations

from collections import deque
from collections.abc import MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
from typing import Any, Callable, Iterator, Mapping

from backend.runtime.requests import RequestValidationError


_DEFAULT_CONVERSATION_KEY = "__conversation_default__"


@dataclass
class _Waiter:
    request_id: str
    token: object = field(default_factory=object)


@dataclass
class _ConversationQueue:
    owner_request_id: str = ""
    owner_thread_id: int | None = None
    depth: int = 0
    waiters: deque[_Waiter] = field(default_factory=deque)


class ConversationTurnSerializer:
    """让同一会话一次只运行一个完整 turn，且不同会话互不等待。"""

    def __init__(self, *, poll_interval: float = 0.05) -> None:
        self._condition = threading.Condition()
        self._queues: dict[str, _ConversationQueue] = {}
        self._poll_interval = max(0.01, float(poll_interval))

    @staticmethod
    def conversation_key(
        body: Mapping[str, Any] | None,
        *,
        current_conversation_id: Callable[[], Any],
    ) -> str:
        """Resolve the key before the first snapshot read.

        A supplied identity is preferred for isolated QQ sessions. Nested
        recovery calls already carry the server-owned snapshot, so that
        snapshot identity is accepted before falling back to the active local
        context.
        """

        candidate: Any = body.get("conversationId") if isinstance(body, Mapping) else None
        if not str(candidate or "").strip() and isinstance(body, Mapping):
            snapshot = body.get("_turnContextSnapshot")
            if isinstance(snapshot, Mapping):
                candidate = snapshot.get("conversationId")
        if not str(candidate or "").strip():
            candidate = current_conversation_id()
        value = str(candidate or "").strip()
        return value or _DEFAULT_CONVERSATION_KEY

    @contextmanager
    def scope(
        self,
        body: Mapping[str, Any] | None,
        *,
        request_id: str,
        current_conversation_id: Callable[[], Any],
        cancelled: Callable[[], bool],
        on_wait: Callable[[], None] | None = None,
        cancel_error: Callable[[], BaseException] | None = None,
    ) -> Iterator[str]:
        """Acquire a FIFO lease and release it on every exit path."""

        key = self.conversation_key(
            body,
            current_conversation_id=current_conversation_id,
        )
        if (
            key != _DEFAULT_CONVERSATION_KEY
            and isinstance(body, MutableMapping)
            and not str(body.get("conversationId") or "").strip()
        ):
            # Pin the identity selected before the first snapshot read.  If
            # the active desktop conversation changes while this turn waits,
            # the snapshot validator must still see the original identity.
            body["conversationId"] = key
        # Production requests always carry an ID.  Keep anonymous adapters
        # releasable as well; an empty owner marker means "free" internally.
        owner_request_id = str(request_id or "").strip() or (
            f"__anonymous_{threading.get_ident()}"
        )
        owner_thread_id = threading.get_ident()
        self._acquire(
            key,
            owner_request_id=owner_request_id,
            owner_thread_id=owner_thread_id,
            cancelled=cancelled,
            on_wait=on_wait,
            cancel_error=cancel_error,
        )
        try:
            yield key
        finally:
            self._release(
                key,
                owner_request_id=owner_request_id,
                owner_thread_id=owner_thread_id,
            )

    def _acquire(
        self,
        key: str,
        *,
        owner_request_id: str,
        owner_thread_id: int,
        cancelled: Callable[[], bool],
        on_wait: Callable[[], None] | None,
        cancel_error: Callable[[], BaseException] | None,
    ) -> None:
        waiter: _Waiter | None = None
        queued = False
        with self._condition:
            queue = self._queues.setdefault(key, _ConversationQueue())
            # Recovery and escalation can re-enter the full pipeline on the
            # same thread/request. Keep that turn inside its original lease.
            if (
                queue.owner_request_id == owner_request_id
                and queue.owner_thread_id == owner_thread_id
                and owner_request_id
            ):
                queue.depth += 1
                return
            waiter = _Waiter(owner_request_id)
            queue.waiters.append(waiter)
            queued = queue.owner_request_id != "" or queue.waiters[0] is not waiter

        if queued and on_wait is not None:
            try:
                on_wait()
            except Exception:
                # Progress reporting must never break turn serialization.
                pass

        assert waiter is not None
        while True:
            with self._condition:
                # A queued waiter keeps its queue alive until it either owns
                # the turn or removes itself, so a missing queue is an
                # invariant violation rather than a recoverable state.
                queue = self._queues[key]
                is_next = bool(queue.waiters) and queue.waiters[0] is waiter
                if cancelled():
                    self._remove_waiter_locked(key, queue, waiter)
                    raise self._cancel_exception(cancel_error)
                if queue.owner_request_id == "" and is_next:
                    queue.waiters.popleft()
                    queue.owner_request_id = owner_request_id
                    queue.owner_thread_id = owner_thread_id
                    queue.depth = 1
                    self._condition.notify_all()
                    return
                self._condition.wait(timeout=self._poll_interval)

    def _release(
        self,
        key: str,
        *,
        owner_request_id: str,
        owner_thread_id: int,
    ) -> None:
        with self._condition:
            queue = self._queues.get(key)
            if (
                queue is None
                or queue.owner_request_id != owner_request_id
                or queue.owner_thread_id != owner_thread_id
            ):
                return
            queue.depth = max(0, queue.depth - 1)
            if queue.depth:
                return
            queue.owner_request_id = ""
            queue.owner_thread_id = None
            if not queue.waiters:
                self._queues.pop(key, None)
            self._condition.notify_all()

    def _remove_waiter_locked(
        self,
        key: str,
        queue: _ConversationQueue,
        waiter: _Waiter,
    ) -> None:
        try:
            queue.waiters.remove(waiter)
        except ValueError:
            pass
        if not queue.owner_request_id and not queue.waiters:
            self._queues.pop(key, None)
        self._condition.notify_all()

    @staticmethod
    def _cancel_exception(
        factory: Callable[[], BaseException] | None,
    ) -> BaseException:
        if factory is not None:
            error = factory()
            if isinstance(error, BaseException):
                return error
        return RequestValidationError(
            "request_cancelled",
            "这轮回答已被用户终止。",
        )


__all__ = ["ConversationTurnSerializer"]
