"""有界搜索与读页轮次。

每次调用独占本轮结果、失败列表和来源索引；不保存跨轮状态。
工具执行回调负责真实请求与认知回执，本模块负责次数限制及结果合并。
覆盖规则由外层装配，来源身份和查询规则复用已有模块。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from backend.common.values import clamp_int
from backend.research.evidence import merge_web_search_results, research_loop_sources
from backend.research.query import web_search_freshness
from tools.safe_web import SafeWebError, canonicalize_public_url



def execute_web_research_plan(
    settings: dict[str, Any],
    plan: dict[str, Any],
    resolution: dict[str, Any] | None = None,
    *,
    request_id: str = "",
    cognitive_body: dict[str, Any] | None = None,
    execute_tool: Callable[..., dict[str, Any]],
    planned_coverage: Callable[..., dict[str, Any]],
    field_coverage: Callable[..., dict[str, Any]],
    write_log: Callable[..., Any],
) -> dict[str, Any]:
    """执行最多三个计划查询，统一来源索引并保留每条失败信息。"""

    resolution = resolution or {}
    queries = [
        item for item in (plan.get("queries") if isinstance(plan.get("queries"), list) else [])
        if isinstance(item, dict) and str(item.get("query") or "").strip()
    ][:3]
    if not queries:
        return {"name": "web_search", "ok": False, "error": "research_plan_has_no_query"}
    successful: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    query_index_offset = clamp_int(plan.get("_queryIndexOffset"), 0, 20, 0)
    for index, item in enumerate(queries, 1):
        query = str(item.get("query") or "").strip()
        planned_freshness = str(item.get("freshness") or "").strip().lower()
        requested_freshness = (
            planned_freshness
            if planned_freshness in {"pd", "pw", "pm", "py"}
            else web_search_freshness(
                f"{query} {plan.get('_originalResolvedQuestion') or plan.get('resolvedQuestion') or ''}"
            )
        )
        result = execute_tool("web_search", settings, {
            "query": query,
            "freshness": requested_freshness,
            "plannedResearch": True,
            "sportsIntent": str(resolution.get("intent") or "") if resolution.get("kind") == "sports" else "",
            "sportsSubject": str(resolution.get("subject") or "") if resolution.get("kind") == "sports" else "",
            "sportsEntities": list(resolution.get("entities") or []) if resolution.get("kind") == "sports" else [],
            "analysisFocus": list(resolution.get("analysisFocus") or []) if resolution.get("kind") == "sports" else [],
            "requestId": request_id,
            "sourcePolicyId": str(plan.get("sourcePolicyId") or ""),
            "entityTerms": list(plan.get("entityTerms") or []),
        }, cognitive_body=cognitive_body, request_id=request_id)
        if not result.get("ok"):
            errors.append({"query": query[:240], "error": str(result.get("error") or "search_failed")[:500]})
            continue
        annotated = dict(result)
        annotated["sources"] = [
            {
                **source,
                "researchQueryIndexes": [query_index_offset + index],
                "researchQuery": query[:240],
                "researchPurpose": str(item.get("purpose") or "")[:160],
                "researchFreshness": requested_freshness,
            }
            for source in (result.get("sources") if isinstance(result.get("sources"), list) else [])
            if isinstance(source, dict)
        ]
        successful.append(annotated)
    if not successful:
        write_log(
            "web_research_all_queries_failed",
            requestId=request_id,
            resolvedQuestion=str(plan.get("resolvedQuestion") or "")[:500],
            queries=[str(item.get("query") or "")[:240] for item in queries],
            queryErrors=errors,
        )
        return {
            "name": "web_search",
            "ok": False,
            "error": "all_planned_searches_failed",
            "queryErrors": errors,
            "researchPlan": plan,
        }
    merged = successful[0]
    for supplemental in successful[1:]:
        merged = merge_web_search_results(merged, supplemental)
    merged["name"] = "web_search"
    merged["ok"] = True
    merged["researchPlan"] = plan
    merged["queryErrors"] = errors
    if resolution.get("kind") == "sports" and resolution.get("intent"):
        merged["evidenceStatus"] = field_coverage(
            merged,
            str(resolution.get("intent") or ""),
            [str(item) for item in resolution.get("entities", []) if str(item).strip()],
            [str(item) for item in resolution.get("analysisFocus", []) if str(item).strip()],
        )
    else:
        merged["evidenceStatus"] = planned_coverage(merged, plan)
    return merged


def execute_research_read_round(
    settings: dict[str, Any],
    read_requests: Sequence[dict[str, Any]],
    plan: dict[str, Any],
    *, cognitive_body: dict[str, Any] | None = None, request_id: str = "",
    execute_tool: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    """最多读取两个来源，继承搜索索引并返回实际尝试的 URL。"""
    merged: dict[str, Any] = {}
    read_urls: list[str] = []
    errors: list[dict[str, str]] = []
    existing_sources = research_loop_sources(
        plan.get("_currentEvidence")
        if isinstance(plan.get("_currentEvidence"), dict)
        else {}
    )
    source_by_url = {
        canonicalize_public_url(str(source.get("url") or "")): source
        for source in existing_sources
        if str(source.get("url") or "").strip()
    }
    for item in list(read_requests)[:2]:
        try:
            url = canonicalize_public_url(str(item.get("url") or "").strip())
        except (SafeWebError, ValueError, TypeError):
            continue
        if not url:
            continue
        read_urls.append(url)
        original = source_by_url.get(url, {})
        result = execute_tool("read_web_page", settings, {
            "url": url,
            "researchQuery": str(
                original.get("researchQuery")
                or plan.get("resolvedQuestion")
                or ""
            ),
            "sourcePolicyId": str(plan.get("sourcePolicyId") or ""),
            "entityTerms": list(plan.get("entityTerms") or []),
        }, cognitive_body=cognitive_body, request_id=request_id)
        if not result.get("ok"):
            errors.append({
                "url": url[:500],
                "error": str(result.get("error") or "read_failed")[:500],
            })
            continue
        annotated_sources: list[dict[str, Any]] = []
        for source in research_loop_sources(result):
            annotated_sources.append({
                **source,
                "researchQueryIndexes": list(
                    original.get("researchQueryIndexes") or [1]
                ),
                "researchQuery": str(
                    original.get("researchQuery")
                    or plan.get("resolvedQuestion")
                    or ""
                )[:240],
                "researchPurpose": str(
                    item.get("purpose") or "读取高价值来源正文"
                )[:160],
            })
        result = {**result, "sources": annotated_sources}
        merged = (
            merge_web_search_results(merged, result)
            if merged
            else result
        )
    return merged, read_urls, errors
