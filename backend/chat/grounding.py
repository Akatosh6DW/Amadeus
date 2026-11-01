"""事实证据不足时的结构化诊断状态。

本模块不执行查证、改写或提交状态；对话收尾器持有审查顺序与重试额度。
它只判断外部事实是否仍缺少足够回执。
"""
from __future__ import annotations

from typing import Any

from agent.epistemic_architecture import EpistemicTurnState

def hard_fact_gate_requires_terminal_boundary(
    epistemic_state: EpistemicTurnState,
    receipt: Any,
) -> bool:
    """HARD claims may not release guessed details after verification fails."""

    hard_requirements = tuple(
        item for item in epistemic_state.fact_requirements
        if item.verification_required
        and item.verification_level == "HARD"
        and item.domain == "external_world"
        and item.evidence_class == "web_evidence"
    )
    has_sufficient_receipt = bool(
        receipt
        and receipt.status == "completed"
        and receipt.source_uids
        and receipt.evidence_sufficient
    )
    return bool(hard_requirements) and not has_sufficient_receipt


def soft_fact_gate_can_degrade(
    epistemic_state: EpistemicTurnState,
    gate_decision: Any,
) -> bool:
    """SOFT Web debt may patch its scope but may never terminate the turn."""

    external = tuple(
        item for item in epistemic_state.fact_requirements
        if item.verification_required
        and item.domain == "external_world"
        and item.evidence_class == "web_evidence"
    )
    if not external or any(item.verification_level == "HARD" for item in external):
        return False
    reasons = set(getattr(gate_decision, "reasons", ()) or ())
    return bool(reasons) and reasons.issubset({
        "soft_fact_requires_local_uncertainty_patch",
        "citation_without_sufficient_evidence",
        "required_fact_answer_without_sufficient_receipt",
    })
