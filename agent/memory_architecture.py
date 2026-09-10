"""Versioned records shared by the four engineering memory domains."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import math
import re
from typing import Any, Literal, Mapping, Sequence
import uuid

from .fingerprints import legacy_persistent_fingerprint


MemoryDomain = Literal[
    "task_state",
    "user_memory",
    "correction",
    "episode",
    "shared_reference",
    "episodic_archive",
    "relationship_state",
]


class MemoryNeed(str, Enum):
    NONE = "none"
    RECENT_ONLY = "recent_only"
    PERSONAL = "personal"
    EPISODIC = "episodic"
    SHARED_REFERENCE = "shared_reference"
    RELATIONSHIP = "relationship"
    MIXED = "mixed"


@dataclass(frozen=True)
class MemoryUsageDecision:
    use_for_reasoning: bool
    allow_explicit_mention: bool
    mention_style: str = "implicit"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"none", "null"}:
        return None
    return text


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    memory_type: MemoryDomain
    content: str
    source_turn_ids: tuple[str, ...]
    created_at: str
    last_confirmed_at: str
    subject: str | None = None
    predicate: str | None = None
    object_value: str | None = None
    polarity: str = "positive"
    modality: str = "asserted"
    temporal: str = "current"
    source_span: str | None = None
    valid_until: str | None = None
    superseded_by: str | None = None
    confidence: float = 1.0
    user_pinned: bool = False
    version: int = 1
    previous_version_id: str | None = None

    @property
    def active(self) -> bool:
        if self.superseded_by:
            return False
        if not self.valid_until:
            return True
        try:
            deadline = datetime.fromisoformat(self.valid_until)
            current = datetime.now(timezone.utc)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return deadline >= current
        except ValueError:
            # Preserve legacy records with malformed optional metadata; the
            # validator can surface the metadata issue without silently
            # deleting otherwise recoverable user content.
            return True

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemoryRecord":
        memory_type = str(value.get("memoryType") or value.get("memory_type") or "user_memory")
        if memory_type not in {
            "task_state", "user_memory", "correction", "episode",
            "shared_reference", "episodic_archive", "relationship_state",
        }:
            memory_type = "user_memory"
        source_values = value.get("sourceTurnIds", value.get("source_turn_ids", ()))
        if isinstance(source_values, str):
            source_values = (source_values,)
        elif not isinstance(source_values, Sequence):
            source_values = ()
        content = str(value.get("content") or "")
        memory_id = str(value.get("memoryId") or value.get("memory_id") or "").strip()
        if not memory_id:
            digest = legacy_persistent_fingerprint(
                _normalized_content(content), length=20
            )
            memory_id = "mem_legacy_" + digest
        return cls(
            memory_id=memory_id,
            memory_type=memory_type,  # type: ignore[arg-type]
            content=content,
            source_turn_ids=tuple(str(item) for item in source_values if str(item)),
            created_at=str(value.get("createdAt") or value.get("created_at") or now_iso()),
            last_confirmed_at=str(value.get("lastConfirmedAt") or value.get("last_confirmed_at") or now_iso()),
            subject=_optional_text(value.get("subject")),
            predicate=_optional_text(value.get("predicate")),
            object_value=_optional_text(value.get("object", value.get("object_value"))),
            polarity=(
                str(value.get("polarity") or "positive").strip().lower()
                if str(value.get("polarity") or "positive").strip().lower()
                in {"positive", "negative"}
                else "positive"
            ),
            modality=(
                str(value.get("modality") or "asserted").strip().lower()
                if str(value.get("modality") or "asserted").strip().lower()
                in {"asserted", "requested", "corrected"}
                else "asserted"
            ),
            temporal=(
                str(value.get("temporal") or "current").strip().lower()
                if str(value.get("temporal") or "current").strip().lower()
                in {"current", "habitual", "future", "historical"}
                else "current"
            ),
            source_span=_optional_text(value.get("sourceSpan", value.get("source_span"))),
            valid_until=_optional_text(value.get("validUntil", value.get("valid_until"))),
            superseded_by=_optional_text(value.get("supersededBy", value.get("superseded_by"))),
            confidence=max(0.0, min(1.0, _safe_float(value.get("confidence"), 1.0))),
            user_pinned=_safe_bool(value.get("userPinned", value.get("user_pinned", False))),
            version=max(1, _safe_int(value.get("version"), 1)),
            previous_version_id=_optional_text(
                value.get("previousVersionId", value.get("previous_version_id"))
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "memoryId": self.memory_id,
            "memoryType": self.memory_type,
            "content": self.content,
            "sourceTurnIds": list(self.source_turn_ids),
            "createdAt": self.created_at,
            "lastConfirmedAt": self.last_confirmed_at,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object_value,
            "polarity": self.polarity,
            "modality": self.modality,
            "temporal": self.temporal,
            "sourceSpan": self.source_span,
            "validUntil": self.valid_until,
            "supersededBy": self.superseded_by,
            "confidence": round(self.confidence, 6),
            "userPinned": self.user_pinned,
            "version": self.version,
            "previousVersionId": self.previous_version_id,
            "active": self.active,
        }


def _normalized_content(content: str) -> str:
    return re.sub(r"[\W_]+", "", str(content or "").casefold(), flags=re.UNICODE)


def new_memory_record(
    content: str,
    *,
    memory_type: MemoryDomain = "user_memory",
    source_turn_ids: Sequence[str] = (),
    confidence: float = 1.0,
    user_pinned: bool = False,
    subject: str | None = None,
    predicate: str | None = None,
    object_value: str | None = None,
    polarity: str = "positive",
    modality: str = "asserted",
    temporal: str = "current",
    source_span: str | None = None,
    valid_until: str | None = None,
    created_at: str | None = None,
    deterministic_legacy_id: bool = False,
) -> MemoryRecord:
    timestamp = created_at or now_iso()
    if deterministic_legacy_id:
        digest = legacy_persistent_fingerprint(
            _normalized_content(content), length=20
        )
        memory_id = "mem_legacy_" + digest
    else:
        memory_id = "mem_" + uuid.uuid4().hex
    return MemoryRecord(
        memory_id=memory_id,
        memory_type=memory_type,
        content=str(content).strip(),
        source_turn_ids=tuple(dict.fromkeys(str(item) for item in source_turn_ids if str(item))),
        created_at=timestamp,
        last_confirmed_at=timestamp,
        subject=_optional_text(subject),
        predicate=_optional_text(predicate),
        object_value=_optional_text(object_value),
        polarity=polarity if polarity in {"positive", "negative"} else "positive",
        modality=(
            modality if modality in {"asserted", "requested", "corrected"}
            else "asserted"
        ),
        temporal=(
            temporal if temporal in {"current", "habitual", "future", "historical"}
            else "current"
        ),
        source_span=_optional_text(source_span),
        valid_until=_optional_text(valid_until),
        confidence=max(0.0, min(1.0, float(confidence))),
        user_pinned=user_pinned,
    )


def confirm_or_add_memories(
    records: Sequence[MemoryRecord],
    contents: Sequence[str],
    *,
    source_turn_id: str = "",
    confirmed_at: str | None = None,
) -> tuple[MemoryRecord, ...]:
    timestamp = confirmed_at or now_iso()
    result = list(records)
    active_by_content = {
        _normalized_content(item.content): index
        for index, item in enumerate(result)
        if item.active and _normalized_content(item.content)
    }
    for raw_content in contents:
        content = str(raw_content).strip()
        normalized = _normalized_content(content)
        if not normalized:
            continue
        existing_index = active_by_content.get(normalized)
        if existing_index is not None:
            existing = result[existing_index]
            source_ids = tuple(dict.fromkeys((
                *existing.source_turn_ids,
                *((source_turn_id,) if source_turn_id else ()),
            )))
            result[existing_index] = replace(
                existing,
                last_confirmed_at=timestamp,
                source_turn_ids=source_ids,
                confidence=max(existing.confidence, 0.95),
            )
            continue
        record = new_memory_record(
            content,
            source_turn_ids=(source_turn_id,) if source_turn_id else (),
            created_at=timestamp,
        )
        active_by_content[normalized] = len(result)
        result.append(record)
    return tuple(result)


def version_memory_record(
    records: Sequence[MemoryRecord],
    memory_id: str,
    new_content: str,
    *,
    source_turn_id: str = "",
    changed_at: str | None = None,
) -> tuple[MemoryRecord, ...]:
    timestamp = changed_at or now_iso()
    result = list(records)
    for index, existing in enumerate(result):
        if existing.memory_id != memory_id or not existing.active:
            continue
        replacement = new_memory_record(
            new_content,
            memory_type=existing.memory_type,
            source_turn_ids=(*existing.source_turn_ids, *((source_turn_id,) if source_turn_id else ())),
            confidence=existing.confidence,
            user_pinned=existing.user_pinned,
            subject=existing.subject,
            predicate=existing.predicate,
            object_value=existing.object_value,
            polarity=existing.polarity,
            modality=existing.modality,
            temporal=existing.temporal,
            source_span=existing.source_span,
            created_at=timestamp,
        )
        replacement = replace(
            replacement,
            version=existing.version + 1,
            previous_version_id=existing.memory_id,
        )
        result[index] = replace(existing, superseded_by=replacement.memory_id)
        result.append(replacement)
        return tuple(result)
    raise KeyError("active memory record not found")


def reconcile_memory_contents(
    records: Sequence[MemoryRecord],
    contents: Sequence[str],
    *,
    source_turn_id: str = "",
    changed_at: str | None = None,
) -> tuple[MemoryRecord, ...]:
    """Apply a user-visible list edit without erasing the audit history.

    Exact content matches retain their identity even if reordered. Remaining
    old/new rows are paired in display order as edits; extra old rows become
    deletion tombstones and extra new rows become fresh records.
    """

    timestamp = changed_at or now_iso()
    result = list(records)
    requested = [str(item).strip() for item in contents if str(item).strip()]
    active_indexes = [index for index, item in enumerate(result) if item.active]
    active_by_content: dict[str, list[int]] = {}
    for index in active_indexes:
        normalized = _normalized_content(result[index].content)
        if normalized:
            active_by_content.setdefault(normalized, []).append(index)

    matched_old: set[int] = set()
    matched_new: set[int] = set()
    for new_index, content in enumerate(requested):
        candidates = active_by_content.get(_normalized_content(content), [])
        old_index = next((item for item in candidates if item not in matched_old), None)
        if old_index is None:
            continue
        existing = result[old_index]
        source_ids = tuple(dict.fromkeys((
            *existing.source_turn_ids,
            *((source_turn_id,) if source_turn_id else ()),
        )))
        result[old_index] = replace(
            existing,
            last_confirmed_at=timestamp,
            source_turn_ids=source_ids,
        )
        matched_old.add(old_index)
        matched_new.add(new_index)

    unmatched_old = [index for index in active_indexes if index not in matched_old]
    unmatched_new = [index for index in range(len(requested)) if index not in matched_new]
    paired = min(len(unmatched_old), len(unmatched_new))
    for offset in range(paired):
        old_index = unmatched_old[offset]
        existing = result[old_index]
        replacement = new_memory_record(
            requested[unmatched_new[offset]],
            memory_type=existing.memory_type,
            source_turn_ids=(
                *existing.source_turn_ids,
                *((source_turn_id,) if source_turn_id else ()),
            ),
            confidence=existing.confidence,
            user_pinned=existing.user_pinned,
            subject=existing.subject,
            predicate=existing.predicate,
            object_value=None,
            polarity=existing.polarity,
            modality=existing.modality,
            temporal=existing.temporal,
            source_span=None,
            created_at=timestamp,
        )
        replacement = replace(
            replacement,
            version=existing.version + 1,
            previous_version_id=existing.memory_id,
        )
        result[old_index] = replace(existing, superseded_by=replacement.memory_id)
        result.append(replacement)

    for old_index in unmatched_old[paired:]:
        existing = result[old_index]
        result[old_index] = replace(
            existing,
            superseded_by="deleted:" + timestamp,
            last_confirmed_at=timestamp,
        )

    for new_index in unmatched_new[paired:]:
        result.append(new_memory_record(
            requested[new_index],
            source_turn_ids=(source_turn_id,) if source_turn_id else (),
            created_at=timestamp,
        ))
    return tuple(result)


def retrieval_weight(record: MemoryRecord, *, at: str | None = None, half_life_days: float = 180.0) -> float:
    """Decay retrieval weight without deleting or mutating the record."""

    if not record.active:
        return 0.0
    try:
        current = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
        confirmed = datetime.fromisoformat(record.last_confirmed_at)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if confirmed.tzinfo is None:
            confirmed = confirmed.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (current - confirmed).total_seconds() / 86400.0)
    except ValueError:
        age_days = 0.0
    decay = math.pow(0.5, age_days / max(1.0, float(half_life_days)))
    pin_bonus = 1.15 if record.user_pinned else 1.0
    return round(max(0.0, min(1.15, record.confidence * decay * pin_bonus)), 6)


_RECENT_ONLY_RE = re.compile(
    r"^(?:嗯+|哦+|对(?:啊|呀|的)?|是的|不是|然后呢|继续|这样啊|还真是|那个呢|行|好)$",
    re.I,
)
def assess_memory_need(
    user_message: str,
    recent_messages: Sequence[Mapping[str, Any]] = (),
) -> MemoryNeed:
    """Classify recall need locally; this must never add a synchronous LLM call."""

    text = re.sub(r"\s+", "", str(user_message or ""))
    if not text:
        return MemoryNeed.NONE
    if _RECENT_ONLY_RE.fullmatch(text):
        return MemoryNeed.RECENT_ONLY
    # Natural recall intent is far too broad for a finite keyword inventory.
    # Only an empty turn and a tiny exact acknowledgement set may suppress
    # long-term retrieval; every other real request reaches relevance ranking,
    # where unrelated memories still score out normally.
    del recent_messages
    return MemoryNeed.MIXED


def memory_type_matches_need(record: MemoryRecord, need: MemoryNeed) -> bool:
    if need in {MemoryNeed.NONE, MemoryNeed.RECENT_ONLY}:
        return False
    if need == MemoryNeed.MIXED:
        return True
    if need == MemoryNeed.PERSONAL:
        return record.memory_type in {"user_memory", "correction", "task_state"}
    if need == MemoryNeed.EPISODIC:
        return record.memory_type in {"episode", "episodic_archive", "correction", "user_memory"}
    if need == MemoryNeed.SHARED_REFERENCE:
        return record.memory_type in {"shared_reference", "episode", "episodic_archive", "correction"}
    if need == MemoryNeed.RELATIONSHIP:
        return record.memory_type in {"relationship_state", "episode", "correction", "user_memory"}
    return True


def memory_usage_decision(record: MemoryRecord, need: MemoryNeed) -> MemoryUsageDecision:
    """Retrieval is not permission to drag a memory into the spoken reply."""

    if not record.active or need in {MemoryNeed.NONE, MemoryNeed.RECENT_ONLY}:
        return MemoryUsageDecision(False, False, "none")
    explicit = need in {
        MemoryNeed.EPISODIC,
        MemoryNeed.SHARED_REFERENCE,
        MemoryNeed.RELATIONSHIP,
        MemoryNeed.MIXED,
    }
    if record.memory_type == "correction":
        return MemoryUsageDecision(True, False, "silent_constraint")
    if record.memory_type == "relationship_state":
        return MemoryUsageDecision(
            True,
            need in {MemoryNeed.RELATIONSHIP, MemoryNeed.MIXED},
            "relationship_constraint",
        )
    if record.memory_type in {"episode", "episodic_archive", "shared_reference"}:
        return MemoryUsageDecision(True, explicit, "natural_recall" if explicit else "implicit")
    return MemoryUsageDecision(True, explicit and record.user_pinned, "implicit")


def metadata_retrieval_score(
    record: MemoryRecord,
    *,
    lexical_relevance: float,
    need: MemoryNeed,
    at: str | None = None,
) -> float:
    if not record.active or not memory_type_matches_need(record, need):
        return 0.0
    if record.valid_until:
        try:
            current = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
            valid_until = datetime.fromisoformat(record.valid_until)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
            if valid_until < current:
                return 0.0
        except ValueError:
            pass
    score = max(0.0, float(lexical_relevance)) * retrieval_weight(record, at=at)
    if record.user_pinned:
        score *= 1.12
    if record.memory_type == "correction":
        # Corrections constrain matching facts, but they are not normally the
        # episode a user is asking to remember.  Boost them for personal or
        # relationship recall, not for a broad episodic query where an actual
        # episode record should rank first.
        score *= 0.95 if need == MemoryNeed.EPISODIC else 1.35
    elif record.memory_type == "shared_reference" and need in {
        MemoryNeed.SHARED_REFERENCE, MemoryNeed.MIXED,
    }:
        score *= 1.2
    elif record.memory_type in {"episode", "episodic_archive"} and need in {
        MemoryNeed.EPISODIC, MemoryNeed.MIXED,
    }:
        score *= 1.12
    return round(score, 8)
