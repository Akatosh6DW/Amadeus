"""长期记忆召回的唯一缓存所有者。

排序和 NeedGate 规则继续归 `backend.context.recall`；这里仅把应用层的
存储端口、语义运行时和缓存实例装配在 memory 领域，避免 server 再持有一
份检索缓存。每次调用同步当前语义运行时，测试替换或运行时切换会清空旧缓存。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agent.memory_architecture import MemoryNeed, MemoryRecord
from agent.semantic_memory_retrieval import SemanticMemoryRuntime
from backend.context.recall import MemoryRecallService


@dataclass(frozen=True)
class MemoryRetrievalPorts:
    """召回所需的作用域存储和语义运行时端口。"""

    read_records: Callable[[dict[str, Any] | None], list[MemoryRecord]]
    path: Callable[[dict[str, Any] | None], Path]
    generation: Callable[[dict[str, Any] | None], int]
    clear_generation: Callable[[dict[str, Any] | None], int]
    scope: Callable[[], str]
    semantic_runtime: Callable[[], SemanticMemoryRuntime]
    clock: Callable[[], datetime]


class _PortMemoryStore:
    def __init__(self, ports: MemoryRetrievalPorts) -> None:
        self.ports = ports

    def read_records(self, settings: dict[str, Any]) -> list[MemoryRecord]:
        return self.ports.read_records(settings)

    def path(self, settings: dict[str, Any] | None) -> Path:
        return self.ports.path(settings)

    def generation(self, settings: dict[str, Any] | None) -> int:
        return self.ports.generation(settings)

    def clear_generation(self, settings: dict[str, Any] | None) -> int:
        return self.ports.clear_generation(settings)

    def scope(self) -> str:
        return self.ports.scope()


class MemoryRetrievalService:
    """Wrap the existing recall rules while owning one cache instance."""

    def __init__(self, ports: MemoryRetrievalPorts) -> None:
        self.ports = ports
        self.recall = MemoryRecallService(
            _PortMemoryStore(ports),
            ports.semantic_runtime(),
            clock=ports.clock,
        )

    def _sync_runtime(self) -> bool:
        runtime = self.ports.semantic_runtime()
        if self.recall.semantic_runtime is not runtime:
            self.recall.semantic_runtime = runtime
            self.recall.invalidate()
            return True
        return False

    def invalidate(self) -> None:
        if not self._sync_runtime():
            self.recall.invalidate()

    def relevant(
        self,
        memories: list[str],
        user_message: str,
        messages: list[dict[str, Any]],
        *,
        limit: int | None = None,
        need: MemoryNeed | None = None,
        records: Sequence[MemoryRecord] | None = None,
        settings: dict[str, Any] | None = None,
        retrieval_query: str | None = None,
        cognitive_state: Mapping[str, Any] | None = None,
    ) -> list[str]:
        self._sync_runtime()
        return self.recall.relevant_memories(
            memories,
            user_message,
            messages,
            limit=limit,
            need=need,
            records=records,
            settings=settings,
            retrieval_query=retrieval_query,
            cognitive_state=cognitive_state,
        )

    def retrieve(
        self,
        settings: dict[str, Any],
        supplied_memories: Sequence[Any],
        user_message: str,
        messages: list[dict[str, Any]],
        *,
        limit: int,
        cognitive_state: Mapping[str, Any] | None = None,
        context_state: Mapping[str, Any] | None = None,
        rank_memories: Callable[..., list[str]] | None = None,
    ) -> tuple[list[str], list[MemoryRecord], MemoryNeed]:
        self._sync_runtime()
        return self.recall.retrieve_turn_memories(
            settings,
            supplied_memories,
            user_message,
            messages,
            limit=limit,
            cognitive_state=cognitive_state,
            context_state=context_state,
            rank_memories=rank_memories,
        )

    def relevant_conversation_summaries(
        self,
        context: dict[str, Any],
        user_message: str,
        need: MemoryNeed | None = None,
        *,
        allow_mixed: bool = False,
        retrieval_query: str | None = None,
        cognitive_state: Mapping[str, Any] | None = None,
    ) -> list[str]:
        self._sync_runtime()
        return self.recall.relevant_conversation_summaries(
            context,
            user_message,
            need,
            allow_mixed=allow_mixed,
            retrieval_query=retrieval_query,
            cognitive_state=cognitive_state,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
