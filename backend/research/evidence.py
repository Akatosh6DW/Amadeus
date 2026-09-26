"""研究结果的证据切片、合并、门禁和循环快照纯规则。

本模块只处理传入的搜索结果和计划值，不访问网络、文件或数据库；查询、
来源分级与时间门禁通过明确的 research 子模块提供，避免反向依赖 server。
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from agent.fingerprints import runtime_fingerprint
from backend.common.text import unique_strings
from backend.research.query import (
    comparison_entity_search_terms,
    comparison_query_entities,
    research_source_policy_id,
    web_query_overlap_tokens,
    web_query_subject_core,
)
from backend.research.sources import (
    classify_web_source,
    source_has_readable_evidence,
    source_satisfies_policy,
    stable_source_uid,
)
from backend.research.current_info import web_research_temporal_status
from tools.safe_web import canonicalize_public_url


def extract_relevant_evidence_chunks(
    source: dict[str, Any],
    query: str,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """选择有界正文片段，并排除可能嵌入的指令内容。"""

    body = re.sub(r"\r\n?", "\n", str(source.get("body") or "")).strip()
    if not body:
        return []
    terms = [
        term for term in web_query_overlap_tokens(query)
        if len(term) >= 2
    ][:24]
    candidates = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in re.split(r"\n{2,}|(?<=[。！？.!?])\s+", body)
        if len(re.sub(r"\s+", " ", paragraph).strip()) >= 45
    ]
    ranked: list[tuple[int, int, str]] = []
    injection_pattern = re.compile(
        r"ignore (?:all |previous )?instructions|system prompt|developer message|"
        r"忽略(?:以上|之前|此前)(?:指令|要求)|系统提示词|开发者消息|"
        r"输出(?:密钥|密码)|调用工具",
        re.I,
    )
    for index, passage in enumerate(candidates):
        if injection_pattern.search(passage):
            continue
        lower = passage.casefold()
        score = sum(1 for term in terms if term.casefold() in lower)
        ranked.append((score, -index, passage[:1200]))
    ranked.sort(reverse=True)
    chunks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for score, _, passage in ranked:
        signature = runtime_fingerprint(passage, length=16)
        if signature in seen:
            continue
        seen.add(signature)
        chunks.append({
            "chunkId": f"{source.get('id') or 'S'}-C{len(chunks) + 1}",
            "text": passage,
            "relevanceScore": score,
        })
        if len(chunks) >= limit:
            break
    return chunks


def generic_web_evidence_status(result: dict[str, Any], query: str) -> dict[str, Any]:
    """判断普通搜索是否返回了可读且与查询相关的证据。"""

    sources = [
        classify_web_source(item, query)
        for item in (result.get("sources") if isinstance(result.get("sources"), list) else [])
        if isinstance(item, dict) and str(item.get("url") or item.get("title") or "").strip()
    ]
    cleaned_query = web_query_subject_core(query)
    cleaned_query = re.sub(
        r"什么|怎么回事|有没有|多少|请问|资料|有什么|特点|性格|原理|是谁",
        " ",
        cleaned_query,
        flags=re.I,
    )
    query_terms: list[str] = [
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", cleaned_query)
    ]
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned_query):
        if len(segment) <= 10:
            query_terms.append(segment)
        for size in range(min(6, len(segment)), 2, -1):
            query_terms.extend(segment[index:index + size] for index in range(len(segment) - size + 1))
    query_terms = unique_strings(query_terms)[:48]
    substantive_ids: list[str] = []
    relevant_ids: list[str] = []
    relevant_substantive_ids: list[str] = []
    matched_terms: dict[str, list[str]] = {}
    for index, source in enumerate(sources, 1):
        source_id = str(source.get("id") or f"S{index}")
        text = " ".join(
            str(source.get(key) or "")
            for key in ("title", "domain", "url", "snippet", "excerpt", "body")
        )
        compact = re.sub(r"\s+", " ", text).strip()
        if source_has_readable_evidence(source):
            substantive_ids.append(source_id)
        source_lower = compact.lower()
        matches = [term for term in query_terms if term and term in source_lower]
        if matches:
            relevant_ids.append(source_id)
            matched_terms[source_id] = matches[:8]
    relevant_substantive_ids = [
        source_id for source_id in relevant_ids if source_id in set(substantive_ids)
    ]
    comparison_entities = comparison_query_entities(query)
    comparison_requested = bool(comparison_entities or re.search(
        r"哪个|哪一个|相比|比较|对比|区别|差异|强于|弱于|优于|不如|"
        r"\b(?:vs\.?|versus|compare|comparison)\b",
        str(query or ""),
        flags=re.I,
    ))
    entity_source_ids: dict[str, list[str]] = {}
    for entity in comparison_entities:
        entity_terms = comparison_entity_search_terms(entity)
        source_ids: list[str] = []
        for index, source in enumerate(sources, 1):
            source_id = str(source.get("id") or f"S{index}")
            source_text = " ".join(
                str(source.get(key) or "")
                for key in ("title", "domain", "url", "snippet", "excerpt", "body")
            ).lower()
            matched_entity_terms = [
                term for term in entity_terms if term.lower() in source_text
            ]
            if not matched_entity_terms or source_id not in set(substantive_ids):
                continue
            # Very short CJK names can also be ordinary words. Treat a source
            # as comparison evidence only when it co-occurs with the other
            # compared subject; otherwise pages about an unrelated use of the
            # same two characters can make a false-positive comparison.
            if any(
                bool(re.fullmatch(r"[\u3400-\u9fff]{1,2}", term))
                for term in matched_entity_terms
            ) and not any(
                any(alias.lower() in source_text for alias in comparison_entity_search_terms(other))
                for other in comparison_entities
                if other != entity
            ) and str(source.get("researchTarget") or "") != entity:
                continue
            source_ids.append(source_id)
        entity_source_ids[entity] = source_ids
    missing_comparison_entities = [
        entity for entity, source_ids in entity_source_ids.items() if not source_ids
    ]
    comparison_relevant_substantive_ids = [
        source_id
        for source_id in relevant_substantive_ids
        if any(source_id in source_ids for source_ids in entity_source_ids.values())
    ]
    minimum_sources = 2 if comparison_requested else 1
    qualifying_source_ids = (
        comparison_relevant_substantive_ids
        if comparison_requested
        else relevant_substantive_ids
    )
    sufficient = (
        bool(query_terms)
        and len(qualifying_source_ids) >= minimum_sources
        and not missing_comparison_entities
    )
    return {
        "kind": "generic",
        "query": str(query or "")[:500],
        "sourceCount": len(sources),
        "substantiveSourceIds": substantive_ids,
        "relevantSourceIds": relevant_ids,
        "relevantSubstantiveSourceIds": relevant_substantive_ids,
        "queryTerms": query_terms,
        "matchedTerms": matched_terms,
        "comparisonRequested": comparison_requested,
        "comparisonEntities": comparison_entities,
        "comparisonEntitySourceIds": entity_source_ids,
        "comparisonRelevantSubstantiveSourceIds": comparison_relevant_substantive_ids,
        "missingComparisonEntities": missing_comparison_entities,
        "minimumRelevantSources": minimum_sources,
        "sufficient": sufficient,
        "complete": sufficient,
    }


def merge_web_search_results(primary: dict[str, Any], supplemental: dict[str, Any]) -> dict[str, Any]:
    """按规范化来源身份合并检索结果，优先保留正文和查询索引。"""
    merged = dict(primary)
    date_filter_outcomes: list[dict[str, Any]] = []
    seen_date_filters: set[str] = set()
    for result in (primary, supplemental):
        raw_outcomes = (
            result.get("dateFilterOutcomes")
            if isinstance(result.get("dateFilterOutcomes"), list)
            else [result.get("dateFilter")]
        )
        for outcome in raw_outcomes:
            if not isinstance(outcome, dict):
                continue
            signature = json.dumps(outcome, ensure_ascii=False, sort_keys=True, default=str)
            if signature in seen_date_filters:
                continue
            seen_date_filters.add(signature)
            date_filter_outcomes.append(dict(outcome))
    sources: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for result in (primary, supplemental):
        for source in result.get("sources") if isinstance(result.get("sources"), list) else []:
            if not isinstance(source, dict):
                continue
            canonical_url = canonicalize_public_url(str(source.get("url") or "").strip())
            identity = str(
                source.get("sourceUid")
                or canonical_url
                or source.get("title")
                or ""
            ).strip()
            if not identity:
                continue
            if identity in positions:
                existing = sources[positions[identity]]
                for key, value in source.items():
                    if key == "researchQueryIndexes" and isinstance(value, list):
                        existing[key] = sorted({
                            *(
                                int(item) for item in existing.get(key, [])
                                if isinstance(item, int) or str(item).isdigit()
                            ),
                            *(
                                int(item) for item in value
                                if isinstance(item, int) or str(item).isdigit()
                            ),
                        })
                        continue
                    if value and (not existing.get(key) or key in {"body", "snippet", "accessedAt"}):
                        existing[key] = value
                continue
            item = dict(source)
            if canonical_url:
                item["url"] = canonical_url
                item["canonicalUrl"] = canonical_url
            item["sourceUid"] = str(
                item.get("sourceUid")
                or stable_source_uid(str(item.get("url") or ""))
            )
            item["id"] = f"S{len(sources) + 1}"
            positions[identity] = len(sources)
            sources.append(item)
    merged["sources"] = sources
    merged["supplementalSearch"] = True
    if date_filter_outcomes:
        merged["dateFilterOutcomes"] = date_filter_outcomes
    if "cacheHit" in primary or "cacheHit" in supplemental:
        merged["cacheHit"] = bool(primary.get("cacheHit") and supplemental.get("cacheHit"))
    return merged


def planned_web_evidence_status(
    result: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """按查询覆盖、来源策略、严格锚点和时间门禁验证计划证据。"""

    resolved_question = str(plan.get("resolvedQuestion") or "")
    policy_id = str(
        plan.get("sourcePolicyId")
        or research_source_policy_id(resolved_question)
    )
    question_analysis = (
        plan.get("questionAnalysis")
        if isinstance(plan.get("questionAnalysis"), dict)
        else {}
    )
    entity_terms = [
        *(
            question_analysis.get("protectedTerms")
            if isinstance(question_analysis.get("protectedTerms"), list)
            else []
        ),
        *(
            plan.get("entityTerms")
            if isinstance(plan.get("entityTerms"), list)
            else []
        ),
    ]
    sources = [
        classify_web_source(
            source,
            resolved_question,
            source_policy_id=policy_id,
            entity_terms=entity_terms,
        )
        for source in (result.get("sources") if isinstance(result.get("sources"), list) else [])
        if isinstance(source, dict)
    ]
    substantive_ids: list[str] = []
    primary_substantive_ids: list[str] = []
    secondary_source_ids: list[str] = []
    rejected_source_ids: list[str] = []
    policy_source_ids: list[str] = []
    for index, source in enumerate(sources, 1):
        source_id = str(source.get("id") or f"S{index}")
        readable = source_has_readable_evidence(source)
        if (
            policy_id == "official_entity"
            and str(source.get("sourceType") or "") in {
                "official_entity_homepage",
                "official_documentation",
            }
            and len(str(source.get("body") or "").strip()) < 120
        ):
            # A search snippet can identify a likely official domain, but it
            # cannot prove what the site actually provides.  QueryLoop must
            # read the page/About/docs body before the evidence gate passes.
            readable = False
        if not readable:
            rejected_source_ids.append(source_id)
            continue
        substantive_ids.append(source_id)
        if source_satisfies_policy(source, policy_id):
            policy_source_ids.append(source_id)
        if str(source.get("sourceTier") or "") in {"C", "D"}:
            secondary_source_ids.append(source_id)
        else:
            primary_substantive_ids.append(source_id)
    # Eligibility belongs to the requested source policy. Tier C is still a
    # readable secondary source for general facts; it must not disappear just
    # because stricter policies require official or primary material.
    policy_eligible_ids = set(policy_source_ids)
    queries = [
        str(item.get("query") or "")
        for item in (plan.get("queries") if isinstance(plan.get("queries"), list) else [])
        if isinstance(item, dict) and str(item.get("query") or "").strip()
    ]
    covered_query_indexes: set[int] = set()
    relevant_ids: list[str] = []
    relevant_substantive_ids: list[str] = []
    query_statuses: list[dict[str, Any]] = []
    for query_index, query in enumerate(queries, 1):
        query_sources = [
            source
            for source in sources
            if query_index in {
                int(item)
                for item in source.get("researchQueryIndexes", [])
                if isinstance(item, int) or str(item).isdigit()
            }
        ]
        status = generic_web_evidence_status({"sources": query_sources}, query)
        matched = [
            str(item)
            for item in status.get("relevantSubstantiveSourceIds", [])
            if str(item) in policy_eligible_ids
        ]
        query_statuses.append({
            "queryIndex": query_index,
            "query": query[:240],
            "relevantSourceIds": status.get("relevantSourceIds", []),
            "relevantSubstantiveSourceIds": matched,
            "sufficient": bool(matched),
        })
        relevant_ids.extend(
            str(item)
            for item in status.get("relevantSourceIds", [])
            if str(item)
        )
        if matched:
            covered_query_indexes.add(query_index)
            relevant_substantive_ids.extend(matched)
    relevant_ids = unique_strings(relevant_ids)
    relevant_substantive_ids = unique_strings(relevant_substantive_ids)
    user_anchor = str(plan.get("userAnchor") or "").strip()
    anchor_status = (
        generic_web_evidence_status({"sources": sources}, user_anchor)
        if user_anchor else {}
    )
    anchor_relevant_ids = [
        str(item)
        for item in anchor_status.get("relevantSourceIds", [])
        if str(item)
    ]
    anchor_relevant_substantive_ids = [
        str(item)
        for item in anchor_status.get("relevantSubstantiveSourceIds", [])
        if str(item)
    ]
    if user_anchor and plan.get("strictAnchorMatch"):
        compact_anchor = re.sub(r"[\W_]+", "", user_anchor, flags=re.UNICODE).casefold()
        exact_anchor_ids: list[str] = []
        for index, source in enumerate(sources, 1):
            source_id = str(source.get("id") or f"S{index}")
            source_text = " ".join(
                str(source.get(key) or "")
                for key in ("title", "snippet", "excerpt", "body")
            )
            compact_source = re.sub(r"[\W_]+", "", source_text, flags=re.UNICODE).casefold()
            if compact_anchor and compact_anchor in compact_source:
                exact_anchor_ids.append(source_id)
        anchor_relevant_ids = exact_anchor_ids
        anchor_relevant_substantive_ids = [
            source_id for source_id in exact_anchor_ids if source_id in policy_eligible_ids
        ]
        relevant_ids = [
            source_id for source_id in relevant_ids if source_id in set(exact_anchor_ids)
        ]
        relevant_substantive_ids = [
            source_id
            for source_id in relevant_substantive_ids
            if source_id in set(anchor_relevant_substantive_ids)
        ]
    # Normal plans are already checked against every model-selected query.
    # Requiring a literal user-anchor match as well breaks multilingual cases
    # such as Chinese “伦敦天气” -> English “London weather”.  Keep the extra
    # hard anchor only for ambiguous umbrella terms where the planner explicitly
    # requested strict matching.
    anchor_covered = (
        not plan.get("strictAnchorMatch")
        or not user_anchor
        or bool(anchor_relevant_substantive_ids)
    )
    minimum_sources = 2 if len(queries) > 1 else 1
    requires_primary = policy_id in {
        "academic_primary", "official_policy", "official_software",
        "official_entity", "current_event", "temporal_primary",
    }
    required_source_satisfied = bool(policy_source_ids) or not requires_primary
    temporal_coverage = web_research_temporal_status(
        result,
        plan,
        relevant_source_ids=relevant_ids,
        policy_source_ids=policy_source_ids,
    )
    base_sufficient = (
        len(relevant_substantive_ids) >= minimum_sources
        and len(covered_query_indexes) == len(queries)
        and anchor_covered
        and required_source_satisfied
    )
    sufficient = bool(base_sufficient and temporal_coverage.get("coverageSufficient"))
    eligible_answer_ids = set(relevant_substantive_ids)
    if requires_primary:
        eligible_answer_ids &= set(policy_source_ids)
    answer_source_ids = unique_strings(
        source_id for source_id in relevant_substantive_ids
        if source_id in eligible_answer_ids
    )
    return {
        "kind": "planned_research",
        "resolvedQuestion": resolved_question[:500],
        "sourcePolicyId": policy_id,
        "queries": queries,
        "sourceCount": len(sources),
        "substantiveSourceIds": substantive_ids,
        "primarySubstantiveSourceIds": primary_substantive_ids,
        "secondarySourceIds": secondary_source_ids,
        "rejectedSourceIds": rejected_source_ids,
        "policySourceIds": policy_source_ids,
        "answerSourceIds": answer_source_ids,
        "requiredSourceSatisfied": required_source_satisfied,
        "baseSufficient": base_sufficient,
        "temporalCoverage": temporal_coverage,
        "relevantSourceIds": relevant_ids,
        "relevantSubstantiveSourceIds": relevant_substantive_ids,
        "anchorRelevantSourceIds": anchor_relevant_ids,
        "anchorRelevantSubstantiveSourceIds": anchor_relevant_substantive_ids,
        "userAnchor": user_anchor,
        "anchorCovered": anchor_covered,
        "queryStatuses": query_statuses,
        "coveredQueryIndexes": sorted(covered_query_indexes),
        "minimumRelevantSources": minimum_sources,
        "sufficient": sufficient,
        "complete": sufficient,
    }


def research_evidence_fingerprint(result: dict[str, Any]) -> tuple[str, ...]:
    """生成研究证据快照指纹，用于循环判断是否取得新证据。"""
    fingerprints: list[str] = []
    for source in (
        result.get("sources")
        if isinstance(result.get("sources"), list)
        else []
    ):
        if not isinstance(source, dict):
            continue
        identity = str(
            source.get("sourceUid")
            or source.get("canonicalUrl")
            or source.get("url")
            or ""
        ).strip()
        if not identity:
            continue
        fingerprints.append(
            "|".join([
                identity,
                str(source.get("fetchStatus") or ""),
                str(len(str(source.get("body") or ""))),
                str(len(str(source.get("snippet") or source.get("excerpt") or ""))),
            ])
        )
    return tuple(sorted(set(fingerprints)))


def research_loop_sources(result: dict[str, Any]) -> list[dict[str, Any]]:
    """返回结果中的字典来源，保持循环读取的窄边界。"""
    return [
        source
        for source in (
            result.get("sources")
            if isinstance(result.get("sources"), list)
            else []
        )
        if isinstance(source, dict)
    ]


__all__ = [
    "extract_relevant_evidence_chunks",
    "generic_web_evidence_status",
    "merge_web_search_results",
    "planned_web_evidence_status",
    "research_evidence_fingerprint",
    "research_loop_sources",
]
