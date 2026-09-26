"""本地浏览器动作的 HTTP 参数边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol
import uuid


class ActionResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class ActionOperations:
    load_settings: Callable[[], dict[str, Any]]
    open_browser: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class ActionRoutes:
    def __init__(self, operations: ActionOperations) -> None:
        self.operations = operations

    def post(self, response: ActionResponse, path: str, body: dict[str, Any]) -> bool:
        if path != "/api/actions/open-browser":
            return False
        args = {
            "url": str(body.get("url") or ""),
            "query": str(body.get("query") or ""),
            "browserIntent": str(body.get("browserIntent") or ""),
            "actionId": str(body.get("actionId") or f"ui_{uuid.uuid4().hex}"),
        }
        response.send_json(self.operations.open_browser(self.operations.load_settings(), args))
        return True
