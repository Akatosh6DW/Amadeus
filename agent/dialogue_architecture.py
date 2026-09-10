"""Adaptive turn policy and user-visible reply validation.

This module deliberately contains no provider, storage, tool, or Live2D code.
It is the shared policy layer used by both the fast utterance stream and the
tool-backed response path.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from typing import Any, Iterable, Literal, Mapping

from .proactive_control import ProactiveDirective, infer_explicit_proactive_directive
from .fingerprints import runtime_fingerprint


ReplyMode = Literal["micro", "short", "normal", "deep"]
DialogueAction = Literal[
    "answer",
    "react_in_character",
    "tease",
    "correct",
    "comfort",
    "clarify",
    "suggest_next_step",
    "summarize",
]
SpeechAct = Literal[
    "greeting",
    "acknowledgement",
    "factual_question",
    "relational_question",
    "opinion_question",
    "request",
    "external_action",
    "emotion_disclosure",
    "correction",
    "clarification_answer",
    "other",
]
ObligationDomain = Literal[
    "social",
    "user_state",
    "local_system",
    "local_observation",
    "persona_identity",
    "personal_history",
    "personal_schedule",
    "external_world",
    "stable_knowledge",
]
IdentityQuestionScope = Literal[
    "user_persona_identity",
    "user_real_identity",
    "assistant_identity",
]


@dataclass(frozen=True)
class Vocative:
    text: str
    category: str
    start: int
    end: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class UtteranceInterpretation:
    original_text: str
    semantic_text: str
    vocatives: tuple[Vocative, ...]
    primary_speech_act: SpeechAct
    secondary_speech_acts: tuple[SpeechAct, ...]
    requires_answer: bool
    answer_target: str | None
    confidence: float
    proactive_directive: ProactiveDirective

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["vocatives"] = [item.to_dict() for item in self.vocatives]
        value["secondary_speech_acts"] = list(self.secondary_speech_acts)
        return value


@dataclass(frozen=True)
class AnswerObligation:
    """What the assistant must accomplish, independent of words merely mentioned.

    A user utterance can contain current events, products, or software names without
    asking the assistant to assert anything about them.  Research requirements are
    therefore derived from these obligations, not directly from token co-occurrence.
    """

    obligation_id: str
    act_type: str
    target: str | None
    requires_fact_assertion: bool
    domain: ObligationDomain
    evidence_class: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AnswerObligation":
        domain = str(value.get("domain") or "social")
        if domain not in {
            "social", "user_state", "local_system", "local_observation", "personal_history",
            "personal_schedule", "persona_identity", "external_world", "stable_knowledge",
        }:
            domain = "social"
        return cls(
            obligation_id=str(value.get("obligation_id") or value.get("obligationId") or ""),
            act_type=str(value.get("act_type") or value.get("actType") or "respond"),
            target=str(value.get("target") or "").strip() or None,
            requires_fact_assertion=bool(
                value.get("requires_fact_assertion", value.get("requiresFactAssertion"))
            ),
            domain=domain,  # type: ignore[arg-type]
            evidence_class=str(
                value.get("evidence_class") or value.get("evidenceClass") or "none"
            ),
        )


@dataclass(frozen=True)
class TurnContract:
    contract_id: str
    primary_act: str
    required_acts: tuple[str, ...]
    optional_acts: tuple[str, ...]
    forbidden_substitutions: tuple[str, ...]
    first_visible_must_cover: tuple[str, ...]
    must_be_standalone: bool
    requires_user_response: bool
    reply_mode: ReplyMode

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        for key in (
            "required_acts",
            "optional_acts",
            "forbidden_substitutions",
            "first_visible_must_cover",
        ):
            value[key] = list(value[key])
        return value


@dataclass(frozen=True)
class FinalTurnEnvelope:
    turn_id: str
    contract_id: str
    reply_zh: str
    spoken_ja: str
    required_acts: tuple[str, ...]
    completed_acts: tuple[str, ...]
    depends_on_undelivered_prefix: bool
    standalone: bool
    turn_complete: bool
    upstream_finish_reason: str | None
    delivery_path: str

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["required_acts"] = list(self.required_acts)
        value["completed_acts"] = list(self.completed_acts)
        return value


_QUESTION_START = re.compile(
    r"^(?:你|我|他|她|它|这|那|我们|你们|他们|她们)?"
    r"(?:什么|怎么|哪里|哪儿|哪个|哪些|谁|多少|几|是不是|能不能|"
    r"可不可以|有没有|为什么|为何|要不要|该不该|会不会|是否|能否|如何)"
)
_QUESTION_ANYWHERE = re.compile(
    r"[？?]|(?:是不是|能不能|可不可以|有没有|为什么|为何|怎么|如何|"
    r"谁|什么|哪里|哪儿|哪个|哪些|多少|是否|能否|要不要|该不该|"
    r"会不会)|(?:吗|么|呢)[。！!…~～]*$"
)
_RELATIONSHIP_LANGUAGE = re.compile(
    r"喜欢|(?:我|你)(?:爱|在乎)(?:我|你)?|爱不爱|在不在乎|想我|想你|讨厌|关系|朋友|恋人|对象|伴侣|家人|"
    r"属于|你家的|我家的|我们(?:算|是)|对你来说|对我来说"
)
_OPINION_LANGUAGE = re.compile(
    r"你觉得|你认为|你怎么看|你的看法|评价|值不值得|好不好|怎么样"
)
_GREETING_ONLY = re.compile(
    r"^(?:喂|嗨|你好|早安|早上好|午安|晚上好|晚安|想你了|抱抱|亲亲)"
    r"[，,。！!？?…~～]*$"
)
_ACK_ONLY = re.compile(
    r"^(?:嗯+|哦+|好(?:的)?|行|可以|知道了|明白了|收到|继续|没事|"
    r"算了|对|是的|不是|谢谢|晚安|早安)[，,。！!？?…~～]*$"
)
_TASK_ACTION_PATTERN = (
    r"修复|修改|实现|新增|删除|重构|优化|检查|审查|评审|审阅|审核|评价|点评|"
    r"分析|解释|比较|对比|总结|导出|写|创建|生成|阅读|查找|搜索|打开|运行|测试"
)
_REQUEST_LANGUAGE = re.compile(
    r"^(?:请|麻烦|拜托)?(?:你)?(?:帮我|给我|替我|去|快|赶紧|立刻|马上)?"
    r"(?:" + _TASK_ACTION_PATTERN + r"|告诉我|"
    r"说一下|看一下|做一下|弄一下)"
)
_EXTERNAL_ACTION_LANGUAGE = re.compile(
    r"打开|访问|进入|跳转|启动|唤起|搜索|查找|搜一下"
)
_EMOTION_DISCLOSURE_LANGUAGE = re.compile(
    r"(?:我|自己).{0,8}(?:难受|害怕|焦虑|崩溃|开心|高兴|生气|"
    r"失眠|痛苦|绝望|很累|累死|想哭|想你)"
)
_EXPLICIT_CORRECTION_LANGUAGE = re.compile(
    r"^(?:"
    r"不对(?:[，,。！!：:]|$)|不是这样(?:[，,。！!：:]|$)|"
    r"我不是.{1,80}?(?:而是|只是)|"
    r"(?:你|助手|AMADEUS|阿玛迪斯)[，,]?(?:这|刚才|又|是不是)?[^。！？?!]{0,16}"
    r"(?:说错|理解错|理解反|搞反|记错)|"
    r"(?:我来)?(?:修正|纠正)(?:一下)?(?:[，,。！!：:]|$)|"
    r"准确地说(?:[，,。！!：:]|$)|别把.{1,60}当成"
    r")",
    re.I,
)


def is_explicit_correction_language(value: Any) -> bool:
    """Identify a correction speech act, not any sentence containing disapproval.

    Phrases such as ``我知道这么做不对`` describe the user's judgement about
    an event.  They must not turn the whole event into a correction record.
    Correction detection is therefore anchored to an explicit conversational
    move directed at the assistant or to a contrast the user states up front.
    """

    compact = re.sub(r"\s+", "", str(value or "")).strip()
    return bool(compact and _EXPLICIT_CORRECTION_LANGUAGE.search(compact))


def _vocative_view(value: Any) -> Vocative | None:
    if isinstance(value, Vocative):
        return value
    if isinstance(value, Mapping):
        span = value.get("span") if isinstance(value.get("span"), Mapping) else {}
        return Vocative(
            text=str(value.get("exactText") or value.get("exact_text") or value.get("text") or ""),
            category=str(value.get("vocativeType") or value.get("vocative_type") or value.get("category") or "other"),
            start=int(span.get("start") or value.get("start") or 0),
            end=int(span.get("end") or value.get("end") or 0),
        )
    exact = str(getattr(value, "exact_text", "") or "")
    if not exact:
        return None
    span = getattr(value, "span", None)
    return Vocative(
        text=exact,
        category=str(getattr(value, "vocative_type", "other") or "other"),
        start=int(getattr(span, "start", 0) or 0),
        end=int(getattr(span, "end", 0) or 0),
    )


def interpret_utterance(
    original_text: str,
    *,
    semantic_text: str | None = None,
    vocatives: Iterable[Any] = (),
    external_action: bool = False,
) -> UtteranceInterpretation:
    """Classify what the user is doing before deciding how long to answer."""

    original = str(original_text or "").strip()
    semantic = str(semantic_text if semantic_text is not None else original).strip()
    compact = re.sub(r"\s+", "", semantic)
    vocative_items = tuple(
        item for item in (_vocative_view(value) for value in vocatives) if item is not None
    )
    is_question = bool(_QUESTION_ANYWHERE.search(compact) or _QUESTION_START.search(compact))
    secondary: list[SpeechAct] = []
    answer_target: str | None = None
    confidence = 0.9

    if external_action:
        primary: SpeechAct = "external_action"
        requires_answer = True
        answer_target = "外部动作结果"
    elif is_explicit_correction_language(compact):
        primary = "correction"
        requires_answer = True
        answer_target = "纠正内容"
    elif _EMOTION_DISCLOSURE_LANGUAGE.search(compact) and not is_question:
        primary = "emotion_disclosure"
        requires_answer = True
        answer_target = "用户情绪"
    elif _GREETING_ONLY.fullmatch(compact):
        primary = "greeting"
        requires_answer = False
    elif _ACK_ONLY.fullmatch(compact):
        primary = "acknowledgement"
        requires_answer = False
    elif is_question and identity_question_scope(compact) is not None:
        primary = "factual_question"
        requires_answer = True
        answer_target = "本地角色身份"
    elif is_question and _RELATIONSHIP_LANGUAGE.search(compact):
        primary = "relational_question"
        requires_answer = True
        answer_target = "双方关系或情感立场"
    elif is_question and _OPINION_LANGUAGE.search(compact):
        primary = "opinion_question"
        requires_answer = True
        answer_target = "观点与判断"
    elif is_question:
        primary = "factual_question"
        requires_answer = True
        answer_target = "当前问题"
    elif _REQUEST_LANGUAGE.search(compact):
        primary = (
            "external_action"
            if _EXTERNAL_ACTION_LANGUAGE.search(compact)
            else "request"
        )
        requires_answer = True
        answer_target = "请求结果"
    else:
        primary = "other"
        requires_answer = False
        confidence = 0.68

    if vocative_items:
        secondary.append("acknowledgement")
    if _EMOTION_DISCLOSURE_LANGUAGE.search(compact) and primary != "emotion_disclosure":
        secondary.append("emotion_disclosure")
    return UtteranceInterpretation(
        original_text=original,
        semantic_text=semantic or original,
        vocatives=vocative_items,
        primary_speech_act=primary,
        secondary_speech_acts=tuple(dict.fromkeys(secondary)),
        requires_answer=requires_answer,
        answer_target=answer_target,
        confidence=confidence,
        proactive_directive=infer_explicit_proactive_directive(semantic or original),
    )


_LOCAL_CAPABILITY_QUESTION = re.compile(
    r"(?:你|助手|AMADEUS|Amadeus).{0,18}"
    r"(?:能不能|能否|可以|会不会|会|支持|有没有办法|还不能|不能)"
    r".{0,28}(?:打开|操作|读取|查看|识别|截图|摄像头|浏览器|网页|文件|"
    r"运行|执行|控制|调用|联网|搜索|玩|打游戏)|"
    r"(?:打开|操作|读取|查看|识别|截图|摄像头|浏览器|网页|文件|"
    r"运行|执行|控制|调用|联网|搜索|玩|打游戏).{0,24}"
    r"(?:吗|么|不|能不能|能否|可以吗)",
    re.I,
)
_FOUNDATIONAL_DEFINITION = re.compile(
    r"^(?:请问)?(?:HTTP|HTTPS|TCP|UDP|DNS|JSON|HTML|CSS|SQL|CPU|GPU|"
    r"CNN|RNN|API|TTS)(?:到底)?(?:是|指|是什么意思|是什么)",
    re.I,
)
_PERSONAL_HISTORY_QUESTION = re.compile(
    r"(?:昨天|前天|之前|上次|刚才|刚刚|以前|过去).{0,24}"
    r"(?:我们|你|我).{0,18}(?:约定|说过|聊过|提过|答应|决定|讨论)|"
    r"(?:我们|你|我).{0,18}(?:约定|说过|聊过|提过|答应|决定|讨论)"
    r".{0,24}(?:什么|哪件|内容|结果)",
    re.I,
)
_PERSONAL_SCHEDULE_QUESTION = re.compile(
    r"(?:我的?|我)(?:今天|明天|昨天|上午|下午|晚上|本周|这周)?"
    r"(?:本来)?(?:要|该|应该|计划|安排).{0,16}(?:做什么|干什么|完成什么)|"
    r"(?:我的?)?(?:待办|任务|日程|个人安排|计划事项)",
    re.I,
)
_PUBLIC_WORLD_QUESTION = re.compile(
    r"天气|气温|降雨|新闻|比分|赛程|股价|价格|汇率|航班|列车|"
    r"营业时间|开放时间|现任|在任|政策|法规|版本号|最新版|最新版本|"
    r"(?:当前|目前|现在|今天|本周|本月|今年|截至).{0,18}"
    r"(?:公司|产品|软件|模型|网站|国家|城市|市场|比赛|发布|上线|支持)",
    re.I,
)


def identity_question_scope(text: str) -> IdentityQuestionScope | None:
    """Resolve whose locally configured identity a question asks about.

    This is an authority boundary, not a general intent classifier.  It only
    recognizes direct identity/name questions about one of the two dialogue
    participants.  Public questions such as "凤凰院凶真是谁" remain ordinary
    external-world questions, while explicit legal/real-name wording remains
    separate from the configured AMADEUS relationship identity.
    """

    value = re.sub(r"\s+", "", str(text or ""))
    if not value or not (_QUESTION_ANYWHERE.search(value) or _QUESTION_START.search(value)):
        return None
    real_identity = bool(re.search(r"现实|真实|本名|法定|身份证|户籍", value, re.I))
    user_identity = bool(re.search(
        r"(?:我(?:的)?(?:现实|真实|本名|法定)?(?:名字|姓名|身份|称呼)"
        r"(?:是|叫|是什么|叫什么|怎么称呼|呢|吗|[？?])|"
        r"我(?:到底|究竟)?是谁|你(?:还)?(?:知道|记得|认为|觉得)?我(?:到底|究竟)?是谁|"
        r"你(?:平时|通常|应该|会)?(?:怎么|如何)?(?:叫|称呼)我(?:什么|呢|吗|[？?])|"
        r"你(?:还)?(?:知道|记得)我叫什么(?:名字)?|我(?:应该)?叫什么)",
        value,
        re.I,
    ))
    if user_identity:
        return "user_real_identity" if real_identity else "user_persona_identity"
    assistant_identity = bool(re.search(
        r"(?:(?:你|AMADEUS|Amadeus|amadeus|阿马德乌斯|阿玛迪斯|这个AI|这个ai)(?:的)?"
        r"(?:名字|姓名|身份|原型|人格来源|记忆来源)(?:是|叫|是什么|叫什么|来自谁)?|"
        r"你(?:到底|究竟)?是谁|你是红莉栖吗|(?:AMADEUS|Amadeus|amadeus)是谁|"
        r"(?:AMADEUS|Amadeus|amadeus|阿马德乌斯|阿玛迪斯|这个AI|这个ai)(?:是)?基于谁(?:构建|制作|训练)?|"
        r"谁(?:构建|制作|训练)(?:了)?你|你和红莉栖(?:是)?什么关系)",
        value,
        re.I,
    ))
    return "assistant_identity" if assistant_identity else None


def plan_answer_obligations(
    interpretation: UtteranceInterpretation,
) -> tuple[AnswerObligation, ...]:
    """Turn speech acts into explicit answer duties used by every later layer."""

    text = str(interpretation.semantic_text or interpretation.original_text or "").strip()
    act = interpretation.primary_speech_act
    target = interpretation.answer_target or text[:180] or None
    if act == "factual_question":
        identity_scope = identity_question_scope(text)
        if identity_scope is not None:
            domain: ObligationDomain = "persona_identity"
            evidence_class = "persona_profile"
            target = identity_scope
        elif _LOCAL_CAPABILITY_QUESTION.search(text):
            domain: ObligationDomain = "local_system"
            evidence_class = "local_state"
        elif _PERSONAL_SCHEDULE_QUESTION.search(text):
            domain = "personal_schedule"
            evidence_class = "task_state"
        elif _PERSONAL_HISTORY_QUESTION.search(text):
            domain = "personal_history"
            evidence_class = "conversation_history"
        elif _FOUNDATIONAL_DEFINITION.search(re.sub(r"\s+", "", text)):
            domain = "stable_knowledge"
            evidence_class = "none"
        elif _PUBLIC_WORLD_QUESTION.search(text):
            # This classification records the evidence shape only.  It is not
            # allowed to force Web execution; QueryLoop's model decision owns
            # that transition.
            domain = "external_world"
            evidence_class = "web_evidence"
        else:
            domain = "external_world"
            evidence_class = "web_evidence"
        requires_fact = True
    elif act in {"request", "external_action", "correction"}:
        domain = "local_system" if act == "external_action" else "social"
        evidence_class = "action_receipt" if act == "external_action" else "none"
        requires_fact = act == "external_action"
    elif act in {"relational_question", "opinion_question", "emotion_disclosure"}:
        if act == "opinion_question" and _PUBLIC_WORLD_QUESTION.search(text):
            domain = "external_world"
            evidence_class = "web_evidence"
            requires_fact = True
        else:
            domain = "user_state" if act == "emotion_disclosure" else "social"
            evidence_class = "user_authority" if domain == "user_state" else "none"
            requires_fact = False
    else:
        domain = "social"
        evidence_class = "none"
        requires_fact = False
    obligation_id = "obl_" + runtime_fingerprint(
        f"{act}\x1f{target or ''}\x1f{domain}"
    )
    return (AnswerObligation(
        obligation_id=obligation_id,
        act_type=("answer" if interpretation.requires_answer else "respond") + f"_{act}",
        target=target,
        requires_fact_assertion=requires_fact,
        domain=domain,
        evidence_class=evidence_class,
    ),)


@dataclass(frozen=True)
class TurnPolicy:
    """Soft speaking policy for one conversational turn.

    The preferred sentence range guides the model; it is not a truncation
    boundary. The much wider severe limits exist only to detect a genuinely
    runaway answer that merits one semantic rewrite.
    """

    reply_mode: ReplyMode
    action_budget: int
    allowed_actions: tuple[DialogueAction, ...]
    allow_followup_question: bool
    allow_bullets: bool
    allow_summary: bool
    max_output_tokens: int
    memory_top_k: int
    recent_message_limit: int
    tool_evidence_max_chars: int
    preferred_sentence_min: int
    preferred_sentence_max: int
    max_sentences: int
    max_reply_characters: int
    severe_sentence_limit: int
    severe_character_limit: int
    reason: str

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["allowed_actions"] = list(self.allowed_actions)
        return value


_BASE_POLICIES: dict[ReplyMode, TurnPolicy] = {
    "micro": TurnPolicy(
        reply_mode="micro",
        action_budget=1,
        allowed_actions=("react_in_character",),
        allow_followup_question=False,
        allow_bullets=False,
        allow_summary=False,
        # Includes JSON fields and a complete Japanese speaking line.
        max_output_tokens=320,
        memory_top_k=1,
        recent_message_limit=6,
        tool_evidence_max_chars=5_000,
        preferred_sentence_min=1,
        preferred_sentence_max=2,
        max_sentences=3,
        max_reply_characters=240,
        severe_sentence_limit=6,
        severe_character_limit=420,
        reason="brief social or acknowledgement turn",
    ),
    "short": TurnPolicy(
        reply_mode="short",
        action_budget=2,
        allowed_actions=("answer", "react_in_character"),
        allow_followup_question=False,
        allow_bullets=False,
        allow_summary=False,
        max_output_tokens=560,
        memory_top_k=2,
        recent_message_limit=8,
        tool_evidence_max_chars=9_000,
        preferred_sentence_min=1,
        preferred_sentence_max=4,
        max_sentences=5,
        max_reply_characters=520,
        severe_sentence_limit=10,
        severe_character_limit=1_000,
        reason="one focused question or action",
    ),
    "normal": TurnPolicy(
        reply_mode="normal",
        action_budget=3,
        allowed_actions=("answer", "react_in_character", "clarify"),
        allow_followup_question=False,
        allow_bullets=False,
        allow_summary=False,
        max_output_tokens=1_000,
        memory_top_k=3,
        recent_message_limit=10,
        tool_evidence_max_chars=18_000,
        preferred_sentence_min=2,
        preferred_sentence_max=7,
        max_sentences=10,
        max_reply_characters=1_500,
        severe_sentence_limit=24,
        severe_character_limit=3_000,
        reason="ordinary explanation or discussion",
    ),
    "deep": TurnPolicy(
        reply_mode="deep",
        action_budget=5,
        allowed_actions=(
            "answer",
            "react_in_character",
            "correct",
            "clarify",
            "suggest_next_step",
        ),
        allow_followup_question=True,
        allow_bullets=True,
        allow_summary=True,
        max_output_tokens=2_600,
        memory_top_k=5,
        recent_message_limit=12,
        tool_evidence_max_chars=52_000,
        preferred_sentence_min=4,
        preferred_sentence_max=18,
        max_sentences=30,
        max_reply_characters=7_000,
        severe_sentence_limit=60,
        severe_character_limit=10_000,
        reason="explicitly detailed or genuinely multi-part task",
    ),
}

_DETAIL_MARKERS = re.compile(
    r"详细|深入|完整(?:分析|解释|讲解)|一步一步|一步步|系统(?:地)?讲|"
    r"展开说|技术细节|具体(?:怎么|如何)(?:实现|设计)|原理(?:是什么|分析|讲解)|"
    r"全面(?:分析|对比)|逐项|从头到尾|不要漏|pipeline|架构优化",
    re.I,
)
_BRIEF_MARKERS = re.compile(
    r"简短|简洁|一句话|直接说|只要结论|别展开|不要展开|别啰嗦|少说点",
    re.I,
)
_USER_PLAN_OR_PROMISE = re.compile(
    r"(?:^|[，。；;])(?:等|等到|以后|未来|将来|到时候)?我"
    r".{0,36}(?:打算|准备|计划|会|要|想|先|再|之后|以后|未来|将来|等)",
    re.I,
)
_SOCIAL_MARKERS = re.compile(
    r"^(?:喂|嗨|你好|早安|早上好|午安|晚上好|晚安|助手|克里斯蒂娜|"
    r"红莉栖|想你了|喜欢你|爱你|抱抱|亲亲|谢谢|辛苦了|你真厉害|你真可爱)"
)
_ACKNOWLEDGEMENT = re.compile(
    r"^(?:嗯+|哦+|好(?:的)?|行|可以|知道了|明白了|收到|继续|没事|算了|"
    r"对|是的|不是|谢谢|晚安|早安)[，,。！!？?…~～]*$"
)
_ACK_SEQUENCE = re.compile(
    r"^(?:嗯+|哦+)[，,]?(?:(?:我)?知道了|明白了|好(?:的)?|行)?[。！!…~～]*$"
)
_SIMPLE_QUESTION = re.compile(
    r"^(?:什么|怎么|哪里|哪个|谁|多少|几|是不是|能不能|可不可以|有没有|"
    r"为什么|为何|要不要|该不该|是否|如何)"
)
_TASK_VERBS = re.compile(_TASK_ACTION_PATTERN)
_DISTRESS = re.compile(
    r"难受|害怕|焦虑|崩溃|撑不住|想哭|失眠|痛苦|绝望|很累|累死|受伤"
)
_BRAINSTORM = re.compile(
    r"一起想|讨论一下|你怎么看|你觉得呢|有什么想法|帮我想|头脑风暴"
)
_SUMMARY_REQUEST = re.compile(r"总结|概括|归纳|结论汇总")
_SUGGESTION_REQUEST = re.compile(
    r"建议|下一步|怎么办|怎么改|如何优化|(?:给|设计|制定).{0,3}方案"
)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


def _multi_part_score(text: str) -> int:
    compact = _compact(text)
    separators = len(re.findall(r"[；;]|(?:另外|同时|以及|并且|还要|再加上)", compact))
    enumerated = len(re.findall(r"(?:^|[\s，。；])(?:\d+[.、)]|[一二三四五六七八九十]+、)", text))
    question_parts = len(re.findall(r"[？?]", text))
    return separators + enumerated + max(0, question_parts - 1)


def _substantive_detail_request(
    text: str,
    interpretation: UtteranceInterpretation | None = None,
) -> bool:
    if not _DETAIL_MARKERS.search(text):
        return False
    # Detail words inside a user's plan, promise, or quoted subject matter are
    # not instructions to the assistant.  The speech-act layer owns that
    # distinction; keyword presence alone cannot promote a social turn to deep.
    if (
        interpretation is not None
        and not interpretation.requires_answer
        and _USER_PLAN_OR_PROMISE.search(text)
    ):
        return False
    # “详细说说你为什么这么可爱” is still a social exchange, while an
    # implementation/analysis request genuinely benefits from deep mode.
    return bool(
        _TASK_VERBS.search(text)
        or re.search(r"代码|工程|模型|系统|模块|流程|原因|机制|论文|数据|训练|测试", text)
        or len(_compact(text)) >= 90
    )


def _allowed_actions(
    text: str,
    mode: ReplyMode,
    interpretation: UtteranceInterpretation | None = None,
) -> tuple[DialogueAction, ...]:
    actions: list[DialogueAction] = []
    compact = _compact(text)
    primary = interpretation.primary_speech_act if interpretation else ""
    social = bool(
        _SOCIAL_MARKERS.search(compact)
        or primary in {"greeting", "acknowledgement", "relational_question"}
        or bool(interpretation and interpretation.vocatives)
    )
    asks_or_tasks = bool(
        interpretation and interpretation.requires_answer
        or primary in {"request", "external_action", "correction"}
        or
        re.search(r"[？?]", text)
        or _SIMPLE_QUESTION.search(compact)
        or _TASK_VERBS.search(text)
    )
    # Required answer-like actions always precede optional persona rendering.
    if asks_or_tasks:
        actions.append("answer")
    if _DISTRESS.search(text):
        actions.append("comfort")
    if social:
        actions.append("react_in_character")
    if is_explicit_correction_language(text):
        actions.append("correct")
    if _BRAINSTORM.search(text) and mode in {"normal", "deep"}:
        actions.append("clarify")
    if _SUGGESTION_REQUEST.search(text) and mode in {"normal", "deep"}:
        actions.append("suggest_next_step")
    if _SUMMARY_REQUEST.search(text) and mode == "deep":
        actions.append("summarize")
    if not actions:
        actions.append("react_in_character" if mode == "micro" else "answer")
    # Preserve order while keeping the budget explicit.
    return tuple(dict.fromkeys(actions))


def choose_turn_policy(
    user_text: str,
    *,
    has_tools: bool = False,
    has_attachments: bool = False,
    file_output_expected: bool = False,
    interpretation: UtteranceInterpretation | None = None,
) -> TurnPolicy:
    """Choose a transparent, deterministic soft policy for one turn."""

    active_interpretation = interpretation or interpret_utterance(user_text)
    text = str(active_interpretation.semantic_text or user_text or "").strip()
    compact = _compact(text)
    multi_part = _multi_part_score(text)

    if file_output_expected:
        mode: ReplyMode = "deep"
        reason = "file generation needs complete content outside the spoken reply"
    elif _BRIEF_MARKERS.search(text):
        mode = (
            "micro"
            if len(compact) <= 24 and _SOCIAL_MARKERS.search(compact)
            else "short"
        )
        reason = "user explicitly asked for a concise answer"
    elif _substantive_detail_request(text, active_interpretation):
        mode = "deep"
        reason = "user explicitly requested substantive depth"
    elif multi_part >= 3 or len(compact) >= 360:
        mode = "deep"
        reason = "genuinely multi-part turn"
    elif active_interpretation.primary_speech_act == "relational_question" and len(compact) <= 48:
        mode = "micro"
        reason = "brief relational question still requires a direct stance"
    elif _ACKNOWLEDGEMENT.fullmatch(compact) or _ACK_SEQUENCE.fullmatch(compact) or (
        len(compact) <= 24 and _SOCIAL_MARKERS.search(compact)
    ):
        mode = "micro"
        reason = "brief social or acknowledgement turn"
    elif _BRAINSTORM.search(text) or _SUGGESTION_REQUEST.search(text):
        mode = "normal"
        reason = "discussion or design judgment benefits from a little context"
    elif (
        re.search(r"为什么|原因|机制", text)
        and re.search(r"回复|对话|系统|模型|代码|工具|记忆|失败|问题|重复", text)
    ):
        mode = "normal"
        reason = "causal diagnosis needs more than a one-line fact"
    elif (
        (has_attachments or re.search(r"图片|截图|画面|附件|文件", text))
        and re.search(r"看|分析|评价|判断|检查|表情|内容", text)
    ):
        mode = "normal"
        reason = "attachment or visual interpretation needs grounded discussion"
    elif (
        len(compact) <= 70
        and multi_part == 0
        and (
            re.search(r"[？?]", text)
            or _SIMPLE_QUESTION.search(compact)
            or has_tools
        )
    ):
        mode = "short"
        reason = "one focused question or tool action"
    elif multi_part >= 1 or len(compact) >= 150 or has_attachments:
        mode = "normal"
        reason = "ordinary multi-clause discussion"
    else:
        mode = "normal"
        reason = "ordinary conversation"

    base = _BASE_POLICIES[mode]
    actions = _allowed_actions(text, mode, active_interpretation)
    allow_followup = bool(
        mode in {"normal", "deep"}
        and (
            _BRAINSTORM.search(text)
            or re.search(r"你想知道|你可以问|聊聊|讨论", text)
        )
    )
    allow_summary = bool(mode == "deep" and _SUMMARY_REQUEST.search(text))
    if mode == "deep" and _DETAIL_MARKERS.search(text):
        allow_summary = True
    if _DISTRESS.search(text):
        actions = tuple(dict.fromkeys(("comfort", *actions)))
    budget = max(base.action_budget, min(5, len(actions)))
    tool_chars = base.tool_evidence_max_chars
    if has_tools and mode == "micro":
        tool_chars = _BASE_POLICIES["short"].tool_evidence_max_chars
    return replace(
        base,
        action_budget=budget,
        allowed_actions=actions[:budget],
        allow_followup_question=allow_followup,
        allow_summary=allow_summary,
        reason=reason,
        tool_evidence_max_chars=tool_chars,
    )


def build_turn_contract(
    interpretation: UtteranceInterpretation,
    policy: TurnPolicy,
    *,
    requires_user_response: bool = False,
) -> TurnContract:
    """Build required outcomes before optional persona rendering."""

    required_by_act: dict[SpeechAct, tuple[str, ...]] = {
        "factual_question": ("answer_current_question",),
        "relational_question": ("state_relationship_position",),
        "opinion_question": ("state_reasoned_opinion",),
        "request": ("complete_requested_task",),
        "external_action": ("execute_or_report_action",),
        "emotion_disclosure": ("respond_to_user_emotion",),
        "correction": ("address_user_correction",),
        "clarification_answer": ("continue_pending_topic",),
        "greeting": ("acknowledge_user",),
        "acknowledgement": ("acknowledge_user",),
        "other": ("respond_to_current_utterance",),
    }
    required = required_by_act[interpretation.primary_speech_act]
    optional_map: dict[DialogueAction, str] = {
        "react_in_character": "persona_reaction",
        "tease": "tease",
        "comfort": "comfort",
        "clarify": "clarify",
        "correct": "correct",
        "suggest_next_step": "suggest_next_step",
        "summarize": "summarize",
        "answer": "additional_explanation",
    }
    optional = tuple(dict.fromkeys(
        optional_map[item]
        for item in policy.allowed_actions
        if item in optional_map and item != "answer"
    ))
    primary_act = (
        "answer_" + interpretation.primary_speech_act
        if interpretation.requires_answer
        else "respond_" + interpretation.primary_speech_act
    )
    payload = {
        "primaryAct": primary_act,
        "requiredActs": list(required),
        "optionalActs": list(optional),
        "replyMode": policy.reply_mode,
        "semanticText": interpretation.semantic_text,
    }
    contract_id = "tc_" + runtime_fingerprint(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return TurnContract(
        contract_id=contract_id,
        primary_act=primary_act,
        required_acts=required,
        optional_acts=optional,
        forbidden_substitutions=(
            "persona_reaction_cannot_replace_required_answer",
        ),
        first_visible_must_cover=required if policy.reply_mode in {"micro", "short"} else required[:1],
        must_be_standalone=True,
        requires_user_response=requires_user_response,
        reply_mode=policy.reply_mode,
    )


def bind_missing_input_clarification_contract(
    contract: TurnContract,
    *,
    semantic_text: str = "",
) -> TurnContract:
    """Transition a task contract after a semantic audit confirms missing input.

    The caller, not a keyword matcher, must first establish that the model
    emitted a typed clarification and that the requested task cannot be
    completed from the material currently available.  Asking for that one
    missing input is then the successful terminal outcome of this turn.
    """

    required = ("request_missing_input",)
    payload = {
        "parentContractId": contract.contract_id,
        "requiredActs": list(required),
        "replyMode": contract.reply_mode,
        "semanticText": str(semantic_text or "")[:500],
        "outcomeClass": "missing_input_clarification",
    }
    contract_id = "tc_" + runtime_fingerprint(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return replace(
        contract,
        contract_id=contract_id,
        primary_act="clarify_missing_input",
        required_acts=required,
        first_visible_must_cover=required,
        forbidden_substitutions=tuple(dict.fromkeys((
            *contract.forbidden_substitutions,
            "optional_followup_cannot_replace_a_completable_task",
        ))),
        requires_user_response=True,
    )


def bind_current_visual_observation_contract(
    contract: TurnContract,
    *,
    semantic_text: str = "",
) -> TurnContract:
    """Bind a contract to a visual observation after a tool actually succeeds.

    This transition deliberately depends on a typed observation receipt at the
    caller, not on matching words such as "看看" or "屏幕" in the user text.
    """

    required = ("answer_from_current_visual_observation",)
    payload = {
        "parentContractId": contract.contract_id,
        "requiredActs": list(required),
        "replyMode": contract.reply_mode,
        "semanticText": str(semantic_text or "")[:500],
        "evidenceClass": "fresh_observation",
    }
    contract_id = "tc_" + runtime_fingerprint(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return replace(
        contract,
        contract_id=contract_id,
        required_acts=required,
        first_visible_must_cover=required,
        forbidden_substitutions=tuple(dict.fromkeys((
            *contract.forbidden_substitutions,
            "persona_memory_or_ocr_cannot_replace_current_visual_observation",
            "visual_response_type_must_match_current_semantic_request",
        ))),
    )


def bind_current_visual_failure_contract(
    contract: TurnContract,
    *,
    semantic_text: str = "",
) -> TurnContract:
    """Bind the useful terminal outcome of an attempted visual observation.

    The caller must hold an actual failed visual-tool receipt. This contract
    does not pretend that an image exists; it requires a clear explanation of
    the local capture failure and prevents a generic factual-answer contract
    from turning that explanation into a retry loop or a 503.
    """

    required = ("explain_current_visual_observation_failure",)
    payload = {
        "parentContractId": contract.contract_id,
        "requiredActs": list(required),
        "replyMode": contract.reply_mode,
        "semanticText": str(semantic_text or "")[:500],
        "evidenceClass": "action_receipt",
    }
    contract_id = "tc_" + runtime_fingerprint(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return replace(
        contract,
        contract_id=contract_id,
        primary_act=required[0],
        required_acts=required,
        first_visible_must_cover=required,
        forbidden_substitutions=tuple(dict.fromkeys((
            *contract.forbidden_substitutions,
            "failed_visual_capture_cannot_be_replaced_by_guessed_observation",
        ))),
    )


def turn_contract_prompt(
    interpretation: UtteranceInterpretation,
    contract: TurnContract,
) -> str:
    """Render a concise per-turn contract immediately before the user message."""

    proactive_contract = (
        {"proactiveDirectiveRequired": interpretation.proactive_directive}
        if interpretation.proactive_directive != "unchanged"
        else {"proactiveDirectivePolicy": "model_decides_from_current_user_intent"}
    )
    proactive_instruction = (
        "proactiveDirectiveRequired 是本轮结束后的强制主动联系状态；输出 proactiveDirective 必须与它逐字一致。"
        if interpretation.proactive_directive != "unchanged"
        else
        "proactiveDirectivePolicy 要求根据当前用户整句语义判断：用户希望助手保持安静并由用户稍后回来时输出 "
        "pause_until_next_user_message，明确恢复联系时输出 resume；只是谈到等待、忙碌或时间时输出 unchanged。"
    )
    return "\n".join([
        "[Turn Contract：本轮生成前已确定，必须优先满足]",
        json.dumps({
            "semanticText": interpretation.semantic_text,
            "primarySpeechAct": interpretation.primary_speech_act,
            "answerTarget": interpretation.answer_target,
            **proactive_contract,
            **contract.to_dict(),
        }, ensure_ascii=False, separators=(",", ":")),
        "required_acts 是本轮不可省略的交流结果；optional_acts 只能在完成必答结果后自然加入。",
        "人格吐槽、昵称反应、害羞或调侃绝不能替代 required_acts。",
        "第一条可见中文及其日语发声稿必须覆盖 first_visible_must_cover，并且脱离任何未显示前文也能独立成立。",
        proactive_instruction,
        "输出 completedActs、standalone、dependsOnUndeliveredPrefix、turnComplete；只有真正完成合同时才能声明完成。",
    ])


def contract_completion_issues(
    result: Mapping[str, Any],
    contract: TurnContract,
) -> tuple[str, ...]:
    """Check one provider-independent completion envelope."""

    completed_value = result.get("completedActs")
    completed_items = completed_value if isinstance(completed_value, (list, tuple)) else ()
    completed = {
        str(item)
        for item in completed_items
        if str(item)
    }
    issues: list[str] = []
    missing = [item for item in contract.required_acts if item not in completed]
    if missing:
        issues.append("missing_required_acts:" + ",".join(missing))
    if result.get("standalone") is not True:
        issues.append("standalone_not_confirmed")
    if bool(result.get("dependsOnUndeliveredPrefix")):
        issues.append("depends_on_undelivered_prefix")
    if result.get("turnComplete") is not True:
        issues.append("turn_not_complete")
    if not str(result.get("reply") or "").strip():
        issues.append("empty_reply")
    if not str(result.get("spokenTextJa") or "").strip():
        issues.append("empty_spoken_ja")
    return tuple(issues)


def final_turn_envelope(
    result: Mapping[str, Any],
    contract: TurnContract,
    *,
    turn_id: str,
    delivery_path: str,
    upstream_finish_reason: str | None = None,
) -> FinalTurnEnvelope:
    completed_value = result.get("completedActs")
    completed_items = completed_value if isinstance(completed_value, (list, tuple)) else ()
    completed = tuple(
        str(item)
        for item in completed_items
        if str(item)
    )
    missing = tuple(item for item in contract.required_acts if item not in completed)
    if missing:
        raise ValueError(
            "final_turn_envelope_missing_required_acts:" + ",".join(missing)
        )
    return FinalTurnEnvelope(
        turn_id=str(turn_id),
        contract_id=contract.contract_id,
        reply_zh=str(result.get("reply") or ""),
        spoken_ja=str(result.get("spokenTextJa") or ""),
        required_acts=contract.required_acts,
        completed_acts=completed,
        depends_on_undelivered_prefix=bool(result.get("dependsOnUndeliveredPrefix")),
        standalone=result.get("standalone") is True,
        turn_complete=result.get("turnComplete") is True,
        upstream_finish_reason=upstream_finish_reason,
        delivery_path=str(delivery_path),
    )


def policy_prompt(policy: TurnPolicy) -> str:
    """Render the dynamic policy without turning sentence guidance into a guillotine."""

    action_lines = "\n".join(f"- {item}" for item in policy.allowed_actions)
    return "\n".join([
        "[Turn Policy：本轮表达策略]",
        f"reply_mode: {policy.reply_mode}",
        f"policy_reason: {policy.reason}",
        f"action_budget: {policy.action_budget}",
        "allowed_actions:",
        action_lines,
        f"allow_followup_question: {str(policy.allow_followup_question).lower()}",
        f"allow_bullets: {str(policy.allow_bullets).lower()}",
        f"allow_summary: {str(policy.allow_summary).lower()}",
        (
            "通常用 "
            f"{policy.preferred_sentence_min}–{policy.preferred_sentence_max} 个完整句子即可。"
            f"本模式上限为 {policy.max_sentences} 个完整句子；"
            "若当前任务客观上无法在上限内完整回答，应提高回复模式，而不是漏答、拆碎句子或突然停止。"
            "禁止把句子硬截断。"
        ),
        "这一轮最多选择 action_budget 个真实交流动作，不要把吐槽、解释、关心、总结、建议和追问全套堆在一起。",
        (
            "结论和必要依据已经满足当前需求后立即自然收住。"
            "只有 allow_followup_question=true 且问题确实推进当前话题时，才可以在完成回答后另加问题；"
            "allow_followup_question=false 时，不得为了维持对话而追加新问题、选择菜单、主动服务邀约或"
            "‘我还可以继续做什么’式结尾。只有缺少关键条件、当前请求因此无法完成时，才可提出一个必要的澄清问题。"
            "这约束的是本轮生成方式，不要输出或解释这些规则。"
        ),
        (
            "可以分点，但只在结构确实比连续口语更清楚时使用。"
            if policy.allow_bullets else
            "这一轮优先连续自然对话，不使用报告式分点。"
        ),
    ])


@dataclass(frozen=True)
class DialogueValidation:
    sentence_count: int
    character_count: int
    bullet_count: int
    forbidden_phrase_hits: tuple[str, ...]
    internal_process_hits: tuple[str, ...]
    repeats_previous_reply: bool
    policy_overflow: bool
    severe_overflow: bool
    issues: tuple[str, ...]
    needs_rewrite: bool

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        for key in ("forbidden_phrase_hits", "internal_process_hits", "issues"):
            value[key] = list(value[key])
        return value


_INTERNAL_PROCESS_PATTERNS = (
    "我将调用",
    "我会调用",
    "调用工具",
    "工具调用",
    "路由判断",
    "内部提示",
    "系统提示词",
    "toolResults",
    "toolRequests",
    "reply_mode",
    "action_budget",
)


def count_sentences(text: str) -> int:
    parts = re.split(r"[。！？!?]+|(?<!\d)\.(?!\d)", str(text or "").strip())
    return sum(1 for part in parts if part.strip())


def _bullet_count(text: str) -> int:
    return sum(
        1
        for line in str(text or "").splitlines()
        if re.match(r"^\s*(?:[-*•]|\d+[.、)]|[一二三四五六七八九十]+、)\s*", line)
    )


def _similar_to_previous(reply: str, previous_replies: Iterable[str]) -> bool:
    compact = re.sub(r"[\W_]+", "", reply.lower(), flags=re.UNICODE)
    if len(compact) < 60:
        return False
    for previous in previous_replies:
        old = re.sub(r"[\W_]+", "", str(previous).lower(), flags=re.UNICODE)
        if len(old) >= 60 and SequenceMatcher(None, compact, old).ratio() >= 0.9:
            return True
    return False


def validate_character_reply(
    reply: str,
    policy: TurnPolicy,
    *,
    forbidden_phrases: Iterable[str] = (),
    previous_replies: Iterable[str] = (),
) -> DialogueValidation:
    """Inspect a final character reply without locally authoring a replacement."""

    text = str(reply or "").strip()
    sentences = count_sentences(text)
    characters = len(text)
    bullets = _bullet_count(text)
    forbidden_hits = tuple(
        phrase for phrase in dict.fromkeys(str(item).strip() for item in forbidden_phrases)
        if phrase and phrase in text
    )
    internal_hits = tuple(item for item in _INTERNAL_PROCESS_PATTERNS if item in text)
    repeated = _similar_to_previous(text, previous_replies)
    severe = bool(
        sentences > policy.severe_sentence_limit
        or characters > policy.severe_character_limit
    )
    overflow = bool(
        sentences > policy.max_sentences
        or characters > policy.max_reply_characters
    )
    issues: list[str] = []
    if not text:
        issues.append("empty_reply")
    if severe:
        issues.append("severe_overflow")
    elif overflow:
        issues.append("policy_overflow")
    if bullets and not policy.allow_bullets:
        issues.append("unexpected_bullets")
    if forbidden_hits:
        issues.append("assistant_style_phrase")
    if internal_hits:
        issues.append("internal_process_leak")
    if repeated:
        issues.append("near_duplicate_previous_reply")
    # A single mildly assistant-like phrase is logged, not rewritten. Rewrite
    # is reserved for visible architecture leakage, severe runaway output, or
    # a cluster that clearly breaks the natural-dialogue contract.
    needs_rewrite = bool(
        not text
        or overflow
        or internal_hits
        or repeated
        or len(forbidden_hits) >= 2
        or (policy.reply_mode in {"micro", "short"} and bullets >= 3)
    )
    return DialogueValidation(
        sentence_count=sentences,
        character_count=characters,
        bullet_count=bullets,
        forbidden_phrase_hits=forbidden_hits,
        internal_process_hits=internal_hits,
        repeats_previous_reply=repeated,
        policy_overflow=overflow,
        severe_overflow=severe,
        issues=tuple(issues),
        needs_rewrite=needs_rewrite,
    )
