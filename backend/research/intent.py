"""联网意图与事实查证触发规则。

只处理传入文字、结构化模型结果和有限对话，不访问配置、会话库或网络。
实时新闻、价格、天气与赛事共用时效判断；实际联网权限和执行由外层控制。
"""

from __future__ import annotations

import re
from typing import Any

from agent.dialogue_architecture import is_explicit_correction_language
from agent.research_architecture import extract_latin_entities
from backend.research.query import comparison_query_entities



def extract_public_web_url(text: str) -> str:
    """提取用户文字中的 URL；是否允许访问由联网工具另行验证。"""
    match = re.search(r"https?://[^\s<>'\"，。！？、）】]+", str(text or ""), re.I)
    return match.group(0).rstrip(".,;:!?") if match else ""


def web_research_explicitly_disabled(text: str) -> bool:
    """识别明确的离线要求，让后续判断尊重用户限制。"""

    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if not normalized:
        return False
    return bool(re.search(
        r"(?:不要|不用|无需|禁止)(?:再)?(?:联网|上网|在线查询|网络搜索|网页搜索)|"
        r"(?:别|不要)(?:再)?(?:查网|查网页|查外部资料|搜索外部资料)|"
        r"只(?:用|根据)(?:当前|本轮|这段)?(?:对话|上下文|已提供材料|附件内容)|"
        r"(?:离线|不联网)(?:回答|分析|处理)",
        normalized,
        re.I,
    ))


_PUBLIC_LOOKUP_VERB = re.compile(
    r"查(?:询|阅|找)?|查找|找|寻找|检索|搜索|搜寻|核实|查证|核对|验证|确认|"
    r"阅读|查看|看(?:一下|一看|下|看)|读(?:一下|一看)|"
    r"look\s*up|search|find|check|verify|consult|read",
    re.I,
)
_PUBLIC_SOURCE_MARKER = re.compile(
    r"标准|规范|协议|文档|手册|原文|论文|资料|出处|来源|引用|证据|链接|定义|说明|"
    r"公开(?:资料|文档|信息)?|官方(?:资料|文档|说明|规范)?|"
    r"standard|specification|protocol|documentation|docs?|manual|paper|schema|"
    r"source|citation|reference|link",
    re.I,
)
_PUBLIC_SOURCE_REQUEST = re.compile(
    r"(?:给出|提供|附上|需要|要).{0,32}(?:来源|出处|引用|证据|链接|资料)",
    re.I,
)
_VERSIONED_PUBLIC_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:"
    r"[A-Za-z]{2,}[A-Za-z]*v?\d+(?:[-_./]\d+)*"
    r"|"
    r"[A-Za-z]{2,}(?:[ \t._/-]+[A-Za-z0-9]+){0,2}[ \t._/-]+v?\d+"
    r"(?:[._/-]\d+)*"
    r")"
    r"(?![A-Za-z0-9])",
    re.I,
)
_LOCAL_OR_ATTACHMENT_REFERENCE = re.compile(
    r"附件|上传(?:的)?|本地|工作区|文件系统|文件夹|目录|路径|日志|截图|屏幕|"
    r"(?:本地|当前|我的|工作区|仓库|repo|repository|workspace)(?:的)?(?:代码|项目|仓库|文件|文档|附件|目录|路径)|"
    r"(?:代码|项目|仓库|repo|repository|workspace)(?:里|中|内容|路径)|"
    r"(?:本机|电脑|桌面)(?:上|里|中的?)|"
    r"(?:这个|那个|该|这份|那份|当前|我的|上面|下面)(?:文件|文档|附件|pdf|docx|txt|markdown|md\b)|"
    r"(?:文件|文档|附件)(?:里|中|内容|正文|路径)",
    re.I,
)


def user_explicitly_requests_public_web_research(text: str) -> bool:
    """识别要求查公开标准、文档或来源的语义请求。

    这是 ``user_explicitly_requests_web_research`` 的严格子集：普通的“搜一下
    某人/某项目”仍交给模型在研究循环中决定，不因搜索动词本身直接强制工具。
    只有用户同时指向公开资料，或明确写出联网查询，才向中央执行计划投影联网义务。
    本函数不读取附件、工作区或配置，也不执行网络请求。
    """

    raw = str(text or "")
    normalized = re.sub(r"\s+", "", raw.lower())
    if (
        not normalized
        or extract_public_web_url(raw)
        or web_research_explicitly_disabled(raw)
        or _LOCAL_OR_ATTACHMENT_REFERENCE.search(normalized)
    ):
        return False
    direct_public_lookup = re.search(
        r"(?:联网|上网|在线|网络|网页|网上|internet|online|web).{0,10}(?:查|搜|搜索|查询|检索|找|search|find)|"
        r"(?:查|搜|搜索|查询|检索|找|search|find).{0,10}(?:联网|上网|在线|网络|网页|网上|internet|online|web)",
        normalized,
        re.I,
    )
    if direct_public_lookup:
        return True
    return bool(
        _PUBLIC_SOURCE_REQUEST.search(normalized)
        or (
            _PUBLIC_LOOKUP_VERB.search(normalized)
            and (
                _PUBLIC_SOURCE_MARKER.search(normalized)
                or _VERSIONED_PUBLIC_IDENTIFIER.search(raw)
            )
        )
    )


def user_explicitly_requests_web_research(text: str) -> bool:
    """识别用户主动查询、核实和索要出处的请求。"""

    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if (
        not normalized
        or extract_public_web_url(text)
        or web_research_explicitly_disabled(text)
        or _LOCAL_OR_ATTACHMENT_REFERENCE.search(normalized)
    ):
        return False
    explicit = re.search(
        r"(?:联网|上网)(?:查|搜|搜索|查询)|在线(?:查|搜|搜索|查询)|网络搜索|网页搜索|网上(?:查|搜|搜索|找)|"
        r"搜索一下|搜一下|帮我搜|(?:请|麻烦|帮我|你)?查一下|(?:现在|再|重新|你)?查查|"
        r"(?:现在|再|重新|你)查(?:一)?下|查一下(?:最新|新闻|资料|消息|结果|比分|赛程|价格|天气)|"
        r"(?:核实|查证|验证|确认)(?:一下)?(?:这个|这件事|说法|消息|数据|出处|来源|真假)|"
        r"(?:给出|提供|附上|需要|要)(?:可靠|网页|公开|权威)?(?:来源|出处|引用|证据|链接)|"
        r"websearch|searchonline|lookuponline",
        normalized,
    )
    return bool(explicit or user_explicitly_requests_public_web_research(text))


def is_web_search_query(text: str) -> bool:
    """判断查询是否指向实时变化的信息，各领域共用同一入口。"""
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if (
        not normalized
        or extract_public_web_url(text)
        or web_research_explicitly_disabled(text)
    ):
        return False
    current_marker = re.search(r"今天|今日|现在|当前|刚刚|实时|最新|本周|本月|今年|目前|截至", normalized)
    changing_fact = re.search(
        r"比分|赛果|赛程|比赛|对阵|打完|赛后|排名|积分榜|新闻|消息|天气|气温|降雨|"
        r"股价|股票|价格|票价|汇率|利率|油价|金价|市值|指数|航班|列车|"
        r"上映|发布|更新|版本|任职|总统|首相|CEO|冠军|获奖|地震|疫情",
        normalized,
        re.I,
    )
    inherently_volatile = re.search(
        r"天气|气温|降雨|台风|比分|赛果|赛程|积分榜|排行榜|股价|股票价格|票价|汇率|"
        r"油价|金价|航班|列车|地震|疫情|新闻|热搜|当前版本|最新版本|现任|在任|"
        r"总统是谁|首相是谁|CEO是谁|负责人是谁|主教练是谁|冠军是谁|获奖名单|发售日期|上映日期",
        normalized,
        re.I,
    )
    return bool(
        user_explicitly_requests_web_research(text)
        or inherently_volatile
        or (current_marker and changing_fact)
    )


def is_external_factual_question(text: str) -> bool:
    """判断问题是否包含可公开核验的事实，本身不直接发起联网。"""

    normalized = re.sub(r"\s+", "", str(text or ""))
    if (
        not normalized
        or extract_public_web_url(text)
        or web_research_explicitly_disabled(text)
    ):
        return False
    non_factual = re.search(
        r"写(?:一首|一个|一段|篇)|续写|改写|润色|翻译|角色扮演|陪我|安慰我|"
        r"我(?:现在)?(?:开心|难过|生气|喜欢|爱|想念)|我(?:的)?性格|"
        r"你(?:喜欢|爱|想我|怎么看我|觉得我)|"
        r"根据(?:上文|这段|我说的)|总结(?:这段|上文)|只看上下文|不要联网",
        normalized,
        re.I,
    )
    if non_factual:
        return False
    question_or_analysis = re.search(
        r"什么|谁|哪(?:个|些|里|年|国|种)|几(?:个|年|月|日|点)|多少|是否|有没有|"
        r"为什么|怎么回事|如何|怎样|区别|原因|原理|历史|资料|信息|介绍|解释|评价|分析|"
        r"是真的吗|准确吗|正确吗|靠谱吗|发生了什么|表现如何|意味着什么|"
        r"\?|？",
        normalized,
        re.I,
    )
    factual_entity = re.search(
        r"[A-Za-z][A-Za-z0-9_.+-]{2,}|\d{2,}|版本|模型|API|论文|研究|公司|组织|人物|"
        r"游戏|电影|动画|漫画|小说|作品|角色|剧情|设定|世界观|作者|声优|性格|"
        r"人物|身份|归属|能力|技能|强度|战力|实力|表现|战绩|结局|人物关系|"
        r"赛事|比赛|产品|设备|软件|法律|政策|历史|事件|数据|统计|新闻|"
        r"药物|药品|剂量|疾病|症状|诊断|治疗|公司|破产",
        str(text or ""),
        re.I,
    )
    # 长度不是“外部事实”的证据。把任何八个字以上的疑问都视为事实问题，
    # 会让感情表达、哲学讨论和修辞性反问误触发联网。必须同时出现可公开
    # 核验的对象或事实领域锚点，模型才可以据此升级到网页研究。
    specific_fact_shape = re.search(
        r"[\u3400-\u9fff]{2,}(?:是谁|是什么|有哪些|多少岁|哪年|何时|什么时候|"
        r"出生(?:于|在)?哪|去世(?:于|在)?哪|来自哪里|属于哪|由谁|发生了什么)",
        normalized,
        re.I,
    )
    comparison = comparison_query_entities(text)
    return bool(question_or_analysis and (factual_entity or specific_fact_shape or len(comparison) == 2))


def model_web_research_requested(parsed: Any, user_message: str) -> bool:
    """读取模型的结构化查证标志，不从回复措辞反推联网要求。"""

    if not isinstance(parsed, dict):
        return False
    research_flag = parsed.get("researchNeeded")
    if research_flag is None:
        research_flag = parsed.get("needsWebResearch", parsed.get("needs_web_research"))
    if isinstance(research_flag, str):
        research_flag = research_flag.strip().lower() in {"1", "true", "yes", "需要", "是"}
    # Only the model's structured decision has authority.  Do not reinterpret
    # its prose or the user's words with a regex to manufacture a Web call.
    return bool(research_flag)


def should_auto_research_external_fact(text: str) -> bool:
    """根据时效、精确性和核验对象判断是否需要先查证。"""

    if web_research_explicitly_disabled(text):
        return False
    if is_web_search_query(text):
        return True
    if not is_external_factual_question(text):
        return False
    normalized = re.sub(r"\s+", "", str(text or ""))
    verification = re.search(
        r"据说|听说|传闻|网传|是真的吗|真假|准确吗|正确吗|靠谱吗|原话|出处|来源|证据|"
        r"核实|查证|是否属实|有没有这回事",
        normalized,
        re.I,
    )
    precise_or_high_stakes = re.search(
        r"具体(?:时间|日期|数字|数据)|准确(?:时间|日期|数字|数据)|哪年|何时|排名|统计|"
        r"版本号|发布日期|发售日期|法律|法规|政策|诊断|药物|剂量|投资|股票|汇率|价格",
        normalized,
        re.I,
    )
    raw_text = str(text or "")
    stable_foundational_terms = {
        "PYTHON", "JAVASCRIPT", "TYPESCRIPT", "WINDOWS", "LINUX", "GITHUB",
        "PYTORCH", "LIVE2D", "OPENGL", "WEBGL",
        "AI", "API", "CPU", "GPU", "HTTP", "HTTPS", "JSON", "TTS", "EXE", "KEY",
    }
    # Unicode ``\b`` does not split Latin → Han transitions because both are
    # word characters.  Use the shared script-aware tokenizer so
    # ``FooBar网站`` and ``arXiv论文`` remain visible to the policy engine.
    named_tokens = list(extract_latin_entities(raw_text))
    niche_named_entity = any(
        token.upper() not in stable_foundational_terms for token in named_tokens
    )
    comparison = len(comparison_query_entities(text)) == 2
    public_subject = re.search(
        r"人物|角色|作品|剧情|设定|世界观|作者|声优|身份|归属|能力|技能|强度|战力|"
        r"表现|战绩|结局|公司|组织|产品|型号|设备|软件|模型|论文|研究|历史|事件|"
        r"新闻|数据|统计|法律|法规|政策|药物|疾病|比赛|赛事",
        normalized,
        re.I,
    )
    research_shaped = re.search(
        r"如何评价|怎么评价|如何看待|分析|比较|对比|区别|差异|优缺点|影响|"
        r"发生了什么|为什么|原因|依据|证据|出处",
        normalized,
        re.I,
    )
    biography_or_attribution = re.search(
        r"是谁|由谁|哪年|何时|什么时候|多少岁|出生|去世|来自哪里|属于哪",
        normalized,
        re.I,
    )
    # Stable closed-form knowledge can still use the first model pass. Questions
    # about specific public subjects, comparisons and attribution browse first;
    # the model uncertainty flag remains a second, semantic safety net.
    foundational_only = bool(named_tokens) and all(
        token.upper() in stable_foundational_terms for token in named_tokens
    )
    return bool(
        verification
        or precise_or_high_stakes
        or niche_named_entity
        or comparison
        or biography_or_attribution
        or public_subject
        or (research_shaped and not foundational_only)
    )


def is_external_fact_correction_followup(
    user_message: str,
    messages: list[dict[str, Any]] | None,
) -> bool:
    """只根据传入对话判断事实纠正是否延续查证，不自行读取历史。"""

    current = re.sub(r"\s+", " ", str(user_message or "")).strip()
    if not current or web_research_explicitly_disabled(current):
        return False
    explicit_correction = is_explicit_correction_language(current) or bool(re.search(
        r"专有名词|固定(?:词|称呼|术语)|不要拆|不能拆|拆开|"
        r"你(?:理解|认|搞|弄)错|我说的是|指的是|意思是",
        current,
        flags=re.I,
    ))
    recent = [
        item for item in (messages or [])[-12:]
        if isinstance(item, dict) and str(item.get("content") or "").strip()
    ]
    prior_users = [
        str(item.get("content") or "").strip()
        for item in recent
        if item.get("role") == "user"
        and re.sub(r"\s+", " ", str(item.get("content") or "")).strip() != current
    ]
    if not prior_users:
        return False
    prior_public_research = any(
        should_auto_research_external_fact(item) or is_web_search_query(item)
        for item in prior_users[-4:]
    )
    recent_assistant = next(
        (
            str(item.get("content") or "").strip()
            for item in reversed(recent)
            if item.get("role") == "assistant"
        ),
        "",
    )
    if not prior_public_research and not re.search(r"\[S\d+\]", recent_assistant, re.I):
        return False
    if explicit_correction:
        return True

    # Users also correct a research answer by supplying the missing fact as a
    # plain assertion: "it has an official release and published benchmarks".
    # Treat that as a continuation only when the immediately preceding answer
    # explicitly denied, failed to find, or could not verify such evidence.
    previous_answer_denied_evidence = bool(re.search(
        r"没有找到|没找到|查不到|未(?:找到|发现|确认|查到)|"
        r"没有.{0,24}(?:资料|设定|证据|来源|记录|信息|表现|数据)|"
        r"缺乏.{0,24}(?:资料|设定|证据|来源|记录|信息|表现|数据)|"
        r"(?:不|尚不|并不)明确|无法(?:确认|核验|比较|判断)|"
        r"不能(?:确认|核验|比较|判断)|"
        r"如果你有.{0,30}(?:来源|出处|设定|资料|证据)",
        recent_assistant,
        re.I,
    ))
    corrective_fact_assertion = bool(re.search(
        r"(?:其实|事实上|明明|确实|本来|已经)|"
        r"(?:是|属于).{0,50}(?:官方|正式|续作|新作|版本|设定|内容|记录|资料)|"
        r"(?:有|存在|公布|发布|确认|写明|提到).{0,50}"
        r"(?:官方|正式|设定|内容|记录|资料|来源|出处|数据|表现|战绩|日期|版本)|"
        r"(?:官方|正式|设定|内容|记录|资料|来源|出处|数据|表现|战绩|日期|版本)"
        r".{0,50}(?:有|存在|公布|发布|确认|写明|提到)",
        current,
        re.I,
    ))
    return bool(previous_answer_denied_evidence and corrective_fact_assertion)
