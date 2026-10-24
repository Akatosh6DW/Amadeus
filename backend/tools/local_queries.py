"""本地只读工具：时钟、待办和书签查询，不触发模型或联网。

数据库连接仍来自统一作用域；查询返回当前存储投影，不能产生修改回执。
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from agent.todo_architecture import TodoTargetRef
from todo_store import resolve_task_reference, search_todos, list_todos, todo_change_summary, list_todo_runs
from bookmark_store import list_bookmarks
from backend.tools.todo_actions import public_todo_candidate as _public_todo_candidate

def tool_get_system_time(settings: dict[str, Any]) -> dict[str, Any]:
    if not settings.get("permissionReadSystemTime"):
        return {
            "name": "get_system_time",
            "ok": False,
            "error": "permission_denied",
            "message": "系统时间权限未授予，不能读取当前时间。",
        }
    now = datetime.now().astimezone()
    return {
        "name": "get_system_time",
        "ok": True,
        "iso": now.isoformat(timespec="seconds"),
        "display": now.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour": now.hour,
        "minute": now.minute,
        "second": now.second,
    }


@dataclass(frozen=True)
class LocalQueryPorts:
    database: Callable[..., Any]
    now: Callable[[], str]

class LocalQueries:
    def __init__(self, ports: LocalQueryPorts) -> None:
        self.ports = ports

    def tool_todo_search(
        self,
        settings: dict[str, Any],
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read and resolve local Todo candidates without mutating user state."""

        del settings
        options = dict(args) if isinstance(args, dict) else {}
        try:
            target = TodoTargetRef.from_mapping(
                options.get("targetRef") or options.get("target_ref") or options
            )
        except ValueError as error:
            return {"name": "todo_search", "ok": False, "error": str(error)}
        try:
            limit = max(1, min(20, int(options.get("limit") or 8)))
        except (TypeError, ValueError):
            limit = 8
        with self.ports.database() as connection:
            resolution = resolve_task_reference(connection, target.to_dict(), limit=limit)
            candidates = search_todos(connection, target.to_dict(), limit=limit)
            history = []
            if options.get('includeHistory'):
                task = resolution.get('task') or {}
                task_id = str(task.get('taskId') or target.to_dict().get('taskId') or '')
                try:
                    history_limit = max(1, min(20, int(options.get('historyLimit') or 6)))
                except (TypeError, ValueError):
                    history_limit = 6
                if task_id:
                    history = list_todo_runs(connection, task_id, limit=history_limit)
        return {
            "name": "todo_search",
            "ok": True,
            "targetRef": target.to_dict(),
            "resolution": {
                "status": str(resolution.get("status") or "not_found"),
                "matchType": str(resolution.get("matchType") or ""),
                "error": str(resolution.get("error") or ""),
                "task": (
                    _public_todo_candidate(resolution["task"])
                    if isinstance(resolution.get("task"), Mapping) else None
                ),
            },
            "candidates": [_public_todo_candidate(item) for item in candidates],
            "count": len(candidates),
            "source": "local_sqlite",
            **({'executionHistory': history} if options.get('includeHistory') else {}),
        }


    def tool_todo_list(
        self,
        settings: dict[str, Any],
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read the authoritative local task state without any web dependency."""

        del settings
        options = args if isinstance(args, dict) else {}
        scope = str(options.get("scope") or "active").strip().lower()
        try:
            since_revision = max(0, int(options.get("sinceRevision") or 0))
        except (TypeError, ValueError):
            since_revision = 0
        with self.ports.database() as connection:
            items = list_todos(connection, scope=scope)
            for item in items:
                runs = list_todo_runs(connection, item['taskId'], limit=1)
                if runs:
                    last = runs[0]
                    item['lastExecution'] = {
                        'runId': last['runId'], 'status': last['status'],
                        'finishedAt': last.get('finishedAt', ''),
                        'reply': str((last.get('result') or {}).get('reply') or '')[:1000],
                    }
            active_count = (
                len(items)
                if scope in {"active", "pending"}
                else len(list_todos(connection, scope="active"))
            )
            changes = todo_change_summary(
                connection,
                since_revision=since_revision,
            )
        return {
            "name": "todo_list",
            "ok": True,
            "scope": scope,
            "items": items,
            "count": len(items),
            "activeCount": active_count,
            "revision": changes["revision"],
            "unseenCount": changes["unseenCount"],
            "source": "local_sqlite",
        }


    def tool_bookmark_list(
        self,
        settings: dict[str, Any],
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read the user's authoritative local web bookmarks."""

        del settings
        options = dict(args) if isinstance(args, dict) else {}
        query = str(options.get("query") or options.get("name") or "").strip()[:240]
        try:
            limit = max(1, min(200, int(options.get("limit") or 200)))
        except (TypeError, ValueError):
            limit = 200
        with self.ports.database() as connection:
            items = list_bookmarks(connection, query=query, limit=limit)
        return {
            "name": "bookmark_list",
            "ok": True,
            "items": items,
            "count": len(items),
            "query": query,
            "source": "local_sqlite",
        }


    def todo_context_from_tool_results(self, results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        task_ids: list[str] = []
        candidates: list[dict[str, Any]] = []
        for item in reversed(list(results)):
            if item.get("name") not in {
                "todo_search", "todo_list", "todo_manage",
            }:
                continue
            if not item.get("ok") and not (
                item.get("requiresClarification")
                and isinstance(item.get("candidates"), list)
            ):
                continue
            raw_tasks: list[Mapping[str, Any]] = []
            if isinstance(item.get("task"), Mapping):
                raw_tasks.append(item["task"])
            if isinstance(item.get("candidates"), list):
                raw_tasks.extend(
                    task for task in item["candidates"] if isinstance(task, Mapping)
                )
            if isinstance(item.get("items"), list):
                raw_tasks.extend(
                    task for task in item["items"] if isinstance(task, Mapping)
                )
            for task in raw_tasks:
                public = _public_todo_candidate(task)
                task_id = str(public.get("taskId") or "")
                if not task_id or task_id in task_ids:
                    continue
                task_ids.append(task_id)
                candidates.append(public)
        if not task_ids:
            return {}
        return {
            "recentTaskIds": task_ids[:12],
            "candidateTasks": candidates[:12],
            "observedAt": self.ports.now(),
        }
