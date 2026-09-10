"""Small thread-safe circuit breaker for non-critical memory background jobs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import threading
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BackgroundJobCircuit:
    key: str
    consecutive_failures: int = 0
    opened_until: datetime | None = None
    last_error: str | None = None

    def allow(self, *, now: datetime | None = None) -> bool:
        current = now or _now()
        return self.opened_until is None or current >= self.opened_until

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["opened_until"] = (
            self.opened_until.isoformat() if self.opened_until else None
        )
        return value


class BackgroundCircuitRegistry:
    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: int = 180) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self._circuits: dict[str, BackgroundJobCircuit] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> BackgroundJobCircuit:
        normalized = str(key or "memory_job")[:120]
        with self._lock:
            circuit = self._circuits.setdefault(normalized, BackgroundJobCircuit(normalized))
            if circuit.opened_until and _now() >= circuit.opened_until:
                circuit.opened_until = None
                circuit.consecutive_failures = 0
            return circuit

    def allow(self, key: str) -> bool:
        with self._lock:
            return self.get(key).allow()

    def success(self, key: str) -> BackgroundJobCircuit:
        with self._lock:
            circuit = self.get(key)
            circuit.consecutive_failures = 0
            circuit.opened_until = None
            circuit.last_error = None
            return circuit

    def failure(self, key: str, error: BaseException | str) -> BackgroundJobCircuit:
        with self._lock:
            circuit = self.get(key)
            circuit.consecutive_failures += 1
            circuit.last_error = str(error)[:1000]
            if circuit.consecutive_failures >= self.failure_threshold:
                circuit.opened_until = _now() + timedelta(seconds=self.cooldown_seconds)
            return circuit

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: value.to_dict() for key, value in self._circuits.items()}
