"""Model-selected research effort, with request-wide resource accounting.

Depth is a semantic model decision, never inferred from user keywords. Limits
bound calls and elapsed work, rather than forcing every investigation into the
same number of dependent search/read rounds.
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping, MutableMapping, Sequence

WEB_TOOLS = frozenset({"web_search", "read_web_page"})
DEPTHS = ("quick", "standard", "deep", "exhaustive")
DEFAULT_RESEARCH_BUDGETS = {
    "quick": {"maxCalls": 12, "maxSeconds": 60},
    "standard": {"maxCalls": 32, "maxSeconds": 120},
    "deep": {"maxCalls": 64, "maxSeconds": 240},
    "exhaustive": {"maxCalls": 160, "maxSeconds": 600},
}


def query_language_plan(queries: Sequence[Mapping[str, Any]], topic: str = 'general') -> dict[str, Any]:
    """Report actual locales and the nearest useful allocation; never rewrite queries."""
    topic = topic if topic in {'general', 'news', 'academic'} else 'general'
    counts = {'zh-CN': 0, 'en-US': 0}
    for query in queries:
        locale = str(query.get('locale') or query.get('queryLocale') or '').lower()
        if locale.startswith('zh'):
            counts['zh-CN'] += 1
        elif locale.startswith('en'):
            counts['en-US'] += 1
    total = len(queries)
    chinese = int(total * (0.3 if topic in {'news', 'academic'} else 0.6) + 0.5)
    targets = {'zh-CN': chinese, 'en-US': total - chinese}
    missing = [locale for locale in counts if counts[locale] < targets[locale]]
    return {'target': topic, 'queryCount': total, 'targetCounts': targets,
            'actualCounts': counts, 'undercoveredLocales': missing,
            'nextLocale': missing[0] if missing else ''}

RESEARCH_GUIDANCE = """研究深度由你结合当前目标、历史偏好和证据缺口选择，不能只按某个关键词分类。
联网时给 cognitive.research={depth:'quick'|'standard'|'deep'|'exhaustive',topic:'general'|'news'|'academic',reason:'所需研究深度的理由'}；原生函数无法给 cognitive 时，在联网函数参数给 researchDepth、researchTopic。
quick 用于简单事实查证或闲聊中的小查询；standard 用于一般解释和比较；deep 用于严肃论文推荐、复杂调查或需要审读原文的比较。语气随意不代表研究要求低。任务变复杂时可升级深度，已用资源不会重置。
exhaustive 仅用于主人明确要求持续深查、遍历候选、尽可能穷尽或逐项全面核验的任务；这是更大的资源上限，不要求耗尽。普通问题不自动升级到这一档。
检索与来源选择的软目标：普通话题中文60%、英文40%；新闻和学术中文30%、英文70%。有多次检索时按这些比例分配查询，少量查询允许近似，优先补足尚未覆盖的语言。英文查询要用真正英文检索词并设置 locale='en-US'，中文查询设置 locale='zh-CN'；不能只更改locale却保留另一语言的整句查询。理解不同语言原文后按用户语言整理，比较冲突并标注来源。不要为了凑比例引用无关或低质网页；用户指定语言、地方性问题和更直接的原始资料优先。英文不自动等于可靠。依据用户记忆确定相关性，不把未证实偏好当事实。
输出计划前核对各语言的查询数：普通5条约3中2英；新闻/学术3条约1中2英、10条约3中7英。新闻涉及海外公司的公告时优先用不同英文一手查询覆盖证据缺口，避免重复用中文报道代替英文原文；用户明确限定中文或本地议题可例外。
搜索列表、热度和摘要用于发现候选，不自动证明质量。严肃推荐应阅读入选原文的方法、实验和局限，区分相关性、研究质量、热度与适用性；根据问题判断证据是否足够，不按固定篇数或域名分数代替判断。
每批搜索先核对结果讨论的是否为目标实体和问题；请求成功不等于结果相关。结果跑题时保留专名或完整标题（包括连接词），尝试精确短语、补充消歧上下文或限定原始来源域名再检索，不能直接拿同名但无关的内容回答。
每次读完依据用户目标判断下一步：缺原文就打开链接，缺关键章节就定位或续读，来源冲突则核对原始来源；允许再次使用同一工具读取不同URL、章节或查询。没有有价值的新证据时停止重复。
只有目标已得到证据支持，或确实无法继续时才给最终回答；预算是资源上限，不是必须用完的配额。资源耗尽但工作未完成时，明确部分完成、已有证据与未完成项，不能把初筛说成充分验证。
"""


def normalize_research_budgets(value: Any) -> dict[str, dict[str, int]]:
    configured = value if isinstance(value, Mapping) else {}
    # Upgrade the exact previous defaults; preserve genuinely custom limits.
    if configured == {
        'quick': {'maxCalls': 8, 'maxSeconds': 60},
        'standard': {'maxCalls': 16, 'maxSeconds': 120},
        'deep': {'maxCalls': 24, 'maxSeconds': 180},
    }:
        configured = {}
    result = {}
    for depth, defaults in DEFAULT_RESEARCH_BUDGETS.items():
        limits = configured.get(depth)
        limits = limits if isinstance(limits, Mapping) else {}
        normalized = {}
        for key, ceiling in (("maxCalls", 200), ("maxSeconds", 3600)):
            try:
                normalized[key] = max(1, min(ceiling, int(limits.get(key, defaults[key]))))
            except (TypeError, ValueError):
                normalized[key] = defaults[key]
        result[depth] = normalized
    return result


def select_research_budget(
    body: MutableMapping[str, Any], payload: Mapping[str, Any] | None,
    settings: Mapping[str, Any], *, now: float | None = None,
) -> dict[str, Any] | None:
    value = payload or {}
    cognitive = value.get("cognitive") or {}
    research = cognitive.get("research") if isinstance(cognitive, Mapping) else None
    research = research if isinstance(research, Mapping) else {}
    calls = value.get("calls") or value.get("toolRequests") or []
    calls = calls if isinstance(calls, list) else []
    web = [c for c in calls if isinstance(c, Mapping) and c.get("name") in WEB_TOOLS]
    state = body.get("_researchBudget")
    if not isinstance(state, dict) and not web:
        return None
    choices = [str(research.get("depth") or "")]
    choices.extend(str(c["args"].get("researchDepth") or "") for c in web
                   if isinstance(c.get("args"), Mapping))
    valid = [d for d in choices if d in DEPTHS]
    chosen = max(valid, key=DEPTHS.index) if valid else str((state or {}).get("depth") or "standard")
    if not isinstance(state, dict):
        previous = body.pop("_toolBudget", {})
        state = {"depth": chosen,
                 "startedAt": previous.get("startedAt", time.monotonic() if now is None else now),
                 "callsUsed": previous.get("callsUsed", 0)}
        body["_researchBudget"] = state
    # Escalating effort must never restart the accounting clock or replenish
    # already consumed calls. Omitted metadata on later native calls is normal.
    if DEPTHS.index(chosen) >= DEPTHS.index(state["depth"]):
        state["depth"] = chosen
        if research.get("reason"):
            state["reason"] = str(research["reason"])[:500]
    state.update(normalize_research_budgets(settings.get("webResearchBudgets"))[state["depth"]])
    topics = [research.get('topic')] + [c['args'].get('researchTopic') for c in web
                                      if isinstance(c.get('args'), Mapping)]
    for topic in topics:
        if topic in {'general', 'news', 'academic'}:
            state['topic'] = topic
    return state


def select_tool_budget(
    body: MutableMapping[str, Any], payload: Mapping[str, Any] | None,
    settings: Mapping[str, Any], *, now: float | None = None,
) -> dict[str, Any] | None:
    """Allow dependent tool steps; bound cumulative resources, not chain depth."""
    research = select_research_budget(body, payload, settings, now=now)
    if research is not None:
        return research
    calls = (payload or {}).get("calls") or (payload or {}).get("toolRequests") or []
    state = body.get("_toolBudget")
    if not isinstance(state, dict) and not calls:
        return None
    if not isinstance(state, dict):
        state = {"startedAt": time.monotonic() if now is None else now, "callsUsed": 0}
        body["_toolBudget"] = state
    state.update(normalize_research_budgets(settings.get("webResearchBudgets"))["standard"])
    return state


def sync_tool_usage(body: MutableMapping[str, Any], results: Sequence[Mapping[str, Any]]) -> None:
    state = body.get("_researchBudget") or body.get("_toolBudget")
    if isinstance(state, dict):
        count = sum(1 for result in results if result.get("name") and result.get("attempted") is not False)
        state["callsUsed"] = max(int(state.get("callsUsed", 0)), count)


def record_tool_attempts(body: MutableMapping[str, Any], results: Sequence[Mapping[str, Any]]) -> None:
    """Count a new batch before receipt/evidence deduplication or aggregation."""
    state = body.get("_researchBudget") or body.get("_toolBudget")
    if isinstance(state, dict):
        state["callsUsed"] = int(state.get("callsUsed", 0)) + sum(
            1 for item in results if item.get("name") and item.get("attempted") is not False
            and not item.get('_researchToolCallCount')
        )
        record_query_languages(body, results)


def record_query_languages(body: MutableMapping[str, Any], results: Sequence[Mapping[str, Any]]) -> None:
    state = body.get('_researchBudget')
    if not isinstance(state, dict):
        return
    counts = state.setdefault('queryLanguages', {'zh': 0, 'en': 0, 'other': 0})
    seen = state.setdefault('languageQueries', [])
    for result in results:
        if result.get('name') != 'web_search' or result.get('attempted') is False:
            continue
        queries = result.get('executedLanguageQueries') or [result]
        for item in queries:
            locale = str(item.get('queryLocale') or '').lower()
            key = [str(item.get('query') or ''), locale]
            if key in seen:
                continue
            seen.append(key)
            counts['zh' if locale.startswith('zh') else 'en' if locale.startswith('en') else 'other'] += 1


def tool_budget_exhausted(body: Mapping[str, Any], requests: Sequence[Mapping[str, Any]] = (), *, now: float | None = None) -> str:
    state = body.get("_researchBudget") or body.get("_toolBudget")
    kind = "research" if body.get("_researchBudget") else "tool"
    if not isinstance(state, Mapping):
        return ""
    next_calls = sum(1 for request in requests if request.get("name"))
    if int(state.get("callsUsed", 0)) >= int(state["maxCalls"]) or int(state.get("callsUsed", 0)) + next_calls > int(state["maxCalls"]):
        return f"{kind}_call_budget_exhausted"
    elapsed = (time.monotonic() if now is None else now) - float(state["startedAt"])
    if elapsed >= float(state["maxSeconds"]):
        return f"{kind}_time_budget_exhausted"
    return ""


def research_budget_prompt(body: Mapping[str, Any]) -> str:
    state = body.get("_researchBudget")
    if not isinstance(state, Mapping):
        return ""
    elapsed = max(0, time.monotonic() - float(state["startedAt"]))
    summary = {k: state[k] for k in ("depth", "callsUsed", "maxCalls", "maxSeconds")}
    summary["secondsRemaining"] = max(0, int(float(state["maxSeconds"]) - elapsed))
    summary['queryLanguages'] = state.get('queryLanguages', {})
    summary['languageTarget'] = '中文30% 英文70%' if state.get('topic') in {'news', 'academic'} else '中文60% 英文40%'
    summary['languagePlan'] = query_language_plan(
        [{'query': item[0], 'locale': item[1]} for item in state.get('languageQueries', [])],
        str(state.get('topic') or 'general'),
    )
    return ("\n本请求研究资源（续接累计、不重置）：" + json.dumps(summary, ensure_ascii=False)
            + "\n先检查当前用户目标还有哪些实质证据缺口，再决定继续取证或完成回答；可以复用同一工具读取不同内容。")


def tool_budget_prompt(body: Mapping[str, Any]) -> str:
    if body.get("_researchBudget"):
        return research_budget_prompt(body)
    state = body.get("_toolBudget")
    if not isinstance(state, Mapping):
        return ""
    summary = {key: state[key] for key in ("callsUsed", "maxCalls", "maxSeconds")}
    summary["secondsRemaining"] = max(0, int(float(state["maxSeconds"]) - (time.monotonic() - float(state["startedAt"]))))
    return ("\n本请求工具资源（续接累计、不重置）：" + json.dumps(summary, ensure_ascii=False)
            + "\n依据完整任务和真实回执选择下一步；可连续调用工具完成依赖步骤，不按轮数提前收尾。目标完成即可结束，无需耗尽预算。")


# Existing research executors share the same request-wide accounting.
sync_research_usage = sync_tool_usage
record_research_attempts = record_tool_attempts
research_budget_exhausted = tool_budget_exhausted
