"""Epistemic requirements, research receipts, and dialogue provenance.

This module owns the distinction between:

* a claim that merely appeared in dialogue;
* a claim that the user is authoritative about;
* a public-world claim that still needs verification; and
* a claim supported by a successful tool/research receipt.

It intentionally does not execute tools or render persona prose.  Those
operations consume the immutable records defined here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import re
from typing import Any, Literal, Mapping, Sequence

from .dialogue_architecture import (
    AnswerObligation,
    UtteranceInterpretation,
    interpret_utterance,
    plan_answer_obligations,
)
from .fingerprints import runtime_fingerprint


FreshnessClass = Literal[
    "static",
    "slow_changing",
    "volatile",
    "real_time",
    "unknown",
]
ProvenanceType = Literal[
    "user_assertion",
    "user_personal_authority",
    "assistant_unverified_claim",
    "assistant_inference",
    "tool_observation",
    "verified_web_source",
    "system_state",
    "relationship_state",
]
VerificationStatus = Literal[
    "unverified",
    "supported",
    "contradicted",
    "expired",
    "unknown",
]


_EXPLICIT_CURRENT = re.compile(
    r"现在|当前|目前|最新|今天|今日|截至|本周|本月|今年|"
    r"still|currently|latest|today|as of",
    re.I,
)
_REAL_TIME_PROPERTY = re.compile(
    r"价格|多少钱|股价|汇率|库存|有货|营业|开门|关门|天气|气温|"
    r"比分|赛况|赛程|航班|列车|交通|故障|宕机|预约|余量",
    re.I,
)
_VOLATILE_OFFICE = re.compile(
    r"CEO|首席执行官|总裁|董事长|负责人|总统|首相|总理|部长|现任|在任",
    re.I,
)
_VOLATILE_SOFTWARE = re.compile(
    r"(?:软件|应用|APP|项目|框架|库|模型|API|SDK|接口|版本|系统).{0,24}"
    r"(?:支持|兼容|可用|维护|更新|发布|停更|弃用|价格|版本)|"
    r"(?:支持|兼容|可用|维护|更新|发布|停更|弃用).{0,24}"
    r"(?:软件|应用|APP|项目|框架|库|模型|API|SDK|接口|版本|系统)",
    re.I,
)
_VOLATILE_PRODUCT = re.compile(
    r"(?:游戏|作品|产品|主机|DLC|扩展包|升级包|更新包|补丁|版本).{0,28}"
    r"(?:出了|发布|发售|上线|新增|加入|包含|支持|更新|可用)|"
    r"(?:出了|发布|发售|上线|新增|加入|包含|支持|更新|可用).{0,28}"
    r"(?:游戏|作品|产品|主机|DLC|扩展包|升级包|更新包|补丁|版本)",
    re.I,
)
_VOLATILE_POLICY = re.compile(
    r"政策|法律|法规|规定|签证|移民|税务|利率|课程表|活动日期|营业时间",
    re.I,
)
_STABLE_DEFINITION = re.compile(
    r"^(?:请问)?(?:HTTP|HTTPS|TCP|UDP|DNS|JSON|HTML|CSS|SQL|CPU|GPU)"
    r"(?:到底)?(?:是|指|是什么意思|是什么)",
    re.I,
)
_FACT_QUESTION = re.compile(
    r"谁|哪个|哪一个|多少|什么时候|是否|有没有|有吗|是真的吗|"
    r"是什么|指什么|怎么回事|还支持吗|能用吗|允许吗|合法吗|"
    r"who|what|when|where|which|how much|does .* support",
    re.I,
)
_EPISTEMIC_CHALLENGE = re.compile(
    r"你确定|确定吗|真的吗|不是说|你不是说|不还没|怎么没听过|"
    r"我怎么记得|我怎么记不得|有这个东西吗|哪里来的|"
    r"是不是你编|是不是幻觉|核实|查证|验证",
    re.I,
)
_PUBLIC_WORLD_MARKER = re.compile(
    r"公司|机构|政府|学校|大学|网站|网页|软件|项目|产品|游戏|作品|"
    r"论文|研究|模型|API|SDK|商店|餐厅|医院|政策|法律|法规|"
    r"DLC|扩展包|升级包|更新包|版本|CEO|首相|总统",
    re.I,
)
_PERSONAL_AUTHORITY = re.compile(
    r"(?:^|[，,。；;！？!?])我(?:今天|现在|最近|刚才|刚刚|一直|已经|还|也|就)?"
    r"(?:很|最|挺|特别|非常|有点|比较)?"
    r"(?:喜欢|不喜欢|讨厌|想|打算|准备|计划|感觉|觉得|认为|希望|需要|"
    r"累|困|饿|难受|开心|生气|害怕|买了|没买|玩过|没玩过|正在|住在|叫)",
    re.I,
)
# Research actions are speech acts with different receipt requirements.  Keep
# the verb forms broad enough to cover omitted subjects and passive/completed
# constructions; the matcher below filters negation, attribution, quotations,
# and future intent before a phrase is treated as the assistant's own action.
_RESEARCH_SEARCH_CLAIM = re.compile(
    r"(?:查|搜|搜索|检索|查询)(?:了|过|到(?:了|的)?|完(?:了|毕)?|"
    r"已?(?:完成|完毕))",
    re.I,
)
_RESEARCH_PAGE_READ_CLAIM = re.compile(
    r"(?:看|读|打开|访问|浏览|阅读|翻阅|查阅)(?:了|过|完(?:了)?).{0,24}"
    r"(?:官网|网页|页面|原文|文档|资料|正文)|"
    r"(?:已|已经)(?:看|读|打开|访问|浏览|阅读|翻阅|查阅)(?:完|过)?.{0,24}"
    r"(?:官网|网页|页面|原文|文档|资料|正文)|"
    r"(?:官网|网页|页面|原文|文档|资料|正文)(?:显示|写着|提到|记载|载明|表明)|"
    r"(?:官网|网页|页面|原文|文档|资料|正文).{0,4}(?:已读|读过|看过|阅读过)",
    re.I,
)
_RESEARCH_VERIFICATION_CLAIM = re.compile(
    r"(?:核实|核对|查证|验证|核验)(?:了|过|完(?:了)?|完成|完毕|无误|属实|的)|"
    r"(?:核过|核实过|核对过|查证过|验证过|核验过)(?:了|的)?|"
    r"(?:确认|证实)(?:了|过|完(?:了)?|完成|完毕|无误|属实|的)|"
    r"(?:经|已|已经|确已)(?:核实|核对|查证|验证|核验|确认|证实)",
    re.I,
)
_RESEARCH_NEGATION = re.compile(
    r"(?:没|没有|未|尚未|还没|还没有|不曾|未曾|无法|不能|不可|不再|"
    r"无需|不用|不要|别|仍未|仍然没有)\s*$",
    re.I,
)
_RESEARCH_FUTURE_INTENT = re.compile(
    r"(?:(?:我|你|他|她)\s*)?(?:等|待|如果|假如|要是|倘若|将来|未来|以后|"
    r"稍后|待会儿|一会儿|准备|打算|想(?:要)?|会|要|需要|得)"
    r"\s*(?:我|你|他|她|再)?\s*$",
    re.I,
)
_RESEARCH_ATTRIBUTION_PREFIX = re.compile(
    r"(?:^|[。！？!?；;，,：:])\s*(?:据(?:报道|来源)|"
    r"据(?:他|她|其|他们|她们|某人)?(?:刚才|刚刚|已经|曾经)?"
    r"(?:说|表示|声称|宣称|提到|称|写道)|"
    r"(?:他|她|他们|她们|有人|用户|对方|某人|报道|来源)"
    r"(?:刚才|刚刚|已经|曾经)?(?:说|表示|声称|宣称|提到|称|写道))"
    r"(?:自己|这次|已经|刚才|刚刚)?\s*[,，：:]?$",
    re.I,
)
_RESEARCH_THIRD_PARTY_QUOTE_PREFIX = re.compile(
    r"(?:据(?:报道|来源)|"
    r"据(?:他|她|其|他们|她们|某人)?(?:刚才|刚刚|已经|曾经)?"
    r"(?:说|表示|声称|宣称|提到|称|写道|引用)|"
    r"(?:他|她|他们|她们|有人|用户|对方|某人|报道|来源)"
    r"(?:刚才|刚刚|已经|曾经)?(?:说|表示|声称|宣称|提到|称|写道|引用))"
    r"(?:自己|这次|已经|刚才|刚刚)?\s*[:：,，]?\s*$",
    re.I,
)
_RESEARCH_VERIFICATION_CONTEXT = re.compile(
    r"官网|网页|页面|原文|文档|资料|正文|来源|证据|结论|事实|消息|设定|"
    r"说法|内容|信息|出处|身份|版本|发布|支持|兼容|结果",
    re.I,
)
_RESEARCH_SELF_VERIFICATION_PREFIX = re.compile(
    r"(?:^|[。！？!?；;，,：:])\s*我(?:刚才|刚刚|已经|先)?\s*$",
    re.I,
)


def _mask_research_quoted_text(value: str) -> str:
    """Mask only quoted speech explicitly attributed to another speaker."""

    closing_for = {
        "“": "”",
        "‘": "’",
        "「": "」",
        "『": "』",
        '"': '"',
    }
    chars = list(value)
    for opening_index, opening in enumerate(chars):
        closing = closing_for.get(opening)
        if closing is None or chars[opening_index] == " ":
            continue
        prefix = value[:opening_index]
        if not _RESEARCH_THIRD_PARTY_QUOTE_PREFIX.search(prefix):
            continue
        closing_index = value.find(closing, opening_index + 1)
        end = len(chars) if closing_index < 0 else closing_index + 1
        for index in range(opening_index, end):
            chars[index] = " "
    return "".join(chars)


def _research_action_signals(text: str) -> tuple[bool, bool, bool]:
    """Return ``(search, page_read, verification)`` for owned actions.

    A research action is a completed assertion about work already performed.
    The receipt contract distinguishes a search result from reading a page and
    from claiming verification, so callers need the individual signals rather
    than one broad keyword match.  Quoted and explicitly attributed speech is
    masked before matching; negated and future actions are ignored.
    """

    value = str(text or "")
    if not value:
        return (False, False, False)
    masked = _mask_research_quoted_text(value)
    signals = [False, False, False]
    patterns = (
        _RESEARCH_SEARCH_CLAIM,
        _RESEARCH_PAGE_READ_CLAIM,
        _RESEARCH_VERIFICATION_CLAIM,
    )

    def clause_prefix(position: int) -> str:
        start = max(
            masked.rfind(marker, 0, position)
            for marker in ("。", "！", "？", "!", "?", "；", ";")
        )
        return masked[start + 1:position]

    def clause_context(position: int) -> str:
        start = max(
            masked.rfind(marker, 0, position)
            for marker in ("。", "！", "？", "!", "?", "；", ";")
        )
        endings = [masked.find(marker, position) for marker in ("。", "！", "？", "!", "?", "；", ";")]
        end = min((item for item in endings if item >= 0), default=len(masked))
        return masked[start + 1:end]

    def is_owned_completed_action(match: re.Match[str]) -> bool:
        prefix = masked[max(0, match.start() - 20):match.start()]
        if _RESEARCH_NEGATION.search(prefix):
            return False
        clause = clause_prefix(match.start())
        if _RESEARCH_FUTURE_INTENT.search(clause):
            return False
        if _RESEARCH_ATTRIBUTION_PREFIX.search(clause):
            return False
        return True

    for signal_index, pattern in enumerate(patterns):
        for match in pattern.finditer(masked):
            if not is_owned_completed_action(match):
                continue
            if (
                signal_index == 2
                and re.search(r"验证|确认|证实", match.group(0), re.I)
                and not re.search(r"无误|属实", match.group(0), re.I)
                and not _RESEARCH_VERIFICATION_CONTEXT.search(
                    clause_context(match.start())
                )
                and not _RESEARCH_SELF_VERIFICATION_PREFIX.search(
                    clause_prefix(match.start())
                )
            ):
                continue
            signals[signal_index] = True
            break

    # A verification claim has the same minimum receipt as a page-read claim:
    # it cannot be supported by a query/source listing without readable body
    # evidence.  Keep both flags explicit for the two independent diagnostics.
    if signals[2]:
        signals[1] = True
    return tuple(signals)  # type: ignore[return-value]
_PROACTIVE_EXTERNAL_ASSERTION = re.compile(
    r"(?:新增|加入|发布|发售|上线|更新|停更|弃用|支持|兼容|包含|售价|"
    r"官方(?:公布|宣布|表示)|研究(?:显示|发现)|报道(?:称|显示)|"
    r"版本(?:是|为|号)|现任|目前由|已经|刚刚).{0,60}"
    r"(?:游戏|作品|产品|软件|项目|网站|公司|机构|角色|功能|模式|"
    r"武器|人物|版本|DLC|扩展包|升级包|更新包)|"
    r"(?:游戏|作品|产品|软件|项目|网站|公司|机构|DLC|扩展包|升级包|"
    r"更新包).{0,60}"
    r"(?:新增|加入|发布|发售|上线|更新|停更|弃用|支持|兼容|包含|售价)",
    re.I,
)
_PROACTIVE_PRESUPPOSED_NOVELTY = re.compile(
    r"(?:新|最新|新增).{1,36}(?:(?:里|中)(?:的)?(?:那|有)|的那|有个|有一|推出的).{1,36}"
    r"(?:你|打算|觉得|怎么|要不要|有没有|想不想)",
    re.I,
)
_EVIDENCE_CITATION = re.compile(r"\[(?:S|来源)\s*\d+\]", re.I)
_UNCERTAINTY_LANGUAGE = re.compile(
    r"不确定|不能确定|无法(?:可靠)?确认|还不能确认|没有足够证据|现有来源不足|"
    r"暂时没查到|没有查到|(?:没有|尚未|未能).{0,18}(?:确认|核实|证实)|"
    r"只能说|根据你(?:刚才|之前)?说的|"
    r"可能|也许|大概|似乎|看起来",
    re.I,
)
_PUBLIC_FACT_PREDICATE = re.compile(
    r"倒闭|停更|发布|发售|上线|更新|支持|兼容|包含|属于|是(?:一家|一个|某种)|"
    r"现任|担任|售价|价格|宣布|证实",
    re.I,
)
_EXTERNAL_ATTRIBUTION_ASSERTION = re.compile(
    r"(?:这句话|该引语|这段话|原文|出处).{0,28}(?:出自|来自|归于|作者|原文|作品)|"
    r"(?:(?![我你他她它这那])[\u3400-\u9fff·]){2,20}"
    r"(?:说(?:过|的)?|写(?:过|道)?|认为|主张|提出)",
    re.I,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return f"{prefix}_{runtime_fingerprint(payload)}"


@dataclass(frozen=True)
class FactRequirement:
    requirement_id: str
    subject: str
    predicate: str
    object_type: str | None
    freshness_class: FreshnessClass
    max_evidence_age_seconds: int | None
    verification_required: bool
    preferred_source_types: tuple[str, ...]
    reason: str
    obligation_id: str = ""
    speech_role: str = "assistant_answer"
    assistant_must_assert: bool = True
    domain: str = "external_world"
    evidence_class: str = "web_evidence"
    verification_level: str = "HARD"
    answer_dependency: float = 1.0
    epistemic_risk: float = 1.0
    claim_scope: str = ""
    safe_reasoning_available: bool = False

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["preferred_source_types"] = list(self.preferred_source_types)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FactRequirement":
        freshness = str(value.get("freshness_class") or value.get("freshnessClass") or "unknown")
        if freshness not in {"static", "slow_changing", "volatile", "real_time", "unknown"}:
            freshness = "unknown"
        verification_level = str(
            value.get("verification_level")
            or value.get("verificationLevel")
            or "HARD"
        ).upper()
        if verification_level not in {"NONE", "SOFT", "HARD"}:
            verification_level = "HARD"
        return cls(
            requirement_id=str(
                value.get("requirement_id") or value.get("requirementId") or ""
            ),
            subject=str(value.get("subject") or ""),
            predicate=str(value.get("predicate") or "external_fact"),
            object_type=(
                str(value.get("object_type") or value.get("objectType") or "").strip()
                or None
            ),
            freshness_class=freshness,  # type: ignore[arg-type]
            max_evidence_age_seconds=(
                int(value.get("max_evidence_age_seconds") or value.get("maxEvidenceAgeSeconds"))
                if value.get("max_evidence_age_seconds") is not None
                or value.get("maxEvidenceAgeSeconds") is not None
                else None
            ),
            verification_required=bool(
                value.get("verification_required", value.get("verificationRequired"))
            ),
            preferred_source_types=tuple(
                str(item)
                for item in value.get(
                    "preferred_source_types",
                    value.get("preferredSourceTypes", ()),
                )
                if str(item)
            ),
            reason=str(value.get("reason") or ""),
            obligation_id=str(
                value.get("obligation_id") or value.get("obligationId") or ""
            ),
            speech_role=str(
                value.get("speech_role") or value.get("speechRole") or "assistant_answer"
            ),
            assistant_must_assert=bool(
                value.get("assistant_must_assert", value.get("assistantMustAssert", True))
            ),
            domain=str(value.get("domain") or "external_world"),
            evidence_class=str(
                value.get("evidence_class") or value.get("evidenceClass") or "web_evidence"
            ),
            verification_level=verification_level,
            answer_dependency=max(0.0, min(1.0, float(
                value.get("answer_dependency", value.get("answerDependency", 1.0))
                or 0.0
            ))),
            epistemic_risk=max(0.0, min(1.0, float(
                value.get("epistemic_risk", value.get("epistemicRisk", 1.0))
                or 0.0
            ))),
            claim_scope=str(
                value.get("claim_scope") or value.get("claimScope") or ""
            ).strip()[:500],
            safe_reasoning_available=bool(
                value.get(
                    "safe_reasoning_available",
                    value.get("safeReasoningAvailable", False),
                )
            ),
        )


@dataclass(frozen=True)
class ResearchReceipt:
    research_id: str
    turn_id: str
    status: str
    executed_queries: tuple[str, ...]
    source_uids: tuple[str, ...]
    pages_read: tuple[str, ...]
    started_at: str
    completed_at: str | None
    evidence_sufficient: bool
    # User-facing S-number citations are only evidence when that exact source
    # had readable body text in a successful tool result.  Keep this separate
    # from ``source_uids``: a search result can name a source without reading it.
    read_source_ids: tuple[str, ...] = ()
    read_source_uids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        for key in (
            "executed_queries", "source_uids", "pages_read", "read_source_ids",
            "read_source_uids",
        ):
            value[key] = list(value[key])
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "ResearchReceipt | None":
        data = value or {}
        research_id = str(data.get("research_id") or data.get("researchId") or "")
        if not research_id:
            return None
        return cls(
            research_id=research_id,
            turn_id=str(data.get("turn_id") or data.get("turnId") or ""),
            status=str(data.get("status") or "unknown"),
            executed_queries=tuple(
                str(item)
                for item in data.get(
                    "executed_queries",
                    data.get("executedQueries", ()),
                )
                if str(item)
            ),
            source_uids=tuple(
                str(item)
                for item in data.get("source_uids", data.get("sourceUids", ()))
                if str(item)
            ),
            pages_read=tuple(
                str(item)
                for item in data.get("pages_read", data.get("pagesRead", ()))
                if str(item)
            ),
            started_at=str(data.get("started_at") or data.get("startedAt") or ""),
            completed_at=(
                str(data.get("completed_at") or data.get("completedAt") or "").strip()
                or None
            ),
            evidence_sufficient=bool(
                data.get("evidence_sufficient", data.get("evidenceSufficient"))
            ),
            read_source_ids=tuple(
                str(item)
                for item in data.get(
                    "read_source_ids",
                    data.get("readSourceIds", ()),
                )
                if str(item)
            ),
            read_source_uids=tuple(
                str(item)
                for item in data.get(
                    "read_source_uids",
                    data.get("readSourceUids", ()),
                )
                if str(item)
            ),
        )


@dataclass(frozen=True)
class EvidenceFreshness:
    retrieved_at: str
    source_published_at: str | None
    valid_as_of: str | None
    expires_at: str | None
    freshness_class: FreshnessClass

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    proposition: str
    subject: str | None
    predicate: str | None
    object_value: str | None
    provenance_type: ProvenanceType
    origin_ref: str
    source_uids: tuple[str, ...]
    verification_status: VerificationStatus
    confidence: float
    asserted_at: str
    valid_from: str | None
    valid_until: str | None
    supersedes_claim_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["source_uids"] = list(self.source_uids)
        value["supersedes_claim_ids"] = list(self.supersedes_claim_ids)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ClaimRecord":
        provenance = str(
            value.get("provenance_type") or value.get("provenanceType") or "assistant_unverified_claim"
        )
        if provenance not in {
            "user_assertion", "user_personal_authority",
            "assistant_unverified_claim", "assistant_inference",
            "tool_observation", "verified_web_source", "system_state",
            "relationship_state",
        }:
            provenance = "assistant_unverified_claim"
        status = str(
            value.get("verification_status") or value.get("verificationStatus") or "unknown"
        )
        if status not in {"unverified", "supported", "contradicted", "expired", "unknown"}:
            status = "unknown"
        return cls(
            claim_id=str(value.get("claim_id") or value.get("claimId") or ""),
            proposition=str(value.get("proposition") or ""),
            subject=str(value.get("subject") or "").strip() or None,
            predicate=str(value.get("predicate") or "").strip() or None,
            object_value=str(value.get("object_value") or value.get("objectValue") or "").strip() or None,
            provenance_type=provenance,  # type: ignore[arg-type]
            origin_ref=str(value.get("origin_ref") or value.get("originRef") or ""),
            source_uids=tuple(
                str(item)
                for item in value.get("source_uids", value.get("sourceUids", ()))
                if str(item)
            ),
            verification_status=status,  # type: ignore[arg-type]
            confidence=max(0.0, min(1.0, float(value.get("confidence") or 0.0))),
            asserted_at=str(value.get("asserted_at") or value.get("assertedAt") or ""),
            valid_from=str(value.get("valid_from") or value.get("validFrom") or "").strip() or None,
            valid_until=str(value.get("valid_until") or value.get("validUntil") or "").strip() or None,
            supersedes_claim_ids=tuple(
                str(item)
                for item in value.get(
                    "supersedes_claim_ids",
                    value.get("supersedesClaimIds", ()),
                )
                if str(item)
            ),
        )


@dataclass(frozen=True)
class EpistemicTurnState:
    fact_requirements: tuple[FactRequirement, ...] = ()
    verified_claim_ids: tuple[str, ...] = ()
    unresolved_claim_ids: tuple[str, ...] = ()
    research_receipt_ids: tuple[str, ...] = ()
    allowed_final_claim_types: tuple[str, ...] = (
        "persona_opinion",
        "user_attributed_claim",
        "stable_fact",
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "factRequirements": [
                item.to_dict() for item in self.fact_requirements
            ],
            "verifiedClaimIds": list(self.verified_claim_ids),
            "unresolvedClaimIds": list(self.unresolved_claim_ids),
            "researchReceiptIds": list(self.research_receipt_ids),
            "allowedFinalClaimTypes": list(self.allowed_final_claim_types),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "EpistemicTurnState":
        data = value or {}
        raw_requirements = data.get("factRequirements", data.get("fact_requirements", ()))
        return cls(
            fact_requirements=tuple(
                FactRequirement.from_dict(item)
                for item in raw_requirements
                if isinstance(item, Mapping)
            ),
            verified_claim_ids=tuple(
                str(item)
                for item in data.get("verifiedClaimIds", data.get("verified_claim_ids", ()))
                if str(item)
            ),
            unresolved_claim_ids=tuple(
                str(item)
                for item in data.get("unresolvedClaimIds", data.get("unresolved_claim_ids", ()))
                if str(item)
            ),
            research_receipt_ids=tuple(
                str(item)
                for item in data.get(
                    "researchReceiptIds",
                    data.get("research_receipt_ids", ()),
                )
                if str(item)
            ),
            allowed_final_claim_types=tuple(
                str(item)
                for item in data.get(
                    "allowedFinalClaimTypes",
                    data.get("allowed_final_claim_types", ()),
                )
                if str(item)
            ) or cls().allowed_final_claim_types,
        )


@dataclass(frozen=True)
class CompiledContext:
    recent_dialogue: tuple[str, ...]
    trusted_facts: tuple[ClaimRecord, ...]
    user_assertions: tuple[ClaimRecord, ...]
    unresolved_hypotheses: tuple[ClaimRecord, ...]
    active_tool_results: tuple[str, ...] = ()
    relationship_context: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroundedResponsePlan:
    verified_claims: tuple[ClaimRecord, ...]
    user_assertions: tuple[ClaimRecord, ...]
    permitted_inferences: tuple[ClaimRecord, ...]
    unresolved_points: tuple[str, ...]
    action_facts: tuple[str, ...] = ()
    research_facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssistantClaim:
    claim_id: str
    text: str
    claim_type: str
    assertion_mode: str
    domain: str
    attributed_to: str | None
    adopted_as_truth: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


GateStatus = Literal[
    "PASS",
    "RESEARCH_ESCALATION",
    "REWRITE_WITH_UNCERTAINTY",
    "BLOCK",
]


@dataclass(frozen=True)
class GateDecision:
    status: GateStatus
    unsupported_claim_ids: tuple[str, ...] = ()
    required_evidence_classes: tuple[str, ...] = ()
    missing_verification_targets: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    claims: tuple[AssistantClaim, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "unsupportedClaimIds": list(self.unsupported_claim_ids),
            "requiredEvidenceClasses": list(self.required_evidence_classes),
            "missingVerificationTargets": list(self.missing_verification_targets),
            "reasons": list(self.reasons),
            "claims": [item.to_dict() for item in self.claims],
        }


def knowledge_confidence_gate_decision(
    requirements: Sequence[FactRequirement],
    model_confidence: Any,
    *,
    confidence_provided: bool,
    research_policy: str,
    web_permission: bool,
    receipt: ResearchReceipt | None = None,
) -> GateDecision:
    """Turn model uncertainty into an evidence action without lexical triggers.

    The fact requirements already describe whether the answer must assert a
    public fact.  Confidence only adjusts the action for that existing semantic
    obligation; it never turns creative, social, local, or conversation-grounded
    turns into Web searches merely because a field is missing.
    """

    external_requirements = tuple(
        item
        for item in requirements
        if item.verification_required
        and item.assistant_must_assert
        and item.domain == "external_world"
        and item.evidence_class == "web_evidence"
        and item.verification_level == "HARD"
    )
    if not external_requirements:
        return GateDecision(status="PASS")

    has_sufficient_receipt = bool(
        receipt
        and receipt.status == "completed"
        and receipt.source_uids
        and receipt.evidence_sufficient
    )
    if has_sufficient_receipt:
        return GateDecision(status="PASS")

    raw_confidence = str(model_confidence or "").strip().lower()
    valid_confidence = raw_confidence in {"high", "medium", "low"}
    effective_confidence = (
        raw_confidence
        if confidence_provided and valid_confidence
        else "medium"
    )
    policy = str(research_policy or "allowed").strip().lower()
    must_obtain_evidence = policy == "required"
    confidence_requires_evidence = effective_confidence in {"medium", "low"}
    if not must_obtain_evidence and not confidence_requires_evidence:
        return GateDecision(status="PASS")

    if not confidence_provided or not valid_confidence:
        reason = "model_knowledge_confidence_missing"
    elif must_obtain_evidence:
        reason = "research_policy_requires_external_evidence"
    else:
        reason = f"model_{effective_confidence}_knowledge_confidence"
    status: GateStatus = (
        "RESEARCH_ESCALATION"
        if web_permission and policy != "forbidden"
        else "REWRITE_WITH_UNCERTAINTY"
    )
    return GateDecision(
        status=status,
        required_evidence_classes=("web_evidence",),
        missing_verification_targets=tuple(dict.fromkeys(
            item.subject for item in external_requirements if item.subject
        )),
        reasons=(reason,),
    )


_CONDITIONAL_ASSERTION = re.compile(
    r"如果|假如|要是|倘若|等(?:到|你|我|以后)|以后.{0,18}(?:时|再)|"
    r"将来|未来|真(?:的)?能|到时候|would|if |when ",
    re.I,
)
_USER_ATTRIBUTION = re.compile(
    r"(?:按|照|根据)(?:你|用户)(?:刚才|之前)?(?:说|提供|描述)的|"
    r"你(?:刚才|之前)?(?:说|提到|告诉我)|用户(?:说|表示|计划)",
    re.I,
)
_LOCAL_SYSTEM_ASSERTION = re.compile(
    r"(?:我|AMADEUS|Amadeus|助手).{0,16}(?:有|没有|能|不能|可以|无法|支持|"
    r"不支持|会|不会).{0,30}(?:工具|权限|浏览器|摄像头|屏幕|截图|文件|"
    r"联网|搜索|控制|执行|运行|打游戏)|"
    r"(?:本地|当前系统|工具注册表|权限设置).{0,36}(?:支持|可用|不可用|允许|禁止)",
    re.I,
)


def extract_assistant_claims(
    text: str,
    epistemic_state: EpistemicTurnState | None = None,
) -> tuple[AssistantClaim, ...]:
    """Extract sentence-level claims for evidence matching.

    This deliberately classifies assertion mode before topic.  A sentence about
    a game in a promise or hypothetical remains a conditional/social claim and
    cannot be promoted into a public-world fact merely by containing a noun.
    """

    value = str(text or "").strip()
    if not value:
        return ()
    segments = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])|\n+", value)
        if item.strip()
    ]
    hard_external_requirements = tuple(
        item
        for item in (epistemic_state.fact_requirements if epistemic_state else ())
        if item.domain == "external_world"
        and item.evidence_class == "web_evidence"
        and item.verification_level == "HARD"
    )
    soft_external_requirements = tuple(
        item
        for item in (epistemic_state.fact_requirements if epistemic_state else ())
        if item.domain == "external_world"
        and item.evidence_class == "web_evidence"
        and item.verification_level == "SOFT"
    )
    visual_requirements = tuple(
        item
        for item in (epistemic_state.fact_requirements if epistemic_state else ())
        if item.domain == "local_observation" and item.evidence_class == "fresh_observation"
    )
    claims: list[AssistantClaim] = []
    for index, segment in enumerate(segments):
        attributed_to: str | None = None
        adopted = True
        if _CONDITIONAL_ASSERTION.search(segment):
            claim_type = "conditional"
            assertion_mode = "conditional"
            domain = "social"
            adopted = False
        elif _USER_ATTRIBUTION.search(segment):
            claim_type = "user_attributed_claim"
            assertion_mode = "attributed"
            domain = "user_state"
            attributed_to = "user"
            adopted = False
        elif segment.endswith(("？", "?")):
            claim_type = "social_response"
            assertion_mode = "question"
            domain = "social"
            adopted = False
        elif _LOCAL_SYSTEM_ASSERTION.search(segment):
            claim_type = "local_capability"
            assertion_mode = "assertive"
            domain = "local_system"
        elif any(_research_action_signals(segment)):
            claim_type = "research_action"
            assertion_mode = "assertive"
            domain = "external_world"
        elif visual_requirements and not segment.endswith(("？", "?")):
            claim_type = "current_visual_observation"
            assertion_mode = "assertive"
            domain = "local_observation"
        elif soft_external_requirements and _EXTERNAL_ATTRIBUTION_ASSERTION.search(segment):
            claim_type = "external_fact"
            assertion_mode = "assertive"
            domain = "external_world"
        elif hard_external_requirements:
            # The turn-level obligation has already established that the
            # assistant is answering a HARD external factual question. Do not
            # downgrade an assertive answer to ``stable_knowledge`` merely
            # because its wording missed a small predicate/volatility regex
            # list. That used to let confident but fabricated entity facts
            # bypass the independent research-need audit entirely.
            #
            # Questions, conditionals and user-attributed statements were
            # removed by the branches above, so the remaining adopted claim is
            # precisely the assertion that must retain the external evidence
            # obligation. SOFT requirements deliberately do not enter this
            # branch: their limited factual scope may not expand over the
            # independent reasoning and opinion lane.
            claim_type = "external_fact"
            assertion_mode = "assertive"
            domain = "external_world"
        else:
            claim_type = "persona_or_stable_response"
            assertion_mode = "assertive"
            domain = "stable_knowledge"
        claims.append(AssistantClaim(
            claim_id=_stable_id("aclaim", str(index), segment),
            text=segment,
            claim_type=claim_type,
            assertion_mode=assertion_mode,
            domain=domain,
            attributed_to=attributed_to,
            adopted_as_truth=adopted,
        ))
    return tuple(claims)


def claim_scope_relevance(scope: str, statement: str) -> float:
    """Estimate whether one answer sentence realizes a planned fact scope.

    The model has already selected the claim scope. This function only binds
    that structured scope back to an emitted sentence, so SOFT debt cannot leak
    into the independent reasoning lane.
    """

    def normalized(value: str) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)

    left = normalized(scope)
    right = normalized(statement)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 8 and shorter in longer:
        return 0.96
    matcher = SequenceMatcher(None, left, right)
    ratio = matcher.ratio()
    longest = matcher.find_longest_match().size
    if longest >= 4 and longest / max(1, len(shorter)) >= 0.18:
        return max(ratio, 0.56)
    return ratio


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def plan_fact_requirements(
    text: str,
    *,
    interpretation: UtteranceInterpretation | None = None,
    obligations: Sequence[AnswerObligation] | None = None,
) -> tuple[FactRequirement, ...]:
    """Derive evidence duties from answer obligations, with words only as hints.

    Temporal/product markers still help identify which *property* may be
    volatile.  They cannot create an assistant verification duty unless the
    speech-act layer says the assistant must assert that fact.
    """

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or _STABLE_DEFINITION.search(value):
        return ()

    active_interpretation = interpretation or interpret_utterance(value)
    active_obligations = tuple(
        obligations
        if obligations is not None
        else plan_answer_obligations(active_interpretation)
    )
    assertive_obligations = tuple(
        item
        for item in active_obligations
        if item.requires_fact_assertion
        and item.evidence_class in {
            "web_evidence", "local_state", "task_state", "conversation_history",
            "fresh_observation",
        }
    )
    if not assertive_obligations:
        return ()

    candidates: list[tuple[str, FreshnessClass, int | None, str]] = []
    if _REAL_TIME_PROPERTY.search(value):
        candidates.append(("real_time_status", "real_time", 15 * 60, "real_time_property"))
    if _VOLATILE_OFFICE.search(value):
        candidates.append(("current_office_holder", "volatile", 24 * 60 * 60, "office_holder_property"))
    if _VOLATILE_SOFTWARE.search(value):
        candidates.append(("current_feature_support", "volatile", 7 * 24 * 60 * 60, "software_support_property"))
    if _VOLATILE_PRODUCT.search(value):
        candidates.append(("current_release_or_content", "volatile", 24 * 60 * 60, "product_release_property"))
    if _VOLATILE_POLICY.search(value):
        candidates.append(("current_policy_or_schedule", "volatile", 24 * 60 * 60, "policy_or_schedule_property"))
    if _EXPLICIT_CURRENT.search(value) and _PUBLIC_WORLD_MARKER.search(value):
        candidates.append(("current_external_fact", "volatile", 24 * 60 * 60, "explicit_current_external_fact"))
    if _EPISTEMIC_CHALLENGE.search(value) and (
        _PUBLIC_WORLD_MARKER.search(value)
        or re.search(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", value)
        or re.search(r"\d|《[^》]{2,}》|@[A-Za-z\u4e00-\u9fff]{2,}", value)
    ):
        candidates.append(("challenged_external_claim", "unknown", None, "user_challenges_prior_external_claim"))

    # A factual answer can be stable even when no volatile hint matched.  Such
    # an obligation remains explicit but does not force Web evidence.
    if not candidates:
        candidates.append(("external_fact", "unknown", None, "factual_answer_obligation"))

    requirements: list[FactRequirement] = []
    seen: set[tuple[str, str]] = set()
    subject = value[:180]
    for obligation in assertive_obligations:
        for predicate, freshness, max_age, reason in candidates:
            key = (obligation.obligation_id, predicate)
            if key in seen:
                continue
            seen.add(key)
            requires_web = bool(
                obligation.domain == "external_world"
                and obligation.evidence_class == "web_evidence"
            )
            requirements.append(FactRequirement(
                requirement_id=_stable_id(
                    "fact", subject, predicate, obligation.obligation_id
                ),
                subject=subject,
                predicate=predicate,
                object_type=None,
                freshness_class=freshness,
                max_evidence_age_seconds=max_age,
                verification_required=requires_web,
                preferred_source_types=(
                    "official_documentation",
                    "official_announcement",
                    "primary_source",
                ) if obligation.evidence_class == "web_evidence" else (),
                reason=reason,
                obligation_id=obligation.obligation_id,
                speech_role="assistant_answer",
                assistant_must_assert=True,
                domain=obligation.domain,
                evidence_class=obligation.evidence_class,
                # An unresolved question-shaped utterance is only a hint,
                # not a verified semantic dependency of the whole answer.
                verification_level="SOFT" if reason == "factual_answer_obligation" else "HARD",
                answer_dependency=0.0 if reason == "factual_answer_obligation" else 1.0,
                epistemic_risk=0.0 if reason == "factual_answer_obligation" else 1.0,
                safe_reasoning_available=reason == "factual_answer_obligation",
            ))
    return tuple(requirements)


def build_epistemic_turn_state(
    text: str,
    *,
    interpretation: UtteranceInterpretation | None = None,
    obligations: Sequence[AnswerObligation] | None = None,
) -> EpistemicTurnState:
    requirements = plan_fact_requirements(
        text,
        interpretation=interpretation,
        obligations=obligations,
    )
    allowed = ["persona_opinion", "user_attributed_claim", "stable_fact"]
    if requirements:
        allowed.extend(("verified_external_fact", "evidence_bounded_inference"))
    if any(item.evidence_class == "fresh_observation" for item in requirements):
        allowed.append("verified_local_observation")
    if any(item.evidence_class == "artifact_evidence" for item in requirements):
        allowed.append("verified_local_artifact")
    return EpistemicTurnState(
        fact_requirements=requirements,
        allowed_final_claim_types=tuple(allowed),
    )


def fact_requirements_need_verification(
    requirements: Sequence[FactRequirement],
    *,
    requires_answer: bool,
) -> bool:
    """Public assertions may be attributed; answering their truth needs evidence."""

    if not requires_answer:
        return False
    return any(
        item.verification_required
        and (
            item.freshness_class in {"volatile", "real_time"}
            or item.reason == "user_challenges_prior_external_claim"
        )
        for item in requirements
    )


def research_receipt_from_tool_results(
    turn_id: str,
    tool_results: Sequence[Mapping[str, Any]],
    *,
    started_at: str = "",
    completed_at: str = "",
) -> ResearchReceipt | None:
    web_results = [
        item
        for item in tool_results
        if str(item.get("name") or "") in {"web_search", "read_web_page"}
    ]
    if not web_results:
        return None

    queries: list[str] = []
    source_uids: list[str] = []
    pages_read: list[str] = []
    read_source_ids: list[str] = []
    read_source_uids: list[str] = []
    evidence_sufficient = False
    any_success = False

    def source_has_readable_body(source: Mapping[str, Any]) -> bool:
        """Mirror the Web evidence gate without creating an agent→backend import."""

        fetch_status = str(source.get("fetchStatus") or "").strip().lower()
        if fetch_status in {
            "captcha", "blocked", "login_required", "paywalled",
            "js_required", "non_content", "failed", "fetch_error",
        }:
            return False
        if fetch_status and fetch_status not in {
            "success", "partial", "ok", "fetched",
        }:
            return False
        return len(str(source.get("body") or "").strip()) >= 120

    for item in web_results:
        any_success = any_success or bool(item.get("ok"))
        plan = item.get("researchPlan") if isinstance(item.get("researchPlan"), Mapping) else {}
        for query in _sequence(plan.get("queries")):
            if isinstance(query, Mapping):
                value = str(query.get("query") or "").strip()
            else:
                value = str(query or "").strip()
            if value:
                queries.append(value)
        direct_query = str(item.get("query") or "").strip()
        if direct_query:
            queries.append(direct_query)
        status = item.get("evidenceStatus") if isinstance(item.get("evidenceStatus"), Mapping) else {}
        evidence_sufficient = evidence_sufficient or bool(status.get("sufficient"))
        for source in _sequence(item.get("sources")):
            if not isinstance(source, Mapping):
                continue
            uid = str(
                source.get("sourceUid")
                or source.get("canonicalUrl")
                or source.get("url")
                or ""
            ).strip()
            if uid:
                source_uids.append(uid)
            if bool(item.get("ok")) and source_has_readable_body(source):
                source_id = str(source.get("id") or "").strip().upper()
                if re.fullmatch(r"S\d+", source_id, re.I):
                    read_source_ids.append(source_id)
                if uid:
                    read_source_uids.append(uid)
                url = str(source.get("canonicalUrl") or source.get("url") or uid).strip()
                if url:
                    pages_read.append(url)

    unique_queries = tuple(dict.fromkeys(queries))
    unique_sources = tuple(dict.fromkeys(source_uids))
    unique_pages = tuple(dict.fromkeys(pages_read))
    unique_read_source_ids = tuple(dict.fromkeys(read_source_ids))
    unique_read_source_uids = tuple(dict.fromkeys(read_source_uids))
    status = (
        "completed"
        if any_success and unique_sources and evidence_sufficient
        else "failed"
        if not any_success
        else "insufficient"
    )
    completed = completed_at or _iso_now()
    return ResearchReceipt(
        research_id=_stable_id("research", turn_id, *unique_queries, *unique_sources),
        turn_id=str(turn_id or ""),
        status=status,
        executed_queries=unique_queries,
        source_uids=unique_sources,
        pages_read=unique_pages,
        started_at=started_at or completed,
        completed_at=completed,
        evidence_sufficient=evidence_sufficient,
        read_source_ids=unique_read_source_ids,
        read_source_uids=unique_read_source_uids,
    )


def research_action_claim_violations(
    text: str,
    receipt: ResearchReceipt | None,
) -> tuple[str, ...]:
    value = str(text or "")
    search_claim, page_read_claim, verification_claim = _research_action_signals(value)
    if not any((search_claim, page_read_claim, verification_claim)):
        return ()
    has_executed_research = bool(
        receipt
        and receipt.status in {"completed", "insufficient"}
        and (
            receipt.executed_queries
            or receipt.source_uids
            or receipt.pages_read
        )
    )
    if not has_executed_research:
        return ("research_claim_without_completed_receipt",)
    violations: list[str] = []
    assert receipt is not None
    if page_read_claim and not receipt.pages_read:
        violations.append("page_read_or_verification_claim_without_page_receipt")
    if verification_claim and not receipt.evidence_sufficient:
        violations.append("verification_claim_without_sufficient_evidence")
    return tuple(violations)


def proactive_external_claim_risks(
    text: str,
    *,
    declared_external_claims: Sequence[Any] = (),
) -> tuple[str, ...]:
    """Detect public-world facts smuggled into idle conversation."""

    risks: list[str] = []
    if research_action_claim_violations(text, None):
        risks.append("unsupported_research_action_language")
    if any(str(item).strip() for item in declared_external_claims):
        risks.append("model_declared_external_fact_without_receipt")
    if _PROACTIVE_EXTERNAL_ASSERTION.search(str(text or "")):
        risks.append("proactive_external_assertion_without_receipt")
    if _PROACTIVE_PRESUPPOSED_NOVELTY.search(str(text or "")):
        risks.append("proactive_novel_external_presupposition")
    return tuple(dict.fromkeys(risks))


def user_claim_provenance(text: str) -> ProvenanceType:
    value = str(text or "")
    if (
        _PERSONAL_AUTHORITY.search(value)
        and not (
            _PUBLIC_WORLD_MARKER.search(value)
            and _PUBLIC_FACT_PREDICATE.search(value)
        )
    ):
        return "user_personal_authority"
    return "user_assertion"


def evidence_freshness_for_turn(
    asserted_at: str,
    epistemic_state: Mapping[str, Any] | None,
) -> EvidenceFreshness:
    state = EpistemicTurnState.from_dict(epistemic_state)
    ages = [
        item.max_evidence_age_seconds
        for item in state.fact_requirements
        if item.verification_required and item.max_evidence_age_seconds is not None
    ]
    parsed_at = _parse_datetime(asserted_at)
    expires_at = (
        parsed_at + timedelta(seconds=max(1, min(ages)))
        if parsed_at is not None and ages
        else None
    )
    classes = [item.freshness_class for item in state.fact_requirements]
    freshness: FreshnessClass
    if "real_time" in classes:
        freshness = "real_time"
    elif "volatile" in classes:
        freshness = "volatile"
    elif "slow_changing" in classes:
        freshness = "slow_changing"
    elif "static" in classes:
        freshness = "static"
    else:
        freshness = "unknown"
    return EvidenceFreshness(
        retrieved_at=asserted_at,
        source_published_at=None,
        valid_as_of=asserted_at,
        expires_at=expires_at.isoformat(timespec="seconds") if expires_at else None,
        freshness_class=freshness,
    )


def _assistant_claim_sentences(text: str) -> tuple[str, ...]:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return ()
    parts = [
        match.group(0).strip()
        for match in re.finditer(
            r".+?(?:[。！？!?；;](?:\s*\[(?:S|来源)\s*\d+\])?(?=\s|$)|$)",
            value,
            re.I,
        )
        if match.group(0).strip()
    ]
    return tuple(parts or [value])


def _user_owned_claim_text(turn: Mapping[str, Any]) -> str:
    """Exclude explicitly quoted speech from the user's own claim record.

    The discourse ledger owns quote attribution.  A user forwarding “我已经
    提交” does not thereby assert that *the user* submitted anything.  Older
    turns have no ledger delta and retain the historical behavior.
    """

    raw_user = str(turn.get("user") or "")
    delta = (
        turn.get("referenceLedgerDelta")
        if isinstance(turn.get("referenceLedgerDelta"), Mapping) else {}
    )
    messages = [
        item for item in _sequence(delta.get("messages"))
        if isinstance(item, Mapping)
        and str(item.get("speaker") or "") == "user"
    ]
    user_message_ids = {
        str(item.get("message_id") or item.get("messageId") or "")
        for item in messages
        if str(item.get("message_id") or item.get("messageId") or "")
    }
    spans: list[tuple[int, int]] = []
    for quote in _sequence(delta.get("quotes")):
        if not isinstance(quote, Mapping):
            continue
        message_id = str(
            quote.get("message_id") or quote.get("messageId") or ""
        )
        if message_id not in user_message_ids:
            continue
        span = quote.get("span") if isinstance(quote.get("span"), Mapping) else {}
        try:
            start = max(0, int(span.get("start") or 0))
            end = min(len(raw_user), max(start, int(span.get("end") or start)))
        except (TypeError, ValueError, OverflowError):
            continue
        # Include the surrounding quotation marks when present so an empty
        # forwarded quote does not leave punctuation that looks like a claim.
        if start > 0 and raw_user[start - 1] in {'“', '「', '『', '‘', '"'}:
            start -= 1
        if end < len(raw_user) and raw_user[end] in {'”', '」', '』', '’', '"'}:
            end += 1
        if end > start:
            spans.append((start, end))
    if not spans:
        return re.sub(r"\s+", " ", raw_user).strip()
    kept: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        kept.append(raw_user[cursor:start])
        cursor = end
    kept.append(raw_user[cursor:])
    return re.sub(r"\s+", " ", "".join(kept)).strip(" ，,。；;：:")


def claim_records_from_turn(
    turn: Mapping[str, Any],
    *,
    receipt: ResearchReceipt | None = None,
) -> tuple[ClaimRecord, ...]:
    asserted_at = str(turn.get("at") or _iso_now())
    origin_ref = str(
        turn.get("requestId")
        or turn.get("proactiveEventId")
        or f"turn:{asserted_at}"
    )
    user = _user_owned_claim_text(turn)
    assistant = re.sub(r"\s+", " ", str(turn.get("assistant") or "")).strip()
    records: list[ClaimRecord] = []
    if user:
        provenance = user_claim_provenance(user)
        records.append(ClaimRecord(
            claim_id=_stable_id("claim", origin_ref, "user", user),
            proposition=user[:1600],
            subject=None,
            predicate=None,
            object_value=None,
            provenance_type=provenance,
            origin_ref=origin_ref,
            source_uids=(),
            verification_status=(
                "supported" if provenance == "user_personal_authority" else "unverified"
            ),
            confidence=0.98 if provenance == "user_personal_authority" else 0.6,
            asserted_at=asserted_at,
            valid_from=asserted_at,
            valid_until=None,
        ))
    if assistant:
        source_uids = receipt.source_uids if receipt else ()
        tool_status = str(turn.get("toolStatus") or "")
        tool_used = str(turn.get("toolUsed") or "")
        visual_audit = (
            turn.get("visualGroundingAudit")
            if isinstance(turn.get("visualGroundingAudit"), Mapping)
            else {}
        )
        freshness = evidence_freshness_for_turn(
            asserted_at,
            (
                turn.get("epistemicState")
                if isinstance(turn.get("epistemicState"), Mapping)
                else None
            ),
        )
        if visual_audit.get("passes"):
            for visual_claim in _sequence(visual_audit.get("claims")):
                if not isinstance(visual_claim, Mapping):
                    continue
                kind = str(visual_claim.get("kind") or "").lower()
                status = str(visual_claim.get("status") or "").upper()
                proposition = re.sub(
                    r"\s+", " ", str(visual_claim.get("text") or "")
                ).strip()
                evidence_ids = tuple(dict.fromkeys(
                    str(item)
                    for item in _sequence(visual_claim.get("evidenceIds"))
                    if str(item)
                ))
                if (
                    not proposition
                    or kind not in {
                        "visual_fact", "current_activity", "visible_text",
                        "application_identity",
                    }
                    or status not in {"SUPPORTED", "REASONABLE_INFERENCE"}
                    or not evidence_ids
                ):
                    continue
                directly_supported = status == "SUPPORTED"
                records.append(ClaimRecord(
                    claim_id=_stable_id(
                        "claim", origin_ref, "visual", proposition, *evidence_ids
                    ),
                    proposition=proposition[:1200],
                    subject=None,
                    predicate="current_visual_observation",
                    object_value=None,
                    provenance_type=(
                        "tool_observation" if directly_supported else "assistant_inference"
                    ),
                    origin_ref=origin_ref,
                    source_uids=evidence_ids,
                    verification_status=("supported" if directly_supported else "unverified"),
                    confidence=(
                        max(0.72, min(0.98, float(visual_audit.get("confidence") or 0.0)))
                        if directly_supported else 0.55
                    ),
                    asserted_at=asserted_at,
                    valid_from=asserted_at,
                    valid_until=None,
                ))
        for sentence in _assistant_claim_sentences(assistant):
            cited = bool(_EVIDENCE_CITATION.search(sentence))
            if receipt and receipt.evidence_sufficient and source_uids and cited:
                provenance: ProvenanceType = "verified_web_source"
                verification: VerificationStatus = "supported"
                confidence = 0.9
                valid_until = freshness.expires_at
                sentence_sources = source_uids
            elif tool_status == "ok" and tool_used and not receipt:
                # A successful tool call proves only that the tool ran.  It
                # does not make every sentence in the assistant response a
                # supported observation.  Typed receipts/audits above promote
                # only the concrete claims actually bound to evidence.
                provenance = "assistant_inference"
                verification = "unverified"
                confidence = 0.45
                valid_until = None
                sentence_sources = ()
            elif receipt and receipt.evidence_sufficient and source_uids:
                provenance = "assistant_inference"
                verification = "unverified"
                confidence = 0.55
                valid_until = freshness.expires_at
                sentence_sources = ()
            else:
                provenance = "assistant_unverified_claim"
                verification = "unverified"
                confidence = 0.35
                valid_until = None
                sentence_sources = ()
            records.append(ClaimRecord(
                claim_id=_stable_id("claim", origin_ref, "assistant", sentence),
                proposition=sentence[:1200],
                subject=None,
                predicate=None,
                object_value=None,
                provenance_type=provenance,
                origin_ref=origin_ref,
                source_uids=sentence_sources,
                verification_status=verification,
                confidence=confidence,
                asserted_at=asserted_at,
                valid_from=asserted_at,
                valid_until=valid_until,
            ))
    return tuple(records)


def claim_is_currently_supported(
    claim: ClaimRecord,
    *,
    now: datetime | None = None,
) -> bool:
    if claim.verification_status != "supported":
        return False
    if not claim.valid_until:
        return True
    expiry = _parse_datetime(claim.valid_until)
    if expiry is None:
        return False
    active_now = now or datetime.now(timezone.utc).astimezone()
    if active_now.tzinfo is None:
        active_now = active_now.replace(tzinfo=timezone.utc)
    return active_now <= expiry


def compile_claim_context(
    recent_turns: Sequence[Mapping[str, Any]],
    claim_values: Sequence[Mapping[str, Any]],
    *,
    recent_limit: int = 8,
) -> CompiledContext:
    claims = [
        ClaimRecord.from_dict(item)
        for item in claim_values
        if isinstance(item, Mapping) and str(item.get("proposition") or "").strip()
    ]
    trusted = tuple(
        item for item in claims
        if item.provenance_type in {
            "user_personal_authority",
            "verified_web_source",
            "tool_observation",
            "system_state",
            "relationship_state",
        }
        and claim_is_currently_supported(item)
    )
    user_assertions = tuple(
        item for item in claims
        if item.provenance_type == "user_assertion"
    )
    hypotheses = tuple(
        item for item in claims
        if item.provenance_type in {
            "assistant_unverified_claim",
            "assistant_inference",
        }
        or (
            item.provenance_type != "user_assertion"
            and item.verification_status in {
                "unverified", "unknown", "contradicted", "expired",
            }
        )
    )
    dialogue: list[str] = []
    for turn in recent_turns[-max(1, recent_limit):]:
        user = re.sub(r"\s+", " ", str(turn.get("user") or "")).strip()
        assistant = re.sub(r"\s+", " ", str(turn.get("assistant") or "")).strip()
        if user:
            dialogue.append(f"[用户原话/公共事实未自动验证] {user[:700]}")
        if assistant:
            origin_refs = {
                    str(turn.get("requestId") or ""),
                    str(turn.get("proactiveEventId") or ""),
                    f"turn:{str(turn.get('at') or '')}",
            }
            assistant_sentences = _assistant_claim_sentences(assistant)
            supported_sentences = {
                claim.proposition
                for claim in trusted
                if claim.origin_ref in origin_refs
                and claim.proposition in assistant_sentences
                and claim_is_currently_supported(claim)
            }
            if assistant_sentences and len(supported_sentences) == len(assistant_sentences):
                label = "[助手旧回复/各原子句均有有效证据支持]"
            elif supported_sentences:
                label = "[助手旧回复/仅部分原子句有证据，其余禁止作为事实前提]"
            else:
                label = "[助手旧回复/未验证，禁止作为事实前提]"
            dialogue.append(f"{label} {assistant[:900]}")
    return CompiledContext(
        recent_dialogue=tuple(dialogue),
        trusted_facts=trusted[-16:],
        user_assertions=user_assertions[-12:],
        unresolved_hypotheses=hypotheses[-12:],
    )


def build_grounded_response_plan(
    context: CompiledContext,
    *,
    receipt: ResearchReceipt | None = None,
) -> GroundedResponsePlan:
    research_facts: tuple[str, ...] = ()
    if receipt and receipt.status == "completed":
        research_facts = tuple(
            [
                *(f"executed_query:{item}" for item in receipt.executed_queries),
                *(f"source:{item}" for item in receipt.source_uids),
                *(f"page_read:{item}" for item in receipt.pages_read),
            ]
        )
    return GroundedResponsePlan(
        verified_claims=context.trusted_facts,
        user_assertions=context.user_assertions,
        permitted_inferences=tuple(
            item
            for item in context.unresolved_hypotheses
            if item.provenance_type == "assistant_inference"
        ),
        unresolved_points=tuple(
            item.proposition for item in context.unresolved_hypotheses
        ),
        research_facts=research_facts,
    )


def grounded_claim_gate_decision(
    text: str,
    epistemic_state: EpistemicTurnState,
    receipt: ResearchReceipt | None,
    *,
    research_policy: str = "allowed",
    capability_state: Mapping[str, Any] | None = None,
    visual_evidence_verified: bool = False,
    citation_source_uids: Mapping[str, str] | None = None,
    enforce_web_claims: bool = True,
) -> GateDecision:
    # Models commonly put a citation immediately after sentence punctuation.
    # Bind that marker to the preceding claim before claim segmentation so a
    # locally supported partial answer is not mistaken for an uncited claim.
    claim_text = re.sub(
        r"([。！？.!?])\s*((?:\[S\d+\]\s*)+)",
        r" \2\1",
        str(text or ""),
        flags=re.I,
    )
    claims = extract_assistant_claims(claim_text, epistemic_state)
    # Planning can request more research; delivery leaves Web wording to the model.
    action_violations = (
        list(research_action_claim_violations(text, receipt))
        if enforce_web_claims else []
    )
    if action_violations:
        return GateDecision(
            status="BLOCK",
            unsupported_claim_ids=tuple(
                item.claim_id for item in claims if item.claim_type == "research_action"
            ),
            required_evidence_classes=("web_evidence",),
            reasons=tuple(action_violations),
            claims=claims,
        )

    external_requirements = tuple(
        item for item in epistemic_state.fact_requirements
        if enforce_web_claims and item.verification_required
        and item.assistant_must_assert
        and item.domain == "external_world"
        and item.evidence_class == "web_evidence"
    )
    hard_external_requirements = tuple(
        item for item in external_requirements
        if item.verification_level == "HARD"
    )
    soft_external_requirements = tuple(
        item for item in external_requirements
        if item.verification_level == "SOFT"
    )
    asserted_external = tuple(
        item for item in claims
        if item.adopted_as_truth
        and item.assertion_mode == "assertive"
        and item.domain == "external_world"
        and item.claim_type == "external_fact"
    )
    has_sufficient_receipt = bool(
        receipt
        and receipt.status == "completed"
        and receipt.source_uids
        and receipt.evidence_sufficient
    )
    citation_tokens = tuple(dict.fromkeys(re.findall(r"\[S\d+\]", str(text or ""), re.I)))
    if citation_source_uids is not None:
        visible_source_uids = {
            str(source_id).strip().upper(): str(source_uid).strip()
            for source_id, source_uid in citation_source_uids.items()
            if re.fullmatch(r"S\d+", str(source_id).strip(), re.I)
            and str(source_uid).strip()
        }
        read_source_uids = set(receipt.read_source_uids if receipt else ())
        readable_citation_ids = {
            source_id
            for source_id, source_uid in visible_source_uids.items()
            if source_uid in read_source_uids
        }
    else:
        # Compatibility path for pure agent callers that do not own the final
        # public-source projection. Backend delivery always supplies the stable
        # UID map so repeated batch-local S1 aliases cannot cross-authorize.
        readable_citation_ids = {
            str(item).strip().upper()
            for item in (receipt.read_source_ids if receipt else ())
            if re.fullmatch(r"S\d+", str(item).strip(), re.I)
        }
    cited_source_ids = {
        token.strip("[]").upper() for token in citation_tokens
    }
    if (
        enforce_web_claims
        and citation_tokens
        and not cited_source_ids.issubset(readable_citation_ids)
        and (
            not has_sufficient_receipt
            or citation_source_uids is not None
        )
    ):
        return GateDecision(
            status="REWRITE_WITH_UNCERTAINTY",
            required_evidence_classes=("web_evidence",),
            reasons=("citation_without_sufficient_evidence",),
            claims=claims,
        )

    def claim_has_read_citation(claim: AssistantClaim) -> bool:
        claim_citations = {
            item.upper()
            for item in re.findall(r"\[(S\d+)\]", claim.text, re.I)
        }
        return bool(claim_citations) and claim_citations.issubset(
            readable_citation_ids
        )

    unsupported_external = (
        tuple(
            item for item in asserted_external
            if not _UNCERTAINTY_LANGUAGE.search(item.text)
            and not claim_has_read_citation(item)
        )
        if str(research_policy) != "allowed"
        and hard_external_requirements
        and not has_sufficient_receipt
        else ()
    )
    if unsupported_external:
        status: GateStatus = (
            "REWRITE_WITH_UNCERTAINTY"
            if str(research_policy) == "forbidden"
            else "RESEARCH_ESCALATION"
        )
        return GateDecision(
            status=status,
            unsupported_claim_ids=tuple(item.claim_id for item in unsupported_external),
            required_evidence_classes=("web_evidence",),
            missing_verification_targets=tuple(dict.fromkeys(
                (
                    item.claim_scope or item.subject
                    for item in hard_external_requirements
                    if item.claim_scope or item.subject
                )
            )),
            reasons=("required_fact_answer_without_sufficient_receipt",),
            claims=claims,
        )

    # A SOFT fact is allowed to remain unresolved, but only locally.  It may
    # request a certainty downgrade for its own planned claim scope; it can
    # never turn the independent reasoning/opinion lane into an unsupported
    # external-fact answer or terminate the whole conversational turn.
    if soft_external_requirements and not has_sufficient_receipt:
        soft_scoped_assertions = tuple(
            claim
            for claim in claims
            if claim.adopted_as_truth
            and claim.assertion_mode == "assertive"
            and any(
                claim.domain == "external_world"
                or claim_scope_relevance(
                    item.claim_scope or item.subject,
                    claim.text,
                ) >= 0.72
                for item in soft_external_requirements
                if item.claim_scope or item.subject
            )
        )
        unsupported_soft_assertions = tuple(
            claim
            for claim in soft_scoped_assertions
            if not _UNCERTAINTY_LANGUAGE.search(claim.text)
            and not claim_has_read_citation(claim)
        )
        if unsupported_soft_assertions:
            return GateDecision(
                status="REWRITE_WITH_UNCERTAINTY",
                unsupported_claim_ids=tuple(
                    item.claim_id for item in unsupported_soft_assertions
                ),
                required_evidence_classes=("web_evidence",),
                missing_verification_targets=tuple(dict.fromkeys(
                    item.claim_scope or item.subject
                    for item in soft_external_requirements
                    if item.claim_scope or item.subject
                )),
                reasons=("soft_fact_requires_local_uncertainty_patch",),
                claims=claims,
            )

    local_claims = tuple(
        item for item in claims
        if item.adopted_as_truth and item.domain == "local_system"
    )
    if local_claims and capability_state is not None and not capability_state:
        return GateDecision(
            status="REWRITE_WITH_UNCERTAINTY",
            unsupported_claim_ids=tuple(item.claim_id for item in local_claims),
            required_evidence_classes=("local_state",),
            reasons=("local_capability_claim_without_capability_state",),
            claims=claims,
        )
    visual_requirements = tuple(
        item for item in epistemic_state.fact_requirements
        if item.assistant_must_assert
        and item.domain == "local_observation"
        and item.evidence_class == "fresh_observation"
    )
    visual_claims = tuple(
        item for item in claims
        if item.adopted_as_truth
        and item.domain == "local_observation"
        and item.claim_type == "current_visual_observation"
    )
    if visual_requirements and visual_claims and not visual_evidence_verified:
        return GateDecision(
            status="REWRITE_WITH_UNCERTAINTY",
            unsupported_claim_ids=tuple(item.claim_id for item in visual_claims),
            required_evidence_classes=("fresh_observation",),
            missing_verification_targets=tuple(dict.fromkeys(
                item.subject for item in visual_requirements if item.subject
            )),
            reasons=("current_visual_claim_without_verified_observation",),
            claims=claims,
        )
    return GateDecision(status="PASS", claims=claims)


def grounded_claim_gate_violations(
    text: str,
    epistemic_state: EpistemicTurnState,
    receipt: ResearchReceipt | None,
) -> tuple[str, ...]:
    """Compatibility view for callers that still consume violation strings."""

    decision = grounded_claim_gate_decision(text, epistemic_state, receipt)
    return () if decision.status == "PASS" else decision.reasons


def compiled_context_prompt(context: CompiledContext) -> str:
    plan = build_grounded_response_plan(context)
    return "\n".join([
        "[认识论与来源溯源上下文]",
        "可信事实（仅这里的 supported 且未过期记录可直接作为外部事实使用）：",
        *(
            f"- {item.proposition[:500]}；来源={item.provenance_type}；证据={','.join(item.source_uids) or '用户个人权威/本地工具'}"
            for item in plan.verified_claims
        ),
        "用户公共世界主张（只能表述为“你刚才说……”，回答真伪前需核验）：",
        *(f"- {item.proposition[:500]}" for item in plan.user_assertions),
        "未解决假设（尤其是助手旧回复；不得因重复出现而提升可信度）：",
        *(f"- {item[:500]}" for item in plan.unresolved_points),
        "最近对话（用于连续性，不等于事实缓存）：",
        *context.recent_dialogue,
        (
            "若当前问题质疑某条助手旧说法，先把该说法视为待核验命题；"
            "不能引用另一条助手旧回复给它作证。只有工具结果、有效来源、系统状态，"
            "或用户对自身状态的陈述才能提升可信度。"
        ),
    ])
