"""Research policy, capability envelopes, and bounded query-loop state.

This module owns *whether* a turn may or must use public evidence.  It does not
choose reply length, execute tools, render persona prose, or trust model claims.
Those concerns deliberately stay in their respective layers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import re
import unicodedata
from typing import Any, Literal, Mapping, Sequence

from .dialogue_architecture import (
    AnswerObligation,
    TurnPolicy,
    UtteranceInterpretation,
    plan_answer_obligations,
)
from .epistemic_architecture import (
    EpistemicTurnState,
    FactRequirement,
    fact_requirements_need_verification,
    plan_fact_requirements,
)
from .fingerprints import runtime_fingerprint


ResearchPolicy = Literal["required", "allowed", "forbidden"]

WEB_RESEARCH_TOOLS = ("web_search", "read_web_page")

STABLE_FOUNDATIONAL_TERMS = frozenset({
    "AI", "API", "CPU", "GPU", "HTTP", "HTTPS", "JSON", "TTS", "EXE", "KEY",
    "CNN", "RNN", "SQL", "HTML", "CSS", "TCP", "UDP", "DNS",
    "PYTHON", "JAVASCRIPT", "TYPESCRIPT", "WINDOWS", "LINUX", "GITHUB",
    "PYTORCH", "LIVE2D", "OPENGL", "WEBGL", "ADAM",
})

_EXPLICIT_WEB = re.compile(
    r"联网|上网|在线(?:查|搜|搜索|查询)|网络搜索|网页搜索|网上(?:查|搜|找)|"
    r"搜索一下|搜一下|帮我搜|查一下|查查|核实|查证|验证|确认一下|"
    r"来源|出处|引用|证据|链接",
    re.I,
)
_FORBID_WEB = re.compile(
    r"(?:不要|不用|无需|禁止|别)(?:再)?(?:联网|上网|在线查询|网络搜索|网页搜索|"
    r"查网|查网页|查外部资料|搜索外部资料)|"
    r"只(?:用|根据)(?:当前|本轮|这段)?(?:对话|上下文|已提供材料|附件内容)|"
    r"(?:离线|不联网)(?:回答|分析|处理)",
    re.I,
)
_CURRENT_FACT = re.compile(
    r"今天|今日|现在|当前|刚刚|实时|最新|本周|本月|今年|目前|截至|"
    r"价格|股价|汇率|天气|气温|比分|赛程|新闻|现任|在任|版本号|"
    r"营业时间|开放时间|政策|法规|航班|列车",
    re.I,
)
_HIGH_STAKES = re.compile(
    r"法律|法规|政策|签证|移民|税务|诊断|治疗|药物|药品|剂量|"
    r"投资|股票|汇率|贷款|利率",
    re.I,
)
_PURE_NON_RESEARCH = re.compile(
    r"翻译|改写|润色|续写|写(?:一首|一段|一篇)|角色扮演|陪我|安慰我|"
    r"总结(?:这段|上文|附件|我发的)|根据(?:上文|这段|附件|我提供的)",
    re.I,
)
_EXTERNAL_ENTITY_CLASS = re.compile(
    r"网站|网页|站点|平台|软件|应用|APP|项目|产品|公司|机构|组织|"
    r"论文|研究|模型|API|SDK|仓库|repo|repository|service|website|site",
    re.I,
)
_ENTITY_IDENTITY_OR_PURPOSE = re.compile(
    r"是什么|是干什么|做什么|干嘛的|用途|作用|提供什么|有哪些内容|"
    r"靠谱吗|谁做的|属于谁|what is|what does|purpose|about",
    re.I,
)


def _script_kind(char: str) -> str:
    if not char:
        return "other"
    codepoint = ord(char)
    if (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    ):
        return "han"
    if char.isascii() and char.isalpha():
        return "latin"
    if char.isdigit():
        return "digit"
    name = unicodedata.name(char, "")
    if "LATIN" in name and char.isalpha():
        return "latin"
    return "other"


def split_script_boundaries(text: str) -> str:
    """Insert token boundaries at Latin/digit ↔ Han transitions."""

    value = str(text or "")
    if len(value) < 2:
        return value
    output: list[str] = [value[0]]
    previous = _script_kind(value[0])
    for char in value[1:]:
        current = _script_kind(char)
        if (
            {previous, current} <= {"latin", "digit", "han"}
            and "han" in {previous, current}
            and previous != current
        ):
            if output and not output[-1].isspace():
                output.append(" ")
        output.append(char)
        previous = current
    return "".join(output)


def extract_latin_entities(text: str) -> tuple[str, ...]:
    normalized = split_script_boundaries(text)
    tokens = re.findall(
        r"(?<![A-Za-z0-9_.+-])"
        r"(?:[A-Za-z][A-Za-z0-9_.+-]{2,}|v\d+(?:\.\d+){1,3})"
        r"(?![A-Za-z0-9_.+-])",
        normalized,
        re.I,
    )
    values: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        cleaned = token.strip("._+-")
        key = cleaned.casefold()
        if len(cleaned) < 3 or key in seen:
            continue
        seen.add(key)
        values.append(cleaned)
    return tuple(values)


@dataclass(frozen=True)
class ResearchDecision:
    policy: ResearchPolicy
    reasons: tuple[str, ...] = ()
    unknown_entities: tuple[str, ...] = ()
    unstable_claims: tuple[str, ...] = ()
    verification_targets: tuple[str, ...] = ()
    primary_source_required: bool = False
    confidence: float = 1.0
    source_policy_id: str | None = None
    plan_id: str = ""
    plan_revision: int = 1

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        for key in (
            "reasons",
            "unknown_entities",
            "unstable_claims",
            "verification_targets",
        ):
            value[key] = list(value[key])
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "ResearchDecision":
        data = value or {}
        policy = str(data.get("policy") or "allowed").lower()
        if policy not in {"required", "allowed", "forbidden"}:
            policy = "allowed"
        return cls(
            policy=policy,  # type: ignore[arg-type]
            reasons=tuple(str(item) for item in data.get("reasons", ()) if str(item)),
            unknown_entities=tuple(
                str(item) for item in data.get("unknown_entities", data.get("unknownEntities", ()))
                if str(item)
            ),
            unstable_claims=tuple(
                str(item) for item in data.get("unstable_claims", data.get("unstableClaims", ()))
                if str(item)
            ),
            verification_targets=tuple(
                str(item)
                for item in data.get("verification_targets", data.get("verificationTargets", ()))
                if str(item)
            ),
            primary_source_required=bool(
                data.get("primary_source_required", data.get("primarySourceRequired"))
            ),
            confidence=max(0.0, min(1.0, float(data.get("confidence") or 0.0))),
            source_policy_id=(
                str(data.get("source_policy_id") or data.get("sourcePolicyId") or "").strip()
                or None
            ),
            plan_id=str(data.get("plan_id") or data.get("planId") or ""),
            plan_revision=max(
                1, int(data.get("plan_revision", data.get("planRevision", 1)) or 1)
            ),
        )

@dataclass(frozen=True)
class EpistemicPlan:
    """Single immutable research/evidence plan shared by the whole turn."""

    turn_id: str
    plan_id: str
    revision: int
    research_policy: ResearchPolicy
    policy_reasons: tuple[str, ...]
    verification_targets: tuple[str, ...]
    allowed_evidence_classes: tuple[str, ...]
    required_evidence_classes: tuple[str, ...]
    may_escalate: bool
    answer_obligations: tuple[AnswerObligation, ...]
    fact_requirements: tuple[FactRequirement, ...]
    unknown_entities: tuple[str, ...] = ()
    unstable_claims: tuple[str, ...] = ()
    primary_source_required: bool = False
    source_policy_id: str | None = None
    confidence: float = 1.0

    def to_dict(self) -> dict[str, object]:
        return {
            "turnId": self.turn_id,
            "planId": self.plan_id,
            "revision": self.revision,
            "researchPolicy": self.research_policy,
            "policyReasons": list(self.policy_reasons),
            "verificationTargets": list(self.verification_targets),
            "allowedEvidenceClasses": list(self.allowed_evidence_classes),
            "requiredEvidenceClasses": list(self.required_evidence_classes),
            "mayEscalate": self.may_escalate,
            "answerObligations": [item.to_dict() for item in self.answer_obligations],
            "factRequirements": [item.to_dict() for item in self.fact_requirements],
            "unknownEntities": list(self.unknown_entities),
            "unstableClaims": list(self.unstable_claims),
            "primarySourceRequired": self.primary_source_required,
            "sourcePolicyId": self.source_policy_id,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "EpistemicPlan | None":
        data = value or {}
        plan_id = str(data.get("planId") or data.get("plan_id") or "")
        if not plan_id:
            return None
        policy = str(data.get("researchPolicy") or data.get("research_policy") or "allowed")
        if policy not in {"required", "allowed", "forbidden"}:
            policy = "allowed"
        return cls(
            turn_id=str(data.get("turnId") or data.get("turn_id") or ""),
            plan_id=plan_id,
            revision=max(1, int(data.get("revision") or 1)),
            research_policy=policy,  # type: ignore[arg-type]
            policy_reasons=tuple(
                str(item) for item in data.get("policyReasons", data.get("policy_reasons", ()))
                if str(item)
            ),
            verification_targets=tuple(
                str(item) for item in data.get("verificationTargets", data.get("verification_targets", ()))
                if str(item)
            ),
            allowed_evidence_classes=tuple(
                str(item) for item in data.get("allowedEvidenceClasses", data.get("allowed_evidence_classes", ()))
                if str(item)
            ),
            required_evidence_classes=tuple(
                str(item) for item in data.get("requiredEvidenceClasses", data.get("required_evidence_classes", ()))
                if str(item)
            ),
            may_escalate=bool(data.get("mayEscalate", data.get("may_escalate", True))),
            answer_obligations=tuple(
                AnswerObligation.from_dict(item)
                for item in data.get("answerObligations", data.get("answer_obligations", ()))
                if isinstance(item, Mapping)
            ),
            fact_requirements=tuple(
                FactRequirement.from_dict(item)
                for item in data.get("factRequirements", data.get("fact_requirements", ()))
                if isinstance(item, Mapping)
            ),
            unknown_entities=tuple(
                str(item) for item in data.get("unknownEntities", data.get("unknown_entities", ()))
                if str(item)
            ),
            unstable_claims=tuple(
                str(item) for item in data.get("unstableClaims", data.get("unstable_claims", ()))
                if str(item)
            ),
            primary_source_required=bool(
                data.get("primarySourceRequired", data.get("primary_source_required"))
            ),
            source_policy_id=str(
                data.get("sourcePolicyId") or data.get("source_policy_id") or ""
            ).strip() or None,
            confidence=max(0.0, min(1.0, float(data.get("confidence") or 0.0))),
        )

    def research_decision(self) -> ResearchDecision:
        return ResearchDecision(
            policy=self.research_policy,
            reasons=self.policy_reasons,
            unknown_entities=self.unknown_entities,
            unstable_claims=self.unstable_claims,
            verification_targets=self.verification_targets,
            primary_source_required=self.primary_source_required,
            confidence=self.confidence,
            source_policy_id=self.source_policy_id,
            plan_id=self.plan_id,
            plan_revision=self.revision,
        )

    def epistemic_state(self) -> EpistemicTurnState:
        allowed = ["persona_opinion", "user_attributed_claim", "stable_fact"]
        if any(item.domain == "external_world" for item in self.fact_requirements):
            allowed.extend(("verified_external_fact", "evidence_bounded_inference"))
        if any(item.evidence_class == "fresh_observation" for item in self.fact_requirements):
            allowed.append("verified_local_observation")
        if any(item.evidence_class == "artifact_evidence" for item in self.fact_requirements):
            allowed.append("verified_local_artifact")
        return EpistemicTurnState(
            fact_requirements=self.fact_requirements,
            allowed_final_claim_types=tuple(allowed),
        )

    def with_current_visual_observation(
        self,
        *,
        subject: str = "current visual observation",
        observation_available: bool = True,
    ) -> "EpistemicPlan":
        """Bind a current visual tool receipt to the answer obligation.

        A successful capture supplies fresh observation evidence. A failed
        capture still supplies an authoritative local action receipt and must
        produce a useful failure explanation instead of falling back to a Web
        fact placeholder. The caller owns the actual tool receipt; no lexical
        trigger decides success or failure here.
        """

        if any(
            item.domain == "local_observation"
            and item.evidence_class in {"fresh_observation", "action_receipt"}
            for item in self.fact_requirements
        ):
            return self
        normalized_subject = str(subject or "current visual observation").strip()[:180]
        obligation_id = "obl_" + runtime_fingerprint(
            f"{self.plan_id}\x1fvisual\x1f{normalized_subject}"
        )
        requirement_id = "fact_" + runtime_fingerprint(
            f"{self.plan_id}\x1ffresh_observation\x1f{normalized_subject}"
        )
        obligation = AnswerObligation(
            obligation_id=obligation_id,
            act_type=(
                "answer_current_visual_observation"
                if observation_available
                else "explain_current_visual_observation_failure"
            ),
            target=normalized_subject,
            requires_fact_assertion=True,
            domain="local_observation",
            evidence_class=("fresh_observation" if observation_available else "action_receipt"),
        )
        requirement = FactRequirement(
            requirement_id=requirement_id,
            subject=normalized_subject,
            predicate=(
                "current_visual_observation"
                if observation_available
                else "current_visual_observation_status"
            ),
            object_type=(
                "visible_scene_or_activity"
                if observation_available
                else "visual_tool_status"
            ),
            freshness_class="real_time",
            max_evidence_age_seconds=120,
            verification_required=False,
            preferred_source_types=((
                "current_visual_tool_observation"
                if observation_available
                else "current_visual_tool_attempt"
            ),),
            reason=(
                "successful_current_visual_tool_receipt"
                if observation_available
                else "failed_current_visual_tool_receipt"
            ),
            obligation_id=obligation_id,
            speech_role="assistant_answer",
            assistant_must_assert=True,
            domain="local_observation",
            evidence_class=("fresh_observation" if observation_available else "action_receipt"),
        )
        # ``plan_answer_obligations`` has to describe a factual question before
        # any tool runs, so a deictic visual request initially owns a generic
        # external/Web placeholder.  Once a current visual tool succeeds that
        # placeholder has been superseded by the fresh observation; retaining
        # both makes the later HARD Web gate reject an otherwise verified visual
        # answer for not having an unrelated research receipt.
        #
        # Only replace the generic pre-tool placeholder.  A claim-level plan
        # produced by the independent pre-answer audit (for example identifying
        # a public person from an image) remains an external requirement.
        superseded_obligation_ids = {
            item.obligation_id
            for item in self.fact_requirements
            if item.domain == "external_world"
            and item.evidence_class == "web_evidence"
            and item.predicate == "external_fact"
            and item.reason == "factual_answer_obligation"
        }
        retained_requirements = tuple(
            item for item in self.fact_requirements
            if item.obligation_id not in superseded_obligation_ids
        )
        retained_obligations = tuple(
            item for item in self.answer_obligations
            if item.obligation_id not in superseded_obligation_ids
        )
        local_evidence_class = (
            "fresh_observation" if observation_available else "action_receipt"
        )
        requirements = (*retained_requirements, requirement)
        required_evidence_classes = tuple(dict.fromkeys((
            *(
                item.evidence_class
                for item in retained_requirements
                if item.verification_required and item.verification_level == "HARD"
            ),
            local_evidence_class,
        )))
        return replace(
            self,
            revision=self.revision + 1,
            allowed_evidence_classes=tuple(dict.fromkeys((
                *self.allowed_evidence_classes,
                local_evidence_class,
            ))),
            required_evidence_classes=required_evidence_classes,
            answer_obligations=(*retained_obligations, obligation),
            fact_requirements=requirements,
        )

    def with_current_document_evidence(
        self,
        *,
        subject: str = "current uploaded document",
        content_available: bool = True,
    ) -> "EpistemicPlan":
        """Bind the current local document read attempt to the answer obligation.

        Generic short questions initially receive an external/Web placeholder
        because the speech-act planner has not yet seen tool receipts.  Once the
        current document reader has completed, its local artifact receipt
        supersedes only that placeholder, whether content extraction succeeded
        or failed.  Independently planned public-world claims remain intact and
        may still require research.
        """

        if any(
            item.domain == "local_observation"
            and item.evidence_class == "artifact_evidence"
            for item in self.fact_requirements
        ):
            return self
        normalized_subject = str(subject or "current uploaded document").strip()[:180]
        obligation_id = "obl_" + runtime_fingerprint(
            f"{self.plan_id}\x1fdocument\x1f{normalized_subject}"
        )
        requirement_id = "fact_" + runtime_fingerprint(
            f"{self.plan_id}\x1fartifact_evidence\x1f{normalized_subject}"
        )
        obligation = AnswerObligation(
            obligation_id=obligation_id,
            act_type=(
                "answer_current_document"
                if content_available
                else "explain_current_document_read_failure"
            ),
            target=normalized_subject,
            requires_fact_assertion=True,
            domain="local_observation",
            evidence_class="artifact_evidence",
        )
        requirement = FactRequirement(
            requirement_id=requirement_id,
            subject=normalized_subject,
            predicate=(
                "current_document_content"
                if content_available
                else "current_document_read_status"
            ),
            object_type=("document_content" if content_available else "document_read_status"),
            freshness_class="unknown",
            max_evidence_age_seconds=None,
            verification_required=False,
            preferred_source_types=("current_attachment_reader",),
            reason=(
                "successful_current_document_tool_receipt"
                if content_available
                else "failed_current_document_tool_receipt"
            ),
            obligation_id=obligation_id,
            speech_role="assistant_answer",
            assistant_must_assert=True,
            domain="local_observation",
            evidence_class="artifact_evidence",
        )
        superseded_obligation_ids = {
            item.obligation_id
            for item in self.fact_requirements
            if item.domain == "external_world"
            and item.evidence_class == "web_evidence"
            and item.predicate == "external_fact"
            and item.reason == "factual_answer_obligation"
        }
        retained_requirements = tuple(
            item for item in self.fact_requirements
            if item.obligation_id not in superseded_obligation_ids
        )
        retained_obligations = tuple(
            item for item in self.answer_obligations
            if item.obligation_id not in superseded_obligation_ids
        )
        requirements = (*retained_requirements, requirement)
        required_evidence_classes = tuple(dict.fromkeys((
            *(
                item.evidence_class
                for item in retained_requirements
                if item.verification_required and item.verification_level == "HARD"
            ),
            "artifact_evidence",
        )))
        return replace(
            self,
            revision=self.revision + 1,
            allowed_evidence_classes=tuple(dict.fromkeys((
                *self.allowed_evidence_classes,
                "artifact_evidence",
            ))),
            required_evidence_classes=required_evidence_classes,
            answer_obligations=(*retained_obligations, obligation),
            fact_requirements=requirements,
        )

    def with_evidence_gap(
        self,
        *,
        reason: str,
        unknown_entities: Sequence[str] = (),
        verification_targets: Sequence[str] = (),
    ) -> "EpistemicPlan":
        return replace(
            self,
            revision=self.revision + 1,
            policy_reasons=tuple(dict.fromkeys((*self.policy_reasons, str(reason)))),
            unknown_entities=tuple(dict.fromkeys((*self.unknown_entities, *map(str, unknown_entities)))),
            verification_targets=tuple(dict.fromkeys((*self.verification_targets, *map(str, verification_targets)))),
            confidence=min(self.confidence, 0.55),
        )


def create_epistemic_plan(
    turn_id: str,
    interpretation: UtteranceInterpretation,
    *,
    decision: ResearchDecision | None = None,
    revision: int = 1,
) -> EpistemicPlan:
    obligations = plan_answer_obligations(interpretation)
    requirements = plan_fact_requirements(
        interpretation.semantic_text or interpretation.original_text,
        interpretation=interpretation,
        obligations=obligations,
    )
    active_decision = decision or deterministic_research_decision(interpretation)
    plan_id = "eplan_" + runtime_fingerprint(
        f"{turn_id}\x1f{interpretation.semantic_text}"
    )
    allowed = [
        "user_authority", "local_state", "task_state", "conversation_history",
        "fresh_observation", "action_receipt", "none",
    ]
    if active_decision.policy != "forbidden":
        allowed.append("web_evidence")
    required = tuple(dict.fromkeys(
        item.evidence_class for item in requirements
        if item.verification_required and item.verification_level == "HARD"
    ))
    return EpistemicPlan(
        turn_id=str(turn_id or ""),
        plan_id=plan_id,
        revision=max(1, revision),
        research_policy=active_decision.policy,
        policy_reasons=active_decision.reasons,
        verification_targets=active_decision.verification_targets,
        allowed_evidence_classes=tuple(allowed),
        required_evidence_classes=required,
        may_escalate=active_decision.policy != "forbidden",
        answer_obligations=obligations,
        fact_requirements=requirements,
        unknown_entities=active_decision.unknown_entities,
        unstable_claims=active_decision.unstable_claims,
        primary_source_required=active_decision.primary_source_required,
        source_policy_id=active_decision.source_policy_id,
        confidence=active_decision.confidence,
    )


@dataclass(frozen=True)
class CapabilityEnvelope:
    reply_mode: str
    research_policy: ResearchPolicy
    allowed_tools: tuple[str, ...]
    required_tools: tuple[str, ...]
    may_escalate: bool
    source_policy_id: str | None
    max_tool_rounds: int
    tool_policy: str = "model_decides"
    candidate_tool_families: tuple[str, ...] = ()
    initially_loaded_tool_ids: tuple[str, ...] = ()
    may_search_tools: bool = True
    latency_budget_ms: int = 12000
    perception_target: str = "none"
    ambiguities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["allowed_tools"] = list(self.allowed_tools)
        value["required_tools"] = list(self.required_tools)
        value["candidate_tool_families"] = list(self.candidate_tool_families)
        value["initially_loaded_tool_ids"] = list(self.initially_loaded_tool_ids)
        value["ambiguities"] = list(self.ambiguities)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "CapabilityEnvelope":
        data = value or {}
        policy = str(data.get("research_policy") or data.get("researchPolicy") or "allowed")
        if policy not in {"required", "allowed", "forbidden"}:
            policy = "allowed"
        return cls(
            reply_mode=str(data.get("reply_mode") or data.get("replyMode") or "normal"),
            research_policy=policy,  # type: ignore[arg-type]
            allowed_tools=tuple(
                str(item)
                for item in data.get("allowed_tools", data.get("allowedTools", ()))
                if str(item)
            ),
            required_tools=tuple(
                str(item)
                for item in data.get("required_tools", data.get("requiredTools", ()))
                if str(item)
            ),
            may_escalate=bool(data.get("may_escalate", data.get("mayEscalate"))),
            source_policy_id=(
                str(data.get("source_policy_id") or data.get("sourcePolicyId") or "").strip()
                or None
            ),
            max_tool_rounds=max(
                0,
                min(6, int(data.get("max_tool_rounds", data.get("maxToolRounds", 3)) or 3)),
            ),
            tool_policy=str(
                data.get("tool_policy") or data.get("toolPolicy") or "model_decides"
            ),
            candidate_tool_families=tuple(
                str(item)
                for item in data.get(
                    "candidate_tool_families",
                    data.get("candidateToolFamilies", ()),
                )
                if str(item)
            ),
            initially_loaded_tool_ids=tuple(
                str(item)
                for item in data.get(
                    "initially_loaded_tool_ids",
                    data.get("initiallyLoadedToolIds", ()),
                )
                if str(item)
            ),
            may_search_tools=bool(
                data.get("may_search_tools", data.get("maySearchTools", True))
            ),
            latency_budget_ms=max(
                1000,
                min(
                    60000,
                    int(data.get("latency_budget_ms", data.get("latencyBudgetMs", 12000)) or 12000),
                ),
            ),
            perception_target=str(
                data.get("perception_target") or data.get("perceptionTarget") or "none"
            ),
            ambiguities=tuple(
                str(item)
                for item in data.get("ambiguities", ())
                if str(item)
            ),
        )


@dataclass(frozen=True)
class ResearchLoopState:
    request_id: str
    policy: ResearchPolicy
    status: str = "planned"
    tool_rounds: int = 0
    query_hashes: tuple[str, ...] = ()
    canonical_urls: tuple[str, ...] = ()
    evidence_source_ids: tuple[str, ...] = ()
    no_progress_cycles: int = 0
    stop_reason: str = ""
    active_plan: Mapping[str, Any] | None = None
    evidence_result: Mapping[str, Any] | None = None
    last_review: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        for key in ("query_hashes", "canonical_urls", "evidence_source_ids"):
            value[key] = list(value[key])
        value["active_plan"] = dict(self.active_plan or {})
        value["evidence_result"] = dict(self.evidence_result or {})
        value["last_review"] = dict(self.last_review or {})
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "ResearchLoopState":
        data = value or {}
        policy = str(data.get("policy") or "allowed")
        if policy not in {"required", "allowed", "forbidden"}:
            policy = "allowed"
        return cls(
            request_id=str(data.get("request_id") or data.get("requestId") or ""),
            policy=policy,  # type: ignore[arg-type]
            status=str(data.get("status") or "planned"),
            tool_rounds=max(0, int(data.get("tool_rounds", data.get("toolRounds", 0)) or 0)),
            query_hashes=tuple(
                str(item)
                for item in data.get("query_hashes", data.get("queryHashes", ()))
                if str(item)
            ),
            canonical_urls=tuple(
                str(item)
                for item in data.get("canonical_urls", data.get("canonicalUrls", ()))
                if str(item)
            ),
            evidence_source_ids=tuple(
                str(item)
                for item in data.get("evidence_source_ids", data.get("evidenceSourceIds", ()))
                if str(item)
            ),
            no_progress_cycles=max(
                0,
                int(data.get("no_progress_cycles", data.get("noProgressCycles", 0)) or 0),
            ),
            stop_reason=str(data.get("stop_reason") or data.get("stopReason") or ""),
            active_plan=(
                dict(data.get("active_plan") or data.get("activePlan") or {})
                if isinstance(data.get("active_plan") or data.get("activePlan"), Mapping)
                else {}
            ),
            evidence_result=(
                dict(data.get("evidence_result") or data.get("evidenceResult") or {})
                if isinstance(data.get("evidence_result") or data.get("evidenceResult"), Mapping)
                else {}
            ),
            last_review=(
                dict(data.get("last_review") or data.get("lastReview") or {})
                if isinstance(data.get("last_review") or data.get("lastReview"), Mapping)
                else {}
            ),
        )

    def with_round(
        self,
        *,
        queries: Sequence[str],
        urls: Sequence[str],
        source_ids: Sequence[str],
        active_plan: Mapping[str, Any] | None = None,
        evidence_result: Mapping[str, Any] | None = None,
        last_review: Mapping[str, Any] | None = None,
        evidence_gain: bool | None = None,
    ) -> "ResearchLoopState":
        query_hashes = tuple(
            runtime_fingerprint(
                re.sub(r"\s+", " ", str(item)).strip().casefold(),
                length=32,
            )
            for item in queries
            if str(item).strip()
        )
        merged_queries = tuple(dict.fromkeys((*self.query_hashes, *query_hashes)))
        merged_urls = tuple(dict.fromkeys((*self.canonical_urls, *map(str, urls))))
        merged_sources = tuple(dict.fromkeys((*self.evidence_source_ids, *map(str, source_ids))))
        # A novel query is activity, not evidence progress.  Only a newly
        # observed public URL/source resets the no-progress counter.
        gained = (
            bool(evidence_gain)
            if evidence_gain is not None
            else len(merged_sources) > len(self.evidence_source_ids)
        )
        return replace(
            self,
            status="researching",
            tool_rounds=self.tool_rounds + 1,
            query_hashes=merged_queries,
            canonical_urls=merged_urls,
            evidence_source_ids=merged_sources,
            no_progress_cycles=0 if gained else self.no_progress_cycles + 1,
            active_plan=dict(active_plan or self.active_plan or {}),
            evidence_result=dict(evidence_result or self.evidence_result or {}),
            last_review=dict(last_review or self.last_review or {}),
        )

    def finished(
        self,
        *,
        status: str,
        stop_reason: str,
        active_plan: Mapping[str, Any] | None = None,
        evidence_result: Mapping[str, Any] | None = None,
        last_review: Mapping[str, Any] | None = None,
    ) -> "ResearchLoopState":
        return replace(
            self,
            status=str(status or "insufficient"),
            stop_reason=str(stop_reason or ""),
            active_plan=dict(active_plan or self.active_plan or {}),
            evidence_result=dict(evidence_result or self.evidence_result or {}),
            last_review=dict(last_review or self.last_review or {}),
        )


def normalized_query_hash(query: str) -> str:
    normalized = re.sub(r"\s+", " ", str(query or "")).strip().casefold()
    return runtime_fingerprint(normalized, length=32) if normalized else ""


def deterministic_research_decision(
    interpretation: UtteranceInterpretation,
) -> ResearchDecision:
    """Expose research capability without deciding from lexical heuristics.

    The main dialogue model owns the positive/negative Web choice and expresses
    it as a structured ``tool_use`` in the same QueryLoop.  Backend settings
    remain the hard permission boundary; this proposal is deliberately
    ``allowed`` for every ordinary turn so no regex can pre-empt that choice.
    """

    text = str(interpretation.semantic_text or interpretation.original_text or "").strip()
    latin_entities = extract_latin_entities(text)
    non_foundational = tuple(
        item for item in latin_entities if item.upper() not in STABLE_FOUNDATIONAL_TERMS
    )
    return ResearchDecision(
        "allowed",
        reasons=("model_decides_in_query_loop",),
        unknown_entities=non_foundational,
        confidence=0.9,
    )


def capability_envelope(
    turn_policy: TurnPolicy,
    decision: ResearchDecision,
    *,
    web_permission: bool = True,
) -> CapabilityEnvelope:
    allowed = (
        WEB_RESEARCH_TOOLS
        if decision.policy != "forbidden" and web_permission
        else ()
    )
    required = (
        ("web_search",)
        if decision.policy == "required" and web_permission
        else ()
    )
    return CapabilityEnvelope(
        reply_mode=turn_policy.reply_mode,
        research_policy=decision.policy,
        allowed_tools=tuple(allowed),
        required_tools=tuple(required),
        may_escalate=decision.policy == "allowed" and web_permission,
        source_policy_id=decision.source_policy_id,
        max_tool_rounds=3 if decision.policy != "forbidden" else 0,
    )
