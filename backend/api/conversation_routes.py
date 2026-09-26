"""会话历史、检索、切换与清空接口。

HTTP 入口先处理认证、存储作用域和异常响应。本模块只解析路径/查询参数、
调用会话服务并组织响应；历史、代际、等待状态仍由原应用服务与仓储持有。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
import urllib.parse


class JsonResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class ConversationOperations:
    storage_health: Callable[[], Mapping[str, Any]]
    archive_preview: Callable[[int], Any]
    active_database: Callable[[], Any]
    public_history: Callable[[bool], dict[str, Any]]
    session_summaries: Callable[[], dict[str, Any]]
    search_history: Callable[[str, str, int, int], dict[str, Any]]
    append_proactive_event: Callable[[dict[str, Any]], dict[str, Any]]
    record_delivery_event: Callable[[dict[str, Any]], dict[str, Any]]
    recovery_build: Callable[[], dict[str, Any]]
    start_new: Callable[[], dict[str, Any]]
    delete: Callable[[Any], dict[str, Any]]
    switch: Callable[[Any], dict[str, Any]]
    clear: Callable[[], dict[str, Any]]
    clear_short_memory: Callable[[], dict[str, Any]]


class ConversationRoutes:
    def __init__(self, operations: ConversationOperations) -> None:
        self.operations = operations

    def get(self, response: JsonResponse, path: str, query_string: str = "") -> bool:
        if path == "/api/conversation/recovery-preview":
            response.send_json({
                "ok": True,
                "storageHealth": self.operations.storage_health(),
                "archivePreview": self.operations.archive_preview(8),
                "activeDatabase": str(self.operations.active_database()),
                "policy": "read_only_preview_no_active_file_replacement",
            })
            return True
        if path == "/api/conversation/history":
            query = urllib.parse.parse_qs(query_string, keep_blank_values=True)
            current_only = str((query.get("currentOnly") or ["1"])[0]).lower() in {
                "1", "true", "yes",
            }
            response.send_json(self.operations.public_history(not current_only))
            return True
        if path == "/api/conversations":
            response.send_json(self.operations.session_summaries())
            return True
        if path == "/api/conversation/search":
            query = urllib.parse.parse_qs(query_string, keep_blank_values=True)
            try:
                limit = int((query.get("limit") or ["30"])[0])
                offset = int((query.get("offset") or ["0"])[0])
            except (TypeError, ValueError):
                response.send_json({"ok": False, "error": "invalid_search_page"}, 400)
                return True
            response.send_json(self.operations.search_history(
                (query.get("q") or [""])[0],
                (query.get("role") or ["all"])[0],
                limit,
                offset,
            ))
            return True
        return False

    def post(self, response: JsonResponse, path: str, body: dict[str, Any]) -> bool:
        if path == "/api/conversation/proactive":
            response.send_json(self.operations.append_proactive_event(body))
        elif path == "/api/conversation/delivery":
            response.send_json(self.operations.record_delivery_event(body))
        elif path == "/api/conversation/recovery-build":
            response.send_json(self.operations.recovery_build())
        elif path == "/api/conversation/new":
            context = self.operations.start_new()
            response.send_json({"ok": True, "context": context})
        elif path == "/api/conversation/delete":
            response.send_json(self.operations.delete(body.get("conversationId")))
        elif path == "/api/conversation/switch":
            context = self.operations.switch(body.get("conversationId"))
            response.send_json({
                "ok": True,
                "context": context,
                "history": self.operations.public_history(False),
            })
        elif path == "/api/conversation/clear":
            context = self.operations.clear()
            response.send_json({"ok": True, "context": context})
        elif path == "/api/conversation/short-memory/clear":
            context = self.operations.clear_short_memory()
            response.send_json({"ok": True, "context": context})
        else:
            return False
        return True
