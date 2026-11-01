"""Recover useful dialogue semantics from provider formatting drift.

This module never decides tools, research policy, persona, or the answer
content.  It only extracts a candidate from the *same* provider response and
classifies whether that candidate is safe enough for field-only completion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from backend.providers.errors import classify_provider_response_body
from backend.providers.reply_protocol import (
    is_reply_envelope,
    looks_like_truncated_reply_envelope,
    parse_json_document,
    parse_json_mapping,
    unwrap_reply_envelope,
)

_TAG_PATTERN = re.compile(
    r"(?is)<(?P<name>textZh|reply|spokenTextJa|emotion)>\s*"
    r"(?P<value>.*?)\s*</(?P=name)>"
)
_LINE_FIELD_PATTERN = re.compile(
    r"(?im)^\s*(?P<name>textZh|reply|spokenTextJa|emotion)\s*[:：]\s*(?P<value>.+?)\s*$"
)


@dataclass(frozen=True)
class SemanticRecoveryResult:
    status: str
    mode: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    raw_length: int = 0

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "mode": self.mode,
            "reasons": list(self.reasons),
            "rawLength": self.raw_length,
        }
        if include_payload:
            result["payload"] = dict(self.payload)
        return result


def _json_mapping(value: str) -> Mapping[str, Any] | None:
    return parse_json_mapping(value)


def _balanced_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for opening, closing in (("{", "}"), ("[", "]")):
        start = text.find(opening)
        while start >= 0:
            depth = 0
            quoted = False
            escaped = False
            for index in range(start, len(text)):
                char = text[index]
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                    continue
                if char == '"':
                    quoted = True
                elif char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start : index + 1])
                        break
            start = text.find(opening, start + 1)
    return candidates


def _close_truncated_object(text: str) -> str | None:
    stripped = text.strip()
    if not stripped.startswith("{") or stripped.endswith("}"):
        return None
    quoted = False
    escaped = False
    curly = 0
    square = 0
    for char in stripped:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
        if curly < 0 or square < 0:
            return None
    if quoted or curly <= 0 or square < 0:
        return None
    return stripped.rstrip().rstrip(",") + ("]" * square) + ("}" * curly)


def _dialogue_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    value = unwrap_reply_envelope(value)
    payload = dict(value.get("payload") or {}) if isinstance(value.get("payload"), Mapping) else dict(value)
    if isinstance(value.get("payload"), Mapping):
        payload = {**value, **dict(value["payload"])}
    if isinstance(payload.get("utterances"), list):
        utterances = [item for item in payload["utterances"] if isinstance(item, Mapping)]
        if utterances:
            payload = dict(utterances[0])
    reply = str(
        payload.get("reply")
        or payload.get("textZh")
        or payload.get("zh")
        or payload.get("text")
        or ""
    ).strip()
    spoken = str(
        payload.get("spokenTextJa")
        or payload.get("spoken_text_ja")
        or payload.get("spokenJa")
        or payload.get("ja")
        or ""
    ).strip()
    return {
        **payload,
        "reply": reply,
        "textZh": reply,
        **({"spokenTextJa": spoken} if spoken else {}),
    }


def _candidate_risks(
    payload: Mapping[str, Any],
    raw: str,
    *,
    plain_text_candidate: bool = False,
    structured_envelope: bool = False,
) -> list[str]:
    reply = str(payload.get("reply") or payload.get("textZh") or "").strip()
    reasons: list[str] = []
    if not reply:
        reasons.append("missing_visible_reply")
        return reasons
    if len(reply) > 6000:
        reasons.append("reply_too_large")
    # Once the transport has yielded a complete, recognized envelope, the
    # visible reply is content.  Adapter error detection belongs to the raw
    # body/plain-text path; otherwise legitimate prose such as ``Error
    # handling ...`` would be reclassified after successful parsing.
    if not structured_envelope and classify_provider_response_body(reply).is_error:
        reasons.append("provider_error_text")
    # Plain salvage is the legacy Chinese dialogue path.  Keep its transport
    # sanity check so an arbitrary provider diagnostic (for example
    # ``provider protocol noise``) cannot become a visible utterance.  This is
    # deliberately scoped to unstructured salvage; parsed envelopes may carry
    # English technical prose, code or JSON examples without this gate.
    # This is salvage transport sanity only; parsed envelopes never use this
    # language heuristic and technical prose remains visible content.
    # regex-purpose: protocol
    if plain_text_candidate and not re.search(r"[\u3400-\u9fff]", reply):
        complete_plain_artifact = False
        parsed_artifact = parse_json_document(raw)
        if isinstance(parsed_artifact, (Mapping, list)) and not is_reply_envelope(parsed_artifact):
            complete_plain_artifact = True
        elif raw.strip().startswith("```") and raw.strip().endswith("```"):
            complete_plain_artifact = True
        if not complete_plain_artifact:
            reasons.append("no_chinese_visible_text")
    return reasons


def recover_semantic_output(
    raw_text: str,
    *,
    allow_plain_text: bool,
) -> SemanticRecoveryResult:
    raw = str(raw_text or "").strip()
    if not raw:
        return SemanticRecoveryResult("rejected", "empty", reasons=("empty_response",))
    if len(raw) > 1_000_000:
        return SemanticRecoveryResult(
            "rejected",
            "oversized",
            reasons=("response_exceeds_recovery_bound",),
            raw_length=len(raw),
        )

    direct = _json_mapping(raw)
    if direct is not None:
        normalized = unwrap_reply_envelope(direct)
        if is_reply_envelope(normalized):
            payload = _dialogue_payload(normalized)
            risks = _candidate_risks(payload, raw, structured_envelope=True)
            return SemanticRecoveryResult(
                "accepted" if not risks else "rejected",
                "fenced_json" if raw.startswith("```") else "strict_json",
                payload,
                tuple(risks),
                len(raw),
            )
        provider_classification = classify_provider_response_body(direct)
        if provider_classification.is_error:
            return SemanticRecoveryResult(
                "rejected",
                provider_classification.category,
                reasons=(provider_classification.category,),
                raw_length=len(raw),
            )
        # A complete JSON object without a visible reply may be a user-requested
        # code/JSON example.  Preserve it as text when salvage is enabled; never
        # claim that an arbitrary object was a valid dialogue envelope.
        if not allow_plain_text:
            return SemanticRecoveryResult(
                "rejected",
                "strict_json",
                reasons=("missing_visible_reply",),
                raw_length=len(raw),
            )

    # ``parse_json_mapping`` already handles a fence that wraps the entire
    # response.  A fence embedded in surrounding prose is a user-facing code
    # example and must remain part of that prose; extracting it would violate
    # the whole-body envelope boundary.

    # A complete envelope is unwrapped only when it is the whole response.
    # Do not extract a ``{"reply": ...}`` object from ordinary prose: that
    # object may be a user-facing JSON example, and stripping its surrounding
    # explanation would lose legitimate content.  ``_balanced_json_candidates``
    # remains available to compatibility callers, but embedded values stay
    # plain text here.

    closed = _close_truncated_object(raw)
    if closed:
        parsed = _json_mapping(closed)
        if parsed is not None:
            payload = _dialogue_payload(parsed)
            risks = _candidate_risks(payload, raw, structured_envelope=True)
            return SemanticRecoveryResult(
                "accepted" if not risks else "ambiguous",
                "closed_partial_json",
                payload,
                tuple(risks or ["partial_json_requires_field_completion"]),
                len(raw),
            )

    if looks_like_truncated_reply_envelope(raw):
        # Do not fall through to plain-text salvage: a malformed protocol
        # object must never be shown verbatim.  The bounded closing repair above
        # is the only permitted salvage path for a complete visible reply.
        return SemanticRecoveryResult(
            "rejected",
            "protocol_invalid",
            reasons=("malformed_or_truncated_envelope",),
            raw_length=len(raw),
        )

    tagged: dict[str, Any] = {}
    for match in _TAG_PATTERN.finditer(raw):
        tagged[match.group("name")] = match.group("value").strip()
    if not tagged:
        for match in _LINE_FIELD_PATTERN.finditer(raw):
            tagged[match.group("name")] = match.group("value").strip()
    if tagged:
        payload = _dialogue_payload(tagged)
        risks = _candidate_risks(payload, raw)
        return SemanticRecoveryResult(
            "accepted" if not risks else "ambiguous",
            "tagged_fields",
            payload,
            tuple(risks),
            len(raw),
        )

    if not allow_plain_text:
        return SemanticRecoveryResult(
            "ambiguous",
            "plain_text_disabled",
            reasons=("plain_text_candidate_requires_enabled_salvage",),
            raw_length=len(raw),
        )
    provider_classification = classify_provider_response_body(raw)
    if provider_classification.is_error:
        return SemanticRecoveryResult(
            "rejected",
            "plain_text",
            reasons=(provider_classification.category,),
            raw_length=len(raw),
        )
    payload = {"reply": raw, "textZh": raw}
    risks = _candidate_risks(payload, raw, plain_text_candidate=True)
    return SemanticRecoveryResult(
        "accepted" if not risks else "ambiguous",
        "plain_text",
        payload,
        tuple(risks),
        len(raw),
    )
