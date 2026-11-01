"""Context-pressure accounting and cheapest-first prompt compaction.

This module deliberately has no model or tool dependencies.  It treats the
raw transcript and receipts as authorities and only produces smaller prompt
views.  Human dialogue is the last thing it is allowed to collapse.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from enum import IntEnum
import json
import re
import time
from typing import Any, Callable, Mapping, Sequence

from agent.context_architecture import canonical_turn_id
from agent.fingerprints import runtime_fingerprint
from agent.prompt_views import (
    HISTORY_RECORDS_MARKER, HISTORY_COVERAGE_MARKER,
    history_views, history_identity, deduplicate_prompt_views,
)


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
    # ``model_context_limit`` is the provider hard limit.  Compaction may be
    # triggered earlier by a configured target, but fitting that target is not
    # required when the hard input budget is already met.
    compact_threshold_tokens: int = 0
    compact_threshold_exceeded: bool = False
    # Input/output diagnostics are structural only.  Fingerprints are used to
    # correlate a pressure decision without retaining prompt text in logs.
    input_message_count: int = 0
    output_message_count: int = 0
    input_fingerprint: str = ""
    output_fingerprint: str = ""
    input_source_counts: tuple[tuple[str, int], ...] = ()
    output_source_counts: tuple[tuple[str, int], ...] = ()
    diagnostic_decision: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["level"] = self.level.name.lower()
        value["applied_levels"] = [level.name.lower() for level in self.applied_levels]
        value["applied_stages"] = list(self.applied_stages)
        value["stage_input_tokens"] = list(self.stage_input_tokens)
        value["input_source_counts"] = dict(self.input_source_counts)
        value["output_source_counts"] = dict(self.output_source_counts)
        value["inputHash"] = self.input_fingerprint
        value["outputHash"] = self.output_fingerprint
        value["inputCount"] = self.input_message_count
        value["outputCount"] = self.output_message_count
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


# regex-purpose: protocol
_TOOL_JSON_MARKER_RE = re.compile(
    r"\[(?:工具结果[^\]]*|联网研究[^\]]*|已执行工具的证据[^\]]*|"
    r"工具证据身份投影[^\]]*|通用联网证据综合任务[^\]]*|"
    r"联网研究后的最终回答任务[^\]]*|体育联网证据综合任务[^\]]*)\]",
    re.I,
)
# regex-purpose: protocol
_COGNITIVE_TOOL_MARKER_RE = re.compile(
    r"\[(?:已执行工具的证据[^\]]*|工具证据身份投影[^\]]*|"
    r"通用联网证据综合任务[^\]]*|联网研究后的最终回答任务[^\]]*|"
    r"体育联网证据综合任务[^\]]*)\]",
    re.I,
)
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
    "owner",
    "ownerid",
    "ownerturnid",
    "sourceowner",
    "receipt",
    "receipts",
    "toolreceipt",
}
_PROTECTED_STATUS_KEY_NAMES = {
    "status",
    "evidencestatus",
    "executionstatus",
    "actionreceipt",
    "authorization",
    "authorizations",
    "authority",
    "sourcetype",
    "evidencerefs",
    "evidenceids",
    "fetchstatus",
    "qualityflags",
    "taskstatus",
    "outcomestatus",
}
_COGNITIVE_CONTROL_KEY_NAMES = {
    "task",
    "preferences",
    "currentinput",
    "currentuserinput",
    "currentuserrequest",
    "outcome",
    "directive",
    "constraints",
    "correction",
    "corrections",
    "correctionhistory",
    "authorization",
    "authorizations",
    "requiredacts",
    "permissions",
    "permission",
}
_COGNITIVE_PACKET_KEY_NAMES = {
    "activebeliefs",
    "context",
    "contextcorrection",
    "currentstateevidencerequirement",
    "currentuserrequest",
    "dialoguewaiting",
    "evidence",
    "failedrequests",
    "preferences",
    "proactivecontrol",
    "references",
    "successcriteriacatalog",
    "task",
}
_COGNITIVE_BODY_KEY_NAMES = {
    "bodyexcerpt",
    "bodytext",
    "evidencechunktext",
    "rawtext",
}
_TOOL_FACT_LIST_LIMITS = {
    "contacts": 8,
    "recentactions": 4,
    "recentevents": 4,
    "messages": 8,
    "links": 8,
}
_TOOL_EVIDENCE_SOURCE_TYPES = {
    "tool",
    "web",
    "visual",
    "attachment",
    "tool_evidence",
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

# These names are deliberately accepted as metadata rather than inferred from
# the prose of the prompt.  Callers that already know the source can pass an
# out-of-band ``source_metadata`` sequence to the public budgeting APIs; the
# same names are also understood when a message carries metadata for older
# integrations.
_SOURCE_METADATA_KEYS = (
    "contextSource",
    "context_source",
    "_contextSource",
    "_context_source",
    "pressureSource",
    "pressure_source",
    "_pressureSource",
    "_pressure_source",
    "sourceType",
    "source_type",
    "origin",
    "conversationOrigin",
    "conversation_origin",
    "messageOrigin",
    "message_origin",
    "toolOrigin",
    "tool_origin",
    "source",
)
_SOURCE_ALIASES = {
    "tool": "tool_evidence",
    "toolresult": "tool_evidence",
    "toolresults": "tool_evidence",
    "toolevidence": "tool_evidence",
    "cognitivetool": "tool_evidence",
    "cognitiveevidence": "tool_evidence",
    "webevidence": "tool_evidence",
    "research": "tool_evidence",
    "webresearch": "tool_evidence",
    "web": "tool_evidence",
    "ocr": "tool_evidence",
    "visual": "tool_evidence",
    "html": "tool_evidence",
    "attachment": "attachment",
    "file": "attachment",
    "history": "recent_dialogue",
    "dialogue": "recent_dialogue",
    "recentdialogue": "recent_dialogue",
    "midterm": "midterm_dialogue",
    "midtermdialogue": "midterm_dialogue",
    "summary": "summary",
    "compaction": "summary",
    "memory": "long_memory",
    "longmemory": "long_memory",
    "relationship": "relationship",
    "persona": "persona",
    "system": "system_prompt",
    "systemprompt": "system_prompt",
    "fixedrules": "system_prompt",
    "conversation": "recent_dialogue",
    "chat": "recent_dialogue",
    "user": "recent_dialogue",
    "assistant": "recent_dialogue",
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


def _content_text(value: Any) -> str:
    """Return a stable textual view for both string and multimodal content."""

    if isinstance(value, str):
        return value
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        # Transport base64 is not text fed through the model tokenizer. Keep
        # its data out of text accounting; reserve visual tokens separately.
        value = [({"type": part.get("type"), "visualInput": True}
                  if isinstance(part, Mapping) and part.get("type") in {"image_url", "input_image"}
                  else part) for part in value]
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _normalized_source(value: Any) -> str:
    normalized = _normalized_key(value)
    return _SOURCE_ALIASES.get(normalized, str(value or "").strip().lower())


def _explicit_message_source(message: Mapping[str, Any] | None) -> str:
    if not isinstance(message, Mapping):
        return ""
    role = str(message.get("role") or "").strip().lower()
    if role in {"tool", "function"}:
        return "tool_evidence"
    for key in _SOURCE_METADATA_KEYS:
        value = message.get(key)
        if value not in (None, "", False):
            source = _normalized_source(value)
            if source:
                return source
    for container_key in ("contextMetadata", "context_metadata", "metadata", "meta"):
        metadata = message.get(container_key)
        if not isinstance(metadata, Mapping):
            continue
        for key in _SOURCE_METADATA_KEYS:
            value = metadata.get(key)
            if value not in (None, "", False):
                source = _normalized_source(value)
                if source:
                    return source
    return ""


def _value_has_structured_tool_evidence(value: Any) -> bool:
    """Detect a cognitive/tool packet without treating arbitrary JSON as one."""

    found = False

    def visit(candidate: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(candidate, Mapping):
            normalized_keys = {_normalized_key(key) for key in candidate}
            if normalized_keys.intersection({
                "receiptprojection", "actionreceipt", "toolcallid", "evidencerefs",
                "evidencestatus", "executionstatus", "sourcetype",
            }):
                found = True
                return
            # A packet with an evidence array is only a tool packet when an
            # item exposes one of the source/receipt fields above.  This keeps
            # ordinary dialogue JSON from being compacted as an external tool.
            for key, nested in candidate.items():
                if _normalized_key(key) in {"evidence", "toolresults", "sources", "results"}:
                    if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                        for item in nested:
                            visit(item)
                            if found:
                                return
                    elif isinstance(nested, Mapping):
                        visit(nested)
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for nested in candidate:
                visit(nested)
                if found:
                    return

    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes)):
        visit(value)
    return found


def _json_candidate_spans(text: str) -> tuple[tuple[int, int, Any], ...]:
    """Extract bounded JSON values and their source spans.

    A cognitive prompt contains a packet JSON followed by another protocol
    object.  Parsing must therefore accept a valid JSON value with trailing
    prompt text and return the span so the caller can replace only that value.
    """

    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, Any]] = []
    # Braces inside a long web body are common; only inspect line starts and
    # the first few structural positions to keep pressure decisions cheap.
    starts: list[int] = []
    # regex-purpose: format
    for match in re.finditer(r"(?m)^[ \t]*[\[{]", text):
        starts.append(match.start())
        if len(starts) >= 64:
            break
    for start in reversed(starts):
        candidate_text = text[start:].lstrip()
        json_start = start + (len(text[start:]) - len(candidate_text))
        try:
            value, consumed = decoder.raw_decode(candidate_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        candidates.append((json_start, json_start + consumed, value))
        if len(candidates) >= 8:
            break
    return tuple(candidates)


def _json_candidates(text: str) -> tuple[Any, ...]:
    """Extract bounded JSON candidates embedded in explanatory prompt text."""

    return tuple(candidate for _start, _end, candidate in _json_candidate_spans(text)[:4])


def message_contains_tool_evidence(
    message: Mapping[str, Any] | str,
    *,
    source_metadata: Any = None,
) -> bool:
    """Return whether a message carries tool/web evidence, including embedded packets."""

    role = ""
    if isinstance(message, Mapping):
        explicit = _explicit_message_source(message)
        if explicit == "tool_evidence":
            return True
        role = str(message.get("role") or "").strip().lower()
        text = _content_text(message.get("content"))
    else:
        text = str(message or "")
    if isinstance(source_metadata, Mapping):
        explicit = _normalized_source(
            source_metadata.get("source")
            or source_metadata.get("kind")
            or source_metadata.get("sourceType")
            or source_metadata.get("source_type")
            or source_metadata.get("origin")
        )
        if explicit == "tool_evidence":
            return True
    elif source_metadata not in (None, "", False):
        if _normalized_source(source_metadata) == "tool_evidence":
            return True
    # A marker in ordinary user/assistant prose is merely quoted text.  Marker
    # syntax is authoritative only on a protocol role; structured receipt
    # fields below remain valid evidence when embedded in a dialogue packet.
    if role in {"system", "tool", "function"} and (
        _TOOL_JSON_MARKER_RE.search(text) or _COGNITIVE_TOOL_MARKER_RE.search(text)
    ):
        return True
    if not text:
        return False
    for candidate in _json_candidates(text):
        if _value_has_structured_tool_evidence(candidate):
            return True
    return False


def prompt_message_source(
    message: Mapping[str, Any] | str,
    *,
    index: int = 0,
    total: int = 1,
    source_metadata: Any = None,
) -> str:
    """Classify a prompt message using explicit metadata before safe fallbacks.

    ``source_metadata`` is intentionally out-of-band so a caller can annotate
    messages without adding provider-unknown fields to the API payload.
    """

    explicit = _explicit_message_source(message if isinstance(message, Mapping) else None)
    if not explicit:
        if isinstance(source_metadata, Mapping):
            explicit = _normalized_source(
                source_metadata.get("source")
                or source_metadata.get("kind")
                or source_metadata.get("contextSource")
                or source_metadata.get("sourceType")
                or source_metadata.get("source_type")
                or source_metadata.get("origin")
            )
        elif source_metadata not in (None, "", False):
            explicit = _normalized_source(source_metadata)
    if explicit in {
        "tool_evidence", "attachment", "recent_dialogue", "midterm_dialogue",
        "summary", "long_memory", "relationship", "persona", "system_prompt",
    }:
        return explicit

    text = _content_text(message.get("content")) if isinstance(message, Mapping) else str(message or "")
    if message_contains_tool_evidence(message, source_metadata=source_metadata):
        return "tool_evidence"
    if "[附件" in text or "[本机文件" in text:
        return "attachment"
    if "[用户显式回复锚点" in text:
        return "recent_dialogue"
    role = str(message.get("role") or "").lower() if isinstance(message, Mapping) else ""
    if role in {"user", "assistant"}:
        if "[短期对话锚点" in text or index >= max(0, total - 8):
            return "recent_dialogue"
        return "midterm_dialogue"
    if text.startswith(("[对话历史原文：数据]", "[当前会话短期上下文", "[QQ 子对话的近期")):
        return "recent_dialogue"
    if HISTORY_RECORDS_MARKER in text and role == "system":
        return "midterm_dialogue"
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
    if role == "system" or (index == 0 and not role):
        return "system_prompt"
    if "人格" in text or "amadeus" in text.lower():
        return "persona"
    return "other"


def message_token_estimate(message: Mapping[str, Any], *, source_metadata: Any = None) -> int:
    """Estimate content plus the role/envelope overhead sent to the provider."""

    content = _content_text(message.get("content"))
    total = estimate_tokens(content)
    parts = message.get("content")
    if isinstance(parts, list):
        # A conservative local visual allowance, not a provider billing count.
        # The caller's system margin still covers estimation uncertainty.
        total += 4096 * sum(isinstance(part, Mapping) and part.get("type") in {"image_url", "input_image"}
                            for part in parts)
    # Most chat providers charge a small per-message envelope in addition to
    # content.  Include role/name and explicit metadata supplied to the API;
    # out-of-band source metadata is not serialized and therefore costs zero.
    total += 4 + estimate_tokens(message.get("role"))
    if message.get("name"):
        total += estimate_tokens(message.get("name"))
    for key in ("tool_call_id", "toolCallId"):
        if message.get(key):
            total += estimate_tokens(message.get(key))
    return max(1, total)


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


def _message_bucket(
    text: str,
    index: int,
    total: int,
    *,
    message: Mapping[str, Any] | None = None,
    source_metadata: Any = None,
) -> str:
    return prompt_message_source(
        message if isinstance(message, Mapping) else text,
        index=index,
        total=total,
        source_metadata=source_metadata,
    )


def budget_snapshot(
    messages: Sequence[Mapping[str, Any]],
    *,
    model_context_limit: int,
    reserved_output_tokens: int,
    reserved_system_margin: int,
    compact_threshold_tokens: int | None = None,
    source_metadata: Sequence[Any] | Mapping[int, Any] | None = None,
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
    hard_input_budget = max(
        1,
        int(model_context_limit)
        - max(0, int(reserved_output_tokens))
        - max(0, int(reserved_system_margin)),
    )
    if compact_threshold_tokens is None or int(compact_threshold_tokens) <= 0:
        # Keep a wide hard-budget safety margin even when callers do not
        # configure an application-specific target.  The cap avoids turning a
        # very large provider window into an unnecessarily aggressive prompt.
        effective_threshold = min(128_000, max(1, hard_input_budget * 70 // 100))
    else:
        effective_threshold = min(
            hard_input_budget,
            max(1, int(compact_threshold_tokens)),
        )
    for index, message in enumerate(messages):
        metadata = (
            source_metadata.get(index)
            if isinstance(source_metadata, Mapping)
            else source_metadata[index]
            if isinstance(source_metadata, Sequence)
            and not isinstance(source_metadata, (str, bytes))
            and index < len(source_metadata)
            else None
        )
        text = _content_text(message.get("content"))
        totals[_message_bucket(
            text,
            index,
            total_messages,
            message=message,
            source_metadata=metadata,
        )] += message_token_estimate(message, source_metadata=metadata)
    total_input = sum(totals.values())
    usable = hard_input_budget
    ratio = total_input / usable
    over_budget = max(0, total_input - usable)
    threshold_exceeded = total_input > effective_threshold
    level_ratio = ratio
    if threshold_exceeded and level_ratio < 0.72:
        # A deliberately low early target must still trigger a first pass even
        # when the provider hard budget has a large amount of spare capacity.
        level_ratio = 0.72
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
        level=pressure_level(level_ratio),
        fits_budget=over_budget == 0,
        over_budget_tokens=over_budget,
        compact_threshold_tokens=effective_threshold,
        compact_threshold_exceeded=threshold_exceeded,
    )


def _diagnostic_fingerprint(messages: Sequence[Mapping[str, Any]]) -> str:
    """Hash a prompt projection without returning any prompt value."""

    try:
        serialized = json.dumps(
            [dict(message) for message in messages],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        serialized = repr(tuple(dict(message) for message in messages))
    return runtime_fingerprint(serialized, length=24)


def _diagnostic_source_counts(
    messages: Sequence[Mapping[str, Any]],
    source_metadata: Sequence[Any] | Mapping[int, Any] | None = None,
) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    total = len(messages)
    for index, message in enumerate(messages):
        metadata = (
            source_metadata.get(index)
            if isinstance(source_metadata, Mapping)
            else source_metadata[index]
            if isinstance(source_metadata, Sequence)
            and not isinstance(source_metadata, (str, bytes))
            and index < len(source_metadata)
            else None
        )
        source = _message_bucket(
            _content_text(message.get("content")),
            index,
            total,
            message=message,
            source_metadata=metadata,
        )
        counts[source] = counts.get(source, 0) + 1
    return tuple(sorted(counts.items()))


def context_pressure_diagnostic(
    input_messages: Sequence[Mapping[str, Any]],
    output_messages: Sequence[Mapping[str, Any]],
    snapshot: ContextBudgetSnapshot,
    *,
    source_metadata: Sequence[Any] | Mapping[int, Any] | None = None,
) -> dict[str, Any]:
    """Return a log-safe pressure decision record.

    The record carries source categories, counts, hashes, and decision state;
    it intentionally contains no message text, identifiers, or credentials.
    """

    input_sources = _diagnostic_source_counts(input_messages, source_metadata)
    output_sources = _diagnostic_source_counts(output_messages)
    input_fingerprint = _diagnostic_fingerprint(input_messages)
    output_fingerprint = _diagnostic_fingerprint(output_messages)
    input_tokens = sum(message_token_estimate(message) for message in input_messages)
    output_tokens = sum(message_token_estimate(message) for message in output_messages)
    decision = snapshot.diagnostic_decision or (
        "compacted" if snapshot.applied_stages else "no_op"
    )
    return {
        "sourceType": "context_pressure",
        "decision": decision,
        "inputCount": len(input_messages),
        "outputCount": len(output_messages),
        "inputHash": input_fingerprint,
        "outputHash": output_fingerprint,
        "input": {
            "messageCount": len(input_messages),
            "tokenEstimate": input_tokens,
            "fingerprint": input_fingerprint,
            "sourceCounts": dict(input_sources),
        },
        "output": {
            "messageCount": len(output_messages),
            "tokenEstimate": output_tokens,
            "fingerprint": output_fingerprint,
            "sourceCounts": dict(output_sources),
        },
        "changedMessageCount": abs(len(output_messages) - len(input_messages)),
        "level": snapshot.level.name.lower(),
        "appliedStages": list(snapshot.applied_stages),
        "fitsBudget": bool(snapshot.fits_budget),
        "compactThresholdExceeded": bool(snapshot.compact_threshold_exceeded),
    }


def _attach_pressure_diagnostics(
    snapshot: ContextBudgetSnapshot,
    input_messages: Sequence[Mapping[str, Any]],
    output_messages: Sequence[Mapping[str, Any]],
    *,
    source_metadata: Sequence[Any] | Mapping[int, Any] | None,
    decision: str,
) -> ContextBudgetSnapshot:
    return replace(
        snapshot,
        input_message_count=len(input_messages),
        output_message_count=len(output_messages),
        input_fingerprint=_diagnostic_fingerprint(input_messages),
        output_fingerprint=_diagnostic_fingerprint(output_messages),
        input_source_counts=_diagnostic_source_counts(input_messages, source_metadata),
        output_source_counts=_diagnostic_source_counts(output_messages),
        diagnostic_decision=decision,
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


def _protected_status_projection(value: Any) -> dict[str, Any]:
    """Keep compact status/authority fields from omitted evidence entries."""

    projection: dict[str, Any] = {}

    def visit(candidate: Any) -> None:
        if isinstance(candidate, Mapping):
            for key, nested in candidate.items():
                normalized = _normalized_key(key)
                if normalized in _PROTECTED_STATUS_KEY_NAMES:
                    existing = projection.get(str(key))
                    # Status, authority and authorization are semantic state,
                    # not low-value evidence body.  Preserve them byte-for-byte
                    # in the derived projection; an oversized result must fail
                    # the hard budget rather than silently lose a negation.
                    bounded_nested = deepcopy(nested)
                    if existing is None:
                        projection[str(key)] = bounded_nested
                    elif isinstance(existing, list):
                        existing.append(bounded_nested)
                    else:
                        projection[str(key)] = [existing, bounded_nested]
                visit(nested)
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for nested in candidate:
                visit(nested)

    visit(value)
    return projection


def _control_projection(value: Any) -> dict[str, Any]:
    projection: dict[str, Any] = {}

    def visit(candidate: Any) -> None:
        if isinstance(candidate, Mapping):
            for key, nested in candidate.items():
                if _normalized_key(key) in _COGNITIVE_CONTROL_KEY_NAMES:
                    # Task state, preferences, current request, permissions
                    # and corrections are all protected control data.  Keep
                    # complete values in projection-only fallbacks.
                    value = deepcopy(nested)
                    existing = projection.get(str(key))
                    if existing is None:
                        projection[str(key)] = value
                    elif isinstance(existing, list):
                        existing.append(value)
                    else:
                        projection[str(key)] = [existing, value]
                visit(nested)
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            for nested in candidate:
                visit(nested)

    visit(value)
    return projection


def _context_projection(value: Any) -> dict[str, Any]:
    projection = _identity_projection(value)
    statuses = _protected_status_projection(value)
    if statuses:
        projection["protectedStatusProjection"] = statuses
    controls = _control_projection(value)
    if controls:
        projection["controlProjection"] = controls
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


def _embedded_tool_json_payload(value: str) -> tuple[int, int, Any] | None:
    """Find a structured packet inside a larger protocol prompt.

    The returned span lets the caller replace only the JSON value.  Protocol
    instructions before and after a cognitive packet remain byte-for-byte in
    the derived prompt view.
    """

    for start, end, candidate in _json_candidate_spans(value):
        if _value_has_structured_tool_evidence(candidate):
            return start, end, candidate
    return None


def _snipped_json_string(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = f"…[低价值工具正文已截短:{len(value)} chars]…"
    available = max(0, limit - len(marker))
    head = available * 2 // 3
    tail = available - head
    return value[:head] + marker + (value[-tail:] if tail else "")


def _compact_status_value(value: Any, *, depth: int = 0) -> Any:
    """Keep status/authority metadata complete in the derived view."""

    return deepcopy(value)


def _compact_control_value(value: Any, *, depth: int = 0) -> Any:
    """Keep cognitive control state complete; hard budget is the guardrail."""

    return deepcopy(value)


def _is_cognitive_packet(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    keys = {_normalized_key(key) for key in value}
    if "evidence" not in keys:
        return False
    return len(keys.intersection(_COGNITIVE_PACKET_KEY_NAMES)) >= 2


def _cognitive_source_type(value: Mapping[str, Any]) -> str:
    for key, nested in value.items():
        if _normalized_key(key) in {"sourcetype", "source_type"}:
            return _normalized_source(nested)
    return ""


def _compact_tool_facts(
    value: Any,
    *,
    string_limit: int,
    max_characters: int = 2_600,
) -> Any:
    """Bound structured tool facts while keeping receipt and policy state.

    ``toolFacts`` is a prompt-only projection.  It may contain a complete
    status snapshot (for example a contact directory and recent event list),
    while the enclosing evidence entry already carries its receipt identity
    and execution status.  Keep exact policy/control mappings when they fit,
    bound conversational collections and preserve an explicit omission marker
    when the snapshot is larger than the pressure budget.  The durable receipt
    remains available for a later trace/refetch operation.
    """

    if not isinstance(value, Mapping):
        return deepcopy(value)
    existing_pressure = value.get("_contextPressure")
    if isinstance(existing_pressure, Mapping) and existing_pressure.get("toolFactsTruncated"):
        return deepcopy(value)

    def compact(candidate: Any, *, key: str = "", limit: int) -> Any:
        normalized = _normalized_key(key)
        if isinstance(candidate, Mapping):
            # Policy snapshots are authority data.  Do not slice their arrays
            # unless the whole facts object still exceeds the prompt budget;
            # conversation-level status/history collections are lower-value.
            if normalized in {"policy", "previouspolicy"}:
                return deepcopy(candidate)
            return {
                str(nested_key): compact(nested, key=str(nested_key), limit=limit)
                for nested_key, nested in candidate.items()
            }
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            item_limit = _TOOL_FACT_LIST_LIMITS.get(normalized, min(8, max(2, limit // 256)))
            selected = [
                compact(nested, key=key, limit=limit)
                for nested in list(candidate)[:item_limit]
            ]
            omitted = len(candidate) - len(selected)
            if omitted > 0:
                selected.append({
                    "_contextPressureOmittedItems": omitted,
                    "_contextPressureSource": "toolFacts",
                })
            return selected
        if isinstance(candidate, str) and len(candidate) > limit:
            return _snipped_json_string(candidate, limit=max(48, int(limit)))
        return deepcopy(candidate)

    original_characters = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    first = compact(value, limit=max(96, int(string_limit)))
    if len(json.dumps(first, ensure_ascii=False, separators=(",", ":"))) <= max(1, int(max_characters)):
        return first

    # A second pass bounds nested event/message strings as well as collections.
    tight = compact(value, limit=max(96, min(256, int(string_limit) // 2)))
    if len(json.dumps(tight, ensure_ascii=False, separators=(",", ":"))) <= max(1, int(max_characters)):
        return tight

    # If a snapshot is still oversized, retain identity/status and the exact
    # policy when it is reasonably bounded.  The receipt/evidence id on the
    # parent entry makes omitted details recoverable without inventing state.
    projection: dict[str, Any] = {}
    omitted_keys: list[str] = []
    optional_keys = {
        "contacts",
        "messages",
        "recentactions",
        "recentevents",
        "activitysummary",
    }
    optional_order = {
        key: index for index, key in enumerate(
            ("contacts", "messages", "recentactions", "recentevents", "activitysummary")
        )
    }
    optional_items = sorted(
        ((str(key), nested) for key, nested in value.items()
         if _normalized_key(key) in optional_keys),
        key=lambda item: optional_order.get(_normalized_key(item[0]), 99),
    )
    for key, nested in value.items():
        normalized = _normalized_key(key)
        if normalized in _IDENTITY_KEY_NAMES or normalized in _PROTECTED_STATUS_KEY_NAMES or normalized in {
            "policy", "previouspolicy", "policyrevision", "contactscomplete", "targetid", "targettype", "account",
        }:
            projection[str(key)] = deepcopy(nested)
        elif normalized in optional_keys:
            # Optional facts are added below in semantic priority order so a
            # contact/message lookup survives before diagnostic histories.
            continue
        else:
            omitted_keys.append(str(key))
    optional_budget = max(256, int(max_characters) - 600)
    for key, nested in optional_items:
        candidate = compact(nested, key=key, limit=128)
        trial = {**projection, str(key): candidate}
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) <= optional_budget:
            projection[key] = candidate
        else:
            omitted_keys.append(key)
    projection["_contextPressure"] = {
        "toolFactsTruncated": True,
        "originalCharacters": original_characters,
        "omittedKeys": omitted_keys,
        "message": "结构化工具事实已压缩；完整回执仍可按 evidence id 追溯或重新读取。",
    }
    return projection


def _compact_cognitive_evidence_value(
    value: Any,
    *,
    string_limit: int,
    body_enabled: bool = False,
    key: str = "",
) -> Any:
    """Compact evidence bodies and oversized structured tool facts."""

    normalized = _normalized_key(key)
    if normalized in _PROTECTED_STATUS_KEY_NAMES or normalized in _COGNITIVE_CONTROL_KEY_NAMES:
        return deepcopy(value)
    if normalized == "toolfacts":
        return _compact_tool_facts(
            value,
            string_limit=string_limit,
        )
    if _is_identity_key(key):
        return deepcopy(value)
    if isinstance(value, Mapping):
        entry_body_enabled = body_enabled
        if _cognitive_source_type(value) in _TOOL_EVIDENCE_SOURCE_TYPES:
            entry_body_enabled = True
        return {
            str(nested_key): _compact_cognitive_evidence_value(
                nested,
                string_limit=string_limit,
                body_enabled=entry_body_enabled,
                key=str(nested_key),
            )
            for nested_key, nested in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        # Preserve every evidence entry.  Pressure is applied to body text,
        # never by dropping a source/status object from the packet.
        return [
            _compact_cognitive_evidence_value(
                nested,
                string_limit=string_limit,
                body_enabled=body_enabled,
                key=key,
            )
            for nested in value
        ]
    if isinstance(value, str) and body_enabled and (
        normalized in _LOW_VALUE_KEY_NAMES or normalized in _COGNITIVE_BODY_KEY_NAMES
    ):
        return _snipped_json_string(value, limit=max(48, string_limit))
    return deepcopy(value)


def _compact_cognitive_packet(value: Any, *, string_limit: int) -> Any:
    """Deep-copy a cognitive packet and bound low-value tool-source payloads."""

    packet = deepcopy(value)
    if not isinstance(packet, Mapping):
        return packet
    for key, nested in list(packet.items()):
        if _normalized_key(key) == "evidence":
            packet[key] = _compact_cognitive_evidence_value(
                nested,
                string_limit=string_limit,
            )
    return packet


def _compact_json_value(
    value: Any,
    *,
    string_limit: int,
    list_limit: int,
    key: str = "",
) -> Any:
    normalized = _normalized_key(key)
    if normalized in _PROTECTED_STATUS_KEY_NAMES:
        return _compact_status_value(value)
    if normalized in _COGNITIVE_CONTROL_KEY_NAMES:
        return _compact_control_value(value)
    if _is_identity_key(key):
        return value
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
            omitted_projection = _context_projection(value[len(selected):])
            compacted.append({
                "_contextPressureOmittedItems": omitted,
                **({"contextProjection": omitted_projection} if omitted_projection else {}),
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
    cognitive_packet = _is_cognitive_packet(payload)
    attempts = (
        (max(256, min(2_048, limit // 3)), 16),
        (max(128, min(1_024, limit // 5)), 8),
        (96, 4),
        (48, 1),
    )
    for string_limit, list_limit in attempts:
        compacted = (
            _compact_cognitive_packet(payload, string_limit=string_limit)
            if cognitive_packet else
            _compact_json_value(
                payload,
                string_limit=string_limit,
                list_limit=list_limit,
            )
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

    # A cognitive fallback must retain the complete packet contract.  Only
    # tool-source evidence bodies may be bounded; active beliefs, references,
    # context corrections and success criteria remain full values so an
    # oversized control packet fails the hard budget instead of losing state.
    projection = (
        _compact_cognitive_packet(payload, string_limit=48)
        if cognitive_packet else _context_projection(payload)
    )
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
    embedded: tuple[int, int, Any] | None = None
    if parsed is None:
        # Cognitive prompts embed the packet between protocol instructions and
        # a local contract.  Keep those outer sections intact instead of
        # replacing the entire prompt with a synthetic tool marker.
        embedded = _embedded_tool_json_payload(value)
    if embedded is not None:
        start, end, payload = embedded
        projection = _identity_projection(payload)
        sources = tuple(dict.fromkeys((*sources, *_source_ids_from_value(payload))))
        prefix = value[:start]
        suffix = value[end:]
        compact_payload = _json_compact_view(
            payload,
            original_characters=len(value),
            limit=max(256, limit - len(prefix) - len(suffix) - 1),
        )
        compact = prefix + json.dumps(
            compact_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + suffix
    elif parsed is not None:
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


_HISTORY_PROTECTION_MARKERS = (
    "[用户显式回复锚点",
    "[权限上下文]",
    "[本轮语义判断",
    "[Turn Contract",
    "[Claim-Level Fact Policy",
    "[protected_claim]",
    "[required_acts]",
    "[unresolved]",
    "[protection_reasons]",
)
_HISTORY_PROTECTION_KEYS = {
    "protected",
    "contextprotected",
    "preserve",
    "pinned",
    "pin",
    "donotcompact",
    "do_not_compact",
    "hardprotected",
}


def _message_flag(message: Mapping[str, Any], key_names: set[str]) -> bool:
    def enabled(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
        return bool(value)

    for key, value in message.items():
        if _normalized_key(key) in {_normalized_key(item) for item in key_names} and enabled(value):
            return True
    for container_key in ("contextProtection", "context_protection", "contextMetadata", "context_metadata", "metadata", "meta"):
        nested = message.get(container_key)
        if not isinstance(nested, Mapping):
            continue
        for key, value in nested.items():
            if _normalized_key(key) in {_normalized_key(item) for item in key_names} and enabled(value):
                return True
    return False


def _message_is_protected(
    message: Mapping[str, Any],
    *,
    index: int,
    total: int,
) -> bool:
    text = _content_text(message.get("content"))
    source = prompt_message_source(message, index=index, total=total)
    if source in {"system_prompt", "persona", "relationship", "attachment"}:
        return True
    if message_contains_tool_evidence(message):
        # Embedded JSON in an actual user/assistant utterance is part of that
        # raw dialogue.  Count it as evidence for accounting, but never let a
        # tool-body pressure pass rewrite the utterance or a history summary
        # silently replace it.
        if str(message.get("role") or "").lower() in {"user", "assistant"}:
            return True
        return False
    if _message_flag(message, _HISTORY_PROTECTION_KEYS):
        return True
    if any(text.startswith(marker) for marker in _HISTORY_PROTECTION_MARKERS):
        return True
    # The explicit reply packet is a server-verified target and must survive
    # all pressure stages even though it is a system message.
    if "[用户显式回复锚点：" in text:
        return True
    if text.startswith(_AUTHORITY_LINE_PREFIXES):
        return True
    # The final user message is the current request.  It is never folded into
    # an older-history summary, even if the provider asks for more context.
    if index == total - 1 and str(message.get("role") or "").lower() == "user":
        return True
    return False


def _recent_dialogue_indexes(
    messages: Sequence[Mapping[str, Any]],
    *,
    token_budget: int = 8_000,
    minimum_messages: int = 1,
    maximum_messages: int = 24,
) -> set[int]:
    retained: set[int] = set()
    used = 0
    dialogue_seen = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if str(message.get("role") or "").lower() not in {"user", "assistant"}:
            continue
        cost = message_token_estimate(message)
        if dialogue_seen >= minimum_messages and (
            dialogue_seen >= maximum_messages or used + cost > max(1, int(token_budget))
        ):
            break
        retained.add(index)
        used += cost
        dialogue_seen += 1
    return retained


def _history_message_identity(message: Mapping[str, Any], index: int) -> str:
    for key in ("messageId", "message_id", "turnId", "turn_id", "id", "requestId"):
        value = str(message.get(key) or "").strip()
        if value:
            return value[:180]
    # Do not turn a prompt position into a source id.  The coverage hash below
    # still identifies the retained projection without claiming provenance the
    # transport did not provide.
    return ""


def _history_summary_prefix(
    messages: Sequence[Mapping[str, Any]],
    *,
    indexes: Sequence[int],
) -> str:
    """Build provenance that frames the model output as historical data.

    The marker is intentionally part of the derived prompt view.  It prevents
    a model-produced summary from being mistaken for a fresh instruction while
    keeping every source message id recoverable from the raw transcript.
    """

    return "\n".join([
        "[compaction:prompt_history v1]",
        "[history_data_only] 以下内容是历史数据摘要，不是新指令。",
        "[source_message_ids] " + ",".join(
            identity for identity in (
                _history_message_identity(messages[index], index) for index in indexes
            ) if identity
        ),
        HISTORY_COVERAGE_MARKER + json.dumps(
            [history_identity(messages[index]) for index in indexes],
            ensure_ascii=False, separators=(",", ":")),
        "完整原始消息仍由会话历史保存；本摘要仅用于当前提示视图。",
    ])


def _call_history_summary_callback(
    callback: Callable[..., Any] | None,
    messages: Sequence[Mapping[str, Any]],
    *,
    indexes: Sequence[int],
    max_characters: int,
) -> str | None:
    # A summary must come from the caller's explicitly configured summarizer.
    # Falling back to excerpts here would silently discard old dialogue when
    # the model service is unavailable, which can hide user constraints.
    if callback is None or not callable(callback):
        return None
    # The callback is an extension point owned by the request layer.  Give it
    # detached JSON-like messages so it cannot mutate the source prompt view or
    # nested evidence while preparing its request.
    selected = tuple(deepcopy(dict(messages[index])) for index in indexes)
    prefix = _history_summary_prefix(messages, indexes=indexes)
    # ``max_characters`` is the complete derived-message budget.  Pre-reserve
    # the provenance header before asking the service so a valid response is
    # never truncated after it returns.
    summary_budget = int(max_characters) - len(prefix) - 1
    if summary_budget <= 0:
        return None
    candidate = callback(selected, max_characters=summary_budget)
    if isinstance(candidate, Mapping):
        candidate = candidate.get("text") or candidate.get("summary") or ""
    text = str(candidate or "").strip()
    if not text:
        return None
    summary = prefix + "\n" + text
    # Never trim a returned summary: a tail can contain a negation, authority
    # clause, or safety boundary.  A service that exceeds its declared budget
    # is treated as unavailable and the source messages remain untouched.
    if len(summary) > max(0, int(max_characters)):
        return None
    return summary


def _compact_old_dialogue(
    messages: list[dict[str, Any]],
    *,
    max_characters: int,
    summary_callback: Callable[..., Any] | None = None,
    token_budget: int = 8_000,
) -> None:
    if len(messages) < 2:
        return
    if summary_callback is None or not callable(summary_callback):
        return
    total = len(messages)
    recent = _recent_dialogue_indexes(
        messages,
        token_budget=token_budget,
    )
    candidates = [
        index for index, message in enumerate(messages)
        # Untyped user history is authoritative input and remains verbatim.
        # A future typed history policy may opt into user summarization, but a
        # generic pressure pass must not infer that permission from prose.
        if str(message.get("role") or "").lower() == "assistant"
        and index not in recent
        and not _message_is_protected(message, index=index, total=total)
    ]
    if not candidates:
        return
    summary_text = _call_history_summary_callback(
        summary_callback,
        messages,
        indexes=candidates,
        max_characters=max_characters,
    )
    if not summary_text:
        return
    first = candidates[0]
    selected = set(candidates)
    compacted: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index == first:
            compacted.append({"role": "system", "content": summary_text})
        if index not in selected:
            compacted.append(message)
    messages[:] = compacted


def _compact_history_views(
    messages: list[dict[str, Any]], *, max_characters: int,
    summary_callback: Callable[..., Any] | None, hard_input_budget: int,
) -> None:
    """Summarize typed historical user AND assistant records across all views.

    Their outer system role is a data wrapper, not a reason to exempt them.
    Current input, explicit recall, permissions/corrections and reply anchors
    remain verbatim. Only the prompt projection changes; storage is untouched.
    """
    if not callable(summary_callback):
        return
    views = history_views(messages)
    entries = [(view, row) for view in views for row in view.rows()]
    if not entries:
        return
    rows = [row for _, row in entries]
    row_tokens = sum(message_token_estimate(row) for row in rows)
    fixed = sum(message_token_estimate(item) for item in messages) - row_tokens
    recent_budget = max(256, min(8000, hard_input_budget - fixed - estimate_tokens('s' * max_characters)))
    # The current user request lives outside these history records and is
    # already reserved in fixed. A single oversized old answer may be folded.
    recent = _recent_dialogue_indexes(rows, token_budget=recent_budget, minimum_messages=0)
    candidates = [index for index, (view, row) in enumerate(entries)
                  if index not in recent and row.get("role") in {"user", "assistant"}
                  and not view.packet.get("explicitRecall") and not row.get("replyAnchor")
                  and not _message_is_protected(row, index=index, total=len(rows) + 1)]
    if not candidates:
        return
    summary = _call_history_summary_callback(summary_callback, rows,
                                             indexes=candidates, max_characters=max_characters)
    if not summary:
        return
    selected = set(candidates)
    trial = deepcopy(messages)
    for view in history_views(trial):
        kept = [row for i, (original, row) in enumerate(entries)
                if original.index == view.index and i not in selected]
        view.write(trial, kept)
    trial.insert(entries[candidates[0]][0].index, {"role": "system", "content": summary})
    # A short response with a large provenance header must still save tokens.
    if sum(message_token_estimate(m) for m in trial) < sum(message_token_estimate(m) for m in messages):
        messages[:] = trial


def _available_history_token_budget(
    messages: Sequence[Mapping[str, Any]],
    *,
    hard_input_budget: int,
    summary_characters: int,
) -> int:
    fixed_tokens = sum(
        message_token_estimate(message)
        for message in messages
        if str(message.get("role") or "").lower() not in {"user", "assistant"}
    )
    # Reserve room for the provenance header and a small deterministic summary
    # body before retaining recent dialogue.  If fixed rules alone exceed the
    # hard budget, returning one makes the final invariant fail closed.
    summary_reserve = max(256, min(1_200, int(summary_characters) // 3))
    return max(
        1,
        min(8_000, int(hard_input_budget) - fixed_tokens - summary_reserve),
    )


def _apply_pressure_stage(
    messages: list[dict[str, Any]],
    level: PressureLevel,
    *,
    summary_callback: Callable[..., Any] | None = None,
    hard_input_budget: int | None = None,
) -> None:
    tool_limit = {
        PressureLevel.TOOL_PAYLOAD_SNIP: 18_000,
        PressureLevel.TOOL_RESEARCH_MICROCOMPACT: 10_000,
        PressureLevel.CONVERSATION_COLLAPSE: 7_000,
        PressureLevel.SESSION_COMPACT: 4_500,
    }[level]
    for item in messages:
        if str(item.get("role") or "").lower() in {"user", "assistant"}:
            # Structured evidence in original dialogue is accounted for but is
            # not a derived tool-result message we may rewrite.
            continue
        text = str(item.get("content") or "")
        if history_views([item]):
            continue
        if message_contains_tool_evidence(item):
            item["content"] = compact_tool_prompt(
                text,
                max_characters=tool_limit,
            ).compact_text

    if level >= PressureLevel.CONVERSATION_COLLAPSE:
        mid_limit = 6_000 if level == PressureLevel.CONVERSATION_COLLAPSE else 3_600
        for item in messages:
            if str(item.get("role") or "").lower() != "system":
                continue
            text = str(item.get("content") or "")
            if text.startswith("[持久会话上下文") and not history_views([item]):
                item["content"] = _collapse_midterm_prompt(
                    text,
                    max_characters=mid_limit,
                )
        _compact_history_views(messages, max_characters=mid_limit,
                               summary_callback=summary_callback,
                               hard_input_budget=max(1, int(hard_input_budget or 8000)))
        _compact_old_dialogue(
            messages,
            max_characters=mid_limit,
            summary_callback=summary_callback,
            token_budget=(
                _available_history_token_budget(
                    messages,
                    hard_input_budget=max(1, int(hard_input_budget or 1)),
                    summary_characters=mid_limit,
                )
                if hard_input_budget is not None else 8_000
            ),
        )

    if level >= PressureLevel.SESSION_COMPACT:
        for item in messages:
            # Raw user input is authoritative and must never be selected by a
            # keyword in its prose.  A history summary produced in the
            # previous stage is already bounded and provenance-tagged; running
            # it through the generic midterm slicer would destroy its tail.
            if str(item.get("role") or "").lower() != "system":
                continue
            text = str(item.get("content") or "")
            if history_views([item]):
                continue
            if "[compaction:prompt_history v1]" in text:
                continue
            if text.startswith(("[持久会话上下文", "[长期记忆检索]", "[compaction:")):
                item["content"] = _collapse_midterm_prompt(
                    text,
                    max_characters=2_400,
                )
        _compact_history_views(messages, max_characters=2400,
                               summary_callback=summary_callback,
                               hard_input_budget=max(1, int(hard_input_budget or 4000)))
        _compact_old_dialogue(
            messages,
            max_characters=2_400,
            summary_callback=summary_callback,
            token_budget=(
                _available_history_token_budget(
                    messages,
                    hard_input_budget=max(1, int(hard_input_budget or 1)),
                    summary_characters=2_400,
                )
                if hard_input_budget is not None else 4_000
            ),
        )


def apply_prompt_pressure(
    messages: Sequence[Mapping[str, Any]],
    *,
    model_context_limit: int,
    reserved_output_tokens: int,
    reserved_system_margin: int = 2048,
    compact_threshold_tokens: int | None = None,
    source_metadata: Sequence[Any] | Mapping[int, Any] | None = None,
    summary_callback: Callable[..., Any] | None = None,
) -> tuple[list[dict[str, Any]], ContextBudgetSnapshot]:
    """Shrink a derived prompt view without mutating source turns or receipts."""

    started = time.perf_counter()
    source_messages = [dict(item) for item in messages]
    # The short-context and immediate-context builders can expose the same
    # user/assistant line twice.  Remove only exact same-role duplicates before
    # pressure accounting; the optional module is imported lazily so this
    # low-level helper remains usable by older isolated consumers.
    try:
        from agent.prompt_views import deduplicate_prompt_views
    except ImportError:
        deduplicated_messages = source_messages
    else:
        deduplicated_messages = [
            dict(item) for item in deduplicate_prompt_views(source_messages)
        ]
    original = deduplicated_messages
    initial = budget_snapshot(
        source_messages,
        model_context_limit=model_context_limit,
        reserved_output_tokens=reserved_output_tokens,
        reserved_system_margin=reserved_system_margin,
        compact_threshold_tokens=compact_threshold_tokens,
        source_metadata=source_metadata,
    )
    before = budget_snapshot(
        original,
        model_context_limit=model_context_limit,
        reserved_output_tokens=reserved_output_tokens,
        reserved_system_margin=reserved_system_margin,
        compact_threshold_tokens=compact_threshold_tokens,
        source_metadata=source_metadata,
    )
    if before.level == PressureLevel.NO_OP:
        snapshot = replace(
            before,
            tokens_saved=max(0, initial.total_input_tokens - before.total_input_tokens),
            decision_ms=(time.perf_counter() - started) * 1000,
        )
        return original, _attach_pressure_diagnostics(
            snapshot,
            source_messages,
            original,
            source_metadata=source_metadata,
            decision="no_op",
        )

    result = [dict(item) for item in original]
    applied_levels: list[PressureLevel] = []
    stage_input_tokens: list[int] = []
    after = before
    hard_input_budget = before.usable_input_budget
    history_summary_called = False

    def summarize_history_once(
        selected: Sequence[Mapping[str, Any]],
        *,
        max_characters: int,
    ) -> Any:
        nonlocal history_summary_called
        if history_summary_called:
            return None
        history_summary_called = True
        # The caller owns timeout/error policy.  In particular, do not catch
        # TypeError here and issue a second model request with another shape.
        return summary_callback(selected, max_characters=max_characters)  # type: ignore[misc]

    effective_summary_callback = (
        summarize_history_once if callable(summary_callback) else summary_callback
    )
    for level in (
        PressureLevel.TOOL_PAYLOAD_SNIP,
        PressureLevel.TOOL_RESEARCH_MICROCOMPACT,
        PressureLevel.CONVERSATION_COLLAPSE,
        PressureLevel.SESSION_COMPACT,
    ):
        _apply_pressure_stage(
            result,
            level,
            summary_callback=effective_summary_callback,
            hard_input_budget=hard_input_budget,
        )
        applied_levels.append(level)
        after = budget_snapshot(
            result,
            model_context_limit=model_context_limit,
            reserved_output_tokens=reserved_output_tokens,
            reserved_system_margin=reserved_system_margin,
            compact_threshold_tokens=compact_threshold_tokens,
            source_metadata=source_metadata,
        )
        stage_input_tokens.append(after.total_input_tokens)
        # An early target is a compaction preference, while the provider hard
        # budget remains the request invariant.  Keep trying cheaper stages
        # when the prompt fits the provider but is still above that target;
        # protected content may leave it above target and still be valid.
        if after.fits_budget and not after.compact_threshold_exceeded:
            break

    snapshot = replace(
        after,
        level=applied_levels[-1] if applied_levels else after.level,
        tokens_saved=max(0, initial.total_input_tokens - after.total_input_tokens),
        applied_levels=tuple(applied_levels),
        applied_stages=tuple(level.name.lower() for level in applied_levels),
        stage_input_tokens=tuple(stage_input_tokens),
        decision_ms=(time.perf_counter() - started) * 1000,
    )
    return result, _attach_pressure_diagnostics(
        snapshot,
        source_messages,
        result,
        source_metadata=source_metadata,
        decision="compacted",
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


def _turn_has_structured_tool_process(turn: Mapping[str, Any]) -> bool:
    """Use turn metadata/receipts for tool-process classification.

    Ordinary assistant text is intentionally absent from this decision.  A
    tutorial mentioning HTML/OCR or a quoted tool phrase remains a dialogue
    turn unless the transport supplies a role, origin, or receipt.
    """

    if _explicit_message_source(turn) == "tool_evidence":
        return True
    for key in (
        "toolResult", "toolResults", "toolEvidence", "tool_evidence",
        "actionReceipt", "receiptProjection", "evidenceRefs", "evidenceIds",
    ):
        if key in turn and _value_has_structured_tool_evidence(turn.get(key)):
            return True
    return False


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
        # Protection is carried by typed turn metadata.  Ordinary wording such
        # as “你说错了” or “我授权了” must not become an implicit compaction
        # gate merely because it contains a correction or permission keyword.
        correction = False
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
        tool_only = _turn_has_structured_tool_process(turn)
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
