"""联网工具的权限、查询清理、有限补查和来源结果装配。

搜索和读页复用 safe_web 的传输与公开地址检查；补查只沿用已有预算。
赛事附加规则仍在统一联网执行路径中，不新增独立模型调用或思考参数。
"""
from __future__ import annotations
import re
from typing import Any, Callable
from backend.research.planning import outbound_web_query
from backend.research.query import comparison_entity_search_terms, research_source_policy_id
from backend.research.sources import classify_web_source, enrich_web_sources
from backend.research.evidence import generic_web_evidence_status, merge_web_search_results
from backend.research.current_info import sports_web_evidence_status
from tools.safe_web import SafeWebError, read_web_page, web_search

class WebTools:
    def __init__(self, *, write_log: Callable[..., Any]) -> None:
        self.write_log = write_log

    def tool_web_search(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionWebSearch"):
            return {"name": "web_search", "ok": False, "error": "permission_web_search_denied"}
        query = outbound_web_query(args.get("query"))
        if not query:
            return {
                "name": "web_search",
                "ok": False,
                "error": "query_has_insufficient_or_sensitive_context",
            }
        try:
            source_policy_id = str(
                args.get("sourcePolicyId")
                or research_source_policy_id(query)
            )
            entity_terms = [
                str(item)
                for item in (
                    args.get("entityTerms")
                    if isinstance(args.get("entityTerms"), list)
                    else []
                )
                if str(item).strip()
            ]
            search_options = {
                "provider": str(args.get("provider") or settings.get("webSearchProvider") or "duckduckgo"),
                "api_key": str(settings.get("webSearchApiKey") or ""),
                "max_results": int(settings.get("webSearchMaxResults") or 5),
                "read_top_pages": int(settings.get("webReadTopPages") or 2),
                "timeout": float(settings.get("webRequestTimeout") or 12),
                "freshness": str(args.get("freshness") or ""),
                "locale": str(args.get("locale") or settings.get("uiLanguage") or "zh-CN"),
                "region": str(args.get("region") or ""),
                "source_policy_id": source_policy_id,
            }
            result = enrich_web_sources(
                web_search(query, **search_options),
                query,
                source_policy_id=source_policy_id,
                entity_terms=entity_terms,
            )
            sports_intent = str(args.get("sportsIntent") or "")
            sports_entities = [str(item) for item in args.get("sportsEntities", []) if str(item).strip()]
            analysis_focus = [str(item) for item in args.get("analysisFocus", []) if str(item).strip()]
            if sports_intent in {"最新比分", "最新赛况", "赛后评价"}:
                evidence_status = sports_web_evidence_status(result, sports_intent, sports_entities, analysis_focus)
                if not evidence_status.get("complete", evidence_status["sufficient"]):
                    focus_terms = ""
                    if "individual_responsibility" in analysis_focus:
                        focus_terms += " player ratings mistakes errors worst performer manager responsibility"
                    if "discipline_fair_play" in analysis_focus:
                        focus_terms += " fouls yellow cards red cards disciplinary incidents fair play"
                    supplemental_query = re.sub(
                        r"\s+", " ",
                        f"{query} exact final score full time official match report statistics{focus_terms}",
                    ).strip()[:500]
                    try:
                        supplemental = enrich_web_sources(
                            web_search(supplemental_query, **search_options),
                            supplemental_query,
                            source_policy_id=source_policy_id,
                            entity_terms=entity_terms,
                        )
                        result = merge_web_search_results(result, supplemental)
                    except (SafeWebError, ValueError, TypeError) as supplemental_error:
                        result = dict(result)
                        result["supplementalError"] = str(supplemental_error)[:500]
                    result["evidenceStatus"] = sports_web_evidence_status(
                        result, sports_intent, sports_entities, analysis_focus
                    )
                else:
                    result["evidenceStatus"] = evidence_status
            else:
                evidence_status = generic_web_evidence_status(result, query)
                if (
                    not args.get("plannedResearch")
                    and evidence_status.get("comparisonRequested")
                    and not evidence_status.get("complete")
                ):
                    comparison_entities = [
                        str(item) for item in evidence_status.get("comparisonEntities", [])
                        if str(item).strip()
                    ]
                    missing_entities = [
                        str(item) for item in evidence_status.get("missingComparisonEntities", [])
                        if str(item).strip()
                    ]
                    # A comparison can have both names present in one readable page
                    # and still lack independent support. In that case, research
                    # each subject separately instead of repeating the same broad
                    # comparison query.
                    supplemental_targets = (missing_entities or comparison_entities)[:2]
                    comparison_facets = query
                    for comparison_entity in comparison_entities:
                        comparison_facets = comparison_facets.replace(comparison_entity, " ")
                    comparison_facets = re.sub(
                        r"\b(?:vs\.?|versus|compare|comparison)\b|和|跟|同|与|比较|对比|哪个|哪一个|谁|孰",
                        " ",
                        comparison_facets,
                        flags=re.I,
                    )
                    comparison_facets = re.sub(r"\s+", " ", comparison_facets).strip()
                    for entity in supplemental_targets:
                        entity_terms = comparison_entity_search_terms(entity)
                        search_entity = entity_terms[-1] if entity_terms else entity
                        supplemental_query = re.sub(
                            r"\s+",
                            " ",
                            f"{search_entity} {comparison_facets or '资料 特点 实际表现'} 资料",
                        ).strip()[:500]
                        try:
                            supplemental = enrich_web_sources(
                                web_search(supplemental_query, **search_options),
                                supplemental_query,
                                source_policy_id=source_policy_id,
                                entity_terms=entity_terms,
                            )
                            supplemental = dict(supplemental)
                            supplemental["sources"] = [
                                {**source, "researchTarget": entity}
                                for source in (
                                    supplemental.get("sources")
                                    if isinstance(supplemental.get("sources"), list)
                                    else []
                                )
                                if isinstance(source, dict)
                            ]
                            result = merge_web_search_results(result, supplemental)
                        except (SafeWebError, ValueError, TypeError) as supplemental_error:
                            result = dict(result)
                            errors = (
                                list(result.get("supplementalErrors"))
                                if isinstance(result.get("supplementalErrors"), list)
                                else []
                            )
                            errors.append({
                                "query": supplemental_query,
                                "error": str(supplemental_error)[:500],
                            })
                            result["supplementalErrors"] = errors
                    evidence_status = generic_web_evidence_status(result, query)
                result["evidenceStatus"] = evidence_status
            result = enrich_web_sources(
                result,
                query,
                source_policy_id=source_policy_id,
                entity_terms=entity_terms,
            )
            requested_freshness = str(search_options.get("freshness") or "")
            if requested_freshness:
                date_filter_outcomes = (
                    result.get("dateFilterOutcomes")
                    if isinstance(result.get("dateFilterOutcomes"), list)
                    else [result.get("dateFilter")]
                )
                self.write_log(
                    "web_search_freshness_applied",
                    requestId=str(args.get("requestId") or "")[:120],
                    query=query[:500],
                    requestedFreshness=requested_freshness,
                    effectiveProvider=str(result.get("provider") or "")[:40],
                    dateFilterOutcomes=[
                        dict(item) for item in date_filter_outcomes if isinstance(item, dict)
                    ],
                    cacheHit=bool(result.get("cacheHit")),
                    datedSourceIds=[
                        str(source.get("id") or "")
                        for source in result.get("sources", [])
                        if isinstance(source, dict)
                        and (source.get("publishedTimestamp") or source.get("publishedAt") or source.get("dateEvidence"))
                    ],
                )
            return {
                "name": "web_search",
                "ok": True,
                "sourcePolicyId": source_policy_id,
                **result,
            }
        except (SafeWebError, ValueError, TypeError) as error:
            self.write_log(
                "web_search_tool_failed",
                requestId=str(args.get("requestId") or "")[:120],
                query=query[:500],
                errorType=type(error).__name__,
                error=str(error)[:1000],
            )
            return {"name": "web_search", "ok": False, "error": str(error)[:500], "query": query[:500]}


    def tool_read_web_page(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionWebSearch"):
            return {"name": "read_web_page", "ok": False, "error": "permission_web_search_denied"}
        url = str(args.get("url") or "").strip()
        try:
            page = read_web_page(url, timeout=float(settings.get("webRequestTimeout") or 12))
            source = classify_web_source({
                "id": "S1",
                "title": page["title"],
                "url": page["url"],
                "domain": page["domain"],
                "body": page["text"],
                "accessedAt": page["accessedAt"],
                "fetchStatus": page.get("fetchStatus", "success"),
                "mimeType": page.get("mimeType", ""),
                "truncated": bool(page.get("truncated")),
                "promptInjectionMarkers": page.get("promptInjectionMarkers", []),
            }, str(args.get("researchQuery") or url), source_policy_id=str(
                args.get("sourcePolicyId") or ""
            ), entity_terms=[
                str(item)
                for item in (
                    args.get("entityTerms")
                    if isinstance(args.get("entityTerms"), list)
                    else []
                )
                if str(item).strip()
            ])
            return {
                "name": "read_web_page",
                "ok": True,
                "sourcePolicyId": source.get("sourcePolicyId"),
                "sources": [source],
            }
        except (SafeWebError, ValueError, TypeError) as error:
            return {"name": "read_web_page", "ok": False, "error": str(error)[:500], "url": url[:500]}
