"""Context-pressure accounting and cheapest-first prompt compaction.

This module deliberately has no model or tool dependencies.  It treats the
raw transcript and receipts as authorities and only produces smaller prompt
views.  Human dialogue is the last thing it is allowed to collapse.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import IntEnum
import json
import re
import time
from typing import Any, Mapping, Sequence

from agent.dialogue_architecture import is_explicit_correction_language
from agent.context_architecture import canonical_turn_id
from agent.fingerprints import runtime_fingerprint


class PressureLevel(IntEnum):
    NO_OP = 0
    TOOL_PAYLOAD_SNIP = 1
    TOOL_RESEARCH_MICROCOMPACT = 2
    CONVERSATION_COLLAPSE = 3
    SESSION_COMPACT = 4


@dataclass(frozen=True)
class ContextBudgetSnapshot:
    model_context_limit: int
    reserved_output_tokens: int
    reserved_system_margin: int
    system_prompt_tokens: int = 0
    persona_tokens: int = 0
    recent_dialogue_tokens: int = 0
    midterm_dialogue_tokens: int = 0
    summary_tokens: int = 0
    long_memory_tokens: int = 0
    relationship_tokens: int = 0
    tool_evidence_tokens: int = 0
    attachment_tokens: int = 0
    other_tokens: int = 0
    total_input_tokens: int = 0
    usable_input_budget: int = 1
    pressure_ratio: float = 0.0
    decision_ms: float = 0.0
    level: PressureLevel = PressureLevel.NO_OP
    tokens_saved: int = 0
    fits_budget: bool = True
    over_budget_tokens: int = 0
    applied_levels: tuple[PressureLevel, ...] = ()
    applied_stages: tuple[str, ...] = ()
    stage_input_tokens: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["level"] = self.level.name.lower()
        value["applied_levels"] = [level.name.lower() for level in self.applied_levels]
        value["applied_stages"] = list(self.applied_stages)
        value["stage_input_tokens"] = list(self.stage_input_tokens)
        value["pressure_ratio"] = round(self.pressure_ratio, 6)
        value["decision_ms"] = round(self.decision_ms, 3)
        return value


@dataclass(frozen=True)
class CompactableToolPayload:
    receipt_id: str
    tool_name: str
    original_size_chars: int
    original_token_estimate: int
    compact_text: str
    artifact_ref: str | None
    source_ids: tuple[str, ...]
    was_truncated: bool
    can_refetch: bool


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    item_type: str
    source_turn_ids: tuple[str, ...]
    token_estimate: int
    recency_score: float
    semantic_relevance: float
    conversational_importance: float
    contains_user_correction: bool
    contains_pending_question: bool
    contains_unresolved_reference: bool
    contains_shared_episode: bool
    contains_tool_process_only: bool
    collapse_allowed: bool
    contains_explicit_memory: bool = False
    contains_relationship_boundary: bool = False
    is_recent_anchor: bool = False


_TOOL_PROCESS_RE = re.compile(
    r"search query|raw results|html|ocr|vlm intermediate|中间日志|工具过程",
    re.I,
)

_TOOL_JSON_MARKER_RE = re.compile(r"\[(?:工具结果[^\]]*|联网研究[^\]]*)\]", re.I)
_IDENTITY_KEY_NAMES = {
    "receiptprojection",
    "receiptid",
    "requestid",
    "toolcallid",
    "researchid",
    "sourcebatchid",
    "turnid",
    "sourceid",
    "sourceids",
    "artifactref",
    "artifactrefs",
}
_LOW_VALUE_KEY_NAMES = {
    "raw",
    "rawpayload",
    "rawresult",
    "rawresults",
    "html",
    "body",
    "content",
    "text",
    "snippet",
    "transcript",
    "debug",
    "log",
    "logs",
}
_PROTECTION_TAGS = {
    "correction": "correction",
    "pending": "pending",
    "pendingquestion": "pending",
    "unresolved": "unresolved",
    "unresolvedreference": "unresolved",
    "sharedepisode": "sharedEpisode",
    "explicitmemory": "explicitMemory",
    "relationshipboundary": "relationshipBoundary",
    "recentanchor": "recentAnchor",
    "toolprocess": "toolProcess",
}


def estimate_tokens(value: Any) -> int:
    """Conservative local token estimate without adding a tokenizer dependency."""

    text = str(value or "")
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    remainder = max(0, len(text) - cjk)
    # CJK tokenizers are commonly near one character/token; Latin prose is
    # closer to four characters/token.  Punctuation/JSON overhead is covered
    # by the conservative divisor below.
    return max(1, cjk + (remainder + 2) // 3)


def pressure_level(ratio: float) -> PressureLevel:
    if ratio < 0.72:
        return PressureLevel.NO_OP
    if ratio < 0.84:
        return PressureLevel.TOOL_PAYLOAD_SNIP
    if ratio < 0.94:
        return PressureLevel.TOOL_RESEARCH_MICROCOMPACT
    if ratio < 1.05:
        return PressureLevel.CONVERSATION_COLLAPSE
    return PressureLevel.SESSION_COMPACT


def _message_bucket(text: str, index: int, total: int) -> str:
    if "[工具结果" in text or "[联网研究后的最终回答任务" in text:
        return "tool_evidence"
    if "[附件" in text or "[本机文件" in text:
        return "attachment"
    if "[短期对话锚点" in text or index >= max(0, total - 4):
        return "recent_dialogue"
    if "[持久会话上下文" in text:
        return "midterm_dialogue"
    if "较早会话" in text or "compaction:" in text:
        return "summary"
    if "长期记忆" in text:
        return "long_memory"
    if "关系" in text or "好感" in text or "尊严边界" in text:
        return "relationship"
    if index == 0:
        return "system_prompt"
    if "人格" in text or "amadeus" in text.lower():
        return "persona"
    return "other"


def budget_snapshot(
    messages: Sequence[Mapping[str, Any]],
    *,
    model_context_limit: int,
    reserved_output_tokens: int,
    reserved_system_margin: int,
) -> ContextBudgetSnapshot:
    started = time.perf_counter()
    totals = {
        "system_prompt": 0,
        "persona": 0,
        "recent_dialogue": 0,
        "midterm_dialogue": 0,
        "summary": 0,
        "long_memory": 0,
        "relationship": 0,
        "tool_evidence": 0,
        "attachment": 0,
        "other": 0,
    }
    total_messages = len(messages)
    for index, message in enumerate(messages):
        text = str(message.get("content") or "")
        totals[_message_bucket(text, index, total_messages)] += estimate_tokens(text) + 4
    total_input = sum(totals.values())
    usable = max(
        1,
        int(model_context_limit)
        - max(0, int(reserved_output_tokens))
        - max(0, int(reserved_system_margin)),
    )
    ratio = total_input / usable
    over_budget = max(0, total_input - usable)
    return ContextBudgetSnapshot(
        model_context_limit=max(1, int(model_context_limit)),
        reserved_output_tokens=max(0, int(reserved_output_tokens)),
        reserved_system_margin=max(0, int(reserved_system_margin)),
        system_prompt_tokens=totals["system_prompt"],
        persona_tokens=totals["persona"],
        recent_dialogue_tokens=totals["recent_dialogue"],
        midterm_dialogue_tokens=totals["midterm_dialogue"],
        summary_tokens=totals["summary"],
        long_memory_tokens=totals["long_memory"],
        relationship_tokens=totals["relationship"],
        tool_evidence_tokens=totals["tool_evidence"],
        attachment_tokens=totals["attachment"],
        other_tokens=totals["other"],
        total_input_tokens=total_input,
        usable_input_budget=usable,
        pressure_ratio=ratio,
        decision_ms=(time.perf_counter() - started) * 1000,
        level=pressure_level(ratio),
        fits_budget=over_budget == 0,
        over_budget_tokens=over_budget,
    )


def _identity_values(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    receipts = tuple(dict.fromkeys(re.findall(
        r'(?i)(?:receiptId|requestId|toolCallId|researchId|sourceBatchId|turnId)'
        r'["\'\s:=]+([\w.:/-]{2,200})',
        text,
    )))
    sources = tuple(dict.fromkeys(re.findall(r"\bS\d{1,4}\b", text)))
    return receipts, sources


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _is_identity_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return (
        normalized in _IDENTITY_KEY_NAMES
        or ("source" in normalized and normalized.endswith(("id", "ids")))
        or ("artifact" in normalized and normalized.endswith(("ref", "refs")))
    )


def _source_ids_from_value(value: Any) -> tuple[str, ...]:
    found: list[str] = []

    def add(candidate: Any) -> None:
        text = str(candidate or "").strip()
        if text and text not in found:
            found.append(text[:200])

    def visit(candidate: Any, *, in_source: bool = False) -> None:
        if isinstance(candidate, Mapping):
            for key, nested in candidate.items():
                normalized = _normalized_key(key)
                source_context = in_source or "source" in normalized
                if normalized in {"sourceid", "sourceids"}:
                    if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                        for item in nested:
                            add(item)
                    else:
                        add(nested)
                elif normalized == "id" and in_source:
                    add(nested)
                visit(nested, in_source=source_context)
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for nested in candidate:
                visit(nested, in_source=in_source)

    visit(value)
    return tuple(found)


def _identity_projection(value: Any) -> dict[str, Any]:
    projection: dict[str, Any] = {}

    def visit(candidate: Any, *, path: tuple[str, ...] = ()) -> None:
        if isinstance(candidate, Mapping):
            for key, nested in candidate.items():
                key_text = str(key)
                normalized = _normalized_key(key_text)
                if normalized == "receiptprojection" and "receiptProjection" not in projection:
                    projection["receiptProjection"] = nested
                elif _is_identity_key(key_text) and key_text not in projection:
                    projection[key_text] = nested
                elif normalized == "id" and any("source" in part for part in path):
                    projection.setdefault("sourceIds", [])
                    source_ids = projection["sourceIds"]
                    if isinstance(source_ids, list) and nested not in source_ids:
                        source_ids.append(nested)
                visit(nested, path=(*path, normalized))
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for nested in candidate:
                visit(nested, path=path)

    visit(value)
    source_ids = _source_ids_from_value(value)
    if source_ids:
        existing = projection.get("sourceIds")
        combined = list(existing) if isinstance(existing, list) else []
        for source_id in source_ids:
            if source_id not in combined:
                combined.append(source_id)
        projection["sourceIds"] = combined
    return projection


def _identity_projection_from_text(value: str) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for key in (
        "receiptId",
        "requestId",
        "toolCallId",
        "researchId",
        "sourceBatchId",
        "turnId",
        "artifactRef",
    ):
        match = re.search(
            rf'(?i)["\']?{key}["\']?\s*[:=]\s*["\']?([^"\'\s,}}\]]{{2,240}})',
            value,
        )
        if match:
            projection[key] = match.group(1)
    sources = tuple(dict.fromkeys(re.findall(r"\bS\d{1,6}\b", value)))
    if sources:
        projection["sourceIds"] = list(sources)
    return projection


def _tool_json_payload(value: str) -> tuple[str, Any, str, str] | None:
    marker = _TOOL_JSON_MARKER_RE.search(value)
    if marker is None:
        return None
    remainder = value[marker.end():].lstrip()
    decoder = json.JSONDecoder()
    if remainder.startswith(("{", "[")):
        try:
            payload, consumed = decoder.raw_decode(remainder)
            return marker.group(0), payload, "", remainder[consumed:].strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    # Research prompts carry trusted instructions before a final JSON source
    # array.  Parse that terminal value while retaining the instruction block
    # as an explicit field in the compact JSON view.
    starts = [match.start() for match in re.finditer(r"(?m)^[ \t]*[\[{]", remainder)]
    for start in reversed(starts):
        candidate = remainder[start:].lstrip()
        whitespace = len(remainder[start:]) - len(candidate)
        try:
            payload, consumed = decoder.raw_decode(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        suffix = candidate[consumed:].strip()
        if suffix:
            continue
        preamble = remainder[: start + whitespace].strip()
        return marker.group(0), payload, preamble, ""
    return None


def _snipped_json_string(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = f"…[低价值工具正文已截短:{len(value)} chars]…"
    available = max(0, limit - len(marker))
    head = available * 2 // 3
    tail = available - head
    return value[:head] + marker + (value[-tail:] if tail else "")


def _compact_json_value(
    value: Any,
    *,
    string_limit: int,
    list_limit: int,
    key: str = "",
) -> Any:
    if _is_identity_key(key):
        return value
    normalized = _normalized_key(key)
    if isinstance(value, Mapping):
        return {
            str(nested_key): _compact_json_value(
                nested,
                string_limit=string_limit,
                list_limit=list_limit,
                key=str(nested_key),
            )
            for nested_key, nested in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        selected = list(value[:list_limit])
        compacted = [
            _compact_json_value(
                nested,
                string_limit=string_limit,
                list_limit=list_limit,
                key=key,
            )
            for nested in selected
        ]
        omitted = len(value) - len(selected)
        if omitted > 0:
            omitted_projection = _identity_projection(value[len(selected):])
            compacted.append({
                "_contextPressureOmittedItems": omitted,
                **({"identityProjection": omitted_projection} if omitted_projection else {}),
            })
        return compacted
    if isinstance(value, str):
        effective_limit = max(48, string_limit // 2) if normalized in _LOW_VALUE_KEY_NAMES else string_limit
        return _snipped_json_string(value, limit=effective_limit)
    return value


def _json_compact_view(
    payload: Any,
    *,
    original_characters: int,
    limit: int,
    prompt_preamble: str = "",
    prompt_suffix: str = "",
) -> Any:
    attempts = (
        (max(256, min(2_048, limit // 3)), 16),
        (max(128, min(1_024, limit // 5)), 8),
        (96, 4),
        (48, 1),
    )
    for string_limit, list_limit in attempts:
        compacted = _compact_json_value(
            payload,
            string_limit=string_limit,
            list_limit=list_limit,
        )
        metadata = {
            "payloadSnipped": True,
            "message": "低价值工具正文已截短；完整内容仍在原始工具结果/归档",
            "originalCharacters": original_characters,
            "authorityUnchanged": True,
        }
        if isinstance(compacted, Mapping):
            candidate: Any = {**compacted, "_contextPressure": metadata}
        else:
            candidate = {"payload": compacted, "_contextPressure": metadata}
        if prompt_preamble:
            candidate["_promptPreamble"] = prompt_preamble
        if prompt_suffix:
            candidate["_promptSuffix"] = prompt_suffix
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) <= limit:
            return candidate

    projection = _identity_projection(payload)
    candidate = {
        **projection,
        "_contextPressure": {
            "payloadSnipped": True,
            "projectionOnly": True,
            "message": "低价值工具正文已截短；完整内容仍在原始工具结果/归档",
            "originalCharacters": original_characters,
            "authorityUnchanged": True,
        },
    }
    if prompt_preamble:
        candidate["_promptPreamble"] = prompt_preamble
    if prompt_suffix:
        candidate["_promptSuffix"] = prompt_suffix
    return candidate


def _artifact_ref_from_projection(projection: Mapping[str, Any]) -> str | None:
    for key, value in projection.items():
        if "artifact" in _normalized_key(key) and value:
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return str(value[0])[:500] if value else None
            return str(value)[:500]
    return None


def _receipt_id_from_projection(projection: Mapping[str, Any]) -> str:
    direct = projection.get("receiptId")
    if direct:
        return str(direct)[:500]
    nested = projection.get("receiptProjection")
    if isinstance(nested, Mapping) and nested.get("receiptId"):
        return str(nested["receiptId"])[:500]
    return ""


def compact_tool_prompt(text: str, *, max_characters: int) -> CompactableToolPayload:
    value = str(text or "")
    limit = max(900, int(max_characters))
    receipts, sources = _identity_values(value)
    if len(value) <= limit:
        return CompactableToolPayload(
            receipt_id=receipts[0] if receipts else "",
            tool_name="web_research" if "联网研究" in value else "tool_result",
            original_size_chars=len(value),
            original_token_estimate=estimate_tokens(value),
            compact_text=value,
            artifact_ref=None,
            source_ids=sources,
            was_truncated=False,
            can_refetch=bool(sources or receipts),
        )
    parsed = _tool_json_payload(value)
    if parsed is not None:
        marker, payload, prompt_preamble, prompt_suffix = parsed
        projection = _identity_projection(payload)
        sources = tuple(dict.fromkeys((*sources, *_source_ids_from_value(payload))))
        compact_payload = _json_compact_view(
            payload,
            original_characters=len(value),
            limit=max(256, limit - len(marker) - 1),
            prompt_preamble=prompt_preamble,
            prompt_suffix=prompt_suffix,
        )
        compact = marker + "\n" + json.dumps(
            compact_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        projection = _identity_projection_from_text(value)
        fallback = {
            "receiptProjection": projection,
            "_contextPressure": {
                "payloadSnipped": True,
                "parseFailed": True,
                "projectionOnly": True,
                "message": "低价值工具正文已截短；完整内容仍在原始工具结果/归档",
                "originalCharacters": len(value),
                "authorityUnchanged": True,
            },
        }
        marker = "[联网研究]" if "联网研究" in value else "[工具结果]"
        compact = marker + "\n" + json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
        fallback_sources = projection.get("sourceIds")
        if isinstance(fallback_sources, Sequence) and not isinstance(fallback_sources, (str, bytes)):
            sources = tuple(dict.fromkeys((*sources, *(str(item) for item in fallback_sources))))
    artifact_ref = _artifact_ref_from_projection(projection)
    receipt_id = _receipt_id_from_projection(projection)
    return CompactableToolPayload(
        receipt_id=receipt_id or (receipts[0] if receipts else ""),
        tool_name="web_research" if "联网研究" in value else "tool_result",
        original_size_chars=len(value),
        original_token_estimate=estimate_tokens(value),
        compact_text=compact,
        artifact_ref=artifact_ref or (("source-batch:" + sources[0]) if sources else None),
        source_ids=sources,
        was_truncated=True,
        can_refetch=bool(artifact_ref or sources or receipts),
    )


_AUTHORITY_LINE_PREFIXES = (
    "[source_turns]",
    "[required_acts]",
    "[unresolved]",
    "[protection_reasons]",
    "[protected_claim]",
)


def bounded_text_preserving_authority(
    text: str,
    *,
    max_characters: int,
    ordinary_tail_lines: int = 12,
) -> str:
    """Bound a prompt view without cutting typed authority/provenance lines.

    If authority metadata alone exceeds the target, return it intact and let
    the final budget invariant reject the provider call. Silent truncation is
    never a valid way to make a correction or source identity fit.
    """

    value = str(text or "")
    if len(value) <= max_characters:
        return value
    lines = value.splitlines()
    structural: list[str] = []
    protected: list[str] = []
    ordinary: list[str] = []
    for line in lines:
        if line.startswith(_AUTHORITY_LINE_PREFIXES):
            protected.append(line)
        elif line.startswith("[") and not structural:
            structural.append(line)
        else:
            ordinary.append(line)
    marker = "[context-pressure: older low-value dialogue collapsed]"
    authority_lines = list(dict.fromkeys([*structural, *protected]))
    authority = "\n".join(authority_lines)
    remaining = max(0, int(max_characters) - len(authority) - len(marker) - 2)
    selected_tail: list[str] = []
    for line in reversed(ordinary[-max(0, int(ordinary_tail_lines)):]):
        candidate = "\n".join(reversed([line, *selected_tail]))
        if len(candidate) > remaining:
            continue
        selected_tail.insert(0, line)
    parts = [item for item in (authority, "\n".join(selected_tail), marker) if item]
    return "\n".join(parts)


def _collapse_midterm_prompt(text: str, *, max_characters: int) -> str:
    return bounded_text_preserving_authority(
        text,
        max_characters=max_characters,
    )


def _apply_pressure_stage(
    messages: list[dict[str, Any]],
    level: PressureLevel,
) -> None:
    tool_limit = {
        PressureLevel.TOOL_PAYLOAD_SNIP: 18_000,
        PressureLevel.TOOL_RESEARCH_MICROCOMPACT: 10_000,
        PressureLevel.CONVERSATION_COLLAPSE: 7_000,
        PressureLevel.SESSION_COMPACT: 4_500,
    }[level]
    for item in messages:
        text = str(item.get("content") or "")
        if _TOOL_JSON_MARKER_RE.search(text):
            item["content"] = compact_tool_prompt(
                text,
                max_characters=tool_limit,
            ).compact_text

    if level >= PressureLevel.CONVERSATION_COLLAPSE:
        mid_limit = 6_000 if level == PressureLevel.CONVERSATION_COLLAPSE else 3_600
        for item in messages:
            text = str(item.get("content") or "")
            if "[持久会话上下文" in text:
                item["content"] = _collapse_midterm_prompt(
                    text,
                    max_characters=mid_limit,
                )

    if level >= PressureLevel.SESSION_COMPACT:
        for item in messages:
            text = str(item.get("content") or "")
            if "较早会话" in text or "[长期记忆检索]" in text or "compaction:" in text:
                item["content"] = _collapse_midterm_prompt(
                    text,
                    max_characters=2_400,
                )


def apply_prompt_pressure(
    messages: Sequence[Mapping[str, Any]],
    *,
    model_context_limit: int,
    reserved_output_tokens: int,
    reserved_system_margin: int = 2048,
) -> tuple[list[dict[str, Any]], ContextBudgetSnapshot]:
    """Shrink a derived prompt view without mutating source turns or receipts."""

    started = time.perf_counter()
    original = [dict(item) for item in messages]
    before = budget_snapshot(
        original,
        model_context_limit=model_context_limit,
        reserved_output_tokens=reserved_output_tokens,
        reserved_system_margin=reserved_system_margin,
    )
    if before.level == PressureLevel.NO_OP:
        return original, before

    result = [dict(item) for item in original]
    applied_levels: list[PressureLevel] = []
    stage_input_tokens: list[int] = []
    after = before
    for level in (
        PressureLevel.TOOL_PAYLOAD_SNIP,
        PressureLevel.TOOL_RESEARCH_MICROCOMPACT,
        PressureLevel.CONVERSATION_COLLAPSE,
        PressureLevel.SESSION_COMPACT,
    ):
        _apply_pressure_stage(result, level)
        applied_levels.append(level)
        after = budget_snapshot(
            result,
            model_context_limit=model_context_limit,
            reserved_output_tokens=reserved_output_tokens,
            reserved_system_margin=reserved_system_margin,
        )
        stage_input_tokens.append(after.total_input_tokens)
        if after.fits_budget:
            break

    return result, replace(
        after,
        level=applied_levels[-1],
        tokens_saved=max(0, before.total_input_tokens - after.total_input_tokens),
        applied_levels=tuple(applied_levels),
        applied_stages=tuple(level.name.lower() for level in applied_levels),
        stage_input_tokens=tuple(stage_input_tokens),
        decision_ms=(time.perf_counter() - started) * 1000,
    )


def _structured_protection_tags(value: Any) -> set[str]:
    tags: set[str] = set()

    def enabled(candidate: Any) -> bool:
        if isinstance(candidate, str):
            return candidate.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
        return bool(candidate)

    def visit(candidate: Any) -> None:
        if isinstance(candidate, Mapping):
            for key, nested in candidate.items():
                normalized = _normalized_key(key)
                canonical = _PROTECTION_TAGS.get(normalized)
                if canonical is not None:
                    if enabled(nested):
                        tags.add(canonical)
                    continue
                visit(nested)
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for nested in candidate:
                visit(nested)
        elif isinstance(candidate, str):
            for part in re.split(r"[,;|\s]+", candidate):
                canonical = _PROTECTION_TAGS.get(_normalized_key(part))
                if canonical is not None:
                    tags.add(canonical)

    visit(value)
    return tags


def _semantic_terms(value: str) -> set[str]:
    text = str(value or "").casefold()
    terms = {
        item for item in re.findall(r"[a-z0-9_]{2,}", text) if item
    }
    for span in re.findall(r"[\u3400-\u9fff]{2,}", text):
        terms.update(span[index:index + 2] for index in range(len(span) - 1))
        terms.update(span[index:index + 3] for index in range(max(0, len(span) - 2)))
    return terms


def context_item_from_turn(
    turn: Mapping[str, Any],
    *,
    index: int,
    total: int,
    semantic_query: str = "",
) -> ContextItem:
    turn_id = canonical_turn_id(
        turn,
        index,
    )
    if not turn_id:
        turn_id = "turn_" + runtime_fingerprint(str(dict(turn)), length=16)
    text = "\n".join((str(turn.get("user") or ""), str(turn.get("assistant") or "")))
    has_structured_protection = "contextProtection" in turn and turn.get("contextProtection") is not None
    if has_structured_protection:
        protection_tags = _structured_protection_tags(turn.get("contextProtection"))
        correction = "correction" in protection_tags
        pending = "pending" in protection_tags
        unresolved = "unresolved" in protection_tags
        shared = "sharedEpisode" in protection_tags
        explicit_memory = "explicitMemory" in protection_tags
        relationship_boundary = "relationshipBoundary" in protection_tags
        tool_only = "toolProcess" in protection_tags
        recent_anchor = (
            "recentAnchor" in protection_tags
            or index >= max(0, total - 6)
        )
    else:
        correction = is_explicit_correction_language(turn.get("user"))
        pending = bool(turn.get("pendingAssistantQuestion"))
        unresolved = bool(
            turn.get("unresolvedReferenceIds")
            or turn.get("clarificationRequired")
        )
        shared = bool(
            turn.get("sharedEpisodeId")
            or turn.get("activeEpisodeId")
        )
        explicit_memory = False
        relationship_boundary = False
        tool_only = bool(_TOOL_PROCESS_RE.search(text)) and not str(turn.get("user") or "").strip()
        recent_anchor = index >= max(0, total - 6)
    hard_protected = (
        correction
        or pending
        or unresolved
        or shared
        or explicit_memory
        or relationship_boundary
        or recent_anchor
    )
    query_terms = _semantic_terms(semantic_query)
    turn_terms = _semantic_terms(text)
    semantic_relevance = (
        len(query_terms & turn_terms) / max(1, min(len(query_terms), len(turn_terms)))
        if query_terms and turn_terms else 0.0
    )
    collapse_allowed = not hard_protected
    return ContextItem(
        item_id=turn_id[:120],
        item_type="tool_process" if tool_only else "conversation_turn",
        source_turn_ids=(turn_id[:120],),
        token_estimate=estimate_tokens(json.dumps(dict(turn), ensure_ascii=False)),
        recency_score=(index + 1) / max(1, total),
        semantic_relevance=semantic_relevance,
        conversational_importance=(
            1.0 if hard_protected else
            0.0 if tool_only else
            min(0.95, 0.2 + semantic_relevance * 0.7)
        ),
        contains_user_correction=correction,
        contains_pending_question=pending,
        contains_unresolved_reference=unresolved,
        contains_shared_episode=shared,
        contains_tool_process_only=tool_only,
        collapse_allowed=collapse_allowed,
        contains_explicit_memory=explicit_memory,
        contains_relationship_boundary=relationship_boundary,
        is_recent_anchor=recent_anchor,
    )


def select_conversation_collapse_indexes(
    turns: Sequence[Mapping[str, Any]],
    *,
    batch_size: int = 16,
    target_tokens_to_free: int | None = None,
    protected_turn_ids: Sequence[str] = (),
    query_text: str = "",
    min_recent_turns: int = 6,
    max_recent_turns: int = 18,
    recent_token_budget: int = 8_000,
) -> tuple[int, ...]:
    """Select one continuous oldest prefix for conversation compaction.

    Semantic relevance belongs to recall, not to storage layout.  The previous
    selector ranked individual turns and could remove indexes such as 1, 3 and
    4 while leaving 0 and 2 behind.  That broke the narrative sequence.  This
    function now returns only ``range(0, cut)`` and keeps a token-aware recent
    suffix plus unresolved live anchors verbatim.

    ``query_text`` remains accepted for older callers, but intentionally has no
    influence on the cut point.
    """

    dynamic_protection = {
        str(item).strip() for item in protected_turn_ids if str(item).strip()
    }
    limit = max(0, int(batch_size))
    target = None if target_tokens_to_free is None else max(0, int(target_tokens_to_free))
    if target == 0 or limit == 0 or not turns:
        return ()

    total = len(turns)
    minimum = min(total, max(1, int(min_recent_turns)))
    maximum = min(total, max(minimum, int(max_recent_turns)))
    budget = max(1, int(recent_token_budget))
    suffix_tokens = 0
    retained = 0
    for index in range(total - 1, -1, -1):
        token_estimate = estimate_tokens(
            json.dumps(dict(turns[index]), ensure_ascii=False, separators=(",", ":"))
        )
        if retained >= minimum and (
            retained >= maximum or suffix_tokens + token_estimate > budget
        ):
            break
        suffix_tokens += token_estimate
        retained += 1
    raw_suffix_start = total - retained

    boundary = raw_suffix_start
    for index, turn in enumerate(turns[:raw_suffix_start]):
        turn_id = canonical_turn_id(turn, index)
        protection = turn.get("contextProtection")
        structured = isinstance(protection, Mapping)
        tags = _structured_protection_tags(protection) if structured else set()
        has_exact_claim = bool(
            structured
            and isinstance(protection.get("claims"), (list, tuple))
            and any(str(item).strip() for item in protection.get("claims", ()))
        )
        live_anchor = bool(
            turn_id in dynamic_protection
            or "recentAnchor" in tags
        )
        # Old records without typed claims cannot prove that their authority
        # survived a summary.  Keep them raw instead of guessing.  New typed
        # corrections/episodes may compact because their exact user claim is
        # copied into and validated inside the artifact header.
        legacy_or_unverifiable_anchor = bool(
            (not structured and not context_item_from_turn(
                turn,
                index=index,
                total=total,
            ).collapse_allowed)
            or (
                structured
                and tags.intersection({
                    "correction", "sharedEpisode", "explicitMemory",
                    "relationshipBoundary",
                })
                and not has_exact_claim
            )
        )
        if live_anchor or legacy_or_unverifiable_anchor:
            boundary = index
            break

    selected: list[int] = []
    selected_tokens = 0
    for index in range(min(boundary, limit)):
        selected.append(index)
        selected_tokens += estimate_tokens(
            json.dumps(dict(turns[index]), ensure_ascii=False, separators=(",", ":"))
        )
        if target is not None and selected_tokens >= target:
            break
    return tuple(selected)


def conversation_working_set_pressure(
    turns: Sequence[Mapping[str, Any]],
    *,
    budget_tokens: int = 24_000,
) -> float:
    total = sum(
        estimate_tokens(json.dumps(dict(turn), ensure_ascii=False, separators=(",", ":")))
        for turn in turns
    )
    return total / max(1, int(budget_tokens))
