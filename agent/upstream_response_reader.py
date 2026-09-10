"""Bounded reads for complete upstream HTTP response bodies.

The urllib connect timeout also remains on the returned socket.  A provider
can send response headers successfully and then take longer than that timeout
to produce the JSON body, so callers that need a longer body budget use this
reader after the headers have arrived.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
import http.client
import socket
import time
from typing import Any


MAX_BODY_IDLE_TIMEOUT_SECONDS = 45.0
READ_CHUNK_SIZE = 64 * 1024


class UpstreamResponseBodyReadError(OSError):
    """The complete body could not be obtained without exposing a partial body."""

    def __init__(
        self,
        message: str,
        *,
        bytes_received: int,
        elapsed_seconds: float,
        expected_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = "body_read"
        self.bytes_received = max(0, int(bytes_received))
        self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        self.expected_bytes = expected_bytes


class UpstreamResponseBodyTimeout(TimeoutError):
    """The body exceeded its idle or request-wide deadline."""

    def __init__(
        self,
        *,
        timeout_kind: str,
        timeout_seconds: float,
        bytes_received: int,
        elapsed_seconds: float,
        expected_bytes: int | None = None,
    ) -> None:
        self.stage = "body_read"
        self.timeout_kind = str(timeout_kind or "body_timeout")
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.bytes_received = max(0, int(bytes_received))
        self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        self.expected_bytes = expected_bytes
        super().__init__(
            f"upstream response body {self.timeout_kind} after "
            f"{self.elapsed_seconds:.3f}s ({self.bytes_received} bytes)"
        )


def _response_socket(response: Any) -> Any | None:
    """Find the socket beneath urllib's HTTPResponse without assuming one layout."""

    candidates = [
        getattr(response, "sock", None),
        getattr(response, "_sock", None),
        getattr(response, "fp", None),
    ]
    seen: set[int] = set()
    while candidates:
        candidate = candidates.pop(0)
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if callable(getattr(candidate, "settimeout", None)):
            return candidate
        for name in ("raw", "_sock", "sock", "_connection", "fp"):
            nested = getattr(candidate, name, None)
            if nested is not None and id(nested) not in seen:
                candidates.append(nested)
    return None


def _content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    value: Any = None
    if isinstance(headers, Mapping):
        value = headers.get("Content-Length") or headers.get("content-length")
    elif headers is not None:
        try:
            value = headers.get("Content-Length")
        except (AttributeError, TypeError):
            value = None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def read_complete_response_body(
    response: Any,
    *,
    deadline: float,
    idle_timeout: float | None = MAX_BODY_IDLE_TIMEOUT_SECONDS,
    check_cancelled: Callable[[], Any] | None = None,
) -> bytes:
    """Read and return a complete body while enforcing idle and total budgets.

    ``HTTPResponse.read1`` performs at most one underlying socket read, so
    buffered bodies are consumed efficiently while a dribbled response still
    returns as soon as any byte is available.  Small fake responses without
    ``read1`` use ``read(1)``.  The returned value is only produced after EOF
    (or the declared content length), and a content-length mismatch is an
    error. ``idle_timeout=None`` allows a non-streaming inference request to
    use its remaining total budget without restarting still-pending inference.
    """

    started = time.monotonic()
    expected_bytes = _content_length(response)
    idle_budget = (
        max(0.001, min(MAX_BODY_IDLE_TIMEOUT_SECONDS, float(idle_timeout)))
        if idle_timeout is not None else None
    )
    sock = _response_socket(response)
    original_socket_timeout: float | None = None
    original_socket_timeout_available = False
    if sock is not None:
        try:
            original_socket_timeout = sock.gettimeout()
            original_socket_timeout_available = True
        except (AttributeError, OSError):
            original_socket_timeout = None

    body = bytearray()
    try:
        while True:
            if check_cancelled is not None:
                check_cancelled()
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                raise UpstreamResponseBodyTimeout(
                    timeout_kind="total_deadline",
                    timeout_seconds=max(0.0, time.monotonic() - started),
                    bytes_received=len(body),
                    elapsed_seconds=time.monotonic() - started,
                    expected_bytes=expected_bytes,
                )
            if expected_bytes is not None and len(body) >= expected_bytes:
                break

            read_timeout = min(idle_budget, remaining) if idle_budget is not None else remaining
            timeout_is_total_budget = idle_budget is None or remaining <= idle_budget
            if sock is not None:
                try:
                    sock.settimeout(max(0.001, read_timeout))
                except (AttributeError, OSError):
                    pass
            try:
                read1 = getattr(response, "read1", None)
                chunk = read1(READ_CHUNK_SIZE) if callable(read1) else response.read(1)
            except (socket.timeout, TimeoutError) as error:
                if check_cancelled is not None:
                    check_cancelled()
                now = time.monotonic()
                timeout_kind = (
                    "total_deadline"
                    if timeout_is_total_budget or now >= float(deadline)
                    else "idle_timeout"
                )
                raise UpstreamResponseBodyTimeout(
                    timeout_kind=timeout_kind,
                    timeout_seconds=read_timeout,
                    bytes_received=len(body),
                    elapsed_seconds=now - started,
                    expected_bytes=expected_bytes,
                ) from error
            except http.client.IncompleteRead as error:
                partial = getattr(error, "partial", b"")
                partial_length = len(partial) if isinstance(partial, (bytes, bytearray, memoryview)) else 0
                raise UpstreamResponseBodyReadError(
                    "upstream response body ended during a chunk",
                    bytes_received=len(body) + partial_length,
                    elapsed_seconds=time.monotonic() - started,
                    expected_bytes=expected_bytes,
                ) from error
            if chunk in (b"", ""):
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise UpstreamResponseBodyReadError(
                    "upstream response body returned a non-byte chunk",
                    bytes_received=len(body),
                    elapsed_seconds=time.monotonic() - started,
                    expected_bytes=expected_bytes,
                )
            body.extend(bytes(chunk))

        if check_cancelled is not None:
            check_cancelled()
        if expected_bytes is not None and len(body) != expected_bytes:
            raise UpstreamResponseBodyReadError(
                "upstream response body ended before Content-Length",
                bytes_received=len(body),
                elapsed_seconds=time.monotonic() - started,
                expected_bytes=expected_bytes,
            )
        return bytes(body)
    finally:
        if sock is not None and original_socket_timeout_available:
            try:
                sock.settimeout(original_socket_timeout)
            except (AttributeError, OSError):
                pass


def iter_response_lines(
    response: Any,
    *,
    deadline: float,
    check_cancelled: Callable[[], Any] | None = None,
) -> Iterator[Any]:
    """Yield response lines while enforcing one total response deadline.

    ``urllib`` leaves its connect timeout on the response socket.  For a
    streaming response that timeout is only an idle-read guard: a provider can
    keep a request alive forever by sending a comment or empty line before it
    expires.  Set the socket timeout to the remaining request budget before
    every chunk read so keepalives cannot restart the total budget.

    Small test/dummy responses may expose only ``__iter__``.  Preserve that
    compatibility while still checking cancellation and the deadline between
    already-buffered lines; real urllib responses use the bounded chunk path
    because they expose a socket and ``read1`` (or a one-byte ``read``).
    """

    started = time.monotonic()
    reader = getattr(response, "read1", None)
    sock = _response_socket(response)
    if not callable(reader) and sock is not None:
        read = getattr(response, "read", None)
        if callable(read):
            # A socket-backed response without read1 is unusual, but reading
            # one byte at a time still keeps the total deadline enforceable.
            reader = lambda _size: read(1)
    if not callable(reader) or sock is None:
        bytes_received = 0
        for line in response:
            if check_cancelled is not None:
                check_cancelled()
            now = time.monotonic()
            if now >= float(deadline):
                raise UpstreamResponseBodyTimeout(
                    timeout_kind="total_deadline",
                    timeout_seconds=max(0.0, now - started),
                    bytes_received=bytes_received,
                    elapsed_seconds=now - started,
                )
            if isinstance(line, (bytes, bytearray, memoryview)):
                bytes_received += len(line)
            elif isinstance(line, str):
                bytes_received += len(line.encode("utf-8", errors="replace"))
            yield line
        return

    original_socket_timeout: float | None = None
    original_socket_timeout_available = False
    try:
        original_socket_timeout = sock.gettimeout()
        original_socket_timeout_available = True
    except (AttributeError, OSError):
        original_socket_timeout = None

    bytes_received = 0
    buffered = bytearray()

    def remaining_budget() -> float:
        if check_cancelled is not None:
            check_cancelled()
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            now = time.monotonic()
            raise UpstreamResponseBodyTimeout(
                timeout_kind="total_deadline",
                timeout_seconds=max(0.0, now - started),
                bytes_received=bytes_received,
                elapsed_seconds=now - started,
            )
        return remaining

    try:
        while True:
            line_end = buffered.find(b"\n")
            if line_end >= 0:
                remaining_budget()
                line = bytes(buffered[:line_end + 1])
                del buffered[:line_end + 1]
                yield line
                continue
            remaining = remaining_budget()
            try:
                sock.settimeout(max(0.001, remaining))
            except (AttributeError, OSError):
                pass
            try:
                chunk = reader(READ_CHUNK_SIZE)
            except (socket.timeout, TimeoutError) as error:
                if check_cancelled is not None:
                    check_cancelled()
                now = time.monotonic()
                raise UpstreamResponseBodyTimeout(
                    timeout_kind="total_deadline",
                    timeout_seconds=max(0.0, remaining),
                    bytes_received=bytes_received,
                    elapsed_seconds=now - started,
                ) from error
            except http.client.IncompleteRead as error:
                partial = getattr(error, "partial", b"")
                partial_length = len(partial) if isinstance(
                    partial, (bytes, bytearray, memoryview)
                ) else 0
                raise UpstreamResponseBodyReadError(
                    "upstream response body ended during a stream line",
                    bytes_received=bytes_received + partial_length,
                    elapsed_seconds=time.monotonic() - started,
                ) from error
            if chunk in (b"", ""):
                if buffered:
                    remaining_budget()
                    line = bytes(buffered)
                    buffered.clear()
                    yield line
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise UpstreamResponseBodyReadError(
                    "upstream response body returned a non-byte stream chunk",
                    bytes_received=bytes_received,
                    elapsed_seconds=time.monotonic() - started,
                )
            chunk_bytes = bytes(chunk)
            bytes_received += len(chunk_bytes)
            buffered.extend(chunk_bytes)
    finally:
        if sock is not None and original_socket_timeout_available:
            try:
                sock.settimeout(original_socket_timeout)
            except (AttributeError, OSError):
                pass


__all__ = [
    "MAX_BODY_IDLE_TIMEOUT_SECONDS",
    "READ_CHUNK_SIZE",
    "UpstreamResponseBodyReadError",
    "UpstreamResponseBodyTimeout",
    "iter_response_lines",
    "read_complete_response_body",
]
