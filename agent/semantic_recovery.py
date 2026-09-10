"""Recover useful dialogue semantics from provider formatting drift.

This module never decides tools, research policy, persona, or the answer
content.  It only extracts a candidate from the *same* provider response and
classifies whether that candidate is safe enough for field-only completion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping


_PROTOCOL_LEAK = re.compile(
    r"(?i)(?:"
    r"\b(?:system|developer|assistant|tool)_prompt\b|"
    r"\b(?:tool_calls?|toolRequests|function_call|memoryUpdates)\b|"
    r"\b(?:turn_header|turn_trailer|turn_abort)\b|"
    r"<\|(?:system|assistant|tool|end)[^>]*\|>|"
    r"authorization\s*:\s*bearer|"
    r"\bapi[_ -]?key\b\s*[:=]"
    r")"
)
_PROVIDER_ERROR = re.compile(
    r"(?i)^\s*(?:error|exception|traceback|http\s*[45]\d\d|"
    r"service unavailable|rate limit|bad gateway|请求失败|服务不可用|调用失败)\b"
)
_OBVIOUS_FRAGMENT = re.compile(
    r"(?:但是|不过|因为|所以|而且|如果|除非|虽然|以及|然后|换句话说|"
    r"つまり|だから|けれど|そして|または)\s*[，、,:：]?\s*$"
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
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


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
    payload = dict(value.get("payload") or {}) if isinstance(value.get("payload"), Mapping) else dict(value)
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


def _candidate_risks(payload: Mapping[str, Any], raw: str) -> list[str]:
    reply = str(payload.get("reply") or payload.get("textZh") or "").strip()
    reasons: list[str] = []
    if not reply:
        reasons.append("missing_visible_reply")
        return reasons
    if len(reply) > 6000:
        reasons.append("reply_too_large")
    if _PROTOCOL_LEAK.search(reply) or _PROTOCOL_LEAK.search(raw[:12000]):
        reasons.append("protocol_or_prompt_leak")
    if _PROVIDER_ERROR.search(reply):
        reasons.append("provider_error_text")
    if re.search(r"```|^\s*[{[]", reply):
        reasons.append("structured_dump_as_reply")
    if _OBVIOUS_FRAGMENT.search(reply):
        reasons.append("obvious_trailing_fragment")
    if not re.search(r"[\u3400-\u9fff]", reply):
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
        payload = _dialogue_payload(direct)
        risks = _candidate_risks(payload, raw)
        return SemanticRecoveryResult(
            "accepted" if not risks else "rejected",
            "strict_json",
            payload,
            tuple(risks),
            len(raw),
        )

    for fenced in re.findall(r"(?is)```(?:json)?\s*(.*?)```", raw):
        parsed = _json_mapping(fenced.strip())
        if parsed is not None:
            payload = _dialogue_payload(parsed)
            risks = _candidate_risks(payload, fenced)
            return SemanticRecoveryResult(
                "accepted" if not risks else "rejected",
                "fenced_json",
                payload,
                tuple(risks),
                len(raw),
            )

    for candidate in _balanced_json_candidates(raw):
        parsed = _json_mapping(candidate)
        if parsed is not None:
            payload = _dialogue_payload(parsed)
            if not str(payload.get("reply") or "").strip():
                # A tool/protocol fragment embedded in otherwise plain output
                # is not itself a dialogue candidate. Continue so the whole
                # response is screened for leaks below.
                continue
            risks = _candidate_risks(payload, candidate)
            return SemanticRecoveryResult(
                "accepted" if not risks else "rejected",
                "embedded_json",
                payload,
                tuple(risks),
                len(raw),
            )

    closed = _close_truncated_object(raw)
    if closed:
        parsed = _json_mapping(closed)
        if parsed is not None:
            payload = _dialogue_payload(parsed)
            risks = _candidate_risks(payload, raw)
            return SemanticRecoveryResult(
                "accepted" if not risks else "ambiguous",
                "closed_partial_json",
                payload,
                tuple(risks or ["partial_json_requires_field_completion"]),
                len(raw),
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
    if _PROVIDER_ERROR.search(raw):
        return SemanticRecoveryResult(
            "rejected",
            "plain_text",
            reasons=("provider_error_text",),
            raw_length=len(raw),
        )
    payload = {"reply": raw, "textZh": raw}
    risks = _candidate_risks(payload, raw)
    return SemanticRecoveryResult(
        "accepted" if not risks else "ambiguous",
        "plain_text",
        payload,
        tuple(risks),
        len(raw),
    )
