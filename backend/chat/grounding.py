"""事实证据不足时的局部修订和边界策略。

本模块不执行查证或提交状态；对话收尾器持有审查顺序与重试额度。
模型修订保留原有动作回执，不能把未知事实或未执行动作写成完成。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from agent.epistemic_architecture import EpistemicTurnState, claim_scope_relevance
from agent.research_architecture import EpistemicPlan
from agent.persona_context import PersonaContextSnapshot
from backend.attachments.delivery import preserve_attachment_delivery
from backend.chat.presentation import normalize_speech_language

def rewrite_reply_for_grounding_gate(
    settings: dict[str, Any],
    user_message: str,
    candidate: dict[str, Any],
    gate_decision: Any,
    epistemic_plan: EpistemicPlan,
    *,
    request_id: str,
    persona_snapshot: PersonaContextSnapshot | None = None,
    model_call: Callable[..., str],
    normalize_reply: Callable[..., dict[str, Any]],
    persona_prefix: Callable[..., list[dict[str, str]]],
) -> dict[str, Any]:
    """Ask the same configured model to lower unsupported certainty.

    The backend supplies diagnostics and constraints only; it never authors a
    replacement conclusion.  This path is used when Web research is forbidden
    or exhausted but the conversational turn can still be answered safely.
    """

    unsupported = set(getattr(gate_decision, "unsupported_claim_ids", ()) or ())
    claims = [
        item.to_dict()
        for item in (getattr(gate_decision, "claims", ()) or ())
        if not unsupported or item.claim_id in unsupported
    ]
    payload: dict[str, Any] = {
        "model": settings["apiModel"],
        "temperature": 0.35,
        "max_tokens": 1400,
        "messages": [
            *persona_prefix(persona_snapshot, legacy_system_prompt=False),
            {
                "role": "system",
                "content": (
                    "你是同一轮回答的事实边界修订器。只修订缺少对应证据的断言，"
                    "保留原回答的直接回应、人格语气和已支持内容。不得声称执行过未执行的"
                    "搜索、网页读取、文件操作或本地动作；无法确定时自然表达边界，仍要回答"
                    "用户真正的问题。输出一个完整 JSON，至少包含 reply、spokenTextJa、"
                    "emotion、gesture、memoryUpdates。若没有充分网页证据，必须删除 [S1] 一类"
                    "引用标记以及由它们支撑的断言。unsupportedClaims 中的每个事实都不得再"
                    "被正面确认：只能明确降为未确认，或删掉该局部事实后继续回答主问题。"
                    "尤其不能先说无法确认、随后又用‘确实出自’‘原话是’等方式重新断言。"
                    "使用一到三项 speechUnits，每项含 zh、ja、boundaryAfter，让中日文修订"
                    "保持逐项对应；涉及 unsupportedClaims 的单元还要带 claimIds。另输出"
                    "claimPatches:[{claimId,action}]，action 只能是 DELETE、"
                    "DOWNGRADE_CERTAINTY 或 MARK_UNRESOLVED，并覆盖每个 unsupported claimId。"
                    "missingVerificationTargets 是权威事实范围，即使 unsupportedClaims 未列全，"
                    "也不得补写该范围的来源、身份、日期或归属细节。不要输出诊断过程。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "userMessage": user_message,
                    "originalCandidate": candidate,
                    "gateDecision": gate_decision.to_dict(),
                    "unsupportedClaims": claims,
                    "researchPolicy": epistemic_plan.research_policy,
                }, ensure_ascii=False),
            },
        ],
    }
    if settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in settings["apiEndpoint"]:
        payload["thinking"] = {"type": "disabled"}
    content = model_call(
        settings,
        payload,
        timeout=90,
        request_id=request_id,
        purpose="grounding_gate_rewrite",
    )
    rewritten = normalize_reply(content, settings=settings)
    patch_actions = {
        str(item.get("claimId") or ""): str(item.get("action") or "")
        for item in rewritten.get("claimPatches", [])
        if isinstance(item, dict)
    }
    rewritten["groundingGateClaimPatchesValid"] = bool(
        unsupported
        and unsupported.issubset(patch_actions)
        and all(
            patch_actions[item] in {
                "DELETE", "DOWNGRADE_CERTAINTY", "MARK_UNRESOLVED",
            }
            for item in unsupported
        )
    )
    for key in (
        "_conversationToolResults", "_chatMetrics", "attachments", "files",
        "_fileOutputs", "toolUsed", "toolStatus", "toolResults", "actionReceipt",
        "todoContext", "webEvidenceStatus",
    ):
        if key in candidate and key not in rewritten:
            rewritten[key] = candidate[key]
    preserve_attachment_delivery(candidate, rewritten)
    rewritten["groundingGateRewrittenByModel"] = True
    if persona_snapshot is not None:
        rewritten["personaContextSnapshotId"] = persona_snapshot.snapshot_id
    return rewritten


def terminal_grounding_boundary_reply(
    candidate: dict[str, Any], *, failure_stage: str = "",
) -> dict[str, Any]:
    """Return a fact-free answer when the model cannot obey a grounding rewrite.

    This is deliberately narrower than a normal fallback: it does not infer an
    answer from failed search results, repeat source claims, or pretend that an
    insufficient receipt completed verification.  It only exposes the evidence
    boundary already established by the gate so an expected lack of evidence
    cannot abort the whole conversational turn.
    """

    safe = dict(candidate)
    safe.update({
        "reply": "现有证据不足，我现在无法确认这件事。",
        "spokenTextJa": "現時点では証拠が足りないため、この件は断定できません。",
        "emotion": "concerned",
        "gesture": "idle",
        "memoryUpdates": [],
        "turnComplete": True,
        "standalone": True,
        "groundingGateTerminalBoundary": True,
    })
    if failure_stage == "evidence_limited_answer":
        safe.update({
            "reply": "这次网页证据尚未完成核验，回答整理也失败了，暂时无法给出可靠的查询结论。",
            "spokenTextJa": "今回はウェブの証拠を検証しきれず、回答の整理にも失敗したため、確かな検索結果はまだ伝えられないわ。",
            "groundingFailureStage": failure_stage,
        })
    safe["speechText"] = safe["reply"] if normalize_speech_language(safe.get("speechLanguage")) == "zh-CN" else safe["spokenTextJa"]
    safe["memoryAction"] = "none"
    # These flags describe the discarded draft, not this fact-free failure
    # report. Keep the failure stage separately for diagnostics.
    safe.pop("toolGroundingUnverified", None)
    safe.pop("webSynthesisUnverified", None)
    for key in (
        "_memoryCandidates",
        "_memoryUpdateTypes",
        "_modelSpeechUnits",
        "speechUnits",
        "speechUnitContract",
    ):
        safe.pop(key, None)
    return safe


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


def soft_grounding_boundary_reply(
    candidate: dict[str, Any],
    gate_decision: Any,
) -> dict[str, Any]:
    """Downgrade only unresolved SOFT scope while preserving the main answer."""

    safe = dict(candidate)
    reply = re.sub(r"\s*\[S\d+\]", "", str(safe.get("reply") or "")).strip()
    spoken = str(safe.get("spokenTextJa") or "").strip()
    targets = [
        str(item).strip()
        for item in (getattr(gate_decision, "missing_verification_targets", ()) or ())
        if str(item).strip()
    ]
    unsupported_ids = set(getattr(gate_decision, "unsupported_claim_ids", ()) or ())
    unsupported_texts = [
        str(item.text or "").strip()
        for item in (getattr(gate_decision, "claims", ()) or ())
        if item.claim_id in unsupported_ids and str(item.text or "").strip()
    ]

    def unsupported_unit(value: str) -> bool:
        unit = str(value or "").strip()
        return bool(unit) and any(
            claim_scope_relevance(target, unit) >= 0.82
            for target in unsupported_texts
        )

    raw_units = safe.get("_modelSpeechUnits")
    if isinstance(raw_units, list) and raw_units:
        kept_units = [
            dict(item)
            for item in raw_units
            if isinstance(item, dict)
            and not (
                unsupported_ids
                & {
                    str(claim_id).strip()
                    for claim_id in (
                        item.get("claimIds")
                        if isinstance(item.get("claimIds"), list)
                        else []
                    )
                    if str(claim_id).strip()
                }
            )
            and not unsupported_unit(
                str(item.get("zh") or item.get("textZh") or item.get("reply") or "")
            )
        ]
        if kept_units:
            reply = "\n\n".join(
                str(item.get("zh") or item.get("textZh") or item.get("reply") or "").strip()
                for item in kept_units
                if str(item.get("zh") or item.get("textZh") or item.get("reply") or "").strip()
            ).strip()
            spoken = "".join(
                str(item.get("ja") or item.get("spokenTextJa") or "").strip()
                for item in kept_units
            ).strip()
    elif unsupported_texts:
        reply_units = [
            item.strip()
            for item in re.split(r"(?<=[。！？!?；;])|\n+", reply)
            if item.strip()
        ]
        rejected_indexes = {
            index for index, item in enumerate(reply_units) if unsupported_unit(item)
        }
        reply = "".join(
            item for index, item in enumerate(reply_units) if index not in rejected_indexes
        ).strip()
        spoken_units = [
            item.strip()
            for item in re.split(r"(?<=[。！？!?；;])|\n+", spoken)
            if item.strip()
        ]
        if rejected_indexes and len(spoken_units) == len(reply_units):
            spoken = "".join(
                item for index, item in enumerate(spoken_units)
                if index not in rejected_indexes
            ).strip()
    scope = targets[0][:140] if targets else "其中附带的外部事实"
    boundary = f"关于“{scope}”的准确性，我暂时无法可靠确认；不过这不影响下面的讨论。"
    if boundary not in reply:
        reply = f"{boundary}\n\n{reply}".strip()
    spoken_boundary = (
        "付随する外部事実の正確さは今のところ確認できないけれど、"
        "それとは分けて本題には答えるわ。"
    )
    if spoken_boundary not in spoken:
        spoken = f"{spoken_boundary}{spoken}".strip()
    safe.update({
        "reply": reply,
        "spokenTextJa": spoken,
        "turnComplete": True,
        "standalone": True,
        "softFactBoundaryApplied": True,
        "softFactBoundaryTargets": targets,
    })
    for key in ("_modelSpeechUnits", "speechUnits", "speechUnitContract"):
        safe.pop(key, None)
    return safe
