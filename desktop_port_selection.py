"""Pure helpers for choosing an Amadeus System desktop backend port."""

from __future__ import annotations

from collections.abc import Callable, Iterable


def choose_available_backend_port(
    preferred: int,
    fallbacks: Iterable[int],
    *,
    is_listening: Callable[[int], bool],
    is_compatible: Callable[[int], bool],
) -> int:
    """Reuse a compatible server or choose a free port without touching its owner."""

    preferred = int(preferred)
    if not is_listening(preferred) or is_compatible(preferred):
        return preferred

    checked = {preferred}
    for candidate_value in fallbacks:
        candidate = int(candidate_value)
        if candidate in checked or not 1 <= candidate <= 65535:
            continue
        checked.add(candidate)
        if not is_listening(candidate) or is_compatible(candidate):
            return candidate

    raise RuntimeError(
        f"Amadeus System 桌面后端首选端口 {preferred} 及备用端口均被占用。"
    )
