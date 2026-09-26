"""查询文本规则：主题清理、比较主体、称呼、相对时效和来源策略。

只依赖文本和本地时钟，不读取设置、会话或网页，也不执行搜索。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from backend.common.text import unique_strings


def web_query_subject_core(text: str) -> str:
    """去除要求搜索或回答的过程措辞，保留用户真正要查证的主题。"""

    value = web_search_query_from_message(text)
    value = re.sub(
        r"我是让你|我是让|我让你|我让|我的意思是|你应该|麻烦你|请你|不要|别再|而不是|不是|"
        r"单纯|只是|只要|光是|光|先|主动|重新|继续|一下|给我|帮我|"
        r"联网|上网|在线|搜索|检索|查询|查找|搜|查|分析|综合|归纳|比较|对比|"
        r"回答|回复|结果|来源|链接|结论|解释|说明|评价|判断|"
        r"这个|那个|这件事|上一个|上面|刚才|之前|然后|所以|到底|后|"
        r"为什么|怎么|如何|怎样|是否|哪个|哪一个|更|厉害|呢|啊|吧|吗|呀|"
        r"\b(?:search|browse|research|analy[sz]e|analysis|answer|result)\b",
        " ",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s+", " ", value).strip(" ，。！？；,:;!?")
    return value[:500]


def web_query_is_context_only(text: str) -> bool:
    """判断本句是否只有过程纠正，供外层决定是否继承之前的主题。"""

    core = web_query_subject_core(text)
    latin_or_number = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,}", core)
    chinese = re.sub(r"[^\u3400-\u9fff]", "", core)
    return not latin_or_number and len(chinese) < 2


def web_search_freshness(text: str) -> str:
    """按原有相对时间和历史年份规则选择搜索时效，不改写用户的目标年份。"""
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if not normalized:
        return ""
    current_year = datetime.now().astimezone().year
    mentioned_years = {
        int(value)
        for value in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", normalized)
    }
    historical_target_years: set[int] = set()
    for year in mentioned_years:
        year_text = str(year)
        excluded_year = bool(re.search(
            rf"(?:不要|排除|不是|并非|别看|忽略|剔除|exclude|not){year_text}|"
            rf"{year_text}.{{0,10}}(?:那条|旧闻|旧消息).{{0,5}}(?:不要|排除|不是|忽略)",
            normalized,
            re.I,
        ))
        if excluded_year:
            continue
        if re.search(
            rf"(?:截至|截止|asof){year_text}|"
            rf"{year_text}年?.{{0,40}}?(?:最新|进展|消息|情况|状态|研究|报道)",
            normalized,
            re.I,
        ):
            historical_target_years.add(year)
    explicitly_historical = bool(
        historical_target_years
        and max(historical_target_years) < current_year
        and not re.search(r"以来|至今|到现在|截至(?:现在|当前|今天)|since|to(?:the)?present", normalized, re.I)
    )
    if explicitly_historical:
        # “2024 年最新进展” means the latest evidence in that historical
        # period; a relative filter anchored to today would silently rewrite it.
        return ""
    if re.search(r"今天|今日|刚刚|实时|24小时|过去一天|最近一天|pastday|last24hours", normalized, re.I):
        return "pd"
    if re.search(r"本周|这周|最近一周|过去一周|pastweek|lastweek", normalized, re.I):
        return "pw"
    if re.search(r"本月|这个月|最近一个月|过去一个月|pastmonth|lastmonth", normalized, re.I):
        return "pm"
    if re.search(r"最近一年|过去一年|近一年|pastyear|lastyear", normalized, re.I):
        return "py"
    if re.search(r"现在|当前|目前|此刻|rightnow|realtime", normalized, re.I):
        return "pd"
    if re.search(r"最新|近期|最近进展|latest|newest|recent(?:progress|development|news|update)", normalized, re.I):
        # A one-day window routinely hides the previous release/event and gives
        # the reviewer no basis for deciding what is actually latest.
        return "pm"
    return ""


SEARCH_DIRECT_ADDRESS_PATTERN = re.compile(
    r"^\s*(?:(?:嗨|喂|那个)\s*[，,、]?\s*)?"
    r"(?:助手|AMADEUS|阿玛迪乌斯|红莉栖|克里斯蒂娜)\s*[，,：:]?\s*"
    r"(?=(?:你)?(?:能不能|可不可以|可以|麻烦|请)?"
    r"(?:帮我|替我|给我)?(?:联网|在线|上网|去网上)?"
    r"(?:搜索|搜一下|搜一搜|搜|查一下|查询|查找))",
    flags=re.I,
)


def strip_search_direct_address(text: str) -> str:
    """仅在后面明确跟随搜索命令时去掉称呼，保留作为查询主题的人名。"""

    return SEARCH_DIRECT_ADDRESS_PATTERN.sub("", str(text or ""), count=1)


def compact_search_phrase(text: Any) -> str:
    return re.sub(
        r"[^\u3400-\u9fffA-Za-z0-9]+",
        "",
        str(text or ""),
    ).casefold()


def preserve_planned_phrase(query: str, phrase: str) -> str:
    """规划器仅改动分隔符时，恢复已有依据的专有短语。"""

    value = str(query or "")
    canonical = re.sub(r"\s+", " ", str(phrase or "")).strip()
    compact = compact_search_phrase(canonical)
    if len(compact) < 2 or compact not in compact_search_phrase(value):
        return value
    characters = re.findall(r"[\u3400-\u9fffA-Za-z0-9]", canonical)
    if not characters:
        return value
    flexible = r"[\s:：;；._+\-/\\·]*".join(
        re.escape(character) for character in characters
    )
    return re.sub(flexible, lambda _match: canonical, value, count=1, flags=re.I)


def strip_unintended_planned_vocative(query: str, user_message: str) -> str:
    """保留作为主题的助手名字，只删除规划查询中无依据的对话称呼。"""

    value = re.sub(r"\s+", " ", str(query or "")).strip()
    grounded = web_search_query_from_message(user_message)
    for alias in ("助手", "AMADEUS", "阿玛迪乌斯", "红莉栖", "克里斯蒂娜"):
        match = re.match(
            rf"^\s*{re.escape(alias)}(?:\s+|[，,：:、]+)\s*(.+)$",
            value,
            flags=re.I,
        )
        if not match:
            continue
        if compact_search_phrase(grounded).startswith(compact_search_phrase(alias)):
            return value
        return match.group(1).strip()
    return value


def web_search_query_from_message(text: str) -> str:
    """将搜索请求转成查询短语；清空后仍保留原句回退。"""
    query = re.sub(
        r"\s+",
        " ",
        strip_search_direct_address(str(text or "")),
    ).strip()
    query = re.sub(
        r"(?:请|麻烦)?(?:你)?(?:帮我)?(?:联网|在线|上网|去网上)?(?:搜索|搜一下|查一下|查询|查找)(?:一下)?",
        " ",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"^\s*(?:并|然后|再)?(?:做)?(?:分析|综合|归纳|比较|对比|评价)(?:一下)?\s*[：:\-—]*\s*",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(
        r"[？?。；;，,]\s*(?:(?:不要|别)(?:只|单纯)?(?:报告|罗列|复述|粘贴|展示)|"
        r"(?:要|需要|必须)(?:给出|提供|附上|包含)).*$",
        "",
        query,
        flags=re.I,
    )
    query = re.sub(r"(?:并|然后)?(?:给出|附上|标注|提供)(?:可靠|网页|对应)?(?:的)?(?:来源|引用|链接)", " ", query)
    query = re.sub(r"(?:要|需要)?(?:带|有)(?:网页)?(?:来源|引用)", " ", query)
    query = re.sub(r"[，。！？；,:;!?]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    return (query or str(text or "").strip())[:500]


def web_query_overlap_tokens(text: str) -> set[str]:
    """生成拉丁词与中文相邻字片段，用于查询和用户主题的重合检查。"""
    value = re.sub(r"\s+", " ", str(text or "")).lower()
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9_.+-]{2,}", value)
        if token not in {"the", "and", "with", "what", "which", "compare"}
    }
    for segment in re.findall(r"[\u3400-\u9fff]{2,}", value):
        if len(segment) <= 8:
            tokens.add(segment)
        tokens.update(segment[index:index + 2] for index in range(max(0, len(segment) - 1)))
    return tokens


def comparison_query_entities(query: str) -> list[str]:
    """从比较问题中提取两个主体，不使用领域知识猜测主体身份。"""

    value = web_search_query_from_message(query)
    if not re.search(
        r"哪个|哪一个|谁|孰|相比|比较|对比|区别|差异|强弱|强度|战力|优劣|"
        r"\b(?:vs\.?|versus|compare|comparison)\b",
        value,
        flags=re.I,
    ):
        return []
    normalized = re.sub(r"\s+", " ", value).strip(" ，。！？；,:;!?")
    parts: list[str] = []
    used_explicit_bi_particle = False
    latin = re.split(r"\s+(?:vs\.?|versus)\s+", normalized, maxsplit=1, flags=re.I)
    if len(latin) == 2:
        parts = latin
    else:
        spaced = re.match(
            r"^(.{2,80}?)\s+(.{2,80}?)\s+(?:哪个|哪一个|谁|孰|更|比较|对比|区别|差异|强弱|强度|战力|优劣)",
            normalized,
            flags=re.I,
        )
        if spaced:
            parts = [spaced.group(1), spaced.group(2)]
        else:
            for separator in ("和", "跟", "同", "与"):
                if separator in normalized:
                    left, right = normalized.rsplit(separator, 1)
                    parts = [left, right]
                    break
            if not parts:
                bi_match = re.match(
                    r"^(.{2,80}?)比(.{2,80}?)(?:哪个|哪一个|谁|孰|更(?:强|好|优|适合|厉害))",
                    normalized,
                    flags=re.I,
                )
                if bi_match:
                    parts = [bi_match.group(1), bi_match.group(2)]
                    used_explicit_bi_particle = True
    if len(parts) != 2:
        return []
    left = re.sub(r"^(?:请问|比较|对比|分析|评价|看看|说说)\s*", "", parts[0], flags=re.I)
    right_before_marker = re.split(
        r"哪个|哪一个|谁|孰|相比|比较|对比|区别|差异|强弱|强度|战力|优劣|"
        r"更(?:强|好|优|适合|厉害)|谁更|哪边|[？?。；;，,]",
        parts[1],
        maxsplit=1,
        flags=re.I,
    )[0]
    right_had_bi_particle = bool(re.search(r"(?:来)?比\s*$", right_before_marker))
    right = re.sub(r"(?:拿来|来)?比\s*$", "", right_before_marker, flags=re.I)

    def split_dimension(value: str) -> tuple[str, str]:
        match = re.match(r"^(.{2,70}?)的([^的\s]{1,16})$", value.strip())
        if not match:
            return value, ""
        subject, dimension = match.group(1).strip(), match.group(2).strip()
        abstract_dimension = re.search(
            r"(?:度|力|性|量|率|值|价|速|效|感|水平|能力|实力|强弱|优劣|"
            r"特点|差异|区别|表现|成绩|战绩|口碑|质量|价格|成本|时间|规模)$",
            dimension,
            re.I,
        )
        return (subject, dimension) if abstract_dimension else (value, "")

    left_subject, left_dimension = split_dimension(left)
    right_subject, right_dimension = split_dimension(right)
    if left_dimension and right_dimension and left_dimension == right_dimension:
        left, right = left_subject, right_subject
    elif left_dimension and (right_had_bi_particle or used_explicit_bi_particle):
        # “A 的性能和 B 比哪个好” compares A with B; 性能 is the
        # comparison dimension, not part of A's name.
        left = left_subject
    entities = [
        re.sub(r"\s+", " ", item).strip(" 的之在从 ，。！？；,:;!?")
        for item in (left, right)
    ]
    if any(web_query_is_context_only(item) for item in entities):
        return []
    return unique_strings(entities)[:2]


def comparison_entity_search_terms(entity: str) -> list[str]:
    """为比较主体生成保守的文本变体，保留原名和必要限定。"""

    value = re.sub(r"\s+", " ", str(entity or "")).strip(" 的之在从 ，。！？；,:;!?")
    if not value:
        return []
    terms = [value]
    qualifier_stripped = re.sub(
        r"^(?:目前|当前|现在|公认|通常认为)?最[^的\s]{1,10}的",
        "",
        value,
        flags=re.I,
    ).strip()
    if qualifier_stripped and qualifier_stripped != value:
        terms.append(qualifier_stripped)
    return unique_strings(term for term in terms if len(term) >= 2)


def research_source_policy_id(query: str) -> str:
    """根据问题选择来源类别策略；只选策略，不判断具体网页是否可信。"""
    value = str(query or "")
    if re.search(r"论文|研究|学术|arxiv|openreview|doi|paper|study|benchmark", value, re.I):
        return "academic_primary"
    if re.search(
        r"法律|法规|条例|政策|判决|行政|政府|签证|移民|税务|"
        r"\blaw\b|\bregulation\b|\bpolicy\b|\bvisa\b",
        value,
        re.I,
    ):
        return "official_policy"
    if re.search(
        r"API|SDK|文档|版本|安装|配置|参数|兼容|release|changelog|"
        r"github|gitlab|软件|模型接口",
        value,
        re.I,
    ):
        return "official_software"
    if web_search_freshness(value):
        return "current_event"
    return "general_fact"
