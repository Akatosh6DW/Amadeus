"""研究复核的纯规则：判断是否需要复核，并约束复核输出。

本模块只接收显式计划、结果、用户消息和已读取来源；模型调用、网络访问、
日志与状态更新仍由 server 入口负责。规划、时效、来源和 JSON 解析均从
明确的后端模块导入，避免反向依赖 server。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Mapping

from agent.research_architecture import EpistemicPlan
from backend.chat.parsing import parse_json_object
from backend.common.text import unique_strings
from backend.research.current_info import (
    web_research_temporal_contract,
    web_research_temporal_status,
)
from backend.research.intent import is_external_fact_correction_followup
from backend.research.planning import (
    is_retrieval_diagnostic_query,
    normalize_research_query_strategy,
    query_reason_field,
    safe_planned_web_query,
    web_research_anchor,
)
from backend.research.sources import source_has_readable_evidence
from tools.safe_web import SafeWebError, canonicalize_public_url
from tools.web_links import compact_web_links


def _source_available_urls(source: Mapping[str, Any]) -> set[str]:
    """Return base and extracted detail URLs allowed by the review contract."""

    candidates = [source.get("url")]
    candidates.extend(
        link.get("url")
        for link in compact_web_links(
            source.get("links"), base_url=str(source.get("url") or ""), max_links=24,
        )
    )
    available: set[str] = set()
    for candidate in candidates:
        try:
            parsed = urllib.parse.urlsplit(str(candidate or "").strip())
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                continue
            canonical = canonicalize_public_url(str(candidate or "").strip())
        except (SafeWebError, ValueError, TypeError):
            continue
        if canonical:
            available.add(canonical)
    return available


def _structured_research_obligations(plan: Mapping[str, Any]) -> list[str]:
    """Collect explicit answer obligations without inventing a new gate."""

    fact_values: list[str] = []
    for key in ("verificationTargets", "factsToVerify"):
        raw = plan.get(key)
        if isinstance(raw, list):
            fact_values.extend(str(item).strip() for item in raw if str(item).strip())
    analysis = plan.get("questionAnalysis")
    if isinstance(analysis, Mapping):
        raw = analysis.get("factsToVerify")
        if isinstance(raw, list):
            fact_values.extend(str(item).strip() for item in raw if str(item).strip())
    if fact_values:
        # answerRequirements often describes the same fact in presentation
        # language; count the explicit fact fields once to avoid turning a
        # stable one-fact plan into a multi-obligation review.
        return unique_strings(fact_values)
    requirements = plan.get("answerRequirements")
    raw_requirements = requirements if isinstance(requirements, list) else []
    return unique_strings(
        str(item).strip() for item in raw_requirements if str(item).strip()
    )


def web_research_needs_review(
    plan: dict[str, Any],
    result: dict[str, Any],
    user_message: str,
    messages: list[dict[str, Any]] | None,
) -> bool:
    """判断查询复杂度和证据缺口是否需要模型复核。"""
    queries = plan.get("queries") if isinstance(plan.get("queries"), list) else []
    status = result.get("evidenceStatus") if isinstance(result.get("evidenceStatus"), dict) else {}
    temporal_contract = web_research_temporal_contract(user_message, plan)
    obligations = _structured_research_obligations(plan)
    review_was_applied = any(
        key in plan for key in ("planIssues", "entityFindings", "evidenceInterpretation")
    )
    # The main model can request one search before it has emitted a structured
    # fact plan. A relevant first page is not enough to infer that all of the
    # user's requested fields were covered, so give the reviewer one bounded
    # opportunity to select detail links or queue a missing query. This marker
    # is limited to the shortcut path; normal single-concept plans keep their
    # existing fast path.
    unstructured_model_shortcut = bool(
        str(plan.get("plannedBy") or "") == "query_loop_main_model"
        and not obligations
        and queries
        and not review_was_applied
    )
    multiple_explicit_obligations = len(obligations) > 1 and not review_was_applied
    return bool(
        len(queries) > 1
        or plan.get("ambiguities")
        or (bool(status) and not status.get("sufficient"))
        or bool(temporal_contract.get("requiresFreshnessEvidence"))
        or multiple_explicit_obligations
        or unstructured_model_shortcut
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
        if not query and is_retrieval_diagnostic_query(raw_query):
            # A malformed review must not become an outbound request.  Keep a
            # provider/locale route change useful by rebuilding a conservative
            # query from the user's own subject; the discarded report text is
            # never used as query material.
            query = safe_planned_web_query(
                web_research_anchor(user_message, corrected),
                user_message,
                corrected,
                require_overlap=True,
            )
        if not query:
            continue
        strategy = normalize_research_query_strategy(item)
        if any(
            str(existing.get("query") or "").casefold() == query.casefold()
            and normalize_research_query_strategy(existing) == strategy
            for existing in [*plan.get("queries", []), *additional]
            if isinstance(existing, dict)
        ):
            continue
        reason_field, reason = query_reason_field(item, "补足首轮证据缺口")
        additional_item = {
            "query": query,
            reason_field: reason,
        }
        additional_item.update(strategy)
        additional.append(additional_item)
        if len(additional) >= 2:
            break
    plan_issues = [
        re.sub(r"\s+", " ", str(item)).strip()[:180]
        for item in (parsed.get("planIssues") if isinstance(parsed.get("planIssues"), list) else [])
        if str(item).strip()
    ][:6]
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
        url
        for source in review_sources
        for url in _source_available_urls(source)
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
    read_urls: list[dict[str, Any]] = []
    for item in (
        parsed.get("readUrls")
        if isinstance(parsed.get("readUrls"), list)
        else []
    ):
        raw_url = item.get("url") if isinstance(item, dict) else item
        try:
            raw_url_text = str(raw_url or "").strip()
            parsed_url = urllib.parse.urlsplit(raw_url_text)
            if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
                continue
            url = canonicalize_public_url(raw_url_text)
        except (SafeWebError, ValueError, TypeError):
            continue
        if not url:
            continue
        if any(existing["url"] == url for existing in read_urls):
            continue
        reason_field, reason = query_reason_field(item, "读取高价值来源正文")
        read_item: dict[str, Any] = {"url": url, reason_field: reason}
        if url not in available_urls:
            # A model may know a public entry point even when the search
            # adapter returned no usable source row.  Keep it as an explicit
            # next-step candidate, never as already-observed evidence.  The
            # URL is still HTTP(S)-checked and canonicalized here; the
            # existing read tool performs public-address validation before it
            # opens the page. Do not extract or construct URLs from prose.
            read_item["candidate"] = True
            read_item["sourceStatus"] = "candidate"
        read_urls.append(read_item)
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
        "entityFindings": entity_findings,
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
