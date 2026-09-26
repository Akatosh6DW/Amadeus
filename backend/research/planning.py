"""网页研究计划的文本规则；模型请求、日志与持久化留在入口层。

本模块不发起网络请求。时效判断沿用 query.web_search_freshness 的当前时间读取。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from agent.research_architecture import normalized_query_hash
from backend.chat.parsing import parse_json_object
from backend.research.intent import (
    extract_public_web_url,
    is_external_fact_correction_followup,
)
from backend.research.query import (
    compact_search_phrase,
    preserve_planned_phrase,
    strip_unintended_planned_vocative,
    web_query_is_context_only,
    web_query_overlap_tokens,
    web_search_freshness,
    web_search_query_from_message,
)


# 从显式传入的对话提取上一条具体主题；不访问持久会话。
def previous_substantive_web_query(
    messages: list[dict[str, Any]] | None,
    current_message: str = "",
) -> str:
    """Recover the latest real subject from a process-only follow-up."""

    current_compact = re.sub(r"\s+", "", str(current_message or ""))
    for item in reversed(messages or []):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if not content or re.sub(r"\s+", "", content) == current_compact:
            continue
        query = web_search_query_from_message(content)
        if query and not web_query_is_context_only(query):
            return query[:500]
    return ""



# 规划器不可用时沿用原候选顺序，先处理事实纠错，再去掉私密信息。
def fallback_web_research_query(
    user_message: str,
    resolution: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    """Build a conservative unsplit query when semantic planning is unavailable."""

    resolution = resolution or {}
    candidates: list[str] = []
    if is_external_fact_correction_followup(user_message, messages):
        contextual_query = str(
            resolution.get("searchQuery")
            or resolution.get("resolvedQuestion")
            or ""
        ).strip()
        if contextual_query:
            candidates.append(contextual_query)
        inherited = previous_substantive_web_query(messages, user_message)
        if inherited:
            candidates.append(f"{inherited}；用户补充纠正：{user_message}")
    candidates.extend([
        str(resolution.get("resolvedQuestion") or ""),
        user_message,
    ])
    if all(web_query_is_context_only(web_search_query_from_message(item)) for item in candidates if item.strip()):
        candidates.append(previous_substantive_web_query(messages, user_message))
    for candidate in candidates:
        query = web_search_query_from_message(candidate)
        query = outbound_web_query(query)
        if query and not web_query_is_context_only(query):
            return query[:240]
    return ""



# 只保留用户已表达的主题，不推断尚未查证的实体。
def web_research_anchor(user_message: str, resolved_question: str = "") -> str:
    """Keep a compact subject phrase available without guessing entity boundaries."""

    value = web_search_query_from_message(resolved_question or user_message)
    value = re.sub(
        r"^(?:你觉得|你认为|请问|想问一下|帮我看看|麻烦看看|能不能说说)\s*",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"(?:哪个|哪一个|谁|孰)(?:更|最)?(?:强|好|厉害|合适|值得|正确).*$",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value).strip(" ，。！？；,:;!?")
    return value[:80]



# 出站检索词的本地脱敏与最小化边界。
def outbound_web_query(value: Any) -> str:
    """Minimise and privacy-filter text before it leaves the local process."""

    query = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n，。！？；,:;!?")
    if not query:
        return ""
    query = re.sub(
        r"\b[A-Za-z]:\\(?:[^\s\\/:*?\"<>|\r\n]+\\)*[^\s\\/:*?\"<>|\r\n]*",
        " [本地路径] ",
        query,
    )
    query = re.sub(
        r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
        " [邮箱] ",
        query,
    )
    query = re.sub(
        r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)",
        " [手机号] ",
        query,
    )
    query = re.sub(
        r"(?<!\d)(?:\d[ -]?){15,19}(?!\d)",
        " [敏感号码] ",
        query,
    )
    query = re.sub(
        r"\b(?:sk|api)[-_][A-Za-z0-9_-]{12,}\b|"
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}",
        " [密钥] ",
        query,
        flags=re.I,
    )
    query = re.sub(r"\s+", " ", query).strip(" ，。！？；,:;!?")
    if (
        not query
        or web_query_is_context_only(query)
        or re.fullmatch(
            r"(?:这个|那个|这篇|那篇|这条|那条|刚才|刚刚|上面|前面|"
            r"对应|其中)(?:的)?(?:内容|资料|来源|页面|链接|论文|文献)?",
            query,
            re.I,
        )
    ):
        return ""
    semantic = re.sub(r"\[(?:本地路径|邮箱|手机号|敏感号码|密钥)\]", " ", query)
    semantic = re.sub(
        r"^(?:帮我|请|麻烦|你能不能|能不能)?\s*(?:联网|上网|搜索|搜一下|查一下|查查)\s*",
        "",
        semantic,
        flags=re.I,
    ).strip()
    if not (
        re.search(r"[A-Za-z0-9\u3400-\u9fff]{2,}", semantic)
        or re.search(r"\d{4}\.\d{4,5}", semantic)
    ):
        return ""
    return query[:240]



# 校验模型建议的检索词：防泄漏、防提示词注入、与用户问题锚定。
def safe_planned_web_query(
    value: Any,
    user_message: str,
    resolved_question: str = "",
    *,
    require_overlap: bool = False,
) -> str:
    """Validate model-planned search text before it becomes an outbound request."""

    query = strip_unintended_planned_vocative(
        re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n，。！？；,:;!?"),
        user_message,
    )
    query = outbound_web_query(query)
    if not query or len(query) > 240:
        return ""
    if re.search(
        r"system[_\s-]*prompt|developer[_\s-]*message|系统提示词|开发者消息|"
        r"authorization\s*:|bearer\s+[A-Za-z0-9._-]+|"
        r"\b(?:sk|api)[-_][A-Za-z0-9_-]{12,}\b|忽略(?:此前|之前|以上)(?:指令|要求)",
        query,
        flags=re.I,
    ):
        return ""
    if re.search(r"\b[0-9a-f]{48,}\b|[A-Za-z0-9+/]{80,}={0,2}", query):
        return ""
    planned_url = extract_public_web_url(query)
    if planned_url and planned_url != extract_public_web_url(user_message):
        return ""
    grounding = f"{resolved_question} {user_message}".strip()
    overlaps = web_query_overlap_tokens(query) & web_query_overlap_tokens(grounding)
    if require_overlap and not overlaps:
        return ""
    anchor = web_research_anchor(user_message, resolved_question)
    if not overlaps and anchor and anchor not in query:
        query = f"{anchor} {query}"
    return re.sub(r"\s+", " ", query).strip()[:240]



# 保守回退计划仍保留当前年份/时效判断的调用时机。
def fallback_web_research_plan(
    user_message: str,
    resolution: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolution = resolution or {}
    resolved = str(resolution.get("resolvedQuestion") or user_message).strip()
    query = fallback_web_research_query(user_message, resolution, messages)
    requested_freshness = web_search_freshness(f"{resolved} {user_message}")
    query_item = {"query": query, "purpose": "确认回答所需的公开事实"}
    if requested_freshness:
        query_item["freshness"] = requested_freshness
    return {
        "needsWeb": True,
        "resolvedQuestion": resolved[:500],
        "_originalResolvedQuestion": resolved[:500],
        "plannerResolvedQuestion": "",
        "userAnchor": web_research_anchor(user_message),
        "strictAnchorMatch": bool(re.search(
            r"两代|两位|两者|两个|双方|前者|后者|第[一二三四五六七八九十\d]+(?:位|个|代)",
            user_message,
            flags=re.I,
        )),
        "queries": [query_item] if query else [],
        "answerRequirements": ["基于查阅到的证据分析后直接回答用户问题"],
        "ambiguities": [],
        "questionAnalysis": {
            "intent": resolved[:240],
            "subject": web_research_anchor(user_message, resolved),
            "protectedTerms": [],
            "factsToVerify": ["回答所需的外部事实"],
        },
        "fallback": True,
    }



# 解析模型计划，但用户原问题与受保护术语始终为事实锚点。
def normalize_web_research_plan(
    value: Any,
    user_message: str,
    resolution: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolution = resolution or {}
    parsed = parse_json_object(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        return fallback_web_research_plan(user_message, resolution, messages)
    # The planner may propose entities, but it has not seen evidence yet. Keep
    # the actual question authoritative until the retrieval reviewer can prove
    # a more specific mapping from sources.
    original_resolved = str(resolution.get("resolvedQuestion") or user_message).strip()[:500]
    resolved = original_resolved
    planner_resolved = str(parsed.get("resolvedQuestion") or "").strip()[:500]
    raw_analysis = (
        parsed.get("questionAnalysis")
        if isinstance(parsed.get("questionAnalysis"), dict)
        else {}
    )
    grounding = f"{resolved} {user_message}"
    grounding_compact = compact_search_phrase(grounding)

    def grounded_planner_phrase(value: Any, limit: int) -> str:
        phrase = re.sub(r"\s+", " ", str(value or "")).strip()[:limit]
        compact = compact_search_phrase(phrase)
        return phrase if len(compact) >= 2 and compact in grounding_compact else ""

    analysis_subject = grounded_planner_phrase(raw_analysis.get("subject"), 120)
    protected_terms: list[str] = []
    for item in (
        raw_analysis.get("protectedTerms")
        if isinstance(raw_analysis.get("protectedTerms"), list)
        else []
    ):
        phrase = grounded_planner_phrase(item, 100)
        if phrase and compact_search_phrase(phrase) not in {
            compact_search_phrase(existing) for existing in protected_terms
        }:
            protected_terms.append(phrase)
        if len(protected_terms) >= 6:
            break
    facts_to_verify = [
        re.sub(r"\s+", " ", str(item)).strip()[:180]
        for item in (
            raw_analysis.get("factsToVerify")
            if isinstance(raw_analysis.get("factsToVerify"), list)
            else []
        )
        if str(item).strip()
    ][:5]
    analysis_intent = re.sub(
        r"\s+",
        " ",
        str(raw_analysis.get("intent") or planner_resolved or resolved),
    ).strip()[:300]
    if (
        analysis_subject
        and planner_resolved
        and compact_search_phrase(analysis_subject)
        in compact_search_phrase(planner_resolved)
    ):
        # The semantic planner may remove conversational wrappers and resolve
        # pronouns, but only after its central subject has been proven to come
        # from the user's own question/context.
        resolved = planner_resolved
    user_anchor = analysis_subject or web_research_anchor(user_message)
    raw_queries = parsed.get("queries")
    if isinstance(raw_queries, (str, dict)):
        raw_queries = [raw_queries]
    queries: list[dict[str, str]] = []
    requested_freshness = web_search_freshness(f"{resolved} {user_message}")
    for index, item in enumerate(raw_queries if isinstance(raw_queries, list) else []):
        raw_query = item.get("query") if isinstance(item, dict) else item
        query = safe_planned_web_query(
            raw_query,
            user_message,
            resolved,
            require_overlap=index == 0,
        )
        if (
            query
            and analysis_subject
        ):
            if compact_search_phrase(analysis_subject) in compact_search_phrase(query):
                query = preserve_planned_phrase(query, analysis_subject)
            else:
                query = safe_planned_web_query(
                    f"{analysis_subject} {query}",
                    user_message,
                    resolved,
                    require_overlap=index == 0,
                )
        for protected_term in protected_terms:
            query = preserve_planned_phrase(query, protected_term)
        if (
            index == 0
            and user_anchor
            and compact_search_phrase(user_anchor) not in compact_search_phrase(query)
        ):
            query = safe_planned_web_query(
                f"{user_anchor} {query}",
                user_message,
                resolved,
                require_overlap=True,
            )
        if not query or any(existing["query"].casefold() == query.casefold() for existing in queries):
            continue
        purpose = str(item.get("purpose") or "") if isinstance(item, dict) else ""
        purpose = re.sub(r"\s+", " ", purpose).strip()[:160]
        query_item = {"query": query, "purpose": purpose or "补充公开证据"}
        planned_freshness = (
            str(item.get("freshness") or "").strip().lower()
            if isinstance(item, dict)
            else ""
        )
        freshness = (
            planned_freshness
            if requested_freshness and planned_freshness in {"pd", "pw", "pm", "py"}
            else web_search_freshness(f"{query} {resolved} {user_message}")
        )
        if freshness:
            query_item["freshness"] = freshness
        queries.append(query_item)
        if len(queries) >= 3:
            break
    if not queries:
        return fallback_web_research_plan(user_message, resolution, messages)
    ambiguous_group_reference = bool(re.search(
        r"两代|两位|两者|两个|双方|前者|后者|第[一二三四五六七八九十\d]+(?:位|个|代)",
        user_message,
        flags=re.I,
    ))
    if ambiguous_group_reference and user_anchor:
        discovery_query = safe_planned_web_query(
            f"{user_anchor} 指谁 出处 作品 称号",
            user_message,
            resolved,
            require_overlap=True,
        )
        if discovery_query:
            # Before retrieval there is no evidence for planner-invented names.
            # Search only the user's umbrella term first; the reviewer may add
            # specific entities after a source actually identifies them.
            comparison_query = safe_planned_web_query(
                f"{user_anchor} 第一代 第二代 能力 战绩 对比",
                user_message,
                resolved,
                require_overlap=True,
            )
            queries = [{
                "query": discovery_query,
                "purpose": "先确认用户原始统称实际指向的对象与出处",
                **({"freshness": requested_freshness} if requested_freshness else {}),
            }]
            if comparison_query and comparison_query.casefold() != discovery_query.casefold():
                queries.append({
                    "query": comparison_query,
                    "purpose": "不猜具体姓名，先查统称下两方的公开比较材料",
                    **({"freshness": requested_freshness} if requested_freshness else {}),
                })
    requirements = [
        re.sub(r"\s+", " ", str(item)).strip()[:180]
        for item in (parsed.get("answerRequirements") if isinstance(parsed.get("answerRequirements"), list) else [])
        if str(item).strip()
    ][:5]
    ambiguities = [
        re.sub(r"\s+", " ", str(item)).strip()[:160]
        for item in (parsed.get("ambiguities") if isinstance(parsed.get("ambiguities"), list) else [])
        if str(item).strip()
    ][:4]
    return {
        "needsWeb": bool(parsed.get("needsWeb", True)),
        "resolvedQuestion": resolved,
        "_originalResolvedQuestion": original_resolved,
        "plannerResolvedQuestion": planner_resolved,
        "userAnchor": user_anchor,
        "strictAnchorMatch": ambiguous_group_reference,
        "queries": queries,
        "answerRequirements": requirements or ["综合查阅证据后直接回答，不罗列搜索过程"],
        "ambiguities": ambiguities,
        "questionAnalysis": {
            "intent": analysis_intent,
            "subject": analysis_subject or user_anchor,
            "protectedTerms": protected_terms,
            "factsToVerify": facts_to_verify or ["回答所需的外部事实"],
        },
        "fallback": False,
    }



# 研究循环追加查询时保持原有去重、顺序与八项上限。
def merge_research_plan_queries(
    plan: dict[str, Any],
    queries: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    existing = [
        dict(item)
        for item in (
            plan.get("queries")
            if isinstance(plan.get("queries"), list)
            else []
        )
        if isinstance(item, dict) and str(item.get("query") or "").strip()
    ]
    seen = {
        normalized_query_hash(str(item.get("query") or ""))
        for item in existing
    }
    additions: list[dict[str, Any]] = []
    for item in queries:
        query = str(item.get("query") or "").strip()
        query_hash = normalized_query_hash(query)
        if not query_hash or query_hash in seen:
            continue
        seen.add(query_hash)
        additions.append({
            "query": query[:500],
            "purpose": re.sub(
                r"\s+",
                " ",
                str(item.get("purpose") or "补足证据缺口"),
            ).strip()[:160],
        })
    offset = len(existing)
    return {**plan, "queries": [*existing, *additions][:8]}, additions, offset
