"""Release update check, status, and explicit apply routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class UpdateResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class UpdateOperations:
    status: Callable[[], dict[str, Any]]
    check: Callable[[], dict[str, Any]]
    start: Callable[[bool], dict[str, Any]]


class UpdateRoutes:
    def __init__(self, operations: UpdateOperations) -> None:
        self.operations = operations

    def get(self, response: UpdateResponse, path: str) -> bool:
        if path != "/api/update/status":
            return False
        response.send_json(self.operations.status())
        return True

    def post(self, response: UpdateResponse, path: str, body: dict[str, Any]) -> bool:
        if path == "/api/update/check":
            response.send_json(self.operations.check())
            return True
        if path == "/api/update/start":
            confirmed = body.get("confirmed") is True
            result = self.operations.start(confirmed)
            response.send_json(result, 200 if result.get("ok") else 400)
            return True
        return False


__all__ = ["UpdateOperations", "UpdateRoutes"]
