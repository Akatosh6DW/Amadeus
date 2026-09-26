"""单轮输入解释、研究契约补强与副作用前持久化。

PreparedTurn 是路由解释的唯一记录。本模块只在请求 body 或状态库中恢复、补强并
保存这一记录；外部工具副作用仍由调用方在保存成功后执行。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol

from agent.action_grounding import PreparedTurn, prepared_turn_from_dict
from agent.dialogue_architecture import (
    TurnContract,
    TurnPolicy,
    UtteranceInterpretation,
    interpret_utterance,
)
from agent.research_architecture import (
    CapabilityEnvelope,
    EpistemicPlan,
    ResearchDecision,
    capability_envelope,
    create_epistemic_plan,
    deterministic_research_decision,
)
from backend.research.fact_policy import synchronize_prepared_epistemic_plan


class PreparedTurnStore(Protocol):
    def read(self, request_id: str) -> Mapping[str, Any] | None: ...
    def write(self, payload: Mapping[str, Any]) -> Any: ...


class PreparedTurnPlanner(Protocol):
    def prepare(self, user_message: str, *, request_id: str) -> PreparedTurn: ...


class PreparedTurnRuntime(Protocol):
    def write_log(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class PreparedTurnServices:
    store: PreparedTurnStore
    planner: PreparedTurnPlanner
    runtime: PreparedTurnRuntime


def body_prepared_action_turn(
    services: PreparedTurnServices,
    body: dict[str, Any],
    user_message: str,
    *,
    request_id: str = "",
) -> PreparedTurn:
    """复用请求内记录，或按同一请求身份恢复/创建 PreparedTurn。"""

    existing = body.get("_preparedActionTurn")
    if isinstance(existing, PreparedTurn):
        return existing
    normalized_request_id = request_id or str(body.get("requestId") or "")
    persisted = services.store.read(normalized_request_id)
    restored = prepared_turn_from_dict(persisted) if persisted else None
    if (
        isinstance(restored, PreparedTurn)
        and restored.interpreted.original_text.strip() == str(user_message or "").strip()
    ):
        body["_preparedActionTurn"] = restored
        services.runtime.write_log(
            "prepared_turn_restored",
            requestId=normalized_request_id,
            researchPolicy=str(
                (restored.research_decision or {}).get("policy") or ""
            ),
            source="state_database",
        )
        return restored
    prepared = services.planner.prepare(
        user_message,
        request_id=normalized_request_id,
    )
    body["_preparedActionTurn"] = prepared
    return prepared


def utterance_interpretation_from_prepared(
    prepared: PreparedTurn,
) -> UtteranceInterpretation:
    """从唯一准备记录重建对话层解释。"""

    return interpret_utterance(
        prepared.interpreted.original_text,
        semantic_text=prepared.interpreted.semantic_text,
        vocatives=prepared.interpreted.vocatives,
        external_action=prepared.has_explicit_external_action,
    )


def apply_contextual_research_subject(
    prepared: PreparedTurn,
    resolution: dict[str, Any] | None,
) -> PreparedTurn:
    """上下文解析只补充语义，不能自行执行工具。

    解析结果仍须进入 TurnExecutionPlanner。这里不能再次运行词法策略，把 Web
    从 allowed 提升为 required；PreparedTurn 的权威记录保持不变。
    """

    return prepared


def enrich_prepared_turn_with_research(
    prepared: PreparedTurn,
    policy: TurnPolicy,
    contract: TurnContract,
    settings: dict[str, Any],
    *,
    escalation: dict[str, Any] | None = None,
    interpretation: UtteranceInterpretation | None = None,
) -> tuple[PreparedTurn, ResearchDecision, CapabilityEnvelope]:
    """在同一 EpistemicPlan 上加入研究决策、能力包络和交付合同。"""

    interpretation = interpretation or utterance_interpretation_from_prepared(prepared)
    existing_plan = EpistemicPlan.from_dict(prepared.epistemic_plan)
    if existing_plan is None:
        seed_decision = (
            ResearchDecision.from_dict(prepared.research_decision)
            if prepared.research_decision
            else deterministic_research_decision(interpretation)
        )
        existing_plan = create_epistemic_plan(
            prepared.request_id,
            interpretation,
            decision=seed_decision,
        )
    if escalation and existing_plan.research_policy != "forbidden":
        existing_plan = existing_plan.with_evidence_gap(
            reason=str(escalation.get("reason") or "model_research_escalation"),
            unknown_entities=[
                str(item)
                for item in (
                    escalation.get("unknownEntities")
                    if isinstance(escalation.get("unknownEntities"), list)
                    else []
                )
                if str(item).strip()
            ],
            verification_targets=[
                str(item)
                for item in (
                    escalation.get("verificationTargets")
                    if isinstance(escalation.get("verificationTargets"), list)
                    else []
                )
                if str(item).strip()
            ],
        )
    existing_decision = existing_plan.research_decision()
    epistemic_state = existing_plan.epistemic_state()
    envelope = capability_envelope(
        policy,
        existing_decision,
        web_permission=bool(settings.get("permissionWebSearch")),
    )
    enriched = replace(
        prepared,
        research_decision=existing_decision.to_dict(),
        capability_envelope=envelope.to_dict(),
        turn_contract=contract.to_dict(),
        epistemic_state=epistemic_state.to_dict(),
        epistemic_plan=existing_plan.to_dict(),
    )
    return enriched, existing_decision, envelope


def persist_prepared_turn(
    services: PreparedTurnServices,
    prepared: PreparedTurn,
) -> dict[str, Any]:
    """在任何外部副作用之前保存本轮唯一的路由解释。"""

    payload = {"stateVersion": 2, **prepared.to_dict()}
    services.store.write(payload)
    request_id = str(payload.get("requestId") or "")
    interpreted = (
        payload.get("interpretedInput")
        if isinstance(payload.get("interpretedInput"), dict)
        else {}
    )
    candidate = (
        payload.get("actionCandidate")
        if isinstance(payload.get("actionCandidate"), dict)
        else {}
    )
    pending = (
        payload.get("pendingActionRelation")
        if isinstance(payload.get("pendingActionRelation"), dict)
        else {}
    )
    services.runtime.write_log(
        "input_interpreted",
        requestId=request_id,
        dialogueText=str(interpreted.get("dialogueText") or "")[:1000],
        actionParseText=str(interpreted.get("actionParseText") or "")[:1000],
        removedSpans=interpreted.get("removedSpans") or [],
    )
    for vocative in interpreted.get("vocatives") or []:
        if isinstance(vocative, dict):
            services.runtime.write_log(
                "vocative_detected",
                requestId=request_id,
                vocativeType=str(vocative.get("vocative_type") or ""),
                exactText=str(vocative.get("exact_text") or ""),
                span=vocative.get("span") or {},
            )
    services.runtime.write_log(
        "action_candidate_created",
        requestId=request_id,
        actionType=str(candidate.get("actionType") or "none"),
        targetText=str(candidate.get("targetText") or "")[:500],
        explicitness=str(candidate.get("explicitness") or "none"),
        confidence=candidate.get("confidence"),
        negated=bool(candidate.get("negated")),
        diagnostic=bool(candidate.get("diagnostic")),
        evidenceSpans=candidate.get("evidenceSpans") or [],
    )
    services.runtime.write_log(
        "pending_action_relation",
        requestId=request_id,
        relation=str(pending.get("relation") or "none"),
        reason=str(pending.get("reason") or ""),
    )
    services.runtime.write_log(
        "route_selected",
        requestId=request_id,
        route=str(payload.get("route") or "fast_chat"),
        createdFrom=str(payload.get("createdFrom") or ""),
    )
    research_decision = (
        payload.get("researchDecision")
        if isinstance(payload.get("researchDecision"), dict)
        else {}
    )
    capability = (
        payload.get("capabilityEnvelope")
        if isinstance(payload.get("capabilityEnvelope"), dict)
        else {}
    )
    if research_decision:
        services.runtime.write_log(
            "research_decision_created",
            requestId=request_id,
            policy=str(research_decision.get("policy") or ""),
            reasons=research_decision.get("reasons") or [],
            unknownEntities=research_decision.get("unknown_entities") or [],
            verificationTargets=research_decision.get("verification_targets") or [],
            primarySourceRequired=bool(
                research_decision.get("primary_source_required")
            ),
            planId=str(research_decision.get("plan_id") or ""),
            planRevision=int(research_decision.get("plan_revision") or 1),
        )
    epistemic_plan = (
        payload.get("epistemicPlan")
        if isinstance(payload.get("epistemicPlan"), dict)
        else {}
    )
    if epistemic_plan:
        services.runtime.write_log(
            "epistemic_plan_created",
            requestId=request_id,
            planId=str(epistemic_plan.get("planId") or ""),
            revision=int(epistemic_plan.get("revision") or 1),
            researchPolicy=str(epistemic_plan.get("researchPolicy") or ""),
            answerObligations=epistemic_plan.get("answerObligations") or [],
            factRequirements=epistemic_plan.get("factRequirements") or [],
        )
    if capability:
        services.runtime.write_log(
            "capability_envelope_created",
            requestId=request_id,
            replyMode=str(capability.get("reply_mode") or ""),
            researchPolicy=str(capability.get("research_policy") or ""),
            allowedTools=capability.get("allowed_tools") or [],
            requiredTools=capability.get("required_tools") or [],
            mayEscalate=bool(capability.get("may_escalate")),
            maxToolRounds=capability.get("max_tool_rounds"),
        )
    epistemic_state = (
        payload.get("epistemicState")
        if isinstance(payload.get("epistemicState"), dict)
        else {}
    )
    if epistemic_state:
        services.runtime.write_log(
            "epistemic_turn_state_created",
            requestId=request_id,
            factRequirements=epistemic_state.get("factRequirements") or [],
            allowedFinalClaimTypes=epistemic_state.get("allowedFinalClaimTypes") or [],
        )
    return payload
