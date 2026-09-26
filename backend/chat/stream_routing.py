"""流式聊天在首个可见事件前完成的路由与能力计划。

请求 ``body`` 仍是准备结果和执行计划的唯一载体；本模块只决定本轮进入严格、
工具或快速路径。纯执行计划规则直接拥有在这里，状态读取、持久化和日志通过
按领域划分的窄端口接入。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol, Sequence

from agent.research_architecture import ResearchDecision, capability_envelope
from agent.tool_architecture import TurnPreflight
from agent.turn_execution_plan import (
    CapabilityObligation,
    TurnExecutionPlan,
    TurnExecutionPlanner,
    capability_hints_from_preflight,
    execution_plan_from_dict,
)
from backend.chat.input import streaming_user_message
from backend.research.planning import fallback_web_research_query
from backend.research.query import web_search_freshness


class RouteContextOperations(Protocol):
    def contextual_reference_resolution(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_turn_context_artifacts(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_task_semantic_request(self, *args: Any, **kwargs: Any) -> Any: ...
    def frozen_turn_context_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def cognitive_turn_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def read_relationship_state(self, *args: Any, **kwargs: Any) -> Any: ...


class RoutePlanningOperations(Protocol):
    def body_prepared_action_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def apply_contextual_research_subject(self, *args: Any, **kwargs: Any) -> Any: ...
    def turn_contract_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def enrich_prepared_turn_with_research(self, *args: Any, **kwargs: Any) -> Any: ...
    def body_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...
    def reconcile_research_with_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...
    def synchronize_prepared_epistemic_plan(self, *args: Any, **kwargs: Any) -> Any: ...
    def reconcile_preanswer_research_route(self, *args: Any, **kwargs: Any) -> Any: ...
    def capability_with_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...
    def persist_prepared_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_tool_preflight_log(self, *args: Any, **kwargs: Any) -> Any: ...


class RouteToolOperations(Protocol):
    def infer_agent_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def task_bound_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def is_agent_file_action_query(self, *args: Any, **kwargs: Any) -> Any: ...
    def is_time_query(self, *args: Any, **kwargs: Any) -> Any: ...


class RouteRuntimeOperations(Protocol):
    def configured(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_log(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class StreamRouteServices:
    context: RouteContextOperations
    planning: RoutePlanningOperations
    tools: RouteToolOperations
    runtime: RouteRuntimeOperations


def turn_capability_obligations(
    tool_requests: Sequence[dict[str, Any]],
    *,
    has_attachments: bool = False,
    model_tool_escalation: bool = False,
) -> tuple[CapabilityObligation, ...]:
    """把已经有依据的具体需要转换为执行义务。

    建议性质的预检候选不会进入这里。只有用户明确要求动作、当前轮绑定了附件，
    或同一轮模型在回答交付前给出结构化工具升级时，能力才会变成强制义务。
    """
    obligations: list[CapabilityObligation] = []
    for item in tool_requests:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        selection = item.get("selection") if isinstance(item.get("selection"), dict) else {}
        if args.get("browserTargetResolution"):
            source = "browser_target_resolution"
        elif model_tool_escalation or str(selection.get("source") or "").startswith("fast_stream"):
            source = "model_tool_escalation"
        else:
            source = "explicit_user_request"
        obligations.append(
            CapabilityObligation(
                capability=name,
                source=source,
                reason=str(item.get("reason") or "grounded capability requirement"),
            )
        )
    if has_attachments:
        obligations.append(
            CapabilityObligation(
                capability="read_attachment",
                source="bound_attachment",
                reason="the current turn contains an attachment",
            )
        )
    return tuple(obligations)


def finalize_turn_execution_plan(
    body: dict[str, Any],
    *,
    user_message: str,
    decision: ResearchDecision,
    preflight: TurnPreflight,
    tool_requests: Sequence[dict[str, Any]],
    settings: dict[str, Any],
    has_attachments: bool = False,
    model_research_escalation: bool = False,
    model_tool_escalation: bool = False,
    evidence_recovery_escalation: bool = False,
) -> TurnExecutionPlan:
    """返回所有路由共同消费的唯一不可变权限记录。

    路由只创建一次计划，常规执行原样复用；只有同轮结构化模型升级才能产生新
    修订。这避免回退路径或原子路径自行把建议提示升级为强制工具或联网研究。
    """
    existing = execution_plan_from_dict(
        body.get("_turnExecutionPlan") if isinstance(body.get("_turnExecutionPlan"), dict) else None
    )
    escalation = bool(
        model_research_escalation or model_tool_escalation or evidence_recovery_escalation
    )
    if existing is not None and (not escalation):
        return existing
    reasons = list(decision.reasons)
    if model_research_escalation:
        reasons.append("model_research_escalation")
    if evidence_recovery_escalation:
        reasons.append("grounded_claim_gate_evidence_gap")
    hints = list(capability_hints_from_preflight(preflight))
    obligations = list(
        turn_capability_obligations(
            tool_requests,
            has_attachments=has_attachments,
            model_tool_escalation=model_tool_escalation,
        )
    )
    obligated_capabilities = {item.capability for item in obligations}
    for capability_name in preflight.mandatory_capabilities:
        if capability_name in obligated_capabilities:
            continue
        obligations.append(
            CapabilityObligation(
                capability=capability_name,
                source="explicit_user_request",
                reason="semantic preflight marked this capability as mandatory",
            )
        )
        obligated_capabilities.add(capability_name)
    if (model_research_escalation or evidence_recovery_escalation) and (
        not any((item.capability in {"web_search", "read_web_page"} for item in obligations))
    ):
        obligations.append(
            CapabilityObligation(
                capability="web_search",
                source=(
                    "model_research_escalation"
                    if model_research_escalation
                    else "grounded_claim_gate"
                ),
                reason="the same turn identified an external evidence gap",
            )
        )
    plan = TurnExecutionPlanner.finalize(
        turn_id=str(body.get("requestId") or ""),
        proposed_research_policy=(
            "required"
            if model_research_escalation or evidence_recovery_escalation
            else decision.policy
        ),
        research_reasons=tuple(dict.fromkeys(reasons)),
        obligations=obligations,
        hints=hints,
        web_permission=bool(settings.get("permissionWebSearch")),
        revision=(
            max(existing.revision + 1, decision.plan_revision)
            if existing is not None
            else max(1, decision.plan_revision)
        ),
    )
    body["_turnExecutionPlan"] = plan.to_dict()
    return plan


def classify_streaming_chat_route(
    services: StreamRouteServices,
    body: dict[str, Any],
    settings: dict[str, Any],
    *,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在任何用户可见台词出现前，保守选择快速、工具、严格或旧版路径。"""
    user_message, messages = streaming_user_message(body)
    prepared_action_turn = services.planning.body_prepared_action_turn(
        body, user_message, request_id=str(body.get("requestId") or "")
    )
    context_resolution = services.context.contextual_reference_resolution(
        user_message,
        messages,
        request_id=str(body.get("requestId") or ""),
        current_artifacts=services.context.current_turn_context_artifacts(body),
        resolved_task=services.context.current_task_semantic_request(body, user_message),
        context_state=services.context.frozen_turn_context_state(body),
        model_owned=bool(services.context.cognitive_turn_state(body)),
    )
    body["_contextResolution"] = context_resolution
    body["_discourseLedger"] = context_resolution.get("referenceLedger", {})
    prepared_action_turn = services.planning.apply_contextual_research_subject(
        prepared_action_turn, context_resolution
    )
    body["_preparedActionTurn"] = prepared_action_turn
    interpretation, policy, contract = services.planning.turn_contract_context(
        user_message, prepared_action_turn, task_body=body
    )
    prepared_action_turn, research_decision, capability = (
        services.planning.enrich_prepared_turn_with_research(
            prepared_action_turn, policy, contract, settings, interpretation=interpretation
        )
    )
    tool_preflight = services.planning.body_tool_preflight(
        body,
        user_message,
        messages,
        reply_mode=policy.reply_mode,
        speech_act=interpretation.primary_speech_act,
        settings=settings,
    )
    local_research_decision = services.planning.reconcile_research_with_tool_preflight(
        research_decision, tool_preflight, user_message
    )
    if local_research_decision != research_decision:
        research_decision = local_research_decision
        prepared_action_turn, research_decision = (
            services.planning.synchronize_prepared_epistemic_plan(
                prepared_action_turn, research_decision
            )
        )
        capability = capability_envelope(
            policy, research_decision, web_permission=bool(settings.get("permissionWebSearch"))
        )
    prepared_action_turn, research_decision, capability = (
        services.planning.reconcile_preanswer_research_route(
            settings,
            user_message,
            prepared_action_turn,
            research_decision,
            policy,
            body,
            request_id=str(body.get("requestId") or ""),
        )
    )
    capability = services.planning.capability_with_tool_preflight(
        capability, tool_preflight, settings
    )
    prepared_action_turn = replace(prepared_action_turn, capability_envelope=capability.to_dict())
    body["_preparedActionTurn"] = prepared_action_turn
    services.planning.persist_prepared_turn(prepared_action_turn)
    services.planning.write_tool_preflight_log(
        str(body.get("requestId") or ""), tool_preflight, capability
    )
    shared_route_context = {
        "preparedTurn": prepared_action_turn.to_dict(),
        "interpretation": interpretation.to_dict(),
        "turnPolicy": policy.to_dict(),
        "turnContract": contract.to_dict(),
        "researchDecision": research_decision.to_dict(),
        "capabilityEnvelope": capability.to_dict(),
    }
    relationship = services.context.read_relationship_state()
    action = str((preflight or {}).get("action") or "")
    if (
        action in {"locked", "cold"}
        or relationship.get("dignityLocked")
        or relationship.get("coldShoulder")
    ):
        return {
            "route": "strict_boundary",
            "reason": action or "saved_boundary_state",
            "toolRequests": [],
            **shared_route_context,
        }
    if not services.runtime.configured(settings):
        return {
            "route": "fallback_legacy",
            "reason": "model_not_configured",
            "toolRequests": [],
            **shared_route_context,
        }
    if services.context.cognitive_turn_state(body) and (
        not services.context.current_turn_context_artifacts(body)
    ):
        return {
            "route": "fast_chat",
            "reason": "cognitive_semantic_query_loop",
            "toolRequests": [],
            **shared_route_context,
        }
    if services.tools.is_time_query(user_message):
        return {
            "route": "fallback_legacy",
            "reason": "local_time_response",
            "toolRequests": [],
            **shared_route_context,
        }
    attachments = services.context.current_turn_context_artifacts(body)
    tool_requests = services.tools.infer_agent_tool_requests(
        user_message,
        messages,
        context_resolution,
        prepared_action_turn,
        body.get("_todoReconciliation"),
    )
    tool_requests = services.tools.task_bound_tool_requests(body, tool_requests)
    execution_plan = finalize_turn_execution_plan(
        body,
        user_message=user_message,
        decision=research_decision,
        preflight=tool_preflight,
        tool_requests=tool_requests,
        settings=settings,
        has_attachments=bool(attachments),
    )
    if execution_plan.research_policy != research_decision.policy:
        research_decision = replace(
            research_decision,
            policy=execution_plan.research_policy,
            reasons=tuple(
                dict.fromkeys((*research_decision.reasons, "turn_execution_plan_finalized"))
            ),
            plan_revision=research_decision.plan_revision + 1,
        )
        prepared_action_turn, research_decision = (
            services.planning.synchronize_prepared_epistemic_plan(
                prepared_action_turn, research_decision
            )
        )
    capability = capability_envelope(
        policy, research_decision, web_permission=bool(settings.get("permissionWebSearch"))
    )
    capability = services.planning.capability_with_tool_preflight(
        capability,
        tool_preflight,
        settings,
        extra_tool_ids=[
            str(item.get("name") or "") for item in tool_requests if isinstance(item, dict)
        ],
    )
    capability = replace(
        capability,
        research_policy=execution_plan.research_policy,
        required_tools=execution_plan.required_capabilities,
        max_tool_rounds=execution_plan.max_tool_rounds,
        tool_policy="required" if execution_plan.required_capabilities else "model_decides",
    )
    prepared_action_turn = replace(
        prepared_action_turn,
        research_decision=research_decision.to_dict(),
        capability_envelope=capability.to_dict(),
    )
    body["_preparedActionTurn"] = prepared_action_turn
    services.planning.persist_prepared_turn(prepared_action_turn)
    services.runtime.write_log(
        "turn_execution_plan_finalized",
        requestId=str(body.get("requestId") or ""),
        executionPlan=execution_plan.to_dict(),
    )
    shared_route_context.update(
        {
            "preparedTurn": prepared_action_turn.to_dict(),
            "researchDecision": research_decision.to_dict(),
            "capabilityEnvelope": capability.to_dict(),
            "executionPlan": execution_plan.to_dict(),
        }
    )
    if execution_plan.research_policy == "forbidden":
        tool_requests = [
            item
            for item in tool_requests
            if str(item.get("name") or "") not in {"web_search", "read_web_page"}
        ]
    elif execution_plan.research_policy == "required" and (
        not any(
            (
                str(item.get("name") or "") in {"web_search", "read_web_page"}
                for item in tool_requests
            )
        )
    ):
        tool_requests.append(
            {
                "name": "web_search",
                "args": {
                    "query": str(body.get("_preAnswerResearchQuery") or "").strip()
                    or fallback_web_research_query(user_message, context_resolution, messages),
                    "freshness": web_search_freshness(user_message),
                    "researchDecision": research_decision.to_dict(),
                },
                "reason": "unified research policy requires public evidence",
            }
        )
    if services.tools.is_agent_file_action_query(user_message):
        return {
            "route": "tool_gated",
            "reason": "explicit_agent_file_action",
            "toolRequests": tool_requests or [{"name": "file_operation", "args": {}}],
            **shared_route_context,
        }
    if attachments:
        return {
            "route": "tool_gated",
            "reason": "attachments_require_evidence",
            "toolRequests": tool_requests or [{"name": "read_attachment", "args": {}}],
            **shared_route_context,
        }
    if tool_requests:
        return {
            "route": "tool_gated",
            "reason": "tool_evidence_required",
            "toolRequests": tool_requests,
            **shared_route_context,
        }
    if prepared_action_turn.route in {
        "external_action",
        "external_action_needs_target",
        "research_plus_external_action",
    }:
        return {
            "route": "tool_gated",
            "reason": "explicit_external_action_requires_grounding",
            "toolRequests": [],
            **shared_route_context,
        }
    return {
        "route": "fast_chat",
        "reason": "unified_semantic_query_loop",
        "toolRequests": [],
        **shared_route_context,
    }
