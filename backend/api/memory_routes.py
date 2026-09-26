"""记忆读取、生成、更新与清空接口；存储及关系重置由原服务处理。

保留开发作用域独立路径、保存后的实际记录与全部记忆清空的重置顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class JsonResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class MemoryOperations:
    load_settings: Callable[[], dict[str, Any]]
    developer_storage_active: Callable[[], bool]
    memory_path: Callable[[dict[str, Any]], Path]
    read_memory: Callable[[dict[str, Any]], list[str]]
    memory_records: Callable[[dict[str, Any]], list[dict[str, Any]]]
    generate_memory: Callable[[dict[str, Any]], list[str]]
    clear_memory: Callable[..., list[str]]
    reset_dignity_boundary: Callable[[], dict[str, Any]]
    save_settings: Callable[[dict[str, Any]], dict[str, Any]]
    write_memory: Callable[..., list[str]]


class MemoryRoutes:
    def __init__(self, operations: MemoryOperations) -> None:
        self.operations = operations

    def get(self, response: JsonResponse, path: str) -> bool:
        if path != "/api/memory":
            return False
        settings = self.operations.load_settings()
        response.send_json({
            "memoryFile": (
                "data/developer/memory.json"
                if self.operations.developer_storage_active()
                else settings["memoryFile"]
            ),
            "memoryFileAbsolute": str(self.operations.memory_path(settings)),
            "memories": self.operations.read_memory(settings),
            "memoryRecords": self.operations.memory_records(settings),
        })
        return True

    def post_generate(self, response: JsonResponse, path: str, body: dict[str, Any]) -> bool:
        if path != "/api/memory/generate":
            return False
        memories = self.operations.generate_memory(body)
        settings = self.operations.load_settings()
        response.send_json({
            "ok": True,
            "memories": memories,
            "memoryRecords": self.operations.memory_records(settings),
            "memoryFileAbsolute": str(self.operations.memory_path(settings)),
        })
        return True

    def post(self, response: JsonResponse, path: str, body: dict[str, Any]) -> bool:
        if path == "/api/memory/generate":
            return self.post_generate(response, path, body)
        if path == "/api/memory/clear":
            memories = self.operations.clear_memory(
                settings=self.operations.load_settings(),
            )
            relationship = self.operations.reset_dignity_boundary()
            response.send_json({
                "ok": True,
                "memories": memories,
                "memoryRecords": self.operations.memory_records(
                    self.operations.load_settings(),
                ),
                "dignityBoundaryReset": True,
                "dignityViolationCount": int(relationship.get("dignityViolationCount") or 0),
                "dignityLocked": bool(relationship.get("dignityLocked")),
            })
            return True
        if path == "/api/memory":
            update = body.get("memories") if isinstance(body.get("memories"), list) else []
            if self.operations.developer_storage_active():
                settings = self.operations.load_settings()
            elif "memoryFile" in body:
                settings = self.operations.save_settings({"memoryFile": body["memoryFile"]})
            else:
                settings = self.operations.load_settings()
            memories = self.operations.write_memory(update, settings=settings)
            response.send_json({
                "ok": True,
                "memories": memories,
                "memoryRecords": self.operations.memory_records(settings),
                "memoryFile": (
                    "data/developer/memory.json"
                    if self.operations.developer_storage_active()
                    else settings["memoryFile"]
                ),
                "memoryFileAbsolute": str(self.operations.memory_path(settings)),
            })
            return True
        return False
