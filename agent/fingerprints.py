"""Small deterministic fingerprints for non-security runtime bookkeeping.

SHA-256 is intentionally reserved for artifact integrity checks and the small
set of persisted legacy IDs whose value must remain stable across upgrades.
Runtime IDs, cache keys, deterministic sampling, and privacy-preserving log
correlation use a short BLAKE2s fingerprint.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value or "").encode("utf-8")


def runtime_fingerprint(value: Any, *, length: int = 20) -> str:
    """Return a compact deterministic fingerprint, never an integrity proof."""

    bounded = max(8, min(int(length), 64))
    digest_size = max(4, min(32, (bounded + 1) // 2))
    return hashlib.blake2s(_as_bytes(value), digest_size=digest_size).hexdigest()[:bounded]


def runtime_digest(value: Any, *, size: int = 16) -> bytes:
    """Return deterministic bytes for sampling and other non-security choices."""

    return hashlib.blake2s(
        _as_bytes(value),
        digest_size=max(1, min(int(size), 32)),
    ).digest()


def legacy_persistent_fingerprint(value: Any, *, length: int) -> str:
    """Preserve existing on-disk idempotency keys; do not use for new IDs."""

    bounded = max(8, min(int(length), 64))
    return hashlib.sha256(_as_bytes(value)).hexdigest()[:bounded]
