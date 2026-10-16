"""时效信息的共同核验与领域字段解析。

新闻、政策更新、赛事结果等都属于需要核对时间和来源的信息。
本模块提供用户目标、发表日期、时间覆盖规则；比分解析只是领域字段规则，
不承担独立的任务路由、联网流程或可信度策略。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from backend.common.text import unique_strings
from backend.research.query import web_search_freshness, web_search_temporal_intent
from backend.research.sources import source_has_readable_evidence


def normalize_research_answer_scope(
    plan_scope: Mapping[str, Any],
    state_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """纯规范化用户目标；认知状态的读取由 server 入口完成。"""
    raw_scope = state_scope or plan_scope
    original_goal = re.sub(r"\s+", " ", str(raw_scope.get("originalGoal") or "")).strip()[:500]
    interpreted_goal = re.sub(r"\s+", " ", str(raw_scope.get("goal") or "")).strip()[:500]

    def bounded_strings(value: Any, *, limit: int) -> list[str]:
        values = value if isinstance(value, list) else [value] if value else []
        output: list[str] = []
        for item in values:
            if isinstance(item, Mapping):
                item = item.get("text") or item.get("requirement") or item.get("target") or ""
            text = re.sub(r"\s+", " ", str(item or "")).strip()[:limit]
            if text and text not in output:
                output.append(text)
        return output

    return {
        "originalGoal": original_goal,
        "userRequirements": bounded_strings(raw_scope.get("userRequirements"), limit=240)[-8:],
        # 解释后的目标只帮助复核实体，不取代原始用户对时效的要求。
        "goal": interpreted_goal,
        # 排除对象的旧年份不能把“最新”请求误变成历史日期过滤。
        "excludedTargets": bounded_strings(raw_scope.get("excludedTargets"), limit=240)[:8],
    }


def web_research_temporal_contract(
    user_message: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """从计划中的原始目标确定时效要求；排除项不能改变目标时间范围。"""
    answer_scope = normalize_research_answer_scope(
        plan.get("answerScope") if isinstance(plan.get("answerScope"), Mapping) else {}
    )
    original = re.sub(
        r"\s+",
        " ",
        str(
            plan.get("_originalResolvedQuestion")
            or plan.get("resolvedQuestion")
            or user_message
        ),
    ).strip()[:500]
    temporal_requirements = " ".join(answer_scope.get("userRequirements") or [])
    # 排除项的日期描述要避开的旧材料，不属于用户要求的证据时间窗口。
    temporal_text = f"{answer_scope.get('originalGoal') or original} {temporal_requirements}"
    requested_freshness = web_search_freshness(temporal_text)
    for item in plan.get("queries") if isinstance(plan.get("queries"), list) else []:
        if not isinstance(item, Mapping):
            continue
        value = str(item.get("freshness") or "").strip().lower()
        if value in {"pd", "pw", "pm", "py"}:
            requested_freshness = value
            break
    # Search freshness is a provider filter; the answer may still have an
    # explicit current-time target when no fixed window is appropriate.
    latest_intent = web_search_temporal_intent(temporal_text)
    return {
        "currentTime": datetime.now().astimezone().isoformat(timespec="seconds"),
        "originalQuestion": str(user_message or "")[:1200],
        "originalResolvedQuestion": original,
        "userRequirements": list(answer_scope.get("userRequirements") or []),
        "excludedTargets": list(answer_scope.get("excludedTargets") or []),
        "requestedFreshness": requested_freshness,
        "mode": "latest" if latest_intent else "bounded_recent" if requested_freshness else "none",
        "requiresFreshnessEvidence": latest_intent,
        "contractVersion": 1,
    }


def source_published_datetime(source: Mapping[str, Any]) -> datetime | None:
    """解析来源发表日期；访问时间不构成发表日期，无可靠日期时返回 None。"""

    timestamp = str(source.get("publishedTimestamp") or "").strip()
    if timestamp:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
        except ValueError:
            pass
    raw = str(source.get("publishedAt") or source.get("dateEvidence") or "").strip()
    if not raw:
        return None
    iso_match = re.search(r"\b((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", raw)
    chinese_match = re.search(r"((?:19|20)\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日?", raw)
    match = iso_match or chinese_match
    if match:
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
    try:
        parsed = datetime.strptime(
            re.sub(r"(?i)(\d)(?:st|nd|rd|th)\b", r"\1", raw),
            "%B %d, %Y",
        )
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            parsed = datetime.strptime(
                re.sub(r"(?i)(\d)(?:st|nd|rd|th)\b", r"\1", raw),
                "%b %d, %Y",
            )
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def web_research_temporal_status(
    result: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    user_message: str = "",
    relevant_source_ids: Sequence[str] = (),
    policy_source_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """结合正文可读性、来源策略和发布日期计算覆盖，保留未核验的新线索。"""
    contract = web_research_temporal_contract(user_message, plan)
    required = bool(contract.get("requiresFreshnessEvidence"))
    sources = [
        source
        for source in (
            result.get("sources") if isinstance(result.get("sources"), list) else []
        )
        if isinstance(source, Mapping)
    ]
    relevant = {str(item) for item in relevant_source_ids if str(item)}
    policy = {str(item) for item in policy_source_ids if str(item)}
    now_utc = datetime.now(timezone.utc)
    window_days = {
        "pd": 1.5,
        "pw": 8.0,
        "pm": 32.0,
        "py": 370.0,
    }.get(str(contract.get("requestedFreshness") or ""), 0.0)

    def within_requested_window(value: datetime | None) -> bool:
        if value is None or not window_days:
            return False
        normalized = value.astimezone(timezone.utc)
        return now_utc - timedelta(days=window_days) <= normalized <= now_utc + timedelta(days=2)

    dated: list[tuple[datetime, str, Mapping[str, Any]]] = []
    unknown_date_ids: list[str] = []
    filtered_trusted_ids: list[str] = []
    for index, source in enumerate(sources, 1):
        source_id = str(source.get("id") or f"S{index}")
        if relevant and source_id not in relevant:
            continue
        published = source_published_datetime(source)
        if published is None:
            unknown_date_ids.append(source_id)
        else:
            dated.append((published, source_id, source))
        if (
            source_id in policy
            and source_has_readable_evidence(dict(source))
            and bool(source.get("dateFilterSupported"))
            and str(source.get("appliedFreshness") or "")
            in {"pd", "pw", "pm", "py"}
            and (published is None or within_requested_window(published))
        ):
            filtered_trusted_ids.append(source_id)
    verified = [
        item
        for item in dated
        if item[1] in policy and source_has_readable_evidence(dict(item[2]))
    ]
    latest_candidate = max(dated, default=None, key=lambda item: item[0])
    latest_verified = max(verified, default=None, key=lambda item: item[0])
    latest_verified_in_window = max(
        (item for item in verified if within_requested_window(item[0])),
        default=None,
        key=lambda item: item[0],
    )
    unresolved: list[str] = []
    for published, source_id, source in dated:
        if source_id in policy and source_has_readable_evidence(dict(source)):
            continue
        if latest_verified is None or published > latest_verified[0] + timedelta(hours=6):
            unresolved.append(source_id)
    outcomes = [
        dict(item)
        for item in (
            result.get("dateFilterOutcomes")
            if isinstance(result.get("dateFilterOutcomes"), list)
            else [result.get("dateFilter")]
        )
        if isinstance(item, Mapping)
    ]
    freshness_confirmed = bool(latest_verified_in_window or filtered_trusted_ids)
    coverage_sufficient = bool(not required or (freshness_confirmed and not unresolved))
    return {
        **contract,
        "dateFilterOutcomes": outcomes,
        "datedSourceIds": [source_id for _, source_id, _ in sorted(dated, reverse=True)],
        "unknownDateSourceIds": unique_strings(unknown_date_ids),
        "filteredTrustedSourceIds": unique_strings(filtered_trusted_ids),
        "latestCandidateSourceId": latest_candidate[1] if latest_candidate else "",
        "latestCandidateDate": latest_candidate[0].date().isoformat() if latest_candidate else "",
        "latestVerifiedSourceId": latest_verified[1] if latest_verified else "",
        "latestVerifiedDate": latest_verified[0].date().isoformat() if latest_verified else "",
        "latestVerifiedWithinWindowSourceId": (
            latest_verified_in_window[1] if latest_verified_in_window else ""
        ),
        "unresolvedNewerSourceIds": unique_strings(unresolved),
        "freshnessConfirmed": freshness_confirmed,
        "coverageSufficient": coverage_sufficient,
    }


def select_web_research_review_sources(
    result: Mapping[str, Any],
    plan: Mapping[str, Any],
    user_message: str,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """保留通常排名，同时让较晚日期的线索进入有限的来源复核窗口。"""

    raw_sources = [
        dict(source)
        for source in (
            result.get("sources") if isinstance(result.get("sources"), list) else []
        )
        if isinstance(source, Mapping)
    ]
    if len(raw_sources) <= limit:
        return raw_sources
    contract = web_research_temporal_contract(user_message, plan)
    if not contract.get("requestedFreshness"):
        return raw_sources[:limit]
    selected = raw_sources[: max(1, limit - 3)]
    dated = sorted(
        (
            (source_published_datetime(source), index, source)
            for index, source in enumerate(raw_sources)
            if source_published_datetime(source) is not None
        ),
        key=lambda item: (item[0], -item[1]),
        reverse=True,
    )
    seen = {
        str(source.get("sourceUid") or source.get("url") or source.get("id") or "")
        for source in selected
    }
    for _, _, source in dated:
        identity = str(source.get("sourceUid") or source.get("url") or source.get("id") or "")
        if identity in seen:
            continue
        selected.append(source)
        seen.add(identity)
        if len(selected) >= limit:
            break
    for source in raw_sources:
        if len(selected) >= limit:
            break
        identity = str(source.get("sourceUid") or source.get("url") or source.get("id") or "")
        if identity not in seen:
            selected.append(source)
            seen.add(identity)
    return selected[:limit]


# 以下为赛事结果的字段规则，供统一查证流程使用；返回字段兼容现有调用方。
SPORT_SEARCH_TRANSLATIONS = {
    "世界杯": " FIFA World Cup ",
    "欧洲杯": "UEFA Euro",
    "欧冠": "UEFA Champions League",
    "法国": "France",
    "西班牙": "Spain",
    "阿根廷": "Argentina",
    "英格兰": "England",
    "德国": "Germany",
    "意大利": "Italy",
    "葡萄牙": "Portugal",
    "巴西": "Brazil",
    "荷兰": "Netherlands",
    "日本": "Japan",
    "韩国": "South Korea",
}


SPORT_SPOKEN_JA = {
    "法国": "フランス",
    "西班牙": "スペイン",
    "阿根廷": "アルゼンチン",
    "英格兰": "イングランド",
    "德国": "ドイツ",
    "意大利": "イタリア",
    "葡萄牙": "ポルトガル",
    "巴西": "ブラジル",
    "荷兰": "オランダ",
    "日本": "日本",
    "韩国": "韓国",
}


def sports_source_matches_entities(source: dict[str, Any], entities: list[str]) -> bool:
    """用原有中英文别名核对来源是否包含全部指定球队。"""
    if not entities:
        return True
    text = " ".join(str(source.get(key) or "") for key in ("title", "snippet", "excerpt", "body")).lower()
    for entity in entities:
        aliases = {entity.lower(), str(SPORT_SEARCH_TRANSLATIONS.get(entity) or "").lower()}
        if not any(alias and alias in text for alias in aliases):
            return False
    return True


def sports_web_evidence_status(
    result: dict[str, Any],
    intent: str,
    entities: list[str] | None = None,
    analysis_focus: list[str] | None = None,
) -> dict[str, Any]:
    """区分比分、赛后报道、球员责任和纪律证据，保留充分与完整两个结果标记。"""
    entities = [str(item) for item in (entities or []) if str(item).strip()]
    analysis_focus = [str(item) for item in (analysis_focus or []) if str(item).strip()]
    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    relevant_sources = [
        source for source in sources
        if isinstance(source, dict) and sports_source_matches_entities(source, entities)
    ]
    score_sources: list[str] = []
    score_candidates: dict[tuple[tuple[str, int], tuple[str, int]], tuple[str, int, int, str]] = {}
    report_sources: list[str] = []
    responsibility_sources: list[str] = []
    schedule_sources: list[str] = []
    report_pattern = re.compile(
        r"\b(?:full[ -]?time|final score|match report|post-match|defeated|beat|won|victory|drawn?|"
        r"outplay(?:ed)?|advanced?)\b|终场|完赛|赛后|战胜|击败|获胜|晋级|战平|复盘|技术统计",
        re.I,
    )
    responsibility_pattern = re.compile(
        r"\b(?:player ratings?|rated|rating|mistake|error|blunder|fault|culprit|worst|poor|"
        r"struggl(?:e|ed|ing)|sent off|red card|penalty conceded|manager)\b|"
        r"球员评分|失误|最差|责任|红牌|送点|主教练|换人",
        re.I,
    )
    discipline_pattern = re.compile(
        r"\b(?:fouls?|bookings?|yellow cards?|red cards?|cards shown|disciplinary|dangerous tackle|"
        r"violent conduct|unsporting|simulation|dives?|dirty play)\b|"
        r"犯规|黄牌|红牌|吃牌|粗野|恶意动作|危险动作|假摔|纪律",
        re.I,
    )
    schedule_pattern = re.compile(
        r"\b(?:take on|will face|kick[ -]?off|coverage begins|scheduled|starts? at)\b|"
        r"将于|开赛时间|比赛预告|赛程",
        re.I,
    )
    source_records: list[tuple[str, dict[str, Any], str]] = []
    discipline_sources: list[str] = []
    for index, source in enumerate(relevant_sources, 1):
        source_id = str(source.get("id") or f"S{index}")
        text = " ".join(str(source.get(key) or "") for key in ("title", "snippet", "excerpt", "body"))
        source_records.append((source_id, source, text))
        score_result = extract_verified_sports_score(text)
        if score_result:
            left, left_score, right_score, right = score_result
            canonical = tuple(sorted(((left, left_score), (right, right_score))))
            score_candidates[canonical] = score_result
            score_sources.append(source_id)
        if schedule_pattern.search(text):
            schedule_sources.append(source_id)
    score_ambiguous = len(score_candidates) > 1
    selected_score = next(iter(score_candidates.values()), None) if not score_ambiguous else None
    matched_entities = [selected_score[0], selected_score[3]] if selected_score else entities
    for source_id, source, text in source_records:
        if not sports_source_matches_entities(source, matched_entities):
            continue
        if report_pattern.search(text):
            report_sources.append(source_id)
        if responsibility_pattern.search(text):
            responsibility_sources.append(source_id)
        if discipline_pattern.search(text):
            discipline_sources.append(source_id)
    score_found = bool(selected_score)
    report_found = bool(report_sources)
    responsibility_found = bool(responsibility_sources)
    needs_responsibility = "individual_responsibility" in analysis_focus
    needs_discipline = "discipline_fair_play" in analysis_focus
    discipline_found = bool(discipline_sources)
    base_sufficient = (
        score_found
        if intent in {"最新比分", "最新赛况"}
        else bool(score_found and report_found)
    )
    sufficient = bool(base_sufficient and (responsibility_found or not needs_responsibility))
    complete = bool(
        sufficient
        and (discipline_found or not needs_discipline)
    )
    schedule_only = bool(schedule_sources and not score_found and not report_found)
    return {
        "kind": "sports",
        "intent": intent,
        "entities": entities,
        "analysisFocus": analysis_focus,
        "relevantSourceIds": [str(source.get("id") or "") for source in relevant_sources],
        "scoreSourceIds": score_sources,
        "reportSourceIds": report_sources,
        "responsibilitySourceIds": responsibility_sources,
        "disciplineSourceIds": discipline_sources,
        "scoreFound": score_found,
        "scoreAmbiguous": score_ambiguous,
        "matchedEntities": matched_entities,
        "reportFound": report_found,
        "responsibilityFound": responsibility_found,
        "disciplineFound": discipline_found,
        "scheduleOnly": schedule_only,
        "sufficient": sufficient,
        "complete": complete,
    }


def extract_verified_sports_score(text: str) -> tuple[str, int, int, str] | None:
    """从原有球队别名与比分句式提取结果；不匹配时返回 None，不猜比分。"""
    value = re.sub(r"\s+", " ", str(text or ""))
    teams = [(name, name, english) for name, english in SPORT_SEARCH_TRANSLATIONS.items() if name in SPORT_SPOKEN_JA]
    for left_name, left_zh, left_en in teams:
        left_alias = rf"(?:{re.escape(left_zh)}|{re.escape(left_en)})"
        for right_name, right_zh, right_en in teams:
            if left_name == right_name:
                continue
            right_alias = rf"(?:{re.escape(right_zh)}|{re.escape(right_en)})"
            direct = re.search(
                rf"{left_alias}\s*(\d{{1,2}})\s*[-:：]\s*(\d{{1,2}})\s*{right_alias}",
                value,
                re.I,
            )
            if direct:
                return left_zh, int(direct.group(1)), int(direct.group(2)), right_zh
            winner_phrase = re.search(
                rf"{left_alias}.{{0,80}}?(\d{{1,2}})\s*[-:：]\s*(\d{{1,2}})\s+"
                rf"(?:win|victory|defeat).{{0,35}}?(?:over|against)\s+{right_alias}",
                value,
                re.I,
            )
            if winner_phrase:
                return left_zh, int(winner_phrase.group(1)), int(winner_phrase.group(2)), right_zh
    return None
