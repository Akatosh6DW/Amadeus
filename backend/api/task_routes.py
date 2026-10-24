"""待办、提醒和书签的 HTTP 接口。

只校验请求字段并映射响应状态。任务/书签内容、版本冲突与事务仍归原领域
服务；提醒确认必须通过存储服务完成事务，路由不直接操作数据库连接。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol
import urllib.parse


class JsonResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class TaskOperations:
    todo_list: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    todo_manage: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    poll_reminder: Callable[[dict[str, Any]], dict[str, Any]]
    acknowledge_reminder: Callable[[str, bool, str], dict[str, Any]]
    bookmark_list: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    bookmark_manage: Callable[[dict[str, Any], dict[str, Any], str], dict[str, Any]]
    task_history: Callable[[str], dict[str, Any]] | None = None


class TaskRoutes:
    def __init__(self, operations: TaskOperations) -> None:
        self.operations = operations

    def get(self, response: JsonResponse, path: str, query_string: str = "") -> bool:
        if path not in {"/api/bookmarks", "/api/todos", "/api/todos/history"}:
            return False
        query = urllib.parse.parse_qs(query_string, keep_blank_values=True)
        if path == '/api/todos/history':
            task_id = str((query.get('taskId') or [''])[0]).strip()
            if not task_id:
                response.send_json({'ok': False, 'error': 'task_id_required'}, 400)
            elif self.operations.task_history is None:
                response.send_json({'ok': False, 'error': 'task_history_unavailable'}, 503)
            else:
                result = self.operations.task_history(task_id)
                response.send_json(result, 200 if result.get('ok') else 404)
            return True
        if path == "/api/bookmarks":
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except (TypeError, ValueError):
                limit = 200
            response.send_json(self.operations.bookmark_list({}, {
                "query": (query.get("q") or [""])[0],
                "limit": limit,
            }))
            return True
        if path == "/api/todos":
            scope = str((query.get("scope") or ["all"])[0]).strip().lower()
            try:
                since_revision = max(
                    0,
                    int((query.get("sinceRevision") or ["0"])[0] or 0),
                )
            except (TypeError, ValueError):
                since_revision = 0
            response.send_json(self.operations.todo_list({}, {
                "scope": scope,
                "sinceRevision": since_revision,
            }))
            return True
        return False

    def post(self, response: JsonResponse, path: str, body: dict[str, Any]) -> bool:
        if path == "/api/todos/reminders/poll":
            response.send_json(self.operations.poll_reminder(body))
            return True
        if path == "/api/todos/reminders/ack":
            reminder_id = str(body.get("reminderId") or "").strip()
            if not reminder_id:
                response.send_json({
                    "ok": False,
                    "error": "reminder_id_required",
                }, 400)
                return True
            acknowledged = self.operations.acknowledge_reminder(
                reminder_id,
                bool(body.get("delivered")),
                str(body.get("error") or ""),
            )
            response.send_json({
                "ok": bool(acknowledged),
                "reminder": acknowledged or None,
                **({} if acknowledged else {"error": "reminder_not_found"}),
            }, 200 if acknowledged else 404)
            return True
        if path == "/api/todos":
            result = self.operations.todo_manage({}, body)
            response.send_json(result, 200 if result.get("ok") else 400)
            return True
        if path == "/api/bookmarks":
            result = self.operations.bookmark_manage(
                {},
                {**body, "commitIntent": True},
                str(body.get("requestId") or ""),
            )
            response.send_json(result, 200 if result.get("ok") else 400)
            return True
        return False
