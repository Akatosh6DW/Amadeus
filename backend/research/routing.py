"""把本轮语义审计结果接入统一研究路由。

复用请求 body 中已有审计缓存，按原条件补充近期附件；失败保留原路由。
状态写入只发生在当前 body，同一 EpistemicPlan 的修订由纯规则完成。
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping
from agent.action_grounding import PreparedTurn
from agent.dialogue_architecture import TurnPolicy
from agent.research_architecture import CapabilityEnvelope, EpistemicPlan, ResearchDecision, capability_envelope
from backend.research.fact_policy import apply_preanswer_fact_policy, synchronize_prepared_epistemic_plan

@dataclass(frozen=True)
class ResearchRoutingOperations:
    configured: Callable[..., Any]
    cognitive_turn_state: Callable[..., Any]
    read_attachment_metadata: Callable[..., Any]
    audit_model_preanswer_research_need: Callable[..., Any]

class ResearchRouter:
    def __init__(self, operations: ResearchRoutingOperations, *, write_log: Callable[..., Any]) -> None:
        self.operations = operations
        self.write_log = write_log

    def reconcile_preanswer_research_route(
        self,
        settings: dict[str, Any],
        user_message: str,
        prepared: PreparedTurn,
        decision: ResearchDecision,
        turn_policy: TurnPolicy,
        body: dict[str, Any],
        *,
        request_id: str,
    ) -> tuple[PreparedTurn, ResearchDecision, CapabilityEnvelope]:
        """Apply the one pre-answer semantic research decision to every route."""

        plan = EpistemicPlan.from_dict(prepared.epistemic_plan)
        capability = capability_envelope(
            turn_policy,
            decision,
            web_permission=bool(settings.get("permissionWebSearch")),
        )
        eligible = bool(
            self.operations.configured(settings)
            and settings.get("permissionWebSearch")
            and decision.policy == "allowed"
            and plan is not None
            and any(
                item.verification_required
                and item.assistant_must_assert
                and item.domain == "external_world"
                and item.evidence_class == "web_evidence"
                for item in plan.fact_requirements
            )
        )
        if self.operations.cognitive_turn_state(body) or not eligible or plan is None:
            return prepared, decision, capability

        cached = (
            body.get("_preAnswerResearchAudit")
            if isinstance(body.get("_preAnswerResearchAudit"), dict)
            else None
        )
        audit: dict[str, Any]
        if cached is not None:
            audit = dict(cached)
        else:
            try:
                recent_artifacts: list[dict[str, Any]] = [
                    dict(item)
                    for item in (
                        body.get("_processedAttachments")
                        if isinstance(body.get("_processedAttachments"), list)
                        else []
                    )
                    if isinstance(item, Mapping)
                ]
                if not recent_artifacts:
                    seen_artifacts: set[str] = set()
                    for message in reversed(
                        body.get("messages")
                        if isinstance(body.get("messages"), list)
                        else []
                    ):
                        if not isinstance(message, Mapping):
                            continue
                        meta = message.get("meta") if isinstance(message.get("meta"), Mapping) else {}
                        attachments = (
                            meta.get("attachments")
                            if isinstance(meta.get("attachments"), list)
                            else []
                        )
                        for attachment in reversed(attachments):
                            if not isinstance(attachment, Mapping):
                                continue
                            attachment_id = str(
                                attachment.get("id") or attachment.get("attachmentId") or ""
                            ).strip()
                            if not attachment_id or attachment_id in seen_artifacts:
                                continue
                            seen_artifacts.add(attachment_id)
                            metadata = self.operations.read_attachment_metadata(attachment_id)
                            if isinstance(metadata, Mapping):
                                recent_artifacts.append(dict(metadata))
                            if len(recent_artifacts) >= 2:
                                break
                        if len(recent_artifacts) >= 2:
                            break
                audit = self.operations.audit_model_preanswer_research_need(
                    settings,
                    user_message,
                    plan,
                    request_id=request_id,
                    recent_messages=(
                        body.get("messages")
                        if isinstance(body.get("messages"), list)
                        else ()
                    ),
                    recent_artifacts=recent_artifacts,
                )
                body["_preAnswerResearchAudit"] = dict(audit)
                self.write_log(
                    "pre_answer_research_need_audit",
                    requestId=request_id,
                    **audit,
                )
            except Exception as error:
                body["_preAnswerResearchAudit"] = {
                    "failed": True,
                    "errorType": type(error).__name__,
                }
                self.write_log(
                    "pre_answer_research_need_audit_failed",
                    requestId=request_id,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                )
                return prepared, decision, capability

        prepared = apply_preanswer_fact_policy(prepared, audit)
        body["_preparedActionTurn"] = prepared
        plan = EpistemicPlan.from_dict(prepared.epistemic_plan) or plan
        capability = capability_envelope(
            turn_policy,
            decision,
            web_permission=bool(settings.get("permissionWebSearch")),
        )
        if not audit.get("researchNeeded"):
            return prepared, decision, capability

        verification_level = str(
            audit.get("overallVerificationLevel") or "HARD"
        ).upper()
        audit_reason = (
            "pre_answer_model_soft_research_audit"
            if verification_level == "SOFT"
            else "pre_answer_model_research_audit"
        )
        decision = replace(
            decision,
            policy="required",
            reasons=tuple(dict.fromkeys((
                *decision.reasons,
                audit_reason,
            ))),
            verification_targets=tuple(dict.fromkeys((
                *decision.verification_targets,
                *(
                    item.claim_scope or item.subject
                    for item in plan.fact_requirements
                    if item.verification_required and (item.claim_scope or item.subject)
                ),
            ))),
            confidence=0.95,
            plan_revision=decision.plan_revision + 1,
        )
        prepared, decision = synchronize_prepared_epistemic_plan(prepared, decision)
        body["_preAnswerResearchQuery"] = str(audit.get("query") or "").strip()
        body["_preparedActionTurn"] = prepared
        capability = capability_envelope(
            turn_policy,
            decision,
            web_permission=bool(settings.get("permissionWebSearch")),
        )
        return prepared, decision, capability
