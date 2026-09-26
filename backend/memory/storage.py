"""长期记忆文件和版本边界的唯一持久化所有者。

这里保留 memory.json 的兼容格式、records ledger、generation 与
clearGeneration 语义。路径、当前设置、时间和召回缓存失效通过端口提供；
服务自身持有唯一的持久化锁，更新、认知投影和清空都复用它。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Sequence

from agent.memory_architecture import (
    MemoryRecord,
    confirm_or_add_memories,
    new_memory_record,
    reconcile_memory_contents,
)
from backend.common.text import unique_strings
from backend.common.values import safe_nonnegative_int


@dataclass(frozen=True)
class MemoryStoragePorts:
    """入口动态边界；`path` 保留旧 server 路径替换能力。"""

    path: Callable[[dict[str, Any] | None], Path]
    developer_storage_active: Callable[[], bool]
    developer_memory_path: Callable[[], Path]
    load_settings: Callable[[], Mapping[str, Any]]
    data_dir: Callable[[], Path]
    default_settings: Callable[[], Mapping[str, Any]]
    default_memories: Callable[[], Sequence[str]]
    memory_limit: Callable[[], int]
    generic_noise: Callable[[], set[str]]
    now_iso: Callable[[], str]
    invalidate_retrieval_cache: Callable[[], Any]


class MemoryStorageService:
    """拥有 memory.json 及其 generation/clearGeneration 的读写。"""

    def __init__(self, ports: MemoryStoragePorts) -> None:
        self.ports = ports
        self.persistence_lock = threading.RLock()

    def raw_path(self, settings: dict[str, Any] | None = None) -> Path:
        """Resolve the normal/developer path without going through compatibility `path`."""

        if self.ports.developer_storage_active():
            return self.ports.developer_memory_path()
        effective = self.ports.load_settings() if settings is None else settings
        raw_path = Path(str(
            effective.get("memoryFile")
            or self.ports.default_settings().get("memoryFile")
            or "memory.json"
        ))
        if raw_path.is_absolute():
            return raw_path
        parts = raw_path.parts
        if parts and parts[0].casefold() == "data":
            raw_path = Path(*parts[1:]) if len(parts) > 1 else Path("memory.json")
        return (self.ports.data_dir() / raw_path).resolve()

    def path(self, settings: dict[str, Any] | None = None) -> Path:
        """Resolve through the compatibility port so patched server paths remain effective."""

        return self.ports.path(settings)

    def ensure_file(self, settings: dict[str, Any] | None = None) -> Path:
        path = self.path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            self.write(list(self.ports.default_memories()), settings=settings)
        return path

    def read(self, settings: dict[str, Any] | None = None) -> list[str]:
        path = self.ensure_file(settings)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return list(self.ports.default_memories())
        if isinstance(raw, list):
            return unique_strings(raw)[:self.ports.memory_limit()]
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            records: list[MemoryRecord] = []
            for item in raw["records"]:
                if not isinstance(item, dict):
                    continue
                try:
                    records.append(MemoryRecord.from_dict(item))
                except (TypeError, ValueError):
                    continue
            active = [item.content for item in records if item.active and item.content.strip()]
            # Once a records ledger exists it is authoritative, even when every
            # record is tombstoned. Falling back to the denormalized legacy list
            # would resurrect deleted memories.
            return unique_strings(active)[:self.ports.memory_limit()]
        if isinstance(raw, dict) and isinstance(raw.get("memories"), list):
            return unique_strings(raw["memories"])[:self.ports.memory_limit()]
        return list(self.ports.default_memories())

    def read_records(self, settings: dict[str, Any] | None = None) -> list[MemoryRecord]:
        path = self.ensure_file(settings)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {"memories": list(self.ports.default_memories())}
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            records: list[MemoryRecord] = []
            for item in raw["records"]:
                if not isinstance(item, dict):
                    continue
                try:
                    records.append(MemoryRecord.from_dict(item))
                except (TypeError, ValueError):
                    continue
            return records
        legacy = (
            raw if isinstance(raw, list)
            else raw.get("memories", []) if isinstance(raw, dict) else []
        )
        return [
            new_memory_record(str(item), deterministic_legacy_id=True)
            for item in unique_strings(list(legacy))[:self.ports.memory_limit()]
            if str(item).strip()
        ]

    def generation(self, settings: dict[str, Any] | None = None) -> int:
        path = self.ensure_file(settings)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        return safe_nonnegative_int(raw.get("generation")) if isinstance(raw, dict) else 0

    def clear_generation(self, settings: dict[str, Any] | None = None) -> int:
        path = self.ensure_file(settings)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        return safe_nonnegative_int(raw.get("clearGeneration")) if isinstance(raw, dict) else 0

    def write_records(
        self,
        records: list[MemoryRecord],
        settings: dict[str, Any] | None = None,
        *,
        clear_generation: int | None = None,
    ) -> list[str]:
        with self.persistence_lock:
            path = self.path(settings)
            path.parent.mkdir(parents=True, exist_ok=True)
            active = unique_strings([
                item.content
                for item in records
                if item.active
                and item.content.strip()
                and item.content.strip(" ，,:：。！？?!") not in self.ports.generic_noise()
            ])[:self.ports.memory_limit()]
            previous_generation = self.generation(settings) if path.exists() else 0
            previous_clear_generation = self.clear_generation(settings) if path.exists() else 0
            payload = {
                "version": 2,
                "generation": previous_generation + 1,
                "clearGeneration": (
                    previous_clear_generation
                    if clear_generation is None else safe_nonnegative_int(clear_generation)
                ),
                "updatedAt": self.ports.now_iso(),
                "memories": active,
                "records": [item.to_dict() for item in records],
            }
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        self.ports.invalidate_retrieval_cache()
        return active

    def clear_records(self, settings: dict[str, Any] | None = None) -> list[str]:
        """Clear active rows while advancing the CAS boundary for observers."""

        with self.persistence_lock:
            next_clear_generation = self.clear_generation(settings) + 1
            current = self.read_records(settings)
            timestamp = self.ports.now_iso()
            tombstoned = [
                replace(
                    item,
                    superseded_by=(item.superseded_by or "deleted:" + timestamp),
                    last_confirmed_at=timestamp,
                )
                if item.active else item
                for item in current
            ]
            return self.write_records(
                tombstoned,
                settings=settings,
                clear_generation=next_clear_generation,
            )

    def write(self, memories: list[Any], settings: dict[str, Any] | None = None) -> list[str]:
        cleaned = unique_strings([
            str(item).strip()
            for item in memories
            if str(item).strip()
            and str(item).strip(" ，,:：。！？?!") not in self.ports.generic_noise()
        ])[:self.ports.memory_limit()]
        with self.persistence_lock:
            path = self.path(settings)
            if path.exists():
                records = list(reconcile_memory_contents(
                    self.read_records(settings),
                    cleaned,
                    source_turn_id="user_memory_editor",
                ))
            else:
                records = [
                    new_memory_record(item, source_turn_ids=("user_memory_editor",))
                    for item in cleaned
                ]
            return self.write_records(records, settings=settings)
