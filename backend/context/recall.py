"""长期记忆与会话摘要召回。存储归入口所有；本模块独占检索缓存。

NeedGate 在任何持久记录或代际读取前执行。客户端 memory 列表只是展示缓存，
不能恢复已经撤销的记录。认知抑制在排序前及提示渲染时都生效。
"""
from __future__ import annotations

import math
import re
import threading
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from agent.cognitive_context import (
    candidate_is_preferred, candidate_is_suppressed, effective_context_controls,
    effective_memory_retrieval_query, scope_allows_archive,
)
from agent.fingerprints import runtime_fingerprint
from agent.memory_architecture import (
    MemoryNeed, MemoryRecord, assess_memory_need, memory_usage_decision,
    metadata_retrieval_score,
)
from agent.semantic_memory_retrieval import SemanticMemoryRuntime
from agent.turn_context import conversation_id_for_epoch
from backend.chat.parsing import parse_json_object
from backend.common.text import unique_strings
from backend.common.values import safe_nonnegative_int


RELEVANT_MEMORY_LIMIT = 10


class MemoryStore(Protocol):
    """只在召回分支触发的持久存储入口，路径与 scope 仍由应用决定。"""

    def read_records(self, settings: dict[str, Any]) -> list[MemoryRecord]: ...
    def path(self, settings: dict[str, Any] | None) -> Path: ...
    def generation(self, settings: dict[str, Any] | None) -> int: ...
    def clear_generation(self, settings: dict[str, Any] | None) -> int: ...
    def scope(self) -> str: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def memory_keywords(text: str) -> set[str]:
    value = str(text or "").lower()
    chunks = re.findall(r"[a-z0-9_+\-.#]{2,}|[\u4e00-\u9fff]{2,}", value)
    keywords: set[str] = set()
    for chunk in chunks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
            keywords.add(chunk)
            for size in (2, 3):
                if len(chunk) >= size:
                    keywords.update(chunk[i:i + size] for i in range(len(chunk) - size + 1))
        else:
            keywords.add(chunk)
    return {item for item in keywords if item.strip()}


def retrieval_overlap_score(query_terms: set[str], candidate_terms: set[str]) -> float:
    """Score lexical overlap without treating one shared Chinese bigram as a topic match."""
    shared = query_terms & candidate_terms
    strong = {item for item in shared if len(item) >= 3}
    bigrams = {item for item in shared if len(item) == 2}
    if strong:
        return len(strong) * 2.0 + min(3, len(bigrams)) * 0.35
    if len(bigrams) >= 2:
        return len(bigrams) * 0.7
    # Examples such as 世界杯 vs 世界线 share only "世界". A single
    # two-character overlap is too ambiguous to activate long-term memory.
    return 0.0


RETRIEVAL_GENERIC_TERMS = {
    "这个", "那个", "这些", "那些", "之前", "以前", "过去", "曾经",
    "记得", "还记", "说过", "提过", "给你", "用户", "助手", "我们",
    "怎么", "什么", "一下", "看看", "事情", "问题", "感觉", "相关",
    "说的", "的是", "前的", "此前", "的那", "那个", "个女", "之前的",
    "记得给", "还记得", "给你说", "你说的", "世界",
}


def retrieval_content_terms(text: str) -> set[str]:
    """Remove deictic recall boilerplate before topic matching."""

    return {
        item for item in memory_keywords(text)
        if item not in RETRIEVAL_GENERIC_TERMS
    }


def corpus_aware_overlap_score(
    query_terms: set[str],
    candidate_terms: set[str],
    corpus_terms: Sequence[set[str]],
) -> float:
    """Allow one rare, meaningful Chinese noun to retrieve an old episode.

    The legacy scorer required two matching bigrams.  That blocked queries
    such as ``那个女生`` because ``那个`` is generic and ``女生`` was the only
    real topic term.  Corpus frequency supplies the missing evidence without
    letting a common fragment such as ``世界`` join unrelated topics.
    """

    direct = retrieval_overlap_score(query_terms, candidate_terms)
    if direct > 0:
        return direct
    shared_bigrams = {
        item for item in query_terms & candidate_terms
        if len(item) == 2 and item not in RETRIEVAL_GENERIC_TERMS
    }
    if not shared_bigrams:
        return 0.0
    corpus_size = max(1, len(corpus_terms))
    rare_scores = []
    for term in shared_bigrams:
        document_frequency = sum(term in document for document in corpus_terms)
        if document_frequency <= max(2, corpus_size // 8):
            rare_scores.append(1.0 + math.log((corpus_size + 1) / (document_frequency + 1)))
    return max(rare_scores, default=0.0)


def normalized_memory_slot(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


MEMORY_QUERY_SLOT_CUES: dict[str, tuple[str, ...]] = {
    "preference": (
        "喜欢什么", "爱喝什么", "喝什么", "吃什么", "口味", "偏好",
        "讨厌什么", "不喜欢什么",
    ),
    "name": ("名字", "姓名", "怎么称呼", "叫什么"),
    "future_routine": ("以后怎么", "习惯", "每天", "每周", "例行"),
    "shared_reference": ("暗号", "共同称呼", "那个梗", "老梗", "代号"),
    "relationship_commitment": (
        "什么关系", "情侣", "恋人", "伴侣", "在一起", "交往",
    ),
    "episode": ("哪次", "那次", "上次", "之前一起", "共同经历"),
}


def memory_query_slots(text: str) -> set[str]:
    compact = normalized_memory_slot(text)
    return {
        slot
        for slot, cues in MEMORY_QUERY_SLOT_CUES.items()
        if any(normalized_memory_slot(cue) in compact for cue in cues)
    }


def record_memory_slot(record: MemoryRecord | None, content: str) -> str:
    if record is not None and record.predicate:
        predicate = normalized_memory_slot(record.predicate)
        if predicate in {"name", "姓名", "名字", "称呼"}:
            return "name"
        return predicate
    compact = normalized_memory_slot(content)
    if "喜欢" in compact or "讨厌" in compact or "偏好" in compact:
        return "preference"
    if "名字" in compact or "姓名" in compact:
        return "name"
    if record is not None and record.memory_type == "shared_reference":
        return "shared_reference"
    if record is not None and record.memory_type == "relationship_state":
        return "relationship_commitment"
    if record is not None and record.memory_type in {"episode", "episodic_archive"}:
        return "episode"
    return ""


MEMORY_CHARACTER_STOP = set("用户我的你他她它是了的和与在有还记得什么怎么以前过去之前")


def memory_distinctive_characters(text: str) -> set[str]:
    return {
        character
        for character in str(text or "")
        if "\u4e00" <= character <= "\u9fff"
        and character not in MEMORY_CHARACTER_STOP
    }


def conversation_summary_search_text(text: str) -> str:
    """Project our serialized artifact onto prose, not audit IDs, for retrieval.

    These are internal format tags, not linguistic intent rules. The original
    artifact (including provenance and protected claims) remains unchanged.
    """
    lines = []
    for line in str(text or "").splitlines():
        if line.startswith((
            "[compaction:", "[source_turns]", "[protection_reasons]",
            "[protected_claim]", "[required_acts]", "[unresolved]",
        )):
            continue
        while line.startswith("[") and "]" in line:
            line = line.partition("]")[2].lstrip()
        if line:
            lines.append(line)
    # A protected-only artifact has no narrative; use its claim propositions
    # without embedding IDs or provenance field names.
    if not lines:
        for line in str(text or "").splitlines():
            if line.startswith("[protected_claim] "):
                claim = parse_json_object(line.partition(" ")[2]) or {}
                proposition = str(claim.get("text") or claim.get("proposition") or "")
                if proposition:
                    lines.append(proposition)
    return "\n".join(lines)


def retrieved_memory_prompt(
    memories: Sequence[str],
    records: Sequence[MemoryRecord],
    need: MemoryNeed,
    *,
    cognitive_state: Mapping[str, Any] | None = None,
) -> str:
    if not memories:
        return (
            f"[长期记忆检索] MemoryNeed={need.value}；本轮没有可靠匹配项。"
            "禁止为了填充回答而转去旧话题。"
        )
    by_content = {
        re.sub(r"[\W_]+", "", item.content.casefold(), flags=re.UNICODE): item
        for item in records
        if item.active and item.content.strip()
    }
    lines = [
        f"[长期记忆检索] MemoryNeed={need.value}。检索命中不等于允许主动提起；逐条遵守用途标签："
    ]
    for content in memories:
        record = by_content.get(
            re.sub(r"[\W_]+", "", str(content).casefold(), flags=re.UNICODE)
        )
        if record is not None and candidate_is_suppressed(record, cognitive_state):
            continue
        if record is None:
            lines.append(f"- [仅作隐含背景] {content}")
            continue
        decision = memory_usage_decision(record, need)
        if not decision.use_for_reasoning:
            continue
        label = (
            "可在确有帮助时自然回忆"
            if decision.allow_explicit_mention else
            "仅作静默约束，不主动说出记忆来源"
        )
        lines.append(
            f"- [{record.memory_type}; {label}; confidence={record.confidence:.2f}] {content}"
        )
    return "\n".join(lines)


class MemoryRecallService:
    """持有唯一检索缓存，复用入口的语义模型而不重新构造它。"""

    def __init__(
        self,
        store: MemoryStore,
        semantic_runtime: SemanticMemoryRuntime,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.store = store
        self.semantic_runtime = semantic_runtime
        self.clock = clock
        self._cache_lock = threading.RLock()
        self._cache: dict[tuple[Any, ...], list[str]] = {}

    def invalidate(self) -> None:
        """记忆代际变化时同时清理结果和共享语义模型的文档缓存。"""
        with self._cache_lock:
            self._cache.clear()
        self.semantic_runtime.clear_document_cache()

    def relevant_memories(
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
        if not memories:
            return []
        if need in {MemoryNeed.NONE, MemoryNeed.RECENT_ONLY}:
            return []
        effective_limit = max(
            0,
            min(RELEVANT_MEMORY_LIMIT, int(limit if limit is not None else RELEVANT_MEMORY_LIMIT)),
        )
        if effective_limit <= 0:
            return []
        # Long-term recall must be driven by what the user is asking now. Assistant
        # replies may contain guesses or unrelated tangents, so using the whole
        # transcript as a retrieval query can reinforce the assistant's own error.
        controls = effective_context_controls(cognitive_state)
        recent_user_text = "\n".join(
            str(item.get("content") or "")
            for item in messages[-8:]
            if isinstance(item, dict) and item.get("role") == "user"
        )
        explicit_query = (
            str(retrieval_query).strip()
            if retrieval_query is not None
            else str(controls.get("recallQuery") or "").strip()
        )
        query_text = (
            explicit_query
            if explicit_query
            else f"{user_message}\n{recent_user_text}".strip()
        )
        query_terms = retrieval_content_terms(query_text)
        query_slots = memory_query_slots(query_text)
        query_characters = memory_distinctive_characters(query_text)
        recent_transcript = "\n".join(
            str(item.get("content") or "")
            for item in messages[-12:]
            if isinstance(item, dict)
        )
        recent_compact = re.sub(r"[\W_]+", "", recent_transcript.lower(), flags=re.UNICODE)

        def already_visible(memory: str) -> bool:
            compact = re.sub(r"[\W_]+", "", memory.lower(), flags=re.UNICODE)
            if not compact or not recent_compact:
                return False
            if len(compact) >= 8 and compact in recent_compact:
                return True
            return (
                len(compact) >= 24
                and SequenceMatcher(None, compact, recent_compact[-2400:]).ratio() >= 0.9
            )

        record_by_content = {
            re.sub(r"[\W_]+", "", item.content.casefold(), flags=re.UNICODE): item
            for item in (records or ())
            if item.active and item.content.strip()
        }
        memory_file = self.store.path(settings)
        generation = self.store.generation(settings)
        clear_generation = self.store.clear_generation(settings)
        metadata_fingerprint = runtime_fingerprint(
            "\n".join(
                f"{item.memory_id}|{item.version}|{item.memory_type}|{item.content}|"
                f"{item.confidence:.6f}|{int(item.user_pinned)}|{item.last_confirmed_at}|"
                f"{item.valid_until}|{item.superseded_by}|{item.subject}|{item.predicate}|"
                f"{item.object_value}|{item.polarity}|{item.modality}|{item.temporal}"
                for item in (records or ())
            )
        )
        # 同一句查询在不同存储作用域、代际或小时不能复用旧排序。
        cache_key = (
            self.store.scope(),
            str(memory_file),
            generation,
            clear_generation,
            self.clock().strftime("%Y-%m-%dT%H"),
            metadata_fingerprint,
            runtime_fingerprint(query_text),
            (need.value if isinstance(need, MemoryNeed) else "legacy"),
            effective_limit,
            tuple(memories),
            tuple(controls["preferredIds"]),
            tuple(controls["suppressedIds"]),
            controls["recallScope"],
        )
        with self._cache_lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return list(cached)

        memory_term_sets = [retrieval_content_terms(item) for item in memories]
        compact_current_query = normalized_memory_slot(query_text)
        vague_recall_only = compact_current_query in {
            "记得吗", "你记得吗", "还记得吗", "你还记得吗", "想起来了吗",
        }
        dense_scores = (
            self.semantic_runtime.similarities(query_text, memories)
            if not vague_recall_only else None
        )
        dense_ceiling = max(dense_scores, default=-1.0) if dense_scores is not None else -1.0
        scored: list[tuple[float, int, str]] = []
        for index, memory in enumerate(memories):
            if already_visible(memory):
                continue
            terms = memory_term_sets[index]
            overlap = corpus_aware_overlap_score(query_terms, terms, memory_term_sets)
            exact_bonus = 2.5 if memory and memory in query_text else 0
            record = record_by_content.get(
                re.sub(r"[\W_]+", "", memory.casefold(), flags=re.UNICODE)
            )
            if record is not None and candidate_is_suppressed(record, cognitive_state):
                continue
            preferred_match = bool(
                record is not None and candidate_is_preferred(record, cognitive_state)
            )
            record_slot = record_memory_slot(record, memory)
            slot_bonus = 4.5 if record_slot and record_slot in query_slots else 0.0
            object_text = record.object_value if record is not None else memory
            character_overlap = len(
                query_characters & memory_distinctive_characters(object_text or "")
            )
            object_bonus = min(2.0, character_overlap * 0.55) if slot_bonus else 0.0
            relationship_authority = bool(
                record is not None
                and record.memory_type == "relationship_state"
                and need == MemoryNeed.RELATIONSHIP
            )
            dense_score = (
                float(dense_scores[index])
                if dense_scores is not None and index < len(dense_scores) else -1.0
            )
            semantic_match = bool(
                dense_score >= 0.54
                and dense_score >= dense_ceiling - 0.08
            )
            if (
                overlap <= 0
                and exact_bonus <= 0
                and slot_bonus <= 0
                and not relationship_authority
                and not semantic_match
                and not preferred_match
            ):
                continue
            # Typed records already carry last_confirmed_at decay in their metadata.
            # An index bonus used to favor index 0, which is usually the oldest row.
            recency_bonus = max(0, 0.6 - index * 0.01) if record is None else 0.0
            semantic_bonus = (
                max(0.0, (dense_score - 0.45) * 9.0)
                if semantic_match else 0.0
            )
            lexical_score = (
                overlap * 1.8
                + exact_bonus
                + slot_bonus
                + object_bonus
                + recency_bonus
                + semantic_bonus
                + (6.0 if relationship_authority else 0.0)
                + (8.0 if preferred_match else 0.0)
            )
            score = (
                metadata_retrieval_score(
                    record,
                    lexical_relevance=lexical_score,
                    need=need,
                )
                if record is not None and isinstance(need, MemoryNeed)
                else lexical_score
            )
            if score <= 0:
                continue
            scored.append((score, index, memory))
        if not scored:
            with self._cache_lock:
                self._cache[cache_key] = []
            return []
        selected = [
            item for _, _, item
            in sorted(scored, key=lambda row: (-row[0], row[1]))[:effective_limit]
        ]
        selected = unique_strings(selected)
        with self._cache_lock:
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = list(selected)
        return selected

    def retrieve_turn_memories(
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
        """Recall only authoritative active records after the local need gate.

        ``supplied_memories`` is retained in the signature for compatibility with
        older clients, but it is deliberately not treated as a retrieval source.
        The browser's memory list is a presentation cache, not an authority: after
        a correction, clear, expiry, or supersession it can be stale and must not
        be able to resurrect a tombstoned fact.
        """

        need = assess_memory_need(user_message, messages)
        controls = effective_context_controls(cognitive_state)
        if controls["recallQuery"] or controls["preferredIds"] or controls["recallScope"] == "long_term":
            need = MemoryNeed.MIXED
        if need in {MemoryNeed.NONE, MemoryNeed.RECENT_ONLY}:
            # A real NeedGate means zero long-term storage work, not merely an
            # empty ranking after reading the whole memory file.
            return [], [], need
        # The authoritative store is already user/storage scoped.  Apply the
        # staged suppression before ranking as well as at prompt rendering so a
        # stale candidate cannot be reintroduced by either path.
        records = [
            item for item in self.store.read_records(settings)
            if not candidate_is_suppressed(item, cognitive_state)
        ]
        active_contents = [
            item.content for item in records if item.active and item.content.strip()
        ]
        all_memories = unique_strings(active_contents)
        # server 公共检索入口可被旧调用方补丁；默认独立模块仍可直接调用。
        selected = (rank_memories or self.relevant_memories)(
            all_memories,
            user_message,
            messages,
            limit=limit,
            need=need,
            records=records,
            settings=settings,
            retrieval_query=(
                effective_memory_retrieval_query(
                    user_message,
                    messages,
                    cognitive_state,
                    context_state,
                )
                if cognitive_state is not None
                else None
            ),
            cognitive_state=cognitive_state,
        )
        return selected, records, need

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
        del allow_mixed
        controls = effective_context_controls(cognitive_state)
        if (
            need in {MemoryNeed.NONE, MemoryNeed.RECENT_ONLY}
            and not scope_allows_archive(cognitive_state)
        ):
            return []
        current_epoch = safe_nonnegative_int(context.get("conversationEpoch"))
        active_conversation_id = str(
            context.get("conversationId") or conversation_id_for_epoch(current_epoch)
        ).strip()
        summary_items: list[dict[str, Any]] = []
        for item in context.get("summaries", []):
            if not isinstance(item, dict):
                continue
            if item.get("status") == "pending" or bool(item.get("pendingCompaction")):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            item_epoch_value = item.get("conversationEpoch", item.get("contextEpoch"))
            if item_epoch_value is not None and safe_nonnegative_int(item_epoch_value) != current_epoch:
                continue
            item_conversation_id = str(item.get("conversationId") or "").strip()
            if item_conversation_id and item_conversation_id != active_conversation_id:
                continue
            # An identity-less summary from a previous cleared generation cannot
            # be proven current.  Generation zero retains legacy artifacts whose
            # original schema had no conversation identity.
            if (
                current_epoch > 0
                and item_epoch_value is None
                and not item_conversation_id
            ):
                continue
            if candidate_is_suppressed(item, cognitive_state):
                continue
            summary_items.append(item)
        if not summary_items:
            return []
        explicit_query = (
            str(retrieval_query).strip()
            if retrieval_query is not None
            else str(controls.get("recallQuery") or "").strip()
        )
        query_text = explicit_query or str(user_message or "").strip()
        summaries = [str(item.get("text") or "").strip() for item in summary_items]
        query_terms = retrieval_content_terms(query_text)
        search_texts = [conversation_summary_search_text(item) for item in summaries]
        summary_term_sets = [retrieval_content_terms(item) for item in search_texts]
        dense_scores = (
            None
            if normalized_memory_slot(query_text) in {
                "记得吗", "你记得吗", "还记得吗", "你还记得吗", "想起来了吗",
            }
            else self.semantic_runtime.similarities(query_text, search_texts)
        )
        dense_ceiling = max(dense_scores, default=-1.0) if dense_scores is not None else -1.0
        scored: list[tuple[float, int, str]] = []
        for index, summary in enumerate(summaries):
            overlap = corpus_aware_overlap_score(
                query_terms,
                summary_term_sets[index],
                summary_term_sets,
            )
            dense_score = (
                float(dense_scores[index])
                if dense_scores is not None and index < len(dense_scores) else -1.0
            )
            semantic_match = dense_score >= 0.54 and dense_score >= dense_ceiling - 0.08
            preferred_match = candidate_is_preferred(summary_items[index], cognitive_state)
            if overlap <= 0 and not semantic_match and not preferred_match:
                continue
            recency = index / max(1, len(summaries) - 1)
            semantic_bonus = max(0.0, (dense_score - 0.45) * 9.0) if semantic_match else 0.0
            preferred_bonus = 8.0 if preferred_match else 0.0
            scored.append((overlap * 2.0 + semantic_bonus + preferred_bonus + recency, index, summary))
        if not scored:
            # An explicit request to remember is not evidence for any particular
            # old summary.  Returning the latest arbitrary rows here used to turn
            # retrieval failure into confident topic drift.
            return []
        return unique_strings([
            item for _, _, item
            in sorted(scored, key=lambda row: (-row[0], -row[1]))[:4]
        ])
