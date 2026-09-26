"""长期记忆更新与结构化合并策略。

本模块不解析请求、不调用模型，也不创建第二个存储锁。所有读回和写入
都通过入口端口使用 `MemoryStorageService` 的同一把持久化锁；因此普通
编辑、结构化 observer、认知投影和 clear-generation CAS 仍共享一个顺序。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Callable, Collection, Mapping, Sequence

from agent.memory_architecture import (
    MemoryRecord,
    confirm_or_add_memories,
    new_memory_record,
)
from backend.common.text import unique_strings


@dataclass(frozen=True)
class MemoryUpdatePorts:
    """动态兼容端口；函数端口保留旧入口测试和认知投影替换能力。"""

    persistence_lock: Any
    read_records: Callable[[dict[str, Any] | None], list[MemoryRecord]]
    write_records: Callable[..., list[str]]
    read_memory: Callable[[dict[str, Any] | None], list[str]]
    clear_generation: Callable[[dict[str, Any] | None], int]
    normalize_candidate: Callable[[Any], dict[str, Any] | None]
    candidate_types: Callable[[], Collection[str]]
    normalize_slot: Callable[[Any], str]
    generic_noise: Callable[[], Collection[str]]
    now_iso: Callable[[], str]
    write_log: Callable[..., Any]


class MemoryUpdateService:
    """在同一持久化锁中执行旧版字符串和 typed memory 合并。"""

    def __init__(self, ports: MemoryUpdatePorts) -> None:
        self.ports = ports

    def merge(
        self,
        updates: list[Any],
        settings: dict[str, Any] | None = None,
        *,
        source_turn_id: str = "",
    ) -> list[str]:
        cleaned = unique_strings([
            str(item).strip(" ，,:：。！？")
            for item in updates
            if str(item).strip()
            and str(item).strip(" ，,:：。！？?!") not in self.ports.generic_noise()
        ])
        if not cleaned:
            return self.ports.read_memory(settings)
        with self.ports.persistence_lock:
            records = self.ports.read_records(settings)
            merged = confirm_or_add_memories(
                records,
                cleaned,
                source_turn_id=source_turn_id,
            )
            return self.ports.write_records(list(merged), settings=settings)

    def merge_typed(
        self,
        updates: Sequence[Any],
        *,
        user_message: str,
        settings: dict[str, Any] | None = None,
        source_turn_id: str = "",
        expected_clear_generation: int | None = None,
        update_types: Mapping[str, str] | None = None,
    ) -> list[str]:
        normalized_update_types = {
            re.sub(r"[\W_]+", "", str(key).casefold(), flags=re.UNICODE): str(value).strip()
            for key, value in (update_types or {}).items()
            if str(key).strip() and str(value).strip()
        }

        def requested_memory_type(content: str) -> str:
            return str(
                (update_types or {}).get(content)
                or normalized_update_types.get(
                    re.sub(r"[\W_]+", "", content.casefold(), flags=re.UNICODE)
                )
                or ""
            ).strip()

        candidates: list[dict[str, Any]] = []
        candidate_indexes: dict[str, int] = {}
        for raw in updates:
            candidate = self.ports.normalize_candidate(raw)
            if candidate is None:
                continue
            content = str(candidate["content"]).strip(" ，,:：。！？?!")
            if not content or content in self.ports.generic_noise():
                continue
            candidate["content"] = content
            requested_type = requested_memory_type(content)
            if requested_type in self.ports.candidate_types():
                candidate["type"] = requested_type
            normalized = self.ports.normalize_slot(content)
            if normalized in candidate_indexes:
                candidates[candidate_indexes[normalized]] = candidate
            else:
                candidate_indexes[normalized] = len(candidates)
                candidates.append(candidate)
        with self.ports.persistence_lock:
            if (
                expected_clear_generation is not None
                and self.ports.clear_generation(settings) != expected_clear_generation
            ):
                self.ports.write_log(
                    "memory_observer_discarded_after_clear",
                    sourceTurnId=str(source_turn_id)[:120],
                    expectedClearGeneration=expected_clear_generation,
                    currentClearGeneration=self.ports.clear_generation(settings),
                )
                return self.ports.read_memory(settings)
            records = list(self.ports.read_records(settings))
            by_content = {
                re.sub(r"[\W_]+", "", item.content.casefold(), flags=re.UNICODE): index
                for index, item in enumerate(records)
                if item.active and item.content.strip()
            }
            by_slot = {
                (
                    self.ports.normalize_slot(item.subject),
                    self.ports.normalize_slot(item.predicate),
                    self.ports.normalize_slot(item.object_value),
                    str(item.polarity or "positive"),
                ): index
                for index, item in enumerate(records)
                if item.active and item.subject and item.predicate and item.object_value
            }
            timestamp = self.ports.now_iso()
            for candidate in candidates:
                content = str(candidate["content"])
                normalized = re.sub(r"[\W_]+", "", content.casefold(), flags=re.UNICODE)
                candidate_source_turn_ids = tuple(dict.fromkeys((
                    *(
                        str(item)
                        for item in candidate.get("sourceTurnIds", ())
                        if str(item).strip()
                    ),
                    *((source_turn_id,) if source_turn_id else ()),
                )))
                candidate_slot = (
                    self.ports.normalize_slot(candidate.get("subject")),
                    self.ports.normalize_slot(candidate.get("predicate")),
                    self.ports.normalize_slot(candidate.get("object")),
                    str(candidate.get("polarity") or "positive"),
                )
                existing_index = by_content.get(normalized)
                if existing_index is None and all(candidate_slot[:3]):
                    existing_index = by_slot.get(candidate_slot)
                if existing_index is not None:
                    existing = records[existing_index]
                    requested_type = str(candidate.get("type") or requested_memory_type(content))
                    records[existing_index] = replace(
                        existing,
                        memory_type=(
                            requested_type
                            if requested_type in {
                                "user_memory", "task_state", "correction", "episode",
                                "shared_reference", "relationship_state",
                            }
                            else existing.memory_type
                        ),
                        last_confirmed_at=timestamp,
                        source_turn_ids=tuple(dict.fromkeys((
                            *existing.source_turn_ids,
                            *candidate_source_turn_ids,
                        ))),
                        confidence=max(existing.confidence, 0.95),
                        user_pinned=(
                            existing.user_pinned
                            or requested_type == "relationship_state"
                            or bool(candidate.get("userPinned"))
                        ),
                        subject=str(candidate.get("subject") or existing.subject or "") or None,
                        predicate=str(candidate.get("predicate") or existing.predicate or "") or None,
                        object_value=str(candidate.get("object") or existing.object_value or "") or None,
                        polarity=str(candidate.get("polarity") or existing.polarity),
                        modality=str(candidate.get("modality") or existing.modality),
                        temporal=str(candidate.get("temporal") or existing.temporal),
                        source_span=str(candidate.get("sourceSpan") or existing.source_span or "") or None,
                        valid_until=str(candidate.get("validUntil") or existing.valid_until or "") or None,
                    )
                    continue
                requested_type = str(candidate.get("type") or requested_memory_type(content))
                memory_type = (
                    requested_type
                    if requested_type in {
                        "user_memory", "task_state", "correction", "episode",
                        "shared_reference", "relationship_state",
                    }
                    else "user_memory"
                )
                record = new_memory_record(
                    content,
                    memory_type=memory_type,  # type: ignore[arg-type]
                    source_turn_ids=candidate_source_turn_ids,
                    confidence=1.0 if memory_type == "correction" else 0.92,
                    user_pinned=(
                        memory_type == "relationship_state"
                        or bool(candidate.get("userPinned"))
                    ),
                    subject=str(candidate.get("subject") or "") or None,
                    predicate=str(candidate.get("predicate") or "") or None,
                    object_value=str(candidate.get("object") or "") or None,
                    polarity=str(candidate.get("polarity") or "positive"),
                    modality=str(candidate.get("modality") or "asserted"),
                    temporal=str(candidate.get("temporal") or "current"),
                    source_span=str(candidate.get("sourceSpan") or "") or None,
                    valid_until=str(candidate.get("validUntil") or "") or None,
                    created_at=timestamp,
                )
                structured_correction = bool(
                    candidate.get("modality") == "corrected"
                    and candidate.get("subject")
                    and candidate.get("predicate")
                )
                if structured_correction:
                    candidate_subject = self.ports.normalize_slot(candidate.get("subject"))
                    candidate_predicate = self.ports.normalize_slot(candidate.get("predicate"))
                    candidate_object = self.ports.normalize_slot(candidate.get("object"))
                    records = [
                        replace(item, superseded_by=record.memory_id)
                        if item.active
                        and item.memory_id != record.memory_id
                        and self.ports.normalize_slot(item.subject) == candidate_subject
                        and self.ports.normalize_slot(item.predicate) == candidate_predicate
                        and (
                            (
                                candidate.get("cardinality") == "single"
                                and self.ports.normalize_slot(item.object_value) != candidate_object
                            )
                            or (
                                self.ports.normalize_slot(item.object_value) == candidate_object
                                and item.polarity != str(candidate.get("polarity") or "positive")
                            )
                        )
                        else item
                        for item in records
                    ]
                by_content[normalized] = len(records)
                if all(candidate_slot[:3]):
                    by_slot[candidate_slot] = len(records)
                records.append(record)
            return self.ports.write_records(records, settings=settings)
