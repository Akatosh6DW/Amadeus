"""本轮事实范围与研究计划的纯投影规则。

把语义审计给出的 NONE/SOFT/HARD 约束绑定到具体断言，再同步同一轮计划。
本模块不调用模型，不获取证据，不修改用户持久资料。
"""
from __future__ import annotations
from dataclasses import replace
from typing import Any, Mapping
from agent.action_grounding import PreparedTurn
from agent.fingerprints import runtime_fingerprint
from agent.research_architecture import EpistemicPlan, ResearchDecision

def apply_preanswer_fact_policy(
    prepared: PreparedTurn,
    audit: Mapping[str, Any],
) -> PreparedTurn:
    """Bind one semantic NONE/SOFT/HARD decision to concrete claim scopes."""

    plan = EpistemicPlan.from_dict(prepared.epistemic_plan)
    if plan is None:
        return prepared
    external = tuple(
        item for item in plan.fact_requirements
        if item.domain == "external_world" and item.evidence_class == "web_evidence"
    )
    if not external:
        return prepared
    overall_level = str(audit.get("overallVerificationLevel") or "HARD").upper()
    if overall_level not in {"NONE", "SOFT", "HARD"}:
        overall_level = "HARD"
    raw_claims = audit.get("claims") if isinstance(audit.get("claims"), list) else []
    scoped_claims = [item for item in raw_claims if isinstance(item, Mapping)]
    safe_reasoning = bool(audit.get("safeReasoningAvailable"))
    base = external[0]
    replacements = []
    if scoped_claims:
        for index, item in enumerate(scoped_claims):
            scope = str(item.get("claim") or "").strip()[:500]
            level = str(item.get("verificationLevel") or overall_level).upper()
            if not scope or level not in {"NONE", "SOFT", "HARD"}:
                continue
            dependency = max(0.0, min(1.0, float(item.get("answerDependency", 0.0))))
            risk = max(0.0, min(1.0, float(item.get("epistemicRisk", 0.0))))
            scoped_id = "fact_scope_" + runtime_fingerprint(
                f"{base.requirement_id}\x1f{index}\x1f{scope}"
            )
            replacements.append(replace(
                base,
                requirement_id=scoped_id,
                subject=scope,
                verification_required=level != "NONE",
                verification_level=level,
                answer_dependency=dependency,
                epistemic_risk=risk,
                claim_scope=scope,
                safe_reasoning_available=safe_reasoning or level != "HARD",
                reason="pre_answer_claim_policy",
            ))
    if not replacements:
        replacements = [replace(
            item,
            verification_required=overall_level != "NONE",
            verification_level=overall_level,
            answer_dependency=1.0 if overall_level == "HARD" else 0.0,
            epistemic_risk=1.0 if overall_level == "HARD" else 0.0,
            claim_scope=item.subject,
            safe_reasoning_available=safe_reasoning or overall_level != "HARD",
            reason="pre_answer_claim_policy",
        ) for item in external]
    non_external = tuple(item for item in plan.fact_requirements if item not in external)
    requirements = tuple((*non_external, *replacements))
    required_evidence = tuple(dict.fromkeys(
        item.evidence_class for item in requirements
        if item.verification_required and item.verification_level == "HARD"
    ))
    plan = replace(
        plan,
        revision=plan.revision + 1,
        fact_requirements=requirements,
        required_evidence_classes=required_evidence,
    )
    return replace(
        prepared,
        epistemic_plan=plan.to_dict(),
        epistemic_state=plan.epistemic_state().to_dict(),
    )


def synchronize_prepared_epistemic_plan(
    prepared: PreparedTurn,
    decision: ResearchDecision,
) -> tuple[PreparedTurn, ResearchDecision]:
    """Apply a policy revision to the same per-turn EpistemicPlan."""

    plan = EpistemicPlan.from_dict(prepared.epistemic_plan)
    if plan is None:
        return replace(prepared, research_decision=decision.to_dict()), decision
    changed = bool(
        plan.research_policy != decision.policy
        or plan.policy_reasons != decision.reasons
        or plan.verification_targets != decision.verification_targets
        or plan.unknown_entities != decision.unknown_entities
        or plan.unstable_claims != decision.unstable_claims
        or plan.primary_source_required != decision.primary_source_required
        or plan.source_policy_id != decision.source_policy_id
    )
    if changed:
        plan = replace(
            plan,
            revision=max(plan.revision + 1, decision.plan_revision),
            research_policy=decision.policy,
            policy_reasons=decision.reasons,
            verification_targets=decision.verification_targets,
            unknown_entities=decision.unknown_entities,
            unstable_claims=decision.unstable_claims,
            primary_source_required=decision.primary_source_required,
            source_policy_id=decision.source_policy_id,
            confidence=decision.confidence,
            may_escalate=decision.policy != "forbidden",
            allowed_evidence_classes=tuple(
                item for item in plan.allowed_evidence_classes if item != "web_evidence"
            ) + (("web_evidence",) if decision.policy != "forbidden" else ()),
            required_evidence_classes=(
                tuple(dict.fromkeys((*plan.required_evidence_classes, "web_evidence")))
                if decision.policy == "required"
                else tuple(
                    item for item in plan.required_evidence_classes
                    if item != "web_evidence"
                )
            ),
        )
    aligned_decision = plan.research_decision()
    return replace(
        prepared,
        research_decision=aligned_decision.to_dict(),
        epistemic_plan=plan.to_dict(),
        epistemic_state=plan.epistemic_state().to_dict(),
    ), aligned_decision
