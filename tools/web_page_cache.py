"""Small, bounded in-memory cache for extracted web pages.

The cache stores complete page dictionaries before any query-specific
projection.  Callers own the returned copy, while concurrent reads for one
key share one loader Future.  Network work is always performed outside the
cache lock.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from copy import deepcopy
from dataclasses import dataclass
import math
import threading
import time
from typing import Any


PageLoader = Callable[[], dict[str, Any]]
_MISSING = object()


@dataclass(slots=True)
class _CacheEntry:
    value: dict[str, Any]
    expires_at: float
    text_chars: int


class PageReadCache:
    """Cache successful page reads with TTL, LRU and in-flight coalescing."""

    def __init__(
        self,
        *,
        ttl: float = 60.0,
        max_entries: int = 16,
        max_text_chars: int = 4_000_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = self._positive_number(ttl, name="ttl")
        self._max_entries = self._non_negative_int(max_entries, name="max_entries")
        self._max_text_chars = self._non_negative_int(max_text_chars, name="max_text_chars")
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._entries: OrderedDict[Hashable, _CacheEntry] = OrderedDict()
        self._inflight: dict[Hashable, Future] = {}
        self._text_chars = 0

    @staticmethod
    def _positive_number(value: float, *, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a positive finite number") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} must be a positive finite number")
        return number

    @staticmethod
    def _non_negative_int(value: int, *, name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a non-negative integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a non-negative integer") from exc
        if number < 0 or number != value:
            raise ValueError(f"{name} must be a non-negative integer")
        return number

    @staticmethod
    def _copy_value(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise TypeError("page loader must return a dict")
        return deepcopy(value)

    @staticmethod
    def _text_size(value: dict[str, Any]) -> int:
        text = value.get("text")
        return len(text) if isinstance(text, str) else 0

    def _effective_ttl(self, value: dict[str, Any]) -> float | None:
        marker = value.get("_cache_ttl", _MISSING)
        if marker is _MISSING:
            return self._ttl
        try:
            requested = float(marker)
        except (TypeError, ValueError):
            return self._ttl
        if not math.isfinite(requested):
            return self._ttl
        if requested <= 0:
            return None
        return min(self._ttl, requested)

    def _remove_entry(self, key: Hashable) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._text_chars -= entry.text_chars

    def _store(self, key: Hashable, value: dict[str, Any], expires_at: float) -> bool:
        text_chars = self._text_size(value)
        if self._max_entries == 0 or text_chars > self._max_text_chars:
            return False

        self._remove_entry(key)
        while self._entries and (
            len(self._entries) >= self._max_entries
            or self._text_chars + text_chars > self._max_text_chars
        ):
            _, oldest = self._entries.popitem(last=False)
            # The value was just removed, so update the aggregate explicitly.
            # Keeping this under the same lock makes eviction atomic for users.
            self._text_chars -= oldest.text_chars
        self._entries[key] = _CacheEntry(value=value, expires_at=expires_at, text_chars=text_chars)
        self._text_chars += text_chars
        return True

    def get_or_load(
        self,
        key: Hashable,
        loader: PageLoader,
        *,
        timeout: float,
    ) -> tuple[dict[str, Any], bool]:
        """Return a cached page or load it once for concurrent callers.

        The first caller for a missing key owns the loader and reports
        ``cache_hit=False``.  Concurrent waiters share that result and report
        ``cache_hit=True`` only when the result was actually stored.  A
        waiter timeout leaves the in-flight load running for its owner and
        does not poison the key.
        """

        try:
            wait_timeout = max(0.0, float(timeout))
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout must be a non-negative number") from exc

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.expires_at > self._monotonic():
                    self._entries.move_to_end(key)
                    return deepcopy(entry.value), True
                self._remove_entry(key)

            future = self._inflight.get(key)
            if future is None:
                future = Future()
                self._inflight[key] = future
                owner = True
            else:
                owner = False

        if not owner:
            try:
                value, cacheable = future.result(timeout=wait_timeout)
            except FutureTimeoutError as exc:
                raise TimeoutError("timed out waiting for page read") from exc
            return deepcopy(value), bool(cacheable)

        try:
            value = self._copy_value(loader())
            effective_ttl = self._effective_ttl(value)
            cacheable = False
            with self._lock:
                if effective_ttl is not None:
                    cacheable = self._store(
                        key,
                        value,
                        self._monotonic() + effective_ttl,
                    )
                self._inflight.pop(key, None)
                future.set_result((value, cacheable))
            return deepcopy(value), False
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
                future.set_exception(exc)
            raise

    def clear(self) -> None:
        """Remove completed entries; an in-flight loader is left untouched."""

        with self._lock:
            self._entries.clear()
            self._text_chars = 0
