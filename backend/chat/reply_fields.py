"""记忆、关系与主动指令的模型字段规范化。"""
from __future__ import annotations
from typing import Any, Mapping
from agent.proactive_control import PROACTIVE_DIRECTIVES
from backend.common.values import clamp_float, clamp_int


BOUNDARY_ASSESSMENT_KINDS = {
    "none",
    "ordinary_conflict",
    "severe_personal_insult",
    "sincere_apology",
}


RELATION_EVENT_SIGNALS = frozenset({
    "shared_routine", "positive", "distress", "repair",
})


RELATION_EVENT_COMMITMENTS = frozenset({
    "none", "proposed", "accepted", "refused", "ambiguous",
})


MEMORY_CANDIDATE_TYPES = {
    "user_memory", "task_state", "correction", "episode",
    "shared_reference", "relationship_state",
}


def normalize_boundary_assessment(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    kind = str(raw.get("kind") or raw.get("type") or "none").strip().lower()
    aliases = {
        "normal": "none",
        "criticism": "ordinary_conflict",
        "ordinary_argument": "ordinary_conflict",
        "personal_abuse": "severe_personal_insult",
        "personality_insult": "severe_personal_insult",
        "apology": "sincere_apology",
    }
    kind = aliases.get(kind, kind)
    if kind not in BOUNDARY_ASSESSMENT_KINDS:
        kind = "none"
    return {
        "kind": kind,
        "severity": clamp_int(raw.get("severity"), 0, 3, 0),
        "confidence": clamp_float(raw.get("confidence"), 0, 1, 0.5),
        "reason": str(raw.get("reason") or "")[:240],
    }


def normalize_relation_event(value: Any) -> dict[str, Any]:
    """Normalize the model's semantic event without reclassifying its words."""

    if not isinstance(value, Mapping):
        return {
            "signals": [], "commitment": "none", "confidence": 0.0,
            "evidence": [],
        }
    signals = list(dict.fromkeys(
        str(item).strip().lower()
        for item in (value.get("signals") if isinstance(value.get("signals"), list) else [])
        if str(item).strip().lower() in RELATION_EVENT_SIGNALS
    ))
    commitment = str(value.get("commitment") or "none").strip().lower()
    if commitment not in RELATION_EVENT_COMMITMENTS:
        commitment = "ambiguous"
    evidence: list[dict[str, str]] = []
    if isinstance(value.get("evidence"), list):
        for item in value["evidence"][:8]:
            if not isinstance(item, Mapping):
                continue
            speaker = str(item.get("speaker") or "").strip().lower()
            quote = str(item.get("quote") or "").strip()[:800]
            if speaker in {"user", "assistant"} and quote:
                evidence.append({"speaker": speaker, "quote": quote})
    return {
        "signals": signals,
        "commitment": commitment,
        "confidence": clamp_float(value.get("confidence"), 0, 1, 0.0),
        "evidence": evidence,
    }


def normalize_proactive_directive(value: Any) -> str:
    directive = str(value or "").strip().lower()
    return directive if directive in PROACTIVE_DIRECTIVES else "unchanged"


def normalize_memory_candidate(value: Any) -> dict[str, Any] | None:
    """Normalize a model/backend proposal without declaring it true.

    A candidate is deliberately distinct from a MemoryRecord: normalization
    only preserves provenance and slot metadata.  Grounding later decides
    whether the proposal is supported by the user's actual utterance.
    """

    if isinstance(value, str):
        content = value.strip()
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        content = str(value.get("content") or "").strip()
        raw = value
    else:
        return None
    if not content:
        return None
    raw_type = str(raw.get("type") or raw.get("domain") or "").strip()
    type_aliases = {
        "user_profile": "user_memory",
        "preference": "user_memory",
        "project": "task_state",
        "relationship": "relationship_state",
    }
    memory_type = type_aliases.get(raw_type, raw_type)
    if memory_type not in MEMORY_CANDIDATE_TYPES:
        memory_type = ""
    polarity = str(raw.get("polarity") or "positive").strip().lower()
    if polarity not in {"positive", "negative"}:
        polarity = "positive"
    modality = str(raw.get("modality") or "asserted").strip().lower()
    if modality not in {"asserted", "requested", "corrected"}:
        modality = "asserted"
    temporal = str(raw.get("temporal") or "current").strip().lower()
    if temporal not in {"current", "habitual", "future", "historical"}:
        temporal = "current"
    importance = clamp_float(raw.get("importance"), 0, 1, 0.72)
    cardinality = str(raw.get("cardinality") or "multi").strip().lower()
    if cardinality not in {"single", "multi"}:
        cardinality = "multi"
    should_save = bool(raw.get("should_save", raw.get("shouldSave", True)))
    source_turn_values = raw.get("sourceTurnIds", raw.get("source_turn_ids", ()))
    if not isinstance(source_turn_values, (list, tuple)):
        source_turn_values = ()
    evidence_values = raw.get("evidence")
    evidence: list[dict[str, str]] = []
    if isinstance(evidence_values, list):
        for item in evidence_values[:8]:
            if not isinstance(item, Mapping):
                continue
            turn_id = str(item.get("turnId") or item.get("turn_id") or "").strip()[:120]
            quote = str(item.get("quote") or "").strip()[:800]
            if turn_id and quote:
                evidence.append({"turnId": turn_id, "quote": quote})
    return {
        "content": content[:600],
        "type": memory_type,
        "importance": importance,
        "shouldSave": should_save,
        "subject": str(raw.get("subject") or "").strip()[:120],
        "predicate": str(raw.get("predicate") or "").strip()[:120],
        "object": str(raw.get("object") or raw.get("objectValue") or "").strip()[:300],
        "polarity": polarity,
        "modality": modality,
        "temporal": temporal,
        "cardinality": cardinality,
        "sourceSpan": str(raw.get("sourceSpan") or raw.get("source_span") or "").strip()[:600],
        "validUntil": str(raw.get("validUntil") or "").strip()[:80],
        "userPinned": bool(raw.get("userPinned", False)),
        "sourceTurnIds": list(dict.fromkeys(
            str(item).strip()[:120]
            for item in source_turn_values
            if str(item).strip()
        )),
        "evidence": evidence,
    }


def memory_candidates_from_model(candidates: Any) -> list[dict[str, Any]]:
    if not isinstance(candidates, list):
        return []
    updates: list[dict[str, Any]] = []
    for item in candidates:
        candidate = normalize_memory_candidate(item)
        if (
            candidate is not None
            and candidate["shouldSave"]
            and candidate["importance"] >= 0.68
            and candidate["type"] in MEMORY_CANDIDATE_TYPES
        ):
            updates.append(candidate)
    return updates[:4]


def normalize_claim_patches(value: Any) -> list[dict[str, str]]:
    """Keep only the claim-level patch protocol understood by the backend."""

    if not isinstance(value, list):
        return []
    allowed_actions = {"DELETE", "DOWNGRADE_CERTAINTY", "MARK_UNRESOLVED"}
    patches: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        claim_id = str(item.get("claimId") or "").strip()
        action = str(item.get("action") or "").strip().upper()
        if not claim_id or claim_id in seen or action not in allowed_actions:
            continue
        seen.add(claim_id)
        patches.append({"claimId": claim_id, "action": action})
    return patches


def resolve_proactive_directive(
    user_message: str,
    model_directive: Any,
    *,
    model_directive_provided: bool = False,
) -> tuple[str, str]:
    """Accept the model's semantic choice; missing metadata preserves state."""

    del user_message, model_directive_provided
    return normalize_proactive_directive(model_directive), "model_directive"


SPOKEN_JA_NO_EMOJI_RULE = (
    "[日语发音纯文本：硬性要求]所有用于语音合成的日语字段，包括 spokenTextJa、"
    "speechUnits[].ja、progress.ja 和 clarification.ja，都不得包含任何 Emoji 字符或 Emoji 组合。"
    "中文显示字段可以按语境使用 Emoji；不要把其中的 Emoji 复制到日语发音字段，也不要朗读表情符号的名称。"
    "若 Emoji 承载了必要语气，用自然日语措辞表达；若只是装饰，直接省略。"
)
