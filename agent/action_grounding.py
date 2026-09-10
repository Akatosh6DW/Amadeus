"""Deterministic natural-language grounding for external actions.

The dialogue channel keeps the user's exact text.  This module derives a
separate parse view once, records the removed spans, and carries one structured
action candidate through routing and execution.  It deliberately does not know
about providers, storage, browser adapters, persona prompts, or Live2D.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Literal, Mapping, Sequence


ActionType = Literal["none", "open_browser", "browser_search", "open_target"]
PendingActionRelation = Literal[
    "none",
    "continue",
    "cancel",
    "pause",
    "replace",
    "unrelated",
    "ambiguous",
]
PreparedRoute = Literal[
    "fast_chat",
    "external_action",
    "external_action_needs_target",
    "research_plus_external_action",
    "cancel_pending",
    "pause_pending",
]


@dataclass(frozen=True)
class TextSpan:
    start: int
    end: int
    text: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VocativeFact:
    vocative_type: str
    exact_text: str
    span: TextSpan

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["span"] = self.span.to_dict()
        return value


@dataclass(frozen=True)
class InterpretedUserInput:
    original_text: str
    dialogue_text: str
    action_parse_text: str
    action_parse_offset: int
    vocatives: tuple[VocativeFact, ...]
    discourse_markers: tuple[TextSpan, ...]
    removed_spans: tuple[TextSpan, ...]

    @property
    def semantic_text(self) -> str:
        """Return the utterance content after confidently parsed discourse markers."""

        return self.action_parse_text

    def to_dict(self) -> dict[str, object]:
        return {
            "originalText": self.original_text,
            "dialogueText": self.dialogue_text,
            "actionParseText": self.action_parse_text,
            "semanticText": self.semantic_text,
            "actionParseOffset": self.action_parse_offset,
            "vocatives": [item.to_dict() for item in self.vocatives],
            "discourseMarkers": [item.to_dict() for item in self.discourse_markers],
            "removedSpans": [item.to_dict() for item in self.removed_spans],
        }


@dataclass(frozen=True)
class ActionCandidate:
    action_type: ActionType
    target_text: str
    explicitness: Literal["none", "implicit", "explicit"]
    confidence: float
    negated: bool
    diagnostic: bool
    request_form: str
    evidence_spans: tuple[TextSpan, ...]

    @property
    def executable(self) -> bool:
        return (
            self.action_type in {"open_browser", "browser_search"}
            and not self.negated
            and not self.diagnostic
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "actionType": self.action_type,
            "targetText": self.target_text,
            "explicitness": self.explicitness,
            "confidence": self.confidence,
            "negated": self.negated,
            "diagnostic": self.diagnostic,
            "requestForm": self.request_form,
            "evidenceSpans": [item.to_dict() for item in self.evidence_spans],
            "executable": self.executable,
        }


@dataclass(frozen=True)
class PendingActionDecision:
    relation: PendingActionRelation
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PreparedTurn:
    request_id: str
    interpreted: InterpretedUserInput
    action_candidate: ActionCandidate
    pending_relation: PendingActionDecision
    route: PreparedRoute
    created_from: str = "deterministic_action_grounding"
    research_decision: Mapping[str, Any] | None = None
    capability_envelope: Mapping[str, Any] | None = None
    turn_contract: Mapping[str, Any] | None = None
    epistemic_state: Mapping[str, Any] | None = None
    epistemic_plan: Mapping[str, Any] | None = None

    @property
    def has_explicit_external_action(self) -> bool:
        return bool(
            self.action_candidate.executable
            or self.pending_relation.relation == "continue"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "requestId": self.request_id,
            "route": self.route,
            "createdFrom": self.created_from,
            "hasExplicitExternalAction": self.has_explicit_external_action,
            "interpretedInput": self.interpreted.to_dict(),
            "actionCandidate": self.action_candidate.to_dict(),
            "pendingActionRelation": self.pending_relation.to_dict(),
            "researchDecision": dict(self.research_decision or {}),
            "capabilityEnvelope": dict(self.capability_envelope or {}),
            "turnContract": dict(self.turn_contract or {}),
            "epistemicState": dict(self.epistemic_state or {}),
            "epistemicPlan": dict(self.epistemic_plan or {}),
        }


def prepared_turn_from_dict(value: Mapping[str, Any]) -> PreparedTurn | None:
    """Rehydrate one persisted turn without re-running classification."""

    interpreted_raw = (
        value.get("interpretedInput")
        if isinstance(value.get("interpretedInput"), Mapping)
        else {}
    )
    action_raw = (
        value.get("actionCandidate")
        if isinstance(value.get("actionCandidate"), Mapping)
        else {}
    )
    pending_raw = (
        value.get("pendingActionRelation")
        if isinstance(value.get("pendingActionRelation"), Mapping)
        else {}
    )
    try:
        vocatives = tuple(
            VocativeFact(
                str(item.get("vocative_type") or item.get("vocativeType") or ""),
                str(item.get("exact_text") or item.get("exactText") or ""),
                TextSpan(
                    int((item.get("span") or {}).get("start") or 0),
                    int((item.get("span") or {}).get("end") or 0),
                    str((item.get("span") or {}).get("text") or ""),
                ),
            )
            for item in interpreted_raw.get("vocatives", ())
            if isinstance(item, Mapping)
        )
        discourse = tuple(
            TextSpan(
                int(item.get("start") or 0),
                int(item.get("end") or 0),
                str(item.get("text") or ""),
            )
            for item in interpreted_raw.get("discourseMarkers", ())
            if isinstance(item, Mapping)
        )
        removed = tuple(
            TextSpan(
                int(item.get("start") or 0),
                int(item.get("end") or 0),
                str(item.get("text") or ""),
            )
            for item in interpreted_raw.get("removedSpans", ())
            if isinstance(item, Mapping)
        )
        interpreted = InterpretedUserInput(
            original_text=str(
                interpreted_raw.get("originalText")
                or interpreted_raw.get("dialogueText")
                or ""
            ),
            dialogue_text=str(interpreted_raw.get("dialogueText") or ""),
            action_parse_text=str(
                interpreted_raw.get("actionParseText")
                or interpreted_raw.get("semanticText")
                or ""
            ),
            action_parse_offset=int(interpreted_raw.get("actionParseOffset") or 0),
            vocatives=vocatives,
            discourse_markers=discourse,
            removed_spans=removed,
        )
        evidence_spans = tuple(
            TextSpan(
                int(item.get("start") or 0),
                int(item.get("end") or 0),
                str(item.get("text") or ""),
            )
            for item in action_raw.get("evidenceSpans", ())
            if isinstance(item, Mapping)
        )
        action_type = str(action_raw.get("actionType") or "none")
        if action_type not in {"none", "open_browser", "browser_search", "open_target"}:
            action_type = "none"
        explicitness = str(action_raw.get("explicitness") or "none")
        if explicitness not in {"none", "implicit", "explicit"}:
            explicitness = "none"
        candidate = ActionCandidate(
            action_type=action_type,  # type: ignore[arg-type]
            target_text=str(action_raw.get("targetText") or ""),
            explicitness=explicitness,  # type: ignore[arg-type]
            confidence=float(action_raw.get("confidence") or 0.0),
            negated=bool(action_raw.get("negated")),
            diagnostic=bool(action_raw.get("diagnostic")),
            request_form=str(action_raw.get("requestForm") or ""),
            evidence_spans=evidence_spans,
        )
        relation = str(pending_raw.get("relation") or "none")
        if relation not in {
            "none", "continue", "cancel", "pause", "replace",
            "unrelated", "ambiguous",
        }:
            relation = "none"
        route = str(value.get("route") or "fast_chat")
        if route not in {
            "fast_chat", "external_action", "external_action_needs_target",
            "research_plus_external_action", "cancel_pending", "pause_pending",
        }:
            route = "fast_chat"
        return PreparedTurn(
            request_id=str(value.get("requestId") or "")[:120],
            interpreted=interpreted,
            action_candidate=candidate,
            pending_relation=PendingActionDecision(
                relation=relation,  # type: ignore[arg-type]
                reason=str(pending_raw.get("reason") or "persisted_turn"),
            ),
            route=route,  # type: ignore[arg-type]
            created_from=str(value.get("createdFrom") or "persisted_prepared_turn"),
            research_decision=(
                dict(value.get("researchDecision"))
                if isinstance(value.get("researchDecision"), Mapping)
                else None
            ),
            capability_envelope=(
                dict(value.get("capabilityEnvelope"))
                if isinstance(value.get("capabilityEnvelope"), Mapping)
                else None
            ),
            turn_contract=(
                dict(value.get("turnContract"))
                if isinstance(value.get("turnContract"), Mapping)
                else None
            ),
            epistemic_state=(
                dict(value.get("epistemicState"))
                if isinstance(value.get("epistemicState"), Mapping)
                else None
            ),
            epistemic_plan=(
                dict(value.get("epistemicPlan"))
                if isinstance(value.get("epistemicPlan"), Mapping)
                else None
            ),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def _strings(config: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = config.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value if str(item))


def _skip_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _consume_separators(text: str, index: int) -> int:
    index = _skip_spaces(text, index)
    while index < len(text) and text[index] in "，,：:、；;。！？.!?":
        index += 1
        index = _skip_spaces(text, index)
    return index


def _starts_with_any(text: str, index: int, values: Sequence[str]) -> str:
    remaining = text[index:]
    for value in sorted(values, key=len, reverse=True):
        if remaining.casefold().startswith(value.casefold()):
            return value
    return ""


def _marker_boundary(text: str, end: int) -> bool:
    return end >= len(text) or text[end].isspace() or text[end] in "，,：:、；;。！？.!?"


_GENERAL_CLAUSE_PRONOUNS = (
    "你们", "我们", "咱们", "他们", "她们", "它们",
    "你", "我", "咱", "他", "她", "它", "这", "那",
)
_GENERAL_QUESTION_STARTERS = (
    "是不是", "能不能", "可不可以", "有没有", "为什么", "为何",
    "怎么", "如何", "谁", "什么", "哪里", "哪儿", "哪个", "哪些",
    "多少", "是否", "能否", "要不要", "该不该", "会不会",
)
_GENERAL_PREDICATE_STARTERS = (
    "还记得", "记得", "知道", "觉得", "认为", "怎么看", "能", "会",
    "可以", "应该", "要", "想", "帮", "给", "替", "看", "说", "告诉",
    "解释", "分析",
)
_QUESTION_OR_DISCLOSURE_MARKERS = re.compile(
    r"[？?]|(?:吗|么|呢|不|没有|是不是|能不能|可不可以|有没有|"
    r"为什么|为何|怎么|如何|谁|什么|哪里|哪儿|哪个|哪些|多少|"
    r"难受|害怕|焦虑|开心|高兴|生气|喜欢|爱|想你|在乎)"
)


def _looks_like_semantic_clause_start(
    text: str,
    index: int,
    *,
    request_starters: Sequence[str],
) -> bool:
    """Recognize general address-plus-clause syntax without consuming noun titles."""

    cursor = _skip_spaces(text, index)
    tail = text[cursor:].strip()
    if not tail:
        return False
    if _starts_with_any(text, cursor, request_starters):
        return True
    if any(tail.casefold().startswith(item.casefold()) for item in _GENERAL_QUESTION_STARTERS):
        return True
    if any(tail.casefold().startswith(item.casefold()) for item in _GENERAL_CLAUSE_PRONOUNS):
        return bool(_QUESTION_OR_DISCLOSURE_MARKERS.search(tail))
    if any(tail.casefold().startswith(item.casefold()) for item in _GENERAL_PREDICATE_STARTERS):
        return bool(re.search(r"[？?]|(?:吗|么|呢|吧)[。！!…~～]*$", tail))
    return False


def _vocative_boundary(
    text: str,
    end: int,
    *,
    particles: Sequence[str],
    request_starters: Sequence[str],
) -> tuple[bool, int]:
    cursor = end
    particle = _starts_with_any(text, cursor, particles)
    if particle:
        cursor += len(particle)
    if cursor >= len(text):
        return True, cursor
    if text[cursor] in "，,：:、；;。！？.!?":
        return True, _consume_separators(text, cursor)
    # Whitespace alone is not a sufficient vocative boundary: “Amadeus 项目”
    # is a noun phrase. Accept punctuation-free natural address only when the
    # remainder forms a general question, disclosure, or request clause.
    semantic_cursor = _skip_spaces(text, cursor)
    if _looks_like_semantic_clause_start(
        text,
        semantic_cursor,
        request_starters=request_starters,
    ):
        return True, semantic_cursor
    return False, end


def interpret_user_input(
    raw_text: str,
    config: Mapping[str, Any],
) -> InterpretedUserInput:
    original = str(raw_text or "")
    discourse_values = _strings(config, "leadingDiscourseMarkers")
    particles = _strings(config, "vocativeParticles")
    prefixes = _strings(config, "politePrefixes")
    action_verbs = (
        *_strings(config, "browserActionVerbs"),
        *_strings(config, "browserSearchVerbs"),
    )
    request_starters = (*prefixes, *action_verbs)
    raw_vocatives = config.get("vocatives")
    vocative_values: list[tuple[str, str]] = []
    if isinstance(raw_vocatives, Mapping):
        for kind, aliases in raw_vocatives.items():
            if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)):
                vocative_values.extend(
                    (str(kind), str(alias))
                    for alias in aliases
                    if str(alias)
                )
    vocative_values.sort(key=lambda item: len(item[1]), reverse=True)

    cursor = _skip_spaces(original, 0)
    tentative_start = cursor
    discourse: list[TextSpan] = []
    vocatives: list[VocativeFact] = []
    # A bounded token loop handles “喂，那个，助手啊，” without one giant
    # regular expression and retains every exact source span.
    for _ in range(12):
        cursor = _skip_spaces(original, cursor)
        marker = _starts_with_any(original, cursor, discourse_values)
        if marker and _marker_boundary(original, cursor + len(marker)):
            start = cursor
            cursor = _consume_separators(original, cursor + len(marker))
            discourse.append(TextSpan(start, start + len(marker), original[start:start + len(marker)]))
            continue
        matched = False
        for kind, alias in vocative_values:
            if not original[cursor:].casefold().startswith(alias.casefold()):
                continue
            start = cursor
            valid, next_cursor = _vocative_boundary(
                original,
                cursor + len(alias),
                particles=particles,
                request_starters=request_starters,
            )
            if not valid:
                continue
            exact_end = cursor + len(alias)
            particle = _starts_with_any(original, exact_end, particles)
            if particle:
                exact_end += len(particle)
            vocatives.append(VocativeFact(
                kind,
                original[start:exact_end],
                TextSpan(start, exact_end, original[start:exact_end]),
            ))
            cursor = next_cursor
            matched = True
            break
        if matched:
            continue
        break

    # Leading fillers are stripped only when a real vocative was found.  This
    # preserves targets such as “那个网站” and ordinary utterance-initial 嗯.
    if not vocatives:
        cursor = tentative_start
        discourse = []
    cursor = _skip_spaces(original, cursor)
    removed = sorted(
        [*discourse, *(item.span for item in vocatives)],
        key=lambda item: (item.start, item.end),
    )
    return InterpretedUserInput(
        original_text=original,
        dialogue_text=original,
        action_parse_text=original[cursor:].strip(),
        action_parse_offset=cursor,
        vocatives=tuple(vocatives),
        discourse_markers=tuple(discourse),
        removed_spans=tuple(removed),
    )


def _strip_prefixes(value: str, prefixes: Sequence[str]) -> str:
    result = value.strip()
    changed = True
    while changed:
        changed = False
        for prefix in sorted(prefixes, key=len, reverse=True):
            if result.casefold().startswith(prefix.casefold()):
                result = result[len(prefix):].lstrip(" ，,：:、")
                changed = True
                break
    result = re.sub(r"^(?:我想让你|我希望你|能帮我|请帮我|把|将)\s*", "", result, flags=re.I)
    return result.strip()


def _strip_suffixes(value: str, suffixes: Sequence[str]) -> str:
    result = value.strip(" ，,。！？!?；;")
    changed = True
    while changed:
        changed = False
        for suffix in sorted(suffixes, key=len, reverse=True):
            if result.casefold().endswith(suffix.casefold()):
                result = result[:-len(suffix)].rstrip(" ，,。！？!?；;")
                changed = True
                break
    return result.strip()


def _target_looks_web(target: str, full_text: str, config: Mapping[str, Any]) -> bool:
    value = target.strip(" “ ”\"'")
    if not value:
        return any(
            term.casefold() in full_text.casefold()
            for term in _strings(config, "webObjectTerms")
        )
    if re.search(r"https?://|www\.|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,24}", value, re.I):
        return True
    terms = _strings(config, "webObjectTerms")
    if any(term.casefold() in value.casefold() for term in terms):
        return True
    aliases = _strings(config, "knownDestinationAliases")
    normalized = re.sub(r"\s+", "", value).casefold()
    if any(normalized == re.sub(r"\s+", "", alias).casefold() for alias in aliases):
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9._+\- ]{1,100}", value):
        return True
    return bool(re.search(r"(?:站|网|官网|主页)$", value))


def _diagnostic_open_question(text: str) -> bool:
    return bool(re.search(
        r"(?:为什么|为何|怎么|怎么会|原因).{0,16}(?:打不开|无法打开|不能打开|打开不了)|"
        r"(?:打不开|无法打开|不能打开|打开不了).{0,16}(?:为什么|为何|怎么|原因|回事)|"
        r"(?:排查|诊断|分析).{0,12}(?:打不开|打开失败)",
        text,
        re.I,
    ))


def _find_action_verb(text: str, verbs: Sequence[str]) -> tuple[int, int, str] | None:
    matches: list[tuple[int, int, str]] = []
    for verb in sorted(verbs, key=len, reverse=True):
        start = text.casefold().find(verb.casefold())
        if start >= 0:
            matches.append((start, start + len(verb), verb))
    return min(matches, key=lambda item: (item[0], -(item[1] - item[0]))) if matches else None


def _extract_target(
    text: str,
    verb_span: tuple[int, int, str],
    config: Mapping[str, Any],
) -> tuple[str, TextSpan | None]:
    start, end, _verb = verb_span
    prefixes = _strings(config, "politePrefixes")
    suffixes = _strings(config, "politeSuffixes")
    after_raw = text[end:]
    after = _strip_suffixes(_strip_prefixes(after_raw, prefixes), suffixes)
    after = re.sub(r"^(?:默认)?浏览器(?:里|中)?\s*", "", after, flags=re.I)
    revised_target = _explicit_target_revision(after, config)
    if revised_target:
        after = revised_target
    if after and not re.fullmatch(r"(?:一下|下|看看|试试)", after):
        leading_trim = len(after_raw) - len(after_raw.lstrip(" ，,：:、"))
        target_start = end + leading_trim
        found = text.find(after, target_start)
        return after[:240], TextSpan(found, found + len(after), text[found:found + len(after)]) if found >= 0 else None

    before_raw = text[:start]
    # For postfix commands, use the nearest clause rather than the whole turn.
    clause = re.split(r"[，,；;。！？!?]|\bthen\b|然后|接着", before_raw, flags=re.I)[-1]
    clause = re.sub(r"(?:麻烦|拜托|请)?(?:你)?(?:帮我|给我|替我)?\s*$", "", clause)
    target = _strip_suffixes(_strip_prefixes(clause, prefixes), suffixes)
    target = re.sub(r"^(?:把|将)\s*", "", target).strip()
    if not target:
        return "", None
    found = text.rfind(target, 0, start)
    return target[:240], TextSpan(found, found + len(target), text[found:found + len(target)]) if found >= 0 else None


def _explicit_target_revision(text: str, config: Mapping[str, Any]) -> str:
    """Extract the newest target from an explicit user-authored correction.

    This is a small command grammar, not a semantic model guess.  It is used
    only while an external action is being assembled and never turns ordinary
    dialogue into a tool authorization.
    """

    value = str(text or "").strip(" \t\r\n，。！？!?;；:：“”'\"")
    if not value:
        return ""
    markers = sorted(
        _strings(config, "targetCorrectionMarkers"),
        key=len,
        reverse=True,
    )
    matches: list[tuple[int, str]] = []
    folded = value.casefold()
    for marker in markers:
        index = folded.rfind(marker.casefold())
        if index >= 0:
            matches.append((index + len(marker), marker))
    if not matches:
        return ""
    start, _marker = max(matches, key=lambda item: item[0])
    target = value[start:].strip(" \t\r\n，。！？!?;；:：“”'\"")
    target = _strip_suffixes(target, _strings(config, "politeSuffixes"))
    # A correction target is a bounded object, URL or name.  A long clause is
    # dialogue and must not silently become an external side effect.
    if not target or len(target) > 120 or "\n" in target:
        return ""
    return target


def _browser_search_verb_pattern(config: Mapping[str, Any]) -> str:
    """Return configured search verbs with their common ``一下`` forms.

    Search wording by itself belongs to the research/query loop.  The action
    parser only uses these verbs after an explicit browser qualifier has been
    matched, but keeping the verb list config-driven avoids special casing one
    particular wording or target.
    """

    configured = _strings(config, "browserSearchVerbs") or ("搜索", "查找", "搜")
    verbs: set[str] = set()
    for verb in configured:
        if verb.endswith("一下"):
            base = verb[:-2]
            if base:
                verbs.add(base)
        else:
            verbs.add(verb)
    return "|".join(
        re.escape(verb)
        for verb in sorted(verbs, key=lambda item: (-len(item), item.casefold()))
    )


def _explicit_browser_search_match(
    text: str,
    config: Mapping[str, Any],
) -> re.Match[str] | None:
    """Match a browser-visible search request, never a plain research query."""

    return re.search(
        r"^(?:(?:请|麻烦|拜托)?(?:你)?(?:帮我|给我|替我)?\s*)?"
        r"(?:(?:(?:用|使用|在|通过)\s*)(?:默认)?浏览器(?:里|中|内)?\s*"
        r"|(?:(?:打开|启动|唤起)\s*(?:默认)?浏览器(?:里|中|内)?"
        r"(?:后|再|然后)?\s*))"
        rf"(?:{_browser_search_verb_pattern(config)})(?:\s*一下)?\s*(.+)$",
        text,
        re.I,
    )


def parse_action_candidate(
    interpreted: InterpretedUserInput,
    config: Mapping[str, Any],
) -> ActionCandidate:
    text = interpreted.action_parse_text.strip()
    none = ActionCandidate("none", "", "none", 0.0, False, False, "none", ())
    if not text:
        return none
    if _diagnostic_open_question(text):
        return ActionCandidate("none", "", "explicit", 0.99, False, True, "fault_diagnosis", ())

    search_match = _explicit_browser_search_match(text, config)
    if search_match:
        target = _strip_suffixes(
            search_match.group(1),
            _strings(config, "politeSuffixes"),
        ).lstrip(" ，,：:、")
        absolute_start = interpreted.action_parse_offset + search_match.start()
        absolute_end = interpreted.action_parse_offset + search_match.end()
        span = TextSpan(
            absolute_start,
            absolute_end,
            interpreted.original_text[absolute_start:absolute_end],
        )
        return ActionCandidate(
            "browser_search",
            target[:500],
            "explicit",
            0.99,
            False,
            False,
            "direct",
            (span,),
        )

    verbs = _strings(config, "browserActionVerbs")
    verb_span = _find_action_verb(text, verbs)
    if not verb_span:
        return none
    start, end, _verb = verb_span
    prefix_window = text[max(0, start - 14):start]
    suffix_window = text[end:min(len(text), end + 8)]
    negated = bool(
        re.search(r"(?:别|不要|不用|无需|先别|别再|不许)\s*$", prefix_window)
        or re.match(r"\s*(?:不了|失败)", suffix_window)
    )
    target, target_span = _extract_target(text, verb_span, config)
    verb_start = interpreted.action_parse_offset + start
    verb_end = interpreted.action_parse_offset + end
    verb_evidence = TextSpan(
        verb_start,
        verb_end,
        interpreted.original_text[verb_start:verb_end],
    )
    absolute_target_span = None
    if target_span:
        target_start = interpreted.action_parse_offset + target_span.start
        target_end = interpreted.action_parse_offset + target_span.end
        absolute_target_span = TextSpan(
            target_start,
            target_end,
            interpreted.original_text[target_start:target_end],
        )
    evidence = (verb_evidence,) + ((absolute_target_span,) if absolute_target_span else ())
    capability = bool(re.search(r"^(?:你)?(?:能不能|能否|能).{0,12}", text, re.I))
    request_form = "negated" if negated else "capability_request" if capability else "direct"
    if negated:
        action_type: ActionType = "open_browser" if _target_looks_web(target, text, config) else "open_target"
        return ActionCandidate(action_type, target, "explicit", 0.99, True, False, request_form, evidence)
    action_type = "open_browser" if _target_looks_web(target, text, config) else "open_target"
    return ActionCandidate(
        action_type,
        target,
        "implicit" if capability else "explicit",
        0.94 if capability else 0.99,
        False,
        False,
        request_form,
        evidence,
    )


def classify_pending_action_relation(
    pending_action: Mapping[str, Any] | None,
    interpreted: InterpretedUserInput,
    candidate: ActionCandidate,
    config: Mapping[str, Any],
) -> PendingActionDecision:
    pending = pending_action if isinstance(pending_action, Mapping) else {}
    if pending.get("type") != "open_browser":
        return PendingActionDecision("none", "no_active_browser_action")
    text = interpreted.action_parse_text.strip()
    if any(marker in text for marker in _strings(config, "cancelMarkers")):
        return PendingActionDecision("cancel", "explicit_cancel")
    if any(marker in text for marker in _strings(config, "pauseMarkers")):
        return PendingActionDecision("pause", "explicit_pause")
    confirmation_markers = _strings(config, "confirmationMarkers") or (
        "对", "是", "没错", "嗯", "对的", "是的", "确认",
    )
    normalized_confirmation = text.strip(" \t\r\n，。！？!?;；")
    if any(
        normalized_confirmation.casefold() == marker.casefold()
        for marker in confirmation_markers
    ):
        return PendingActionDecision("continue", "confirmation_reply")
    if (
        str(pending.get("status") or "") == "needs_confirmation"
        and re.search(
            r"^(?:对|是|没错|嗯|对的|是的)[，,\s]*(?:它)?(?:就)?是"
            r"(?:一个)?(?:网站|网页|站点)|"
            r"^(?:它)?是(?:一个)?(?:网站|网页|站点)",
            text,
            re.I,
        )
    ):
        return PendingActionDecision("continue", "confirmed_website_type")
    has_source_selector = bool(re.search(
        r"\bS\d+\b|第[一二两三四五六七八九十\d]+(?:个|篇|条)?",
        text,
        re.I,
    ))
    if has_source_selector:
        return PendingActionDecision("continue", "verified_source_selector")
    if re.fullmatch(r"\s*https?://[^\s]+\s*", text, re.I):
        return PendingActionDecision("continue", "direct_url_reply")
    deictic = any(marker in text for marker in _strings(config, "deicticMarkers"))
    has_open_action = bool(re.search(r"打开|访问|进入|跳转|启动|唤起|开一下|进一下", text, re.I))
    if candidate.executable:
        if deictic or not candidate.target_text:
            return PendingActionDecision("continue", "explicit_continuation")
        hint = str(pending.get("targetHint") or "")
        candidate_tokens = set(re.findall(r"[\w\u4e00-\u9fff-]{2,}", candidate.target_text.casefold()))
        hint_tokens = set(re.findall(r"[\w\u4e00-\u9fff-]{2,}", hint.casefold()))
        if candidate_tokens and hint_tokens and candidate_tokens & hint_tokens:
            return PendingActionDecision("continue", "same_target")
        return PendingActionDecision("replace", "new_explicit_target")
    if has_open_action and (
        deictic
        or not candidate.target_text
        or candidate.action_type == "open_target"
    ):
        return PendingActionDecision("continue", "explicit_targetless_continuation")
    if re.search(r"[？?]|为什么|怎么|如何|分析|解释|总结|读取|阅读", text, re.I):
        return PendingActionDecision("unrelated", "new_question_or_task")
    return PendingActionDecision("ambiguous", "insufficient_continuation_evidence")


def prepare_turn(
    raw_text: str,
    config: Mapping[str, Any],
    *,
    pending_action: Mapping[str, Any] | None = None,
    request_id: str = "",
) -> PreparedTurn:
    interpreted = interpret_user_input(raw_text, config)
    candidate = parse_action_candidate(interpreted, config)
    pending = pending_action if isinstance(pending_action, Mapping) else {}
    if pending.get("type") == "open_browser" and candidate.action_type == "none":
        replacement = _explicit_target_revision(interpreted.action_parse_text, config)
        if replacement:
            start = interpreted.original_text.rfind(replacement)
            span = (
                TextSpan(start, start + len(replacement), replacement),
            ) if start >= 0 else ()
            candidate = ActionCandidate(
                "open_browser",
                replacement[:240],
                "explicit",
                0.99,
                False,
                False,
                "target_correction",
                span,
            )
    relation = classify_pending_action_relation(pending_action, interpreted, candidate, config)
    if relation.relation == "cancel":
        route: PreparedRoute = "cancel_pending"
    elif relation.relation == "pause":
        route = "pause_pending"
    elif candidate.executable or relation.relation == "continue":
        has_research = bool(re.search(r"查|搜索|检索|研究", interpreted.action_parse_text, re.I))
        if candidate.action_type == "open_browser" and not candidate.target_text:
            route = "external_action_needs_target"
        else:
            route = "research_plus_external_action" if has_research else "external_action"
    else:
        route = "fast_chat"
    return PreparedTurn(
        request_id=str(request_id or "")[:120],
        interpreted=interpreted,
        action_candidate=candidate,
        pending_relation=relation,
        route=route,
    )
