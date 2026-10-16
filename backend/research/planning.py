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


SUPPORTED_RESEARCH_PROVIDERS = frozenset({
    "duckduckgo", "bing", "yahoo", "brave", "wikipedia",
})
_QUERY_STRATEGY_FIELDS = ("provider", "locale", "region", "freshness")

# A search phrase may be long, contain punctuation, or mention evidence and
# sources as part of the user's actual subject.  Reject only the shape that
# proves a model copied a retrieval report into the query field: a round/result
# marker together with a diagnostic conclusion.  The rationale belongs in the
# structured ``reason``/legacy ``purpose`` field and never leaves the process.
_QUERY_REPORT_MARKER = re.compile(
    r"(?:本轮|上一轮|前一轮|现有\s*\d+\s*(?:个)?\s*来源|"
    r"检索(?:结果|线路|诊断)|搜索(?:结果|线路)|"
    r"retrieval\s+(?:result|diagnostic|path)|search\s+(?:result|results|route)|"
    r"\b(?:sources?|results?)\s*[:#]?\s*\d+|"
    r"\b(?:all|five|several|multiple|\d+)\s+sources?\b|"
    r"\bS\d+(?:\s*[/,]\s*S\d+)+\b|来源\s*S?\d+)",
    flags=re.I,
)
_QUERY_DIAGNOSTIC_CONCLUSION = re.compile(
    r"(?:完全?无关|不相关|检索线路.*(?:失效|失败)|线路.*(?:失效|失败)|"
    r"无法回答|不能回答|未提供.{0,24}(?:证据|实体|时间)|"
    r"(?:没有|缺少|不足).{0,24}(?:证据|实体|时间)|"
    r"\b(?:irrelevant|unrelated|cannot\s+answer|not\s+enough\s+evidence|"
    r"no\s+(?:relevant\s+)?evidence|retrieval\s+failed)\b)",
    flags=re.I,
)


def _normalized_query_strategy(item: dict[str, Any]) -> tuple[str, ...]:
    """Keep same-text queries distinct when the model changes retrieval route."""

    return tuple(
        re.sub(r"\s+", " ", str(item.get(field) or "")).strip().casefold()
        for field in _QUERY_STRATEGY_FIELDS
    )


def normalize_research_query_strategy(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        return {}
    options: dict[str, str] = {}
    provider = str(item.get("provider") or "").strip().lower()
    if provider in SUPPORTED_RESEARCH_PROVIDERS:
        options["provider"] = provider
    for field, limit in (("locale", 40), ("region", 40)):
        value = re.sub(r"\s+", " ", str(item.get(field) or "")).strip()
        if value:
            options[field] = value[:limit]
    freshness = str(item.get("freshness") or "").strip().lower()
    if freshness in {"pd", "pw", "pm", "py"}:
        options["freshness"] = freshness
    return options


def is_retrieval_diagnostic_query(value: Any) -> bool:
    """Return whether *value* is a copied retrieval report, not a query.

    This intentionally requires two independent signals.  Words such as
    ``来源``/``source`` or ``证据``/``evidence`` alone are valid in many user
    searches and therefore remain allowed.
    """

    query = re.sub(r"\s+", " ", str(value or "")).strip()
    return bool(
        query
        and _QUERY_REPORT_MARKER.search(query)
        and _QUERY_DIAGNOSTIC_CONCLUSION.search(query)
    )


def safe_outbound_web_query(value: Any, *, max_length: int = 240) -> str:
    """Apply the outbound safety boundary shared by every research caller."""

    query = outbound_web_query(value)
    if not query or len(query) > max_length or is_retrieval_diagnostic_query(query):
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
    return query[:max_length]


def query_reason_field(item: Any, default: str = "补足证据缺口") -> tuple[str, str]:
    """Read the structured reason while accepting pre-protocol ``purpose``."""

    if not isinstance(item, dict):
        return "reason", default
    field = "reason" if "reason" in item else "purpose"
    reason = re.sub(r"\s+", " ", str(item.get(field) or "")).strip()[:160]
    return field, reason or default


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
    query = safe_outbound_web_query(query)
    if not query:
        return ""
    planned_url = extract_public_web_url(query)
    if planned_url and planned_url != extract_public_web_url(user_message):
        return ""
    grounding = f"{resolved_question} {user_message}".strip()
    overlaps = web_query_overlap_tokens(query) & web_query_overlap_tokens(grounding)
    if require_overlap and not overlaps:
        return ""
    # The question remains in the research plan. Appending it to a translated
    # or narrower query changes the search terms and can turn useful keywords
    # into a no-results request.
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
        "strictAnchorMatch": False,
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
        # Repair spelling/separators of names already present in the query,
        # without appending the full question or a missing subject.
        raw_query = str(raw_query or "")
        for phrase in [analysis_subject, *protected_terms]:
            if phrase:
                raw_query = preserve_planned_phrase(raw_query, phrase)
        query = safe_planned_web_query(
            raw_query,
            user_message,
            resolved,
            require_overlap=index == 0,
        )
        if not query or any(existing["query"].casefold() == query.casefold() for existing in queries):
            continue
        reason_field, reason = query_reason_field(item, "补充公开证据")
        query_item = {"query": query, reason_field: reason}
        query_item.update(normalize_research_query_strategy(item))
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
        # A strict umbrella match is a planner decision, not a side effect of
        # broad words such as "两个" or "前者" in the user's question.
        "strictAnchorMatch": parsed.get("strictAnchorMatch") is True,
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
        (
            normalized_query_hash(str(item.get("query") or "")),
            *_normalized_query_strategy(item),
        )
        for item in existing
    }
    additions: list[dict[str, Any]] = []
    for item in queries:
        if not isinstance(item, dict):
            continue
        query = safe_outbound_web_query(item.get("query"), max_length=500)
        if not query:
            continue
        query_hash = normalized_query_hash(query)
        normalized_item = {
            "query": query[:500],
            **normalize_research_query_strategy(item),
        }
        strategy_key = (query_hash, *_normalized_query_strategy(normalized_item))
        if not query_hash or strategy_key in seen:
            continue
        seen.add(strategy_key)
        reason_field, reason = query_reason_field(item)
        addition = {"query": query, reason_field: reason}
        addition.update(normalize_research_query_strategy(item))
        additions.append(addition)
    offset = len(existing)
    return {**plan, "queries": [*existing, *additions][:8]}, additions, offset
