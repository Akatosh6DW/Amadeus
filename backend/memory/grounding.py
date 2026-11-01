"""长期记忆候选的确定性提取与逐字证据校验。

本模块只负责候选结构的规则处理：模型提出的记录先做字段归一化，
再要求 sourceSpan、object 和用户原话互相可验证。它不读写记忆文件，
也不决定是否调用模型；持久化与模型修复分别归其它 memory 服务。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import re
from typing import Any, Callable, Collection, Mapping, Sequence

from agent.fingerprints import runtime_fingerprint


@dataclass(frozen=True)
class MemoryGroundingPorts:
    """候选规范化和时间端口；规则本身不反向依赖 server。"""

    normalize_candidate: Callable[[Any], dict[str, Any] | None]
    candidate_types: Callable[[], Collection[str]]
    normalize_slot: Callable[[Any], str]
    now: Callable[[], datetime]


_USER_SUBJECTS = frozenset({"user", "human", "requester", "customer"})
_NON_USER_ROLES = frozenset({
    "assistant", "system", "tool", "function", "developer", "model",
})
_NON_CONVERSATION_SOURCES = frozenset({
    "tool", "tool_result", "tool_evidence", "web", "web_evidence",
    "research", "ocr", "visual", "attachment", "html",
})
_QUOTE_BOUNDARIES = {
    ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"),
    ("【", "】"), ("《", "》"), ("〈", "〉"), ("\"", "\""),
    ("'", "'"),
}


def _normalized_source_label(value: Any) -> str:
    # Normalize metadata labels only; this is not a classifier over user prose.
    # regex-purpose: format; canonical structured source label.
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _structured_source_rejections(
    raw: Any,
    *,
    memory_type: Any = None,
) -> tuple[str, ...]:
    """Reject explicit non-user provenance without inspecting fact wording."""

    if not isinstance(raw, Mapping):
        return ()
    reasons: list[str] = []
    normalized_type = _normalized_source_label(memory_type or raw.get("type"))
    normalized_subject = _normalized_source_label(raw.get("subject"))
    if normalized_type in {"user_memory", "user_profile", "preference", "user_preference"}:
        if normalized_subject and normalized_subject not in _USER_SUBJECTS:
            reasons.append("subject_not_user")

    def inspect(source: Any) -> None:
        if not isinstance(source, Mapping):
            return
        role = _normalized_source_label(
            source.get("sourceRole")
            or source.get("source_role")
            or source.get("speaker")
            or source.get("role")
        )
        origin = _normalized_source_label(
            source.get("sourceType")
            or source.get("source_type")
            or source.get("origin")
            or source.get("contextSource")
            or source.get("context_source")
        )
        if role in _NON_USER_ROLES:
            reasons.append("source_not_user")
        if origin in _NON_CONVERSATION_SOURCES:
            reasons.append("source_not_conversational")

    inspect(raw)
    for key in ("source", "sourceMetadata", "source_metadata", "subjectSource", "subject_source"):
        inspect(raw.get(key))
    evidence = raw.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
        for item in evidence:
            inspect(item)
    return tuple(dict.fromkeys(reasons))


def _source_turn_rejections(
    raw: Any,
    source_turns: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    if not isinstance(raw, Mapping):
        return ()
    ids: list[str] = []
    declared = raw.get("sourceTurnIds") or raw.get("source_turn_ids")
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        ids.extend(str(item).strip() for item in declared if str(item).strip())
    source_message_id = raw.get("sourceMessageId") or raw.get("source_message_id")
    if source_message_id:
        ids.append(str(source_message_id).strip())
    source_turn_id = raw.get("sourceTurnId") or raw.get("source_turn_id")
    if source_turn_id:
        ids.append(str(source_turn_id).strip())
    evidence = raw.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
        for item in evidence:
            if isinstance(item, Mapping):
                value = (
                    item.get("sourceMessageId")
                    or item.get("source_message_id")
                    or item.get("sourceTurnId")
                    or item.get("source_turn_id")
                    or item.get("turnId")
                    or item.get("turn_id")
                )
                if value:
                    ids.append(str(value).strip())
    reasons: list[str] = []
    for turn_id in dict.fromkeys(ids):
        turn = source_turns.get(turn_id)
        if not isinstance(turn, Mapping):
            # A declared provenance id is authoritative input, never a value
            # that this gate may synthesize or trust without a supplied row.
            reasons.append("source_turn_not_found")
            continue
        role = _normalized_source_label(
            turn.get("sourceRole")
            or turn.get("source_role")
            or turn.get("role")
        )
        origin = _normalized_source_label(
            turn.get("sourceType")
            or turn.get("source_type")
            or turn.get("origin")
            or turn.get("contextSource")
            or turn.get("context_source")
        )
        if role in _NON_USER_ROLES:
            reasons.append("source_turn_not_user")
        if origin in _NON_CONVERSATION_SOURCES:
            reasons.append("source_turn_not_conversational")
    return tuple(dict.fromkeys(reasons))


def _candidate_source_ids(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    identities: list[str] = []
    for key in ("sourceMessageId", "source_message_id", "sourceTurnId", "source_turn_id"):
        value = str(candidate.get(key) or "").strip()
        if value:
            identities.append(value)
    declared = candidate.get("sourceTurnIds") or candidate.get("source_turn_ids")
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        identities.extend(str(item).strip() for item in declared if str(item).strip())
    evidence = candidate.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            for key in (
                "sourceMessageId", "source_message_id", "sourceTurnId",
                "source_turn_id", "turnId",
            ):
                value = str(item.get(key) or "").strip()
                if value:
                    identities.append(value)
                    break
    return tuple(dict.fromkeys(identities))


def _source_row_texts(
    candidate: Mapping[str, Any],
    source_turns: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    texts: list[str] = []
    for identity in _candidate_source_ids(candidate):
        row = source_turns.get(identity)
        if not isinstance(row, Mapping):
            continue
        role = _normalized_source_label(
            row.get("sourceRole")
            or row.get("source_role")
            or row.get("speaker")
            or row.get("role")
        )
        origin = _normalized_source_label(
            row.get("sourceType")
            or row.get("source_type")
            or row.get("origin")
        )
        if role not in _USER_SUBJECTS or origin in _NON_CONVERSATION_SOURCES:
            continue
        for key in ("content", "text", "user", "message"):
            value = str(row.get(key) or "").strip()
            if value:
                texts.append(value)
                break
    return tuple(dict.fromkeys(texts))


def _verified_user_source(
    candidate: Mapping[str, Any],
    source_span: str,
    source_turns: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return true only for an exact quote from a supplied user source row.

    Quote punctuation is not a discourse classifier.  Once the host has
    supplied a real source row whose role is ``user`` and the candidate's
    source span occurs verbatim in that row, the punctuation can remain part
    of the evidence.  Without that row the legacy assertion gate still
    applies, so a model cannot authorize a quoted phrase by naming it.
    """

    if not isinstance(candidate, Mapping) or not source_span:
        return False
    candidate_role = _normalized_source_label(
        candidate.get("sourceRole")
        or candidate.get("source_role")
        or candidate.get("speaker")
        or candidate.get("role")
    )
    if candidate_role and candidate_role not in _USER_SUBJECTS:
        return False
    candidate_origin = _normalized_source_label(
        candidate.get("sourceType")
        or candidate.get("source_type")
        or candidate.get("origin")
    )
    if candidate_origin in _NON_CONVERSATION_SOURCES:
        return False
    for identity in _candidate_source_ids(candidate):
        row = source_turns.get(identity)
        if not isinstance(row, Mapping):
            continue
        role = _normalized_source_label(
            row.get("sourceRole")
            or row.get("source_role")
            or row.get("speaker")
            or row.get("role")
        )
        origin = _normalized_source_label(
            row.get("sourceType")
            or row.get("source_type")
            or row.get("origin")
        )
        if role not in _USER_SUBJECTS or origin in _NON_CONVERSATION_SOURCES:
            continue
        row_text = next(
            (str(row.get(key) or "") for key in ("content", "text", "user", "message")
             if str(row.get(key) or "").strip()),
            "",
        )
        if source_span in row_text:
            return True
    return False


def _diagnostic_hash(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        payload = repr(value)
    return runtime_fingerprint(payload, length=24)


def _is_quote_wrapped(value: Any) -> bool:
    text = str(value or "").strip()
    if len(text) < 2:
        return False
    return any(text.startswith(left) and text.endswith(right) for left, right in _QUOTE_BOUNDARIES)


class MemoryGroundingService:
    """拥有记忆候选的纯提取、话语断言判断和逐字证据 gate。"""

    def __init__(self, ports: MemoryGroundingPorts) -> None:
        self.ports = ports

    @staticmethod
    def grounding_diagnostic(
        updates: Sequence[Any],
        accepted: Sequence[Mapping[str, Any]],
        *,
        source_type: str = "user_message",
        decision: str | None = None,
        rejection_diagnostics: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Build a log-safe record for a memory subject transformation.

        Only source category, decision, counts, and fingerprints are exposed;
        candidate text, quotes, and credentials never enter the returned
        structure.
        """

        input_count = len(updates)
        output_count = len(accepted)
        resolved_decision = decision or (
            "accepted" if output_count == input_count and input_count else
            "partial" if output_count else
            "rejected"
        )
        input_hash = _diagnostic_hash(list(updates))
        output_hash = _diagnostic_hash(list(accepted))
        return {
            "sourceType": _normalized_source_label(source_type)[:80] or "unknown",
            "decision": str(resolved_decision)[:80],
            "inputCount": input_count,
            "outputCount": output_count,
            "inputHash": input_hash,
            "outputHash": output_hash,
            "input": {
                "count": input_count,
                "fingerprint": input_hash,
            },
            "output": {
                "count": output_count,
                "fingerprint": output_hash,
            },
            "rejectedCount": max(0, input_count - output_count),
            "rejectionReasonCount": len(tuple(rejection_diagnostics)),
        }

    memory_grounding_diagnostic = grounding_diagnostic

    @staticmethod
    def clause_is_asserted(
        clause: str,
        *,
        explicit_request: bool = False,
        modality: str = "",
    ) -> bool:
        """Classify discourse force before extracting any fact from a clause.

        A model-provided modality is the semantic owner of discourse force for
        structured candidates.  Surface punctuation and question words remain
        a fallback only when that field is absent or unknown.
        """

        compact = re.sub(r"\s+", "", str(clause or ""))
        if not compact:
            return False
        normalized_modality = str(modality or "").strip().lower()
        if normalized_modality in {"asserted", "requested", "corrected"}:
            return True
        if explicit_request:
            return True
        if "?" in compact or "？" in compact:
            return False
        if compact.startswith(("如果", "假如", "假设", "要是", "万一")):
            return False
        if compact.startswith(("别人说", "有人说", "他说", "她说", "它说", "听说")):
            return False
        if re.search(r"(?:会不会|是否|是不是|什么|哪(?:个|种|里)|谁|为何|为什么)$", compact):
            return False
        return True

    @staticmethod
    def _candidate(
        content: str,
        *,
        source_span: str,
        predicate: str,
        object_value: str,
        memory_type: str = "user_memory",
        polarity: str = "positive",
        modality: str = "asserted",
        temporal: str = "current",
        importance: float = 0.9,
        valid_until: str = "",
    ) -> dict[str, Any]:
        return {
            "content": content,
            "type": memory_type,
            "importance": importance,
            "shouldSave": True,
            "subject": "user",
            "predicate": predicate,
            "object": object_value,
            "polarity": polarity,
            "modality": modality,
            "temporal": temporal,
            "sourceSpan": source_span,
            "validUntil": valid_until,
            "userPinned": False,
        }

    def extract_candidates(self, text: str) -> list[dict[str, Any]]:
        """Extract a small typed fact set after discourse-level validation.

        Patterns identify candidate slots only. Questions, hypotheticals,
        reported speech and transient observations are rejected by the
        surrounding clause classifier before a candidate can be produced.
        """

        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return []
        candidates: list[dict[str, Any]] = []
        clauses = [
            item.strip()
            for item in re.split(r"(?<=[。！？?!\n])|[；;]", value)
            if item.strip()
        ] or [value]
        for clause in clauses:
            explicit = re.match(
                r"^(?:请)?(?:帮我)?(?:记住|记下来)(?:[:：]\s*|\s*)(.{2,160})",
                clause,
            )
            if explicit:
                fact = explicit.group(1).strip(" ，,:：。！？?!")
                if fact:
                    transient_request = bool(re.search(
                        r"正在|当前|此刻|刚才|今天|这次|屏幕|截图|摄像头|在看|开着",
                        fact,
                    ))
                    candidates.append(self._candidate(
                        "用户要求记住：" + fact,
                        source_span=explicit.group(0).strip(),
                        predicate="explicit_memory_request",
                        object_value=fact,
                        modality="requested",
                        temporal="current",
                        importance=1.0,
                        memory_type="episode" if transient_request else "user_memory",
                        valid_until=(
                            (self.ports.now() + timedelta(hours=24)).isoformat(
                                timespec="seconds"
                            )
                            if transient_request else ""
                        ),
                    ))
                # Do not also infer a second implicit fact from an explicit
                # request; the requested record preserves user authorization.
                continue
            if not self.clause_is_asserted(clause):
                continue
            transient = bool(re.search(
                r"正在|当前|此刻|刚才|今天|这次|屏幕|截图|摄像头|在看|开着",
                clause,
            ))
            if transient:
                continue
            corrected_preference = re.search(
                r"我以前(?:很)?喜欢\s*([^，。！？?!]{1,60})[，,但不过]*现在(?:已经)?不喜欢(?:它|这个|那个)?了?",
                clause,
            )
            if corrected_preference:
                object_value = corrected_preference.group(1).strip(" ，,:：。！？?!")
                candidates.append(self._candidate(
                    "用户现在不喜欢" + object_value,
                    source_span=corrected_preference.group(0).strip(),
                    predicate="preference",
                    object_value=object_value,
                    polarity="negative",
                    modality="corrected",
                ))
                continue
            preference = re.search(
                r"(?:^|[，,。！？?!]\s*)我(?:现在|一直|很|非常)?\s*(喜欢|讨厌|不喜欢)\s*([^，。！？?!]{1,60})",
                clause,
            )
            if preference:
                verb = preference.group(1)
                object_value = preference.group(2).strip(" ，,:：。！？?!")
                if object_value:
                    candidates.append(self._candidate(
                        "用户" + verb + object_value,
                        source_span=preference.group(0).strip(" ，,。！？?!"),
                        predicate="preference",
                        object_value=object_value,
                        polarity="negative" if verb in {"讨厌", "不喜欢"} else "positive",
                        temporal="habitual",
                    ))
                continue
            profile = re.search(
                r"(?:^|[，,。！？?!]\s*)我的\s*([^，。！？?!]{1,24})\s*是\s*([^，。！？?!]{1,100})",
                clause,
            )
            if profile:
                predicate = profile.group(1).strip()
                object_value = profile.group(2).strip()
                if predicate and object_value:
                    candidates.append(self._candidate(
                        f"用户的{predicate}是{object_value}",
                        source_span=profile.group(0).strip(" ，,。！？?!"),
                        predicate=predicate,
                        object_value=object_value,
                    ))
                continue
            future = re.search(r"(?:^|[，,。！？?!]\s*)以后(?:请|要)?\s*(.{2,120})", clause)
            if future:
                object_value = future.group(1).strip(" ，,:：。！？?!")
                object_value = re.sub(r"^我", "", object_value).strip()
                durable_routine = bool(re.search(
                    r"每天|每周|每月|每次|总是|都要|不要|记得|称呼|回复|回答",
                    object_value,
                ))
                if durable_routine:
                    candidates.append(self._candidate(
                        "用户希望以后" + object_value,
                        source_span=future.group(0).strip(" ，,。！？?!"),
                        predicate="future_routine",
                        object_value=object_value,
                        temporal="future",
                    ))
        deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for candidate in candidates:
            key = (
                str(candidate.get("predicate") or ""),
                str(candidate.get("object") or ""),
                str(candidate.get("polarity") or ""),
            )
            deduplicated[key] = candidate
        return list(deduplicated.values())[:4]

    def extract_memories(self, text: str) -> list[str]:
        """Legacy string projection of the structured deterministic extractor."""

        return [item["content"] for item in self.extract_candidates(text)][:4]

    def grounded_candidates(
        self,
        updates: Sequence[Any],
        user_message: str,
        *,
        limit: int = 4,
        diagnostics: list[str] | None = None,
        source_messages: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None = None,
        source_type: str = "user_message",
    ) -> list[dict[str, Any]]:
        """Accept only candidates whose slot and value have user evidence."""

        evidence = str(user_message or "")
        source_turns: dict[str, Mapping[str, Any]] = {}
        if isinstance(source_messages, Mapping):
            source_turns = {
                str(key).strip(): value
                for key, value in source_messages.items()
                if isinstance(value, Mapping)
            }
        elif isinstance(source_messages, Sequence) and not isinstance(source_messages, (str, bytes)):
            for value in source_messages:
                if not isinstance(value, Mapping):
                    continue
                for key in (
                    "sourceMessageId", "source_message_id", "messageId", "message_id",
                    "sourceTurnId", "source_turn_id", "turnId", "turn_id", "id",
                ):
                    identity = str(value.get(key) or "").strip()
                    if identity:
                        source_turns[identity] = value
        grounded: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(updates):
            prefix = f"candidate_{index}:"
            structured_rejections = (
                *_structured_source_rejections(raw),
                *_source_turn_rejections(raw, source_turns),
            )
            if structured_rejections:
                if diagnostics is not None:
                    diagnostics.extend(prefix + reason for reason in dict.fromkeys(structured_rejections))
                continue
            candidate = self.ports.normalize_candidate(raw)
            if candidate is None:
                if diagnostics is not None:
                    diagnostics.append(prefix + "invalid_structure")
                continue
            if not candidate["shouldSave"]:
                if diagnostics is not None:
                    diagnostics.append(prefix + "should_save_false")
                continue
            if candidate.get("type") not in self.ports.candidate_types():
                if diagnostics is not None:
                    diagnostics.append(prefix + "invalid_type")
                continue
            object_value = self.ports.normalize_slot(candidate.get("object"))
            source_span = str(candidate.get("sourceSpan") or "").strip()
            source_texts = _source_row_texts(candidate, source_turns)
            evidence_texts = source_texts or (evidence,)
            if not source_span:
                source_span = next((
                    str(item.get("quote") or "").strip()
                    for item in candidate.get("evidence", [])
                    if isinstance(item, Mapping)
                    and any(
                        str(item.get("quote") or "").strip() in text
                        for text in evidence_texts
                    )
                ), "")
            rejection_reasons: list[str] = []
            if not source_span:
                rejection_reasons.append("missing_source_span")
            elif not any(source_span in text for text in evidence_texts):
                rejection_reasons.append(
                    "source_span_not_in_source_message"
                    if source_texts else "source_span_not_in_user_message"
                )
            elif not self.clause_is_asserted(
                source_span,
                explicit_request=candidate.get("modality") == "requested",
                modality=str(candidate.get("modality") or ""),
            ):
                rejection_reasons.append("source_span_not_asserted")
            elif (
                _is_quote_wrapped(source_span)
                and candidate.get("modality") != "requested"
                and not _verified_user_source(candidate, source_span, source_turns)
            ):
                rejection_reasons.append("quoted_source_not_assertion")
            if not object_value:
                rejection_reasons.append("missing_object")
            elif object_value not in self.ports.normalize_slot(source_span):
                rejection_reasons.append("object_not_in_source_span")
            if rejection_reasons:
                if diagnostics is not None:
                    diagnostics.extend(prefix + reason for reason in rejection_reasons)
                continue
            merged = dict(candidate)
            merged["sourceSpan"] = source_span
            merged_key = self.ports.normalize_slot(merged.get("content"))
            if merged_key and merged_key not in seen:
                seen.add(merged_key)
                grounded.append(merged)
        return grounded[:max(0, min(32, int(limit)))]

    def validated_summary_candidates(
        self,
        value: Any,
        turns: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """只接受能逐字回指用户原话的摘要记忆候选。"""

        if not isinstance(value, list):
            return []

        def turn_identity(turn: Mapping[str, Any], index: int) -> str:
            return str(
                turn.get("requestId")
                or turn.get("turnId")
                or turn.get("sourceTurnId")
                or turn.get("messageId")
                or turn.get("sourceMessageId")
                or turn.get("id")
            )[:120]

        def evidence_text(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()

        user_turns: dict[str, str] = {}
        correction_turn_ids: set[str] = set()
        source_turn_rejections: dict[str, tuple[str, ...]] = {}
        for index, turn in enumerate(turns):
            if not isinstance(turn, Mapping):
                continue
            turn_id = turn_identity(turn, index)
            role = _normalized_source_label(
                turn.get("sourceRole")
                or turn.get("source_role")
                or turn.get("role")
            )
            origin = _normalized_source_label(
                turn.get("sourceType")
                or turn.get("source_type")
                or turn.get("origin")
                or turn.get("contextSource")
                or turn.get("context_source")
            )
            turn_reasons: list[str] = []
            if role in _NON_USER_ROLES:
                turn_reasons.append("source_turn_not_user")
            if origin in _NON_CONVERSATION_SOURCES:
                turn_reasons.append("source_turn_not_conversational")
            if turn_reasons:
                source_turn_rejections[turn_id] = tuple(dict.fromkeys(turn_reasons))
            user_text = evidence_text(turn.get("user"))
            if user_text:
                user_turns[turn_id] = user_text
            protection = (
                turn.get("contextProtection")
                if isinstance(turn.get("contextProtection"), Mapping)
                else {}
            )
            reasons = protection.get("reasons")
            if isinstance(reasons, (list, tuple)) and "correction" in reasons:
                correction_turn_ids.add(turn_id)

        accepted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in value[:8]:
            structured_rejections = _structured_source_rejections(raw)
            if structured_rejections:
                continue
            candidate = self.ports.normalize_candidate(raw)
            if (
                candidate is None
                or not candidate["shouldSave"]
                or candidate["importance"] < 0.68
                or candidate["type"] not in self.ports.candidate_types()
            ):
                continue
            evidence = candidate.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                continue
            source_ids: list[str] = []
            exact_quotes: list[str] = []
            valid = True
            for item in evidence:
                if not isinstance(item, Mapping):
                    valid = False
                    break
                turn_id = str(item.get("turnId") or "").strip()
                quote = evidence_text(item.get("quote"))
                source_text = user_turns.get(turn_id, "")
                if (
                    not turn_id
                    or not quote
                    or _is_quote_wrapped(quote)
                    or quote not in source_text
                    or turn_id in source_turn_rejections
                ):
                    valid = False
                    break
                source_ids.append(turn_id)
                exact_quotes.append(quote)
            if not valid:
                continue
            declared_source_ids = [
                str(item).strip()
                for item in candidate.get("sourceTurnIds", ())
                if str(item).strip()
            ]
            if declared_source_ids and set(declared_source_ids) != set(source_ids):
                continue
            if (
                candidate["type"] == "correction"
                and not correction_turn_ids.intersection(source_ids)
            ):
                # 摘要模型不能把普通对比或不赞同升级成纠正；话语行为在写入时已定型。
                continue
            normalized = self.ports.normalize_slot(candidate["content"])
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidate["sourceTurnIds"] = list(dict.fromkeys(source_ids))
            candidate["sourceSpan"] = " | ".join(dict.fromkeys(exact_quotes))[:600]
            accepted.append(candidate)
        return accepted[:6]

    def grounded_updates(
        self,
        updates: list[Any],
        user_message: str,
        *,
        limit: int = 4,
        source_messages: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None = None,
        source_type: str = "user_message",
    ) -> list[str]:
        return [
            item["content"]
            for item in self.grounded_candidates(
                updates,
                user_message,
                limit=limit,
                source_messages=source_messages,
                source_type=source_type,
            )
        ]
