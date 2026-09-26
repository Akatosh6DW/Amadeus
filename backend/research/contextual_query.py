"""利用显式会话主题组装查询，保留既有对比格式和体育补充规则。

通用实时信息仍经统一研究决策；这里不执行搜索，也不把查询文本当作证据。
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any
import re
from backend.research.query import comparison_query_entities, web_search_query_from_message, web_query_is_context_only
from backend.research.planning import previous_substantive_web_query
from backend.research.current_info import SPORT_SEARCH_TRANSLATIONS
from backend.context.reference import message_needs_recent_context

def format_comparison_web_query(query: str) -> str:
    """Add boundaries between compared names so public search engines segment them."""

    value = re.sub(r"\s+", " ", str(query or "")).strip()
    entities = comparison_query_entities(value)
    if len(entities) != 2:
        return value[:500]
    remainder = value
    for entity in entities:
        remainder = remainder.replace(entity, " ")
    remainder = re.sub(
        r"\b(?:vs\.?|versus)\b|和|跟|同|与",
        " ",
        remainder,
        flags=re.I,
    )
    remainder = re.sub(r"\s+", " ", remainder).strip()
    if re.search(r"哪个更强|哪一个更强|谁更强|孰强|强弱|战力|厉害", value):
        remainder = "强度 能力 战绩 对比"
    elif re.search(r"哪个更好|哪一个更好|谁更好|优劣|优于|不如", value):
        remainder = "特点 实际表现 评价 对比"
    return re.sub(r"\s+", " ", f"{entities[0]} {entities[1]} {remainder}").strip()[:500]


def contextual_web_search_query(
    user_message: str,
    resolution: dict[str, Any],
    messages: list[dict[str, Any]] | None = None,
) -> str:
    if resolution.get("active") and resolution.get("searchQuery"):
        query = web_search_query_from_message(str(resolution["searchQuery"]))
        if resolution.get("kind") == "sports":
            translated = str(
                resolution.get("searchQuery")
                or resolution.get("resolvedQuestion")
                or resolution.get("subject")
                or user_message
            )
            for source, target in SPORT_SEARCH_TRANSLATIONS.items():
                translated = translated.replace(source, target)
            translated = re.sub(
                r"(?:我认为|我觉得|在我看来|我的看法是|我的判断是|我感觉|我赞同|我支持|我反对|"
                r"我不赞同|我不同意).*$",
                " ",
                translated,
                flags=re.I,
            )
            translated = re.sub(r"如何评价|怎么评价|怎么看|如何看待|请分析|分析一下", " ", translated)
            translated = translated.replace("半决赛", " semifinal ").replace("决赛", " final ")
            translated = re.sub(r"[，。！？；,:;!?]+", " ", translated)
            now = datetime.now().astimezone()
            year = str(resolution.get("eventYear") or now.year)
            year_prefix = "" if year in translated else year
            original_matchup = str(resolution.get("matchup") or "").strip()
            translated_matchup = original_matchup
            for source, target in SPORT_SEARCH_TRANSLATIONS.items():
                translated_matchup = translated_matchup.replace(source, target)
            intent_label = str(resolution.get("intent") or "").strip()
            date_hint = ""
            if str(resolution.get("intent") or "") in {"最新比分", "最新赛况", "赛后评价"} and not resolution.get("eventYear"):
                date_hint = now.strftime("%B %d %Y")
                question_text = f"{user_message} {resolution.get('resolvedQuestion') or ''}"
                if now.hour < 4 and re.search(r"今天|今日|今晚|刚刚|刚才", question_text):
                    previous_day = now - timedelta(days=1)
                    date_hint = (
                        f"{previous_day.strftime('%B %d %Y')} {now.strftime('%B %d %Y')} overnight"
                    )
            english_intent = {
                "最新比分": "latest final score result",
                "最新赛况": "latest match result",
                "赛后评价": "match report analysis",
                "能否翻盘": "live score comeback",
                "赛前预测": "match prediction odds team news likely winner",
            }.get(str(resolution.get("intent") or ""), "latest result")
            return re.sub(
                r"\s+", " ",
                f"{year_prefix} {translated} {translated_matchup} {original_matchup} "
                f"{intent_label} {english_intent} official {date_hint}",
            ).strip()[:500]
        if not web_query_is_context_only(query):
            return format_comparison_web_query(query)
    query = web_search_query_from_message(user_message)
    if web_query_is_context_only(query):
        inherited = previous_substantive_web_query(messages, user_message)
        if inherited:
            return format_comparison_web_query(inherited)
        resolved = web_search_query_from_message(str(resolution.get("resolvedQuestion") or ""))
        if resolved and not web_query_is_context_only(resolved):
            return format_comparison_web_query(resolved)
    if message_needs_recent_context(user_message) and messages:
        prior_user = next(
            (
                re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
                for item in reversed(messages)
                if isinstance(item, dict)
                and item.get("role") == "user"
                and str(item.get("content") or "").strip()
            ),
            "",
        )
        if prior_user and prior_user not in query:
            query = web_search_query_from_message(f"{prior_user} {user_message}")
    return format_comparison_web_query(query)
