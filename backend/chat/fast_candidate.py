"""流式候选的交付前核验、查证升级与最终投影恢复。

接收本轮已冻结的上下文，不重新解释用户的省略句。返回的路由标识告诉流式
交付者是否必须丢弃原缓冲语句；修复产生的正文是唯一允许交付的候选。
该服务不拥有流、请求缓存或持久化状态，也不会增加模型修复次数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.action_grounding import PreparedTurn
from agent.cognitive_finalization import FinalProjectionAuditRepairError
from agent.dialogue_architecture import TurnContract, TurnPolicy, UtteranceInterpretation
from agent.epistemic_architecture import grounded_claim_gate_decision, knowledge_confidence_gate_decision
from agent.research_architecture import EpistemicPlan, ResearchDecision, create_epistemic_plan
from agent.turn_execution_plan import TurnExecutionPlan
from backend.chat.errors import GroundingGateError
from backend.common.values import safe_nonnegative_int
from backend.research.review import needs_post_answer_research_audit
from backend.providers.errors import UpstreamProtocolError
from agent.persona_context import PersonaContextSnapshot


@dataclass(frozen=True)
class FastCandidateContext:
    """原流式闭包的本轮输入；body 与候选仍使用原对象，不另复制认知状态。"""

    body: dict[str, Any]
    settings: dict[str, Any]
    user_message: str
    messages: list[dict[str, Any]]
    request_id: str
    started: float
    preflight: dict[str, Any] | None
    fast_persona_snapshot: PersonaContextSnapshot
    prepared_fast_turn: PreparedTurn
    fast_policy: TurnPolicy
    fast_interpretation: UtteranceInterpretation
    fast_contract: TurnContract
    route_execution_plan: TurnExecutionPlan | None
    route_research_decision: ResearchDecision | None
    route_name: str


@dataclass(frozen=True)
class ContextPort:
    """当前轮绑定和原始对话快照，不能调用模型或执行工具。"""

    bind_turn_context_to_result: Callable[..., dict[str, Any]]
    current_conversation_epoch: Callable[[], int]
    body_prepared_action_turn: Callable[..., PreparedTurn]
    turn_contract_context: Callable[..., tuple[UtteranceInterpretation, TurnPolicy, TurnContract]]
    task_review_dialogue_messages: Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True)
class CompletionPort:
    """既有候选检查/收尾与重入流程；调用次数和恢复策略由服务执行体限定。"""

    enforce_fast_dialogue_policy_once: Callable[..., dict[str, Any]]
    finalize_chat_response: Callable[..., dict[str, Any]]
    recover_final_projection_candidate: Callable[..., dict[str, Any]]
    process_chat_request_body: Callable[..., dict[str, Any]]


class FastCandidateFinalizer:
    def __init__(
        self, context: ContextPort, completion: CompletionPort, *,
        audit_research_need: Callable[..., dict[str, Any]],
        write_log: Callable[..., None], update_progress: Callable[..., None],
    ) -> None:
        self.context = context
        self.completion = completion
        self.audit_research_need = audit_research_need
        self.write_log = write_log
        self.update_progress = update_progress

    def finalize(self, ctx: FastCandidateContext, combined_candidate: dict[str, Any]) -> tuple[dict[str, Any], str]:
        self.context.bind_turn_context_to_result(ctx.body, combined_candidate)
        if isinstance(ctx.body.get("_discourseLedger"), Mapping):
            combined_candidate["_referenceLedger"] = dict(
                ctx.body["_discourseLedger"]
            )
        combined_candidate.setdefault("_chatMetrics", {})[
            "personaContextSnapshot"
        ] = ctx.fast_persona_snapshot.safe_diagnostic()
        combined_candidate["personaContextSnapshotId"] = (
            ctx.fast_persona_snapshot.snapshot_id
        )
        if not (ctx.body.get("_cognitiveOutcomeVerification") or {}).get("valid"):
            combined_candidate = self.completion.enforce_fast_dialogue_policy_once(
                ctx.settings,
                ctx.user_message,
                combined_candidate,
                ctx.fast_policy,
                messages=ctx.messages,
                request_id=ctx.request_id,
                persona_snapshot=ctx.fast_persona_snapshot,
            )
        combined_candidate.setdefault("_chatMetrics", {})["conversationEpoch"] = (
            safe_nonnegative_int(
                ctx.body.get("contextEpoch")
                if ctx.body.get("contextEpoch") is not None
                else self.context.current_conversation_epoch()
            )
        )
        if not (ctx.body.get("_cognitiveOutcomeVerification") or {}).get("valid"):
            if (
                ctx.route_execution_plan is not None
                and ctx.route_execution_plan.required_capabilities
            ):
                raise UpstreamProtocolError(
                    "model returned final_answer before required capability: "
                    + ",".join(ctx.route_execution_plan.required_capabilities)
                )
            active_plan = EpistemicPlan.from_dict(
                ctx.prepared_fast_turn.epistemic_plan
            ) or create_epistemic_plan(
                ctx.request_id,
                ctx.fast_interpretation,
                decision=ctx.route_research_decision,
            )
            confidence_gate = knowledge_confidence_gate_decision(
                active_plan.fact_requirements,
                combined_candidate.get("knowledgeConfidence"),
                confidence_provided=bool(
                    combined_candidate.get("knowledgeConfidenceProvided")
                ),
                research_policy=active_plan.research_policy,
                web_permission=bool(ctx.settings.get("permissionWebSearch")),
            )
            self.write_log(
                "fast_knowledge_confidence_gate_decision",
                requestId=ctx.request_id,
                planId=active_plan.plan_id,
                planRevision=active_plan.revision,
                modelConfidence=str(
                    combined_candidate.get("knowledgeConfidence") or ""
                )[:16],
                confidenceProvided=bool(
                    combined_candidate.get("knowledgeConfidenceProvided")
                ),
                decision=confidence_gate.to_dict(),
            )
            if confidence_gate.status == "RESEARCH_ESCALATION":
                ctx.body["_researchEscalation"] = {
                    "reason": "model_knowledge_confidence_below_threshold",
                    "queries": [],
                    "unknownEntities": [],
                    "verificationTargets": list(
                        confidence_gate.missing_verification_targets
                        or active_plan.verification_targets
                        or ("uncertain external fact",)
                    ),
                    "confidence": (
                        0.25
                        if str(combined_candidate.get("knowledgeConfidence")) == "low"
                        else 0.5
                    ),
                    "origin": "knowledge_confidence_gate",
                }
                self.update_progress(
                    ctx.request_id,
                    "research-escalation",
                    "模型对外部事实把握不足，正在联网核实",
                )
                recovered = self.completion.process_chat_request_body(
                    ctx.body,
                    ctx.settings,
                    request_id=ctx.request_id,
                    started_at=ctx.started,
                    preflight=ctx.preflight,
                    persona_snapshot=ctx.fast_persona_snapshot,
                )
                recovered["researchEscalatedFromKnowledgeConfidence"] = True
                return recovered, "research_loop"
            draft_gate = grounded_claim_gate_decision(
                str(combined_candidate.get("reply") or ""),
                active_plan.epistemic_state(),
                None,
                research_policy=(
                    active_plan.research_policy
                    if ctx.settings.get("permissionWebSearch")
                    else "forbidden"
                ),
            )
            if (
                ctx.settings.get("permissionWebSearch")
                and active_plan.research_policy != "forbidden"
                and needs_post_answer_research_audit(
                    active_plan,
                    draft_gate,
                    None,
                )
            ):
                try:
                    semantic_audit = self.audit_research_need(
                        ctx.settings,
                        ctx.user_message,
                        str(combined_candidate.get("reply") or ""),
                        active_plan,
                        request_id=ctx.request_id,
                    )
                    self.write_log(
                        "post_answer_research_need_audit",
                        requestId=ctx.request_id,
                        route="fast_stream",
                        **semantic_audit,
                    )
                    if semantic_audit.get("researchNeeded"):
                        ctx.body["_researchEscalation"] = {
                            "reason": "model_post_answer_semantic_audit",
                            "queries": [semantic_audit["query"]],
                            "unknownEntities": [],
                            "verificationTargets": [
                                "model-identified unsupported external claim"
                            ],
                            "confidence": 0.9,
                            "origin": "post_answer_model_audit",
                        }
                        self.update_progress(
                            ctx.request_id,
                            "research-escalation",
                            "模型审计发现回答依赖实时外部证据，正在核实",
                        )
                        recovered = self.completion.process_chat_request_body(
                            ctx.body,
                            ctx.settings,
                            request_id=ctx.request_id,
                            started_at=ctx.started,
                            preflight=ctx.preflight,
                            persona_snapshot=ctx.fast_persona_snapshot,
                        )
                        recovered["researchEscalatedFromSemanticAudit"] = True
                        return recovered, "research_loop"
                except Exception as audit_error:
                    self.write_log(
                        "post_answer_research_need_audit_failed",
                        requestId=ctx.request_id,
                        route="fast_stream",
                        errorType=type(audit_error).__name__,
                        error=str(audit_error)[:500],
                    )
                    combined_candidate["webEvidenceLimited"] = True
                    combined_candidate["semanticResearchAuditFailed"] = True
        try:
            # The stream has already resolved the current task.  Do
            # not let the finalizer rebuild a weaker contract from an
            # elliptical original message after generation.
            return self.completion.finalize_chat_response(
                ctx.settings,
                ctx.user_message,
                combined_candidate,
                request_id=ctx.request_id,
                started_at=ctx.started,
                preserve_accepted_text=True,
                prepared_turn=ctx.prepared_fast_turn,
                defer_persistence_until_after_utterance=not bool(
                    (ctx.body.get("_cognitiveOverlay") or {}).get("patches")
                ),
                persona_snapshot=ctx.fast_persona_snapshot,
                resolved_interpretation=ctx.fast_interpretation,
                resolved_contract=ctx.fast_contract,
                resolved_dialogue=self.context.task_review_dialogue_messages(ctx.body, ctx.messages),
                cognitive_body=ctx.body,
            ), ctx.route_name
        except FinalProjectionAuditRepairError as audit_error:
            recovered_result = self.completion.recover_final_projection_candidate(
                ctx.body,
                ctx.settings,
                audit_error,
                request_id=ctx.request_id,
                persona_snapshot=ctx.fast_persona_snapshot,
            )
            recovered_prepared = self.context.body_prepared_action_turn(
                ctx.body,
                ctx.user_message,
                request_id=ctx.request_id,
            )
            recovered_interpretation, _, recovered_contract = self.context.turn_contract_context(
                ctx.user_message,
                recovered_prepared,
                task_body=ctx.body,
            )
            finalized = self.completion.finalize_chat_response(
                ctx.settings,
                ctx.user_message,
                recovered_result,
                request_id=ctx.request_id,
                started_at=ctx.started,
                preserve_accepted_text=True,
                prepared_turn=recovered_prepared,
                defer_persistence_until_after_utterance=not bool(
                    (ctx.body.get("_cognitiveOverlay") or {}).get("patches")
                ),
                persona_snapshot=ctx.fast_persona_snapshot,
                resolved_interpretation=recovered_interpretation,
                resolved_contract=recovered_contract,
                resolved_dialogue=self.context.task_review_dialogue_messages(ctx.body, ctx.messages),
                cognitive_body=ctx.body,
            )
            # The rejected fast candidate is still buffered. Returning
            # a distinct route prevents the outer stream from emitting
            # those stale utterances; the repaired finalResult carries
            # the only accepted speech.
            return finalized, "cognitive_final_audit_repair"
        except GroundingGateError as gate_error:
            decision = gate_error.decision
            if (
                getattr(decision, "status", "") != "RESEARCH_ESCALATION"
                or not ctx.settings.get("permissionWebSearch")
            ):
                raise
            ctx.body["_researchEscalation"] = {
                "reason": "grounding_gate_research_escalation",
                "queries": [],
                "unknownEntities": [],
                "verificationTargets": list(
                    getattr(decision, "missing_verification_targets", ())
                    or ("unsupported external claims",)
                ),
                "confidence": 0.45,
                "origin": "final_grounding_gate",
            }
            recovered = self.completion.process_chat_request_body(
                ctx.body,
                ctx.settings,
                request_id=ctx.request_id,
                started_at=ctx.started,
                preflight=ctx.preflight,
                persona_snapshot=ctx.fast_persona_snapshot,
            )
            recovered["researchEscalatedFromGroundingGate"] = True
            return recovered, "research_loop"
