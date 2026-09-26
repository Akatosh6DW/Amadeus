"""运行时缓存查看与清理的 HTTP 分支。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class CacheResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class CacheOperations:
    status: Callable[[], Any]
    clear: Callable[[str], dict[str, Any]]
    log_clear_requested: Callable[[str], None]


class CacheRoutes:
    def __init__(self, operations: CacheOperations) -> None:
        self.operations = operations

    def get(self, response: CacheResponse, path: str) -> bool:
        if path != "/api/cache/status":
            return False
        summaries = self.operations.status()
        response.send_json({
            "ok": True,
            "unused": summaries["unused"].to_dict(),
            "allDisposable": summaries["all_disposable"].to_dict(),
        })
        return True

    def post(self, response: CacheResponse, path: str, body: dict[str, Any]) -> bool:
        if path != "/api/cache/clear":
            return False
        mode = str(body.get("mode") or "unused").strip()
        if mode not in {"unused", "all_disposable"}:
            response.send_json({"ok": False, "error": "invalid_cache_mode"}, 400)
            return True
        self.operations.log_clear_requested(mode)
        response.send_json(self.operations.clear(mode))
        return True
