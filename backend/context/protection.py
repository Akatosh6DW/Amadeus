"""会话压缩前的 typed 上下文保护规则。

本模块只根据本轮语义解释、最终结果和已经核验的记忆更新生成保护投影；
它不读写会话、记忆或模型状态。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from agent.dialogue_architecture import UtteranceInterpretation
from backend.common.text import unique_strings
from backend.conversation.questions import assistant_question_from_text


def context_protection_for_turn(
    user_message: str,
    result: Mapping[str, Any],
    updates: Sequence[Mapping[str, Any]],
    interpretation: UtteranceInterpretation,
) -> dict[str, Any]:
    """在轮次进入可压缩工作集前冻结语义锚点。"""

    reasons: list[str] = []
    transient_reasons: list[str] = []
    if interpretation.primary_speech_act == "correction":
        reasons.append("correction")
    update_types: list[str] = []
    for update in updates:
        if not isinstance(update, Mapping):
            continue
        memory_type = str(update.get("type") or "user_memory").strip()
        modality = str(update.get("modality") or "asserted").strip()
        predicate = str(update.get("predicate") or "").strip()
        if memory_type:
            update_types.append(memory_type)
        if memory_type == "correction" or modality == "corrected":
            reasons.append("correction")
        if modality == "requested" or predicate == "explicit_memory_request":
            reasons.append("explicitMemory")
        if memory_type in {"episode", "episodic_archive", "shared_reference"}:
            reasons.append("sharedEpisode")
        if memory_type == "relationship_state":
            reasons.append("relationshipBoundary")
    requires_response = (
        bool(result.get("requiresUserResponse"))
        if bool(result.get("requiresUserResponseProvided"))
        else bool(assistant_question_from_text(result.get("reply")))
    )
    if requires_response:
        transient_reasons.append("pendingQuestion")
    if result.get("clarificationRequired") or result.get("unresolvedReferenceIds"):
        transient_reasons.append("unresolvedReference")
    reasons = unique_strings(reasons)
    transient_reasons = unique_strings(transient_reasons)
    message = re.sub(r"\s+", " ", str(user_message or "")).strip()[:1200]
    return {
        "version": 1,
        "reasons": reasons,
        "transientReasons": transient_reasons,
        "claims": [message] if message and reasons else [],
        "memoryTypes": unique_strings(update_types),
        "hardProtected": bool(reasons),
    }
