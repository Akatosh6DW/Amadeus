"""研究复核的纯规则：判断是否需要复核，并约束复核输出。

本模块只接收显式计划、结果、用户消息和已读取来源；模型调用、网络访问、
日志与状态更新仍由 server 入口负责。规划、时效、来源和 JSON 解析均从
明确的后端模块导入，避免反向依赖 server。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from agent.research_architecture import EpistemicPlan
from backend.chat.parsing import parse_json_object
from backend.common.text import unique_strings
from backend.research.current_info import web_research_temporal_status
from backend.research.intent import is_external_fact_correction_followup
from backend.research.planning import (
    safe_planned_web_query,
    web_research_anchor,
)
from backend.research.query import web_search_freshness
from backend.research.sources import source_has_readable_evidence
from tools.safe_web import SafeWebError, canonicalize_public_url


def web_research_needs_review(
    plan: dict[str, Any],
    result: dict[str, Any],
    user_message: str,
    messages: list[dict[str, Any]] | None,
) -> bool:
    """判断查询复杂度和证据缺口是否需要模型复核。"""
    queries = plan.get("queries") if isinstance(plan.get("queries"), list) else []
    status = result.get("evidenceStatus") if isinstance(result.get("evidenceStatus"), dict) else {}
    return bool(
        len(queries) > 1
        or plan.get("ambiguities")
        or (bool(status) and not status.get("sufficient"))
        or bool(web_search_freshness(
            f"{plan.get('_originalResolvedQuestion') or plan.get('resolvedQuestion') or ''} {user_message}"
        ))
        or is_external_fact_correction_followup(user_message, messages)
        or re.search(
            r"哪个|哪一个|谁|孰|相比|比较|对比|区别|差异|强弱|优劣|"
            r"\b(?:vs\.?|versus|compare|comparison)\b",
            str(plan.get("resolvedQuestion") or user_message),
            flags=re.I,
        )
    )

def normalize_web_research_review(
    value: Any,
    user_message: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """约束模型复核输出，保留用户目标并核对补查内容和来源。"""
    parsed = parse_json_object(value) if isinstance(value, str) else value
    original_resolved = re.sub(
        r"\s+",
        " ",
        str(
            plan.get("_originalResolvedQuestion")
            or plan.get("resolvedQuestion")
            or user_message
        ),
    ).strip()[:500]
    if not isinstance(parsed, dict):
        return {
            "readyToAnswer": False,
            "correctedResolvedQuestion": original_resolved,
            "evidenceInterpretation": "",
            "entityFindings": [],
            "planIssues": ["review_output_invalid"],
            "temporalAssessment": {},
            "readUrls": [],
            "additionalQueries": [],
        }
    proposed_correction = re.sub(
        r"\s+",
        " ",
        str(parsed.get("correctedResolvedQuestion") or ""),
    ).strip()[:500]
    evidence_interpretation = re.sub(
        r"\s+",
        " ",
        str(parsed.get("evidenceInterpretation") or proposed_correction),
    ).strip()[:500]
    # Retrieval may sharpen an entity mapping, but the evidence it happened to
    # find cannot redefine the user's requested target or time boundary.
    corrected = original_resolved
    additional: list[dict[str, str]] = []
    for item in (
        parsed.get("additionalQueries")
        if isinstance(parsed.get("additionalQueries"), list)
        else []
    ):
        raw_query = item.get("query") if isinstance(item, dict) else item
        query = safe_planned_web_query(
            raw_query,
            user_message,
            f"{corrected} {evidence_interpretation}",
            require_overlap=False,
        )
        if not query:
            continue
        if any(
            str(existing.get("query") or "").casefold() == query.casefold()
            for existing in [*plan.get("queries", []), *additional]
            if isinstance(existing, dict)
        ):
            continue
        purpose = str(item.get("purpose") or "") if isinstance(item, dict) else ""
        additional.append({
            "query": query,
            "purpose": re.sub(r"\s+", " ", purpose).strip()[:160] or "补足首轮证据缺口",
        })
        if len(additional) >= 2:
            break
    plan_issues = [
        re.sub(r"\s+", " ", str(item)).strip()[:180]
        for item in (parsed.get("planIssues") if isinstance(parsed.get("planIssues"), list) else [])
        if str(item).strip()
    ][:6]
    unsupported_mapping = any(
        re.search(r"未.{0,12}支持|没有.{0,12}支持|需确认|错误|不一致|指向别", issue)
        for issue in plan_issues
    )
    if unsupported_mapping:
        # When the review says the first entity mapping was unsupported, go
        # back to the user's own umbrella term. Using the review's corrected
        # guess here would merely search that second guess more confidently.
        anchor = web_research_anchor(user_message)
        existing_queries = [
            str(item.get("query") or "")
            for item in [*plan.get("queries", []), *additional]
            if isinstance(item, dict)
        ]
        if anchor and not any(
            anchor in query and re.search(r"指谁|是谁|含义|所指|出处|作品|称号|身份", query, flags=re.I)
            for query in existing_queries
        ):
            discovery_query = safe_planned_web_query(
                f"{anchor} 指谁 含义 对象",
                user_message,
                corrected,
                require_overlap=True,
            )
            if discovery_query:
                additional.insert(0, {
                    "query": discovery_query,
                    "purpose": "回到用户原始术语确认实际所指，避免继续验证首轮猜测",
                })
                additional = additional[:2]
        corrected = original_resolved
    entity_findings = [
        re.sub(r"\s+", " ", str(item)).strip()[:180]
        for item in (parsed.get("entityFindings") if isinstance(parsed.get("entityFindings"), list) else [])
        if str(item).strip()
    ][:6]
    review_sources = [
        source
        for source in (
            plan.get("_reviewSources")
            if isinstance(plan.get("_reviewSources"), list)
            else []
        )
        if isinstance(source, dict)
    ]
    available_urls = {
        canonicalize_public_url(str(source.get("url") or "").strip())
        for source in review_sources
        if str(source.get("url") or "").strip()
    }
    source_by_id = {
        str(source.get("id") or ""): source
        for source in review_sources
        if str(source.get("id") or "")
    }
    temporal_status = (
        dict(plan.get("_temporalCoverage"))
        if isinstance(plan.get("_temporalCoverage"), Mapping)
        else web_research_temporal_status(
            {"sources": review_sources},
            plan,
            user_message=user_message,
        )
    )
    raw_temporal_assessment = (
        parsed.get("temporalAssessment")
        if isinstance(parsed.get("temporalAssessment"), dict)
        else {}
    )
    assessment_reason = re.sub(
        r"\s+", " ", str(raw_temporal_assessment.get("reason") or "")
    ).strip()[:240]
    unresolved_newer = {
        str(item)
        for item in temporal_status.get("unresolvedNewerSourceIds", [])
        if str(item)
    }
    dismissed_newer: list[dict[str, str]] = []
    for item in (
        raw_temporal_assessment.get("dismissedNewerSources")
        if isinstance(raw_temporal_assessment.get("dismissedNewerSources"), list)
        else []
    ):
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("sourceId") or "").strip()[:40]
        reason = re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()[:180]
        if source_id in unresolved_newer and reason:
            dismissed_newer.append({"sourceId": source_id, "reason": reason})
    dismissed_ids = {item["sourceId"] for item in dismissed_newer}
    remaining_unresolved = unresolved_newer - dismissed_ids
    assessment_source_ids = unique_strings(
        str(item)
        for item in (
            raw_temporal_assessment.get("evidenceSourceIds")
            if isinstance(raw_temporal_assessment.get("evidenceSourceIds"), list)
            else []
        )
        if str(item) in source_by_id
        and str(source_by_id[str(item)].get("sourceTier") or "") in {"A", "B"}
        and source_has_readable_evidence(source_by_id[str(item)])
    )
    review_coverage_complete = bool(
        raw_temporal_assessment.get("coverageComplete")
        and assessment_reason
        and assessment_source_ids
    )
    temporal_required = bool(temporal_status.get("requiresFreshnessEvidence"))
    temporal_coverage_complete = bool(
        not temporal_required
        or (
            not remaining_unresolved
            and (
                temporal_status.get("freshnessConfirmed")
                or review_coverage_complete
            )
        )
    )
    temporal_assessment = {
        "coverageComplete": temporal_coverage_complete,
        "evidenceSourceIds": assessment_source_ids,
        "dismissedNewerSources": dismissed_newer,
        "reason": assessment_reason,
        "unresolvedNewerSourceIds": sorted(remaining_unresolved),
        "latestCandidateDate": str(temporal_status.get("latestCandidateDate") or ""),
        "latestVerifiedDate": str(temporal_status.get("latestVerifiedDate") or ""),
    }
    if temporal_required and not temporal_coverage_complete:
        plan_issues.append("temporal_coverage_unresolved")
        plan_issues = unique_strings(plan_issues)[:6]
    read_urls: list[dict[str, str]] = []
    for item in (
        parsed.get("readUrls")
        if isinstance(parsed.get("readUrls"), list)
        else []
    ):
        raw_url = item.get("url") if isinstance(item, dict) else item
        try:
            url = canonicalize_public_url(str(raw_url or "").strip())
        except (SafeWebError, ValueError, TypeError):
            continue
        if not url or url not in available_urls:
            continue
        if any(existing["url"] == url for existing in read_urls):
            continue
        purpose = str(item.get("purpose") or "") if isinstance(item, dict) else ""
        read_urls.append({
            "url": url,
            "purpose": re.sub(r"\s+", " ", purpose).strip()[:160] or "读取高价值来源正文",
        })
        if len(read_urls) >= 2:
            break
    for source_id in sorted(remaining_unresolved):
        source = source_by_id.get(source_id, {})
        if not source or source_has_readable_evidence(source):
            continue
        try:
            url = canonicalize_public_url(str(source.get("url") or "").strip())
        except (SafeWebError, ValueError, TypeError):
            continue
        if url and url in available_urls and not any(item["url"] == url for item in read_urls):
            read_urls.append({
                "url": url,
                "purpose": "核实比现有正文更新的日期线索是否与用户问题相关",
            })
        if len(read_urls) >= 2:
            break
    return {
        "readyToAnswer": bool(
            parsed.get("readyToAnswer")
            and not additional
            and not read_urls
            and temporal_coverage_complete
        ),
        "correctedResolvedQuestion": corrected,
        "evidenceInterpretation": evidence_interpretation,
        "entityFindings": [] if unsupported_mapping else entity_findings,
        "planIssues": plan_issues,
        "temporalAssessment": temporal_assessment,
        "readUrls": read_urls,
        "additionalQueries": additional,
    }

__all__ = ["web_research_needs_review", "normalize_web_research_review"]


def needs_post_answer_research_audit(
    epistemic_plan: EpistemicPlan,
    gate_decision: Any,
    receipt: Any = None,
) -> bool:
    """Select candidates for semantic review without deciding to browse.

    The deterministic layer may notice that a draft contains an asserted
    public-world fact whose answer obligation expects external evidence.  That
    observation only schedules a model review; it never authorizes Web access.
    """

    if receipt is not None and getattr(receipt, "status", "") == "completed":
        return False
    has_external_requirement = any(
        item.verification_required
        and item.assistant_must_assert
        and item.domain == "external_world"
        and item.evidence_class == "web_evidence"
        for item in epistemic_plan.fact_requirements
    )
    has_asserted_external_claim = any(
        getattr(item, "claim_type", "") == "external_fact"
        and getattr(item, "assertion_mode", "") == "assertive"
        and getattr(item, "adopted_as_truth", False)
        for item in (getattr(gate_decision, "claims", ()) or ())
    )
    return bool(has_external_requirement and has_asserted_external_claim)
