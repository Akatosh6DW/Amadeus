"""研究结果的证据切片、合并、门禁和循环快照纯规则。

本模块只处理传入的搜索结果和计划值，不访问网络、文件或数据库；查询、
来源分级与时间门禁通过明确的 research 子模块提供，避免反向依赖 server。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Sequence

from backend.common.text import unique_strings
from backend.research.query import (
    comparison_entity_search_terms,
    comparison_query_entities,
    research_source_policy_id,
    web_query_subject_core,
)
from backend.research.sources import (
    classify_web_source,
    source_has_readable_evidence,
    source_fetch_state,
    source_satisfies_policy,
    stable_source_uid,
)
from backend.research.current_info import web_research_temporal_status
from tools.safe_web import SafeWebError, canonicalize_public_url
from tools.web_text import extract_query_evidence_chunks, project_web_text


def extract_relevant_evidence_chunks(
    source: dict[str, Any],
    query: str,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """选择查询有正向覆盖的有界正文片段，并排除嵌入式指令。"""

    return extract_query_evidence_chunks(
        source,
        query,
        limit=limit,
        max_chunk_chars=1200,
    )


def _body_covers_query_terms(body: Any, query_terms: Sequence[str]) -> bool:
    """Require query overlap in fetched prose, with weak-token safeguards."""

    body_lower = re.sub(r"\s+", " ", str(body or "")).casefold()
    if not body_lower or not query_terms:
        return False
    # Search names and identifiers in a separator-insensitive form as well.
    # Search results commonly render ``learncomputerscience`` as
    # ``Learn Computer Science`` (or ``new-york`` as ``New York``).  This
    # normalization is deliberately confined to the fetched body; metadata
    # alone still cannot satisfy the substantive-evidence gate below.
    compact_body = re.sub(r"[\W_]+", "", body_lower, flags=re.UNICODE)
    matches: set[str] = set()
    for term in query_terms:
        term_lower = str(term or "").casefold()
        if not term_lower:
            continue
        if term_lower in body_lower:
            matches.add(term_lower)
            continue
        compact_term = re.sub(r"[\W_]+", "", term_lower, flags=re.UNICODE)
        # Do not turn a short weak token such as ``api`` into an arbitrary
        # substring match after punctuation removal.  Chinese terms remain
        # eligible at two characters because the query builder uses Chinese
        # n-grams as meaningful anchors.
        if (
            compact_term
            and (len(compact_term) >= 3 or re.search(r"[\u3400-\u9fff]", compact_term))
            and compact_term in compact_body
        ):
            matches.add(term_lower)
    if not matches:
        return False
    # Short Latin tokens such as ``rfc`` or ``api`` are useful only when the
    # query has no stronger anchor.  Otherwise a generic site heading can make
    # metadata-only or unrelated pages look like read evidence.
    strong_query_terms = {
        term.casefold()
        for term in query_terms
        if len(term) >= 4 or any(character.isdigit() for character in term)
        or re.search(r"[\u3400-\u9fff]", term)
    }
    strong_matches = matches & strong_query_terms
    if strong_matches:
        return True
    return len(matches) >= min(2, len(set(query_terms)))


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
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{2,}", cleaned_query)
    ]
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned_query):
        if len(segment) <= 10:
            query_terms.append(segment)
        for size in range(min(6, len(segment)), 1, -1):
            query_terms.extend(segment[index:index + size] for index in range(len(segment) - size + 1))
    query_terms = unique_strings(query_terms)[:48]
    substantive_ids: list[str] = []
    relevant_ids: list[str] = []
    relevant_substantive_ids: list[str] = []
    body_relevant_ids: set[str] = set()
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
        if (
            source_has_readable_evidence(source)
            and _body_covers_query_terms(source.get("body"), query_terms)
        ):
            body_relevant_ids.add(source_id)
    relevant_substantive_ids = [
        source_id for source_id in relevant_ids
        if source_id in set(substantive_ids) and source_id in body_relevant_ids
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
            source_text = str(source.get("body") or "").lower()
            matched_entity_terms = [
                term for term in entity_terms if term.lower() in source_text
            ]
            if not matched_entity_terms or source_id not in body_relevant_ids:
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
            # A fetched page may have redirected and acquired a different UID.
            # Match its requested URL to the discovered row as well as its
            # final URL, retaining the existing stable citation identity.
            aliases = [
                f"url:{canonicalize_public_url(str(source[key]))}"
                for key in ("requestedUrl", "url", "canonicalUrl", "finalUrl")
                if source.get(key)
            ]
            if source.get("sourceUid"):
                aliases.append(f"uid:{source['sourceUid']}")
            if not aliases and source.get("title"):
                aliases.append(f"title:{source['title']}")
            if not aliases:
                continue
            matches = sorted({positions[key] for key in aliases if key in positions})
            if matches:
                position = matches[0]
                existing = sources[position]
                # Discovery can contain both sides of a redirect. Once the
                # read proves their identity, collapse both rows as well.
                for duplicate in reversed(matches[1:]):
                    _merge_web_source(existing, sources[duplicate])
                    sources.pop(duplicate)
                    positions = {
                        key: position if index == duplicate else index - (index > duplicate)
                        for key, index in positions.items()
                    }
                _merge_web_source(existing, source)
                for key in aliases:
                    positions[key] = position
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
            for key in [*aliases, f"uid:{item['sourceUid']}"]:
                positions[key] = len(sources)
            sources.append(item)
    for index, source in enumerate(sources, start=1):
        source["id"] = f"S{index}"
    merged["sources"] = sources
    merged["supplementalSearch"] = True
    if date_filter_outcomes:
        merged["dateFilterOutcomes"] = date_filter_outcomes
    if "cacheHit" in primary or "cacheHit" in supplemental:
        merged["cacheHit"] = bool(primary.get("cacheHit") and supplemental.get("cacheHit"))
    return merged


_SOURCE_CLASSIFICATION_KEYS = (
    "sourceTier", "sourceType", "sourcePolicyId", "entityMatch", "qualityFlags",
)
_FETCH_BUNDLE_KEYS = (
    "body", "fetchStatus", "bodyError", "bodyChunks", "evidenceChunks",
    "truncated", "promptInjectionMarkers", "mimeType", "accessedAt",
    "requestedUrl", "finalUrl",
)


def _merge_web_source(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Merge one source, selecting one coherent fetch result as a bundle."""
    for key, value in incoming.items():
        if key in {
            "researchQueryIndexes", *_FETCH_BUNDLE_KEYS, *_SOURCE_CLASSIFICATION_KEYS,
        }:
            continue
        if value and (not existing.get(key) or key == "snippet"):
            existing[key] = value
    if isinstance(incoming.get("researchQueryIndexes"), list):
        existing["researchQueryIndexes"] = sorted({
            *(
                int(item) for item in existing.get("researchQueryIndexes", [])
                if isinstance(item, int) or str(item).isdigit()
            ),
            *(
                int(item) for item in incoming["researchQueryIndexes"]
                if isinstance(item, int) or str(item).isdigit()
            ),
        })
    winner_is_incoming = _incoming_fetch_result_wins(existing, incoming)
    if not winner_is_incoming:
        if source_has_readable_evidence(existing):
            existing.pop("bodyError", None)
        return
    winner = dict(incoming)
    _replace_fetch_bundle(existing, winner)
    if winner.get("url"):
        existing["url"] = canonicalize_public_url(str(winner["url"]))
        existing["canonicalUrl"] = existing["url"]
        existing["domain"] = urllib.parse.urlsplit(existing["url"]).hostname or ""
    _merge_source_classification(existing, winner)


def _fetch_result_rank(source: dict[str, Any]) -> int:
    """Rank a complete read above failures and incomplete read attempts."""
    state = source_fetch_state(source)
    if state == "success":
        # A status-only success is not evidence.  Keep a concrete failure
        # above it so that a malformed/empty retry cannot erase its error;
        # either one still loses to a later readable body.
        return 2 if source_has_readable_evidence(source) else -1
    if state == "failed":
        return 0
    if state == "pending":
        return -2
    return -3


def _incoming_fetch_result_wins(
    existing: dict[str, Any], incoming: dict[str, Any],
) -> bool:
    existing_rank = _fetch_result_rank(existing)
    incoming_rank = _fetch_result_rank(incoming)
    if incoming_rank > existing_rank:
        return True
    if incoming_rank < existing_rank:
        return False
    incoming_state = source_fetch_state(incoming)
    # Later readable/successful or failed attempts refresh same-state metadata;
    # pending search rows never erase a known failed/read result.
    return incoming_state in {"success", "failed"}


def _replace_fetch_bundle(target: dict[str, Any], winner: dict[str, Any]) -> None:
    """Replace body and all metadata that describes that body together."""
    winner_state = source_fetch_state(winner)
    winner_is_readable = source_has_readable_evidence(winner)
    for key in _FETCH_BUNDLE_KEYS:
        target.pop(key, None)
    for key in _FETCH_BUNDLE_KEYS:
        if key not in winner:
            continue
        if key in {"bodyChunks", "evidenceChunks", "promptInjectionMarkers"}:
            value = winner[key]
            target[key] = list(value) if isinstance(value, list) else value
        elif key == "bodyError" and winner_is_readable:
            continue
        else:
            target[key] = winner[key]
    if winner_state == "success" and not target.get("fetchStatus"):
        target["fetchStatus"] = "success"


def _merge_source_classification(
    target: dict[str, Any], winner: dict[str, Any],
) -> None:
    """Refresh classification with the shared classifier when context exists."""
    winner_query = str(winner.get("researchQuery") or "").strip()
    query = winner_query or str(target.get("researchQuery") or "").strip()
    policy_id = str(
        winner.get("sourcePolicyId")
        or target.get("sourcePolicyId")
        or ""
    )
    entity_terms = winner.get("entityTerms") or target.get("entityTerms") or []
    candidate = dict(winner)
    has_valid_classification = bool(
        str(candidate.get("sourceTier") or "") in {"A", "B", "C"}
        and str(candidate.get("sourceType") or "").strip()
    )
    target_has_valid_classification = bool(
        str(target.get("sourceTier") or "") in {"A", "B", "C"}
        and str(target.get("sourceType") or "").strip()
    )
    winner_state = source_fetch_state(winner)
    if not winner_query and winner_state != "failed" and has_valid_classification:
        # The upstream result already has a valid class, but still let the
        # shared classifier derive current quality flags from the winning
        # fetch metadata when that field was omitted.
        if "qualityFlags" not in candidate:
            try:
                classified_candidate = classify_web_source(
                    candidate,
                    query,
                    source_policy_id=policy_id,
                    entity_terms=entity_terms if isinstance(entity_terms, list) else (),
                )
            except (SafeWebError, TypeError, ValueError):
                classified_candidate = {}
            candidate["qualityFlags"] = list(
                classified_candidate.get("qualityFlags") or []
            )
        classified = candidate
    elif not winner_query and winner_state != "failed" and target_has_valid_classification:
        # No query is available to recompute a heuristic class. Preserve the
        # valid class, but refresh fetch-derived quality flags so a prior
        # failed attempt cannot survive a successful retry.
        try:
            classified = classify_web_source(
                candidate,
                query,
                source_policy_id=policy_id,
                entity_terms=entity_terms if isinstance(entity_terms, list) else (),
            )
        except (SafeWebError, TypeError, ValueError):
            classified = {}
        target["qualityFlags"] = list(classified.get("qualityFlags") or [])
        return
    else:
        try:
            classified = classify_web_source(
                candidate,
                query,
                source_policy_id=policy_id,
                entity_terms=entity_terms if isinstance(entity_terms, list) else (),
            )
        except (SafeWebError, TypeError, ValueError):
            classified = candidate
    for key in _SOURCE_CLASSIFICATION_KEYS:
        if key not in classified:
            continue
        value = classified[key]
        target[key] = list(value) if key == "qualityFlags" and isinstance(value, list) else value


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
    "project_web_text",
    "generic_web_evidence_status",
    "merge_web_search_results",
    "planned_web_evidence_status",
    "research_evidence_fingerprint",
    "research_loop_sources",
]
