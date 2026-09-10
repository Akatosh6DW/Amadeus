"""Structured speaker, quotation, and cross-turn reference ledger.

The ledger separates message provenance from quoted speech and visual-text
authorship.  It is deliberately provider- and storage-agnostic: server.py owns
I/O while this module supplies deterministic, serializable discourse records.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from typing import Any, Literal, Mapping, Sequence

from .fingerprints import runtime_fingerprint


Speaker = Literal["user", "assistant", "third_party", "system", "unknown"]
SourceKind = Literal[
    "chat_message", "screen_ocr", "attachment_ocr", "tool_observation", "summary",
]
ResolutionStatus = Literal["resolved", "ambiguous", "unresolved", "self_contained"]

LEDGER_SCHEMA = "amadeus.discourse-ledger.v1"
USER_ENTITY_ID = "participant:user"
ASSISTANT_ENTITY_ID = "participant:assistant"
UNKNOWN_ENTITY_ID = "participant:unknown"


def _stable_id(prefix: str, *parts: str) -> str:
    material = "\x1f".join(str(item or "") for item in parts)
    return f"{prefix}_" + runtime_fingerprint(material)


def _speaker(value: Any) -> Speaker:
    normalized = str(value or "unknown").strip().lower()
    if normalized in {"user", "assistant", "third_party", "system", "unknown"}:
        return normalized  # type: ignore[return-value]
    return "unknown"


def _source_kind(value: Any) -> SourceKind:
    normalized = str(value or "chat_message").strip().lower()
    if normalized in {
        "chat_message", "screen_ocr", "attachment_ocr", "tool_observation", "summary",
    }:
        return normalized  # type: ignore[return-value]
    return "chat_message"


def _status(value: Any) -> ResolutionStatus:
    normalized = str(value or "unresolved").strip().lower()
    if normalized in {"resolved", "ambiguous", "unresolved", "self_contained"}:
        return normalized  # type: ignore[return-value]
    return "unresolved"


def _float01(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if number != number:  # NaN must not leak into persisted JSON.
        return 0.0
    return max(0.0, min(1.0, number))


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class TextSpan:
    start: int
    end: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TextSpan":
        return cls(
            start=_nonnegative_int(value.get("start")),
            end=_nonnegative_int(value.get("end")),
            text=str(value.get("text") or ""),
        )


@dataclass(frozen=True)
class LedgerSource:
    source_id: str
    kind: SourceKind
    artifact_id: str | None = None
    submitted_by: Speaker = "unknown"
    bbox: tuple[float, float, float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bbox"] = list(self.bbox) if self.bbox else None
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LedgerSource":
        bbox_value = value.get("bbox")
        bbox = None
        if isinstance(bbox_value, (list, tuple)) and len(bbox_value) == 4:
            try:
                bbox = tuple(float(item) for item in bbox_value)  # type: ignore[assignment]
            except (TypeError, ValueError):
                bbox = None
        return cls(
            source_id=str(value.get("source_id") or value.get("sourceId") or ""),
            kind=_source_kind(value.get("kind")),
            artifact_id=str(value.get("artifact_id") or value.get("artifactId") or "").strip() or None,
            submitted_by=_speaker(value.get("submitted_by") or value.get("submittedBy")),
            bbox=bbox,
        )


@dataclass(frozen=True)
class LedgerMessage:
    message_id: str
    turn_id: str
    speaker: Speaker
    text: str
    source_id: str
    modality: SourceKind = "chat_message"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LedgerMessage":
        return cls(
            message_id=str(value.get("message_id") or value.get("messageId") or ""),
            turn_id=str(value.get("turn_id") or value.get("turnId") or ""),
            speaker=_speaker(value.get("speaker")),
            text=str(value.get("text") or ""),
            source_id=str(value.get("source_id") or value.get("sourceId") or ""),
            modality=_source_kind(value.get("modality")),
            created_at=str(value.get("created_at") or value.get("createdAt") or ""),
        )


@dataclass(frozen=True)
class QuoteRef:
    quote_id: str
    message_id: str
    span: TextSpan
    quoted_speaker: Speaker = "unknown"
    quoted_speaker_entity_id: str | None = None
    source_message_id: str | None = None
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["span"] = self.span.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QuoteRef":
        span = value.get("span") if isinstance(value.get("span"), Mapping) else {}
        return cls(
            quote_id=str(value.get("quote_id") or value.get("quoteId") or ""),
            message_id=str(value.get("message_id") or value.get("messageId") or ""),
            span=TextSpan.from_dict(span),
            quoted_speaker=_speaker(value.get("quoted_speaker") or value.get("quotedSpeaker")),
            quoted_speaker_entity_id=str(
                value.get("quoted_speaker_entity_id") or value.get("quotedSpeakerEntityId") or ""
            ).strip() or None,
            source_message_id=str(
                value.get("source_message_id") or value.get("sourceMessageId") or ""
            ).strip() or None,
            confidence=_float01(value.get("confidence")),
        )


@dataclass(frozen=True)
class Mention:
    mention_id: str
    message_id: str
    span: TextSpan
    kind: str
    grammatical_role: str
    quote_id: str | None = None
    entity_id: str | None = None
    effective_speaker: Speaker = "unknown"
    status: ResolutionStatus = "unresolved"
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["span"] = self.span.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Mention":
        span = value.get("span") if isinstance(value.get("span"), Mapping) else {}
        return cls(
            mention_id=str(value.get("mention_id") or value.get("mentionId") or ""),
            message_id=str(value.get("message_id") or value.get("messageId") or ""),
            span=TextSpan.from_dict(span),
            kind=str(value.get("kind") or "pronoun"),
            grammatical_role=str(value.get("grammatical_role") or value.get("grammaticalRole") or "referent"),
            quote_id=str(value.get("quote_id") or value.get("quoteId") or "").strip() or None,
            entity_id=str(value.get("entity_id") or value.get("entityId") or "").strip() or None,
            effective_speaker=_speaker(value.get("effective_speaker") or value.get("effectiveSpeaker")),
            status=_status(value.get("status")),
            confidence=_float01(value.get("confidence")),
        )


@dataclass(frozen=True)
class ReferenceLink:
    link_id: str
    source_mention_id: str
    target_message_id: str | None = None
    target_entity_id: str | None = None
    relation: str = "coreference"
    candidate_ids: tuple[str, ...] = ()
    status: ResolutionStatus = "unresolved"
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["candidate_ids"] = list(self.candidate_ids)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReferenceLink":
        candidates = value.get("candidate_ids") or value.get("candidateIds") or []
        return cls(
            link_id=str(value.get("link_id") or value.get("linkId") or ""),
            source_mention_id=str(
                value.get("source_mention_id") or value.get("sourceMentionId") or ""
            ),
            target_message_id=str(
                value.get("target_message_id") or value.get("targetMessageId") or ""
            ).strip() or None,
            target_entity_id=str(
                value.get("target_entity_id") or value.get("targetEntityId") or ""
            ).strip() or None,
            relation=str(value.get("relation") or "coreference"),
            candidate_ids=tuple(str(item) for item in candidates if str(item).strip()),
            status=_status(value.get("status")),
            confidence=_float01(value.get("confidence")),
        )


@dataclass(frozen=True)
class ReferenceResolution:
    message_id: str
    mentions: tuple[Mention, ...]
    links: tuple[ReferenceLink, ...]
    antecedent_message_ids: tuple[str, ...]
    unresolved_mention_ids: tuple[str, ...]
    ambiguous_mention_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "mentions": [item.to_dict() for item in self.mentions],
            "links": [item.to_dict() for item in self.links],
            "antecedentMessageIds": list(self.antecedent_message_ids),
            "unresolvedMentionIds": list(self.unresolved_mention_ids),
            "ambiguousMentionIds": list(self.ambiguous_mention_ids),
        }


@dataclass(frozen=True)
class ReferenceAudit:
    valid: bool
    issues: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [dict(item) for item in self.issues],
        }


@dataclass(frozen=True)
class DiscourseLedger:
    schema: str = LEDGER_SCHEMA
    current_message_id: str = ""
    sources: tuple[LedgerSource, ...] = ()
    messages: tuple[LedgerMessage, ...] = ()
    quotes: tuple[QuoteRef, ...] = ()
    mentions: tuple[Mention, ...] = ()
    links: tuple[ReferenceLink, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "currentMessageId": self.current_message_id,
            "sources": [item.to_dict() for item in self.sources],
            "messages": [item.to_dict() for item in self.messages],
            "quotes": [item.to_dict() for item in self.quotes],
            "mentions": [item.to_dict() for item in self.mentions],
            "links": [item.to_dict() for item in self.links],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "DiscourseLedger":
        if not isinstance(value, Mapping):
            return cls()
        return cls(
            schema=str(value.get("schema") or LEDGER_SCHEMA),
            current_message_id=str(value.get("current_message_id") or value.get("currentMessageId") or ""),
            sources=tuple(
                LedgerSource.from_dict(item)
                for item in (value.get("sources") if isinstance(value.get("sources"), list) else [])
                if isinstance(item, Mapping)
            ),
            messages=tuple(
                LedgerMessage.from_dict(item)
                for item in (value.get("messages") if isinstance(value.get("messages"), list) else [])
                if isinstance(item, Mapping)
            ),
            quotes=tuple(
                QuoteRef.from_dict(item)
                for item in (value.get("quotes") if isinstance(value.get("quotes"), list) else [])
                if isinstance(item, Mapping)
            ),
            mentions=tuple(
                Mention.from_dict(item)
                for item in (value.get("mentions") if isinstance(value.get("mentions"), list) else [])
                if isinstance(item, Mapping)
            ),
            links=tuple(
                ReferenceLink.from_dict(item)
                for item in (value.get("links") if isinstance(value.get("links"), list) else [])
                if isinstance(item, Mapping)
            ),
        )


_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』", "\"": "\"", "‘": "’"}
_FIRST_PERSON = ("我们", "咱们", "自己", "我")
_SECOND_PERSON = ("你们", "您", "你")
_THIRD_PERSON = ("他们", "她们", "它们", "对方", "他", "她", "它")
_DEICTIC_EXPRESSIONS = (
    "你提到的那个", "你说的那个", "我提到的那个", "我说的那个",
    "他提到的那个", "他说的那个", "她提到的那个", "她说的那个",
    "你说的那句", "我说的那句", "他说的那句", "她说的那句",
    "这句话", "那句话", "这一点", "那一点", "这个", "那个", "这", "那",
)
_MENTION_SURFACES = tuple(sorted(
    {*_FIRST_PERSON, *_SECOND_PERSON, *_THIRD_PERSON, *_DEICTIC_EXPRESSIONS},
    key=len,
    reverse=True,
))
_PUNCTUATION = set("，,。！？?!；;：:、…~～（）()[]【】<>《》\n\t ")
_DEICTIC_PREDICATE_STARTS = (
    "是", "有", "在", "呢", "吗", "吧", "怎么", "为什么", "什么意思", "如何", "谁", "哪", "能", "会", "行",
)
_SAY_VERBS = ("说", "提到", "表示", "问", "写道", "讲")


def _entity_for_speaker(speaker: Speaker) -> str:
    if speaker == "user":
        return USER_ENTITY_ID
    if speaker == "assistant":
        return ASSISTANT_ENTITY_ID
    return UNKNOWN_ENTITY_ID


def _addressee_for_speaker(speaker: Speaker) -> Speaker:
    if speaker == "user":
        return "assistant"
    if speaker == "assistant":
        return "user"
    return "unknown"


def _quote_spans(message: LedgerMessage) -> list[QuoteRef]:
    text = message.text
    stack: list[tuple[str, int]] = []
    quotes: list[QuoteRef] = []
    for index, character in enumerate(text):
        if stack and character == _QUOTE_PAIRS.get(stack[-1][0]):
            opener, start = stack.pop()
            if index <= start + 1:
                continue
            quote_id = _stable_id("quote", message.message_id, str(start), str(index))
            prefix = "".join(ch for ch in text[max(0, start - 18):start] if ch not in _PUNCTUATION)
            attributed_speaker, entity_id, confidence = _quote_speaker_from_prefix(
                prefix,
                message.speaker,
                quote_id,
            )
            quotes.append(QuoteRef(
                quote_id=quote_id,
                message_id=message.message_id,
                span=TextSpan(start=start + 1, end=index, text=text[start + 1:index]),
                quoted_speaker=attributed_speaker,
                quoted_speaker_entity_id=entity_id,
                confidence=confidence,
            ))
            continue
        if character in _QUOTE_PAIRS:
            if character == "\"" and stack and stack[-1][0] == "\"":
                continue
            stack.append((character, index))
    return quotes


def _quote_speaker_from_prefix(
    prefix: str,
    message_speaker: Speaker,
    quote_id: str,
) -> tuple[Speaker, str, float]:
    compact = str(prefix or "")
    verb_position = -1
    matched_verb = ""
    for verb in _SAY_VERBS:
        position = compact.rfind(verb)
        if position > verb_position:
            verb_position = position
            matched_verb = verb
    if verb_position < 0:
        unknown_id = f"quote:{quote_id}:speaker:unknown"
        return "unknown", unknown_id, 0.0
    subject = compact[:verb_position]
    for marker, role in (("我", message_speaker), ("你", _addressee_for_speaker(message_speaker))):
        if subject.endswith(marker):
            return role, _entity_for_speaker(role), 0.98
    for marker in ("他", "她", "对方", "那个人", "这个人"):
        if subject.endswith(marker):
            entity_id = _stable_id("entity", quote_id, marker)
            return "third_party", entity_id, 0.84
    candidate = subject[-12:].strip()
    if candidate and matched_verb:
        entity_id = _stable_id("entity", "named_quote_speaker", candidate)
        return "third_party", entity_id, 0.72
    unknown_id = f"quote:{quote_id}:speaker:unknown"
    return "unknown", unknown_id, 0.0


def _containing_quote(quotes: Sequence[QuoteRef], start: int, end: int) -> QuoteRef | None:
    return next(
        (item for item in quotes if item.span.start <= start and end <= item.span.end),
        None,
    )


def _self_contained_deictic(text: str, start: int, surface: str) -> bool:
    if surface not in {"这个", "那个", "这", "那"}:
        return False
    end = start + len(surface)
    suffix = text[end:]
    if not suffix:
        return False
    next_character = suffix[0]
    if next_character in _PUNCTUATION:
        return False
    if any(suffix.startswith(item) for item in _DEICTIC_PREDICATE_STARTS):
        return False
    return "\u3400" <= next_character <= "\u9fff" or next_character.isalpha()


def _message_mentions(message: LedgerMessage, quotes: Sequence[QuoteRef]) -> list[Mention]:
    text = message.text
    mentions: list[Mention] = []
    cursor = 0
    while cursor < len(text):
        match_surface = next(
            (surface for surface in _MENTION_SURFACES if text.startswith(surface, cursor)),
            "",
        )
        if not match_surface:
            cursor += 1
            continue
        start, end = cursor, cursor + len(match_surface)
        quote = _containing_quote(quotes, start, end)
        effective_speaker = quote.quoted_speaker if quote else message.speaker
        entity_id: str | None = None
        status: ResolutionStatus = "unresolved"
        confidence = 0.0
        kind = "pronoun"
        grammatical_role = "referent"
        if match_surface in _FIRST_PERSON:
            kind = "reflexive" if match_surface == "自己" else "personal_pronoun"
            grammatical_role = "speaker"
            if match_surface != "自己":
                entity_id = (
                    quote.quoted_speaker_entity_id
                    if quote and quote.quoted_speaker_entity_id else
                    _entity_for_speaker(effective_speaker)
                )
                status = (
                    "resolved"
                    if effective_speaker in {"user", "assistant", "third_party"}
                    else "unresolved"
                )
                confidence = 0.99 if status == "resolved" else 0.0
        elif match_surface in _SECOND_PERSON:
            kind = "personal_pronoun"
            grammatical_role = "addressee"
            target_speaker = _addressee_for_speaker(effective_speaker)
            entity_id = _entity_for_speaker(target_speaker)
            status = "resolved" if target_speaker in {"user", "assistant"} else "unresolved"
            confidence = 0.99 if status == "resolved" else 0.0
        elif match_surface in _THIRD_PERSON:
            kind = "personal_pronoun" if match_surface != "它" else "object_pronoun"
            grammatical_role = "referent"
        else:
            kind = "deictic"
            if _self_contained_deictic(text, start, match_surface):
                status = "self_contained"
                confidence = 1.0
        mention_id = _stable_id("mention", message.message_id, str(start), str(end), match_surface)
        mentions.append(Mention(
            mention_id=mention_id,
            message_id=message.message_id,
            span=TextSpan(start=start, end=end, text=match_surface),
            kind=kind,
            grammatical_role=grammatical_role,
            quote_id=quote.quote_id if quote else None,
            entity_id=entity_id,
            effective_speaker=effective_speaker,
            status=status,
            confidence=confidence,
        ))
        cursor = end
    return mentions


def _message_record(
    *,
    message_id: str,
    turn_id: str,
    speaker: Speaker,
    text: str,
    created_at: str = "",
    modality: SourceKind = "chat_message",
    source_id: str | None = None,
) -> LedgerMessage | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    return LedgerMessage(
        message_id=message_id,
        turn_id=turn_id,
        speaker=speaker,
        text=cleaned,
        source_id=source_id or f"source:{message_id}",
        modality=modality,
        created_at=str(created_at or ""),
    )


def build_discourse_ledger(
    *,
    recent_turns: Sequence[Mapping[str, Any]],
    client_messages: Sequence[Mapping[str, Any]],
    current_user_message: str,
    current_message_id: str,
    pending_question: Mapping[str, Any] | None = None,
    existing_ledger: DiscourseLedger | Mapping[str, Any] | None = None,
) -> DiscourseLedger:
    base = (
        existing_ledger
        if isinstance(existing_ledger, DiscourseLedger)
        else DiscourseLedger.from_dict(existing_ledger)
    )
    message_by_id = {item.message_id: item for item in base.messages if item.message_id}
    for index, turn in enumerate(recent_turns[-80:]):
        if not isinstance(turn, Mapping):
            continue
        turn_id = str(turn.get("turnId") or turn.get("requestId") or f"legacy-{index}")
        created_at = str(turn.get("at") or "")
        for role in ("user", "assistant"):
            record = _message_record(
                message_id=f"{turn_id}:{role}",
                turn_id=turn_id,
                speaker=_speaker(role),
                text=str(turn.get(role) or ""),
                created_at=created_at,
            )
            if record:
                message_by_id[record.message_id] = record
    for index, item in enumerate(client_messages[-40:]):
        if not isinstance(item, Mapping):
            continue
        role = _speaker(item.get("role"))
        if role not in {"user", "assistant"}:
            continue
        text = str(item.get("content") or item.get("text") or "").strip()
        supplied_id = str(item.get("id") or item.get("messageId") or "").strip()
        message_id = supplied_id or _stable_id("clientmsg", role, str(index), text)
        record = _message_record(
            message_id=message_id,
            turn_id=str(item.get("turnId") or message_id),
            speaker=role,
            text=text,
            created_at=str(item.get("createdAt") or ""),
        )
        if record:
            message_by_id[record.message_id] = record
    if isinstance(pending_question, Mapping):
        question_text = str(pending_question.get("text") or "").strip()
        source_turn = str(pending_question.get("sourceTurnId") or "pending")
        record = _message_record(
            message_id=f"pending:{source_turn}:assistant",
            turn_id=source_turn,
            speaker="assistant",
            text=question_text,
            created_at=str(pending_question.get("askedAt") or ""),
        )
        if record:
            message_by_id[record.message_id] = record
    current = _message_record(
        message_id=current_message_id,
        turn_id=current_message_id.rsplit(":", 1)[0],
        speaker="user",
        text=current_user_message,
    )
    if current:
        message_by_id[current.message_id] = current
    messages = list(message_by_id.values())[-240:]
    quotes = [quote for message in messages for quote in _quote_spans(message)]
    mentions = [
        mention
        for message in messages
        for mention in _message_mentions(
            message,
            [item for item in quotes if item.message_id == message.message_id],
        )
    ]
    retained_message_ids = {item.message_id for item in messages}
    sources = [
        item for item in base.sources
        if item.source_id and (
            not item.source_id.startswith("source:")
            or item.source_id.removeprefix("source:") in retained_message_ids
        )
    ]
    source_ids = {item.source_id for item in sources}
    for message in messages:
        if message.source_id not in source_ids:
            sources.append(LedgerSource(
                source_id=message.source_id,
                kind=message.modality,
                submitted_by=message.speaker,
            ))
            source_ids.add(message.source_id)
    ledger = DiscourseLedger(
        current_message_id=current_message_id,
        sources=tuple(sources[-240:]),
        messages=tuple(messages),
        quotes=tuple(quotes[-320:]),
        mentions=tuple(mentions[-640:]),
        links=tuple(
            item for item in base.links
            if any(mention.mention_id == item.source_mention_id for mention in mentions)
        ),
    )
    resolution = resolve_discourse_references(ledger, message_id=current_message_id)
    link_by_id = {
        item.link_id: item
        for item in (*ledger.links, *resolution.links)
        if item.link_id
    }
    return replace(ledger, links=tuple(link_by_id.values())[-640:])


def _ocr_entries(result: Mapping[str, Any]) -> list[tuple[str, tuple[float, float, float, float] | None]]:
    entries: list[tuple[str, tuple[float, float, float, float] | None]] = []
    visual_text = str(result.get("visualOcrText") or "").strip()
    if visual_text:
        entries.append((visual_text, None))
    vision = result.get("vision") if isinstance(result.get("vision"), Mapping) else {}
    artifact = result.get("imageArtifact") if isinstance(result.get("imageArtifact"), Mapping) else {}
    understanding = (
        artifact.get("understanding")
        if isinstance(artifact.get("understanding"), Mapping) else {}
    )
    for value in (vision.get("ocrText"), understanding.get("ocrText")):
        text = str(value or "").strip()
        if text:
            entries.append((text, None))
    for container in (result.get("visualOcrItems"), vision.get("ocrItems")):
        if not isinstance(container, list):
            continue
        for item in container[:80]:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("text") or item.get("visibleText") or "").strip()
            if not text:
                continue
            bbox_value = item.get("bbox")
            bbox = None
            if isinstance(bbox_value, (list, tuple)) and len(bbox_value) == 4:
                try:
                    bbox = tuple(float(value) for value in bbox_value)  # type: ignore[assignment]
                except (TypeError, ValueError):
                    bbox = None
            entries.append((text, bbox))
    deduplicated: list[tuple[str, tuple[float, float, float, float] | None]] = []
    seen: set[str] = set()
    for text, bbox in entries:
        key = " ".join(text.split())
        if key and key not in seen:
            seen.add(key)
            deduplicated.append((text[:4000], bbox))
    return deduplicated


def extend_with_visual_observations(
    ledger: DiscourseLedger,
    results: Sequence[Mapping[str, Any]],
) -> DiscourseLedger:
    messages = {item.message_id: item for item in ledger.messages}
    sources = {item.source_id: item for item in ledger.sources}
    for result in results:
        if not isinstance(result, Mapping) or not result.get("ok"):
            continue
        tool_name = str(result.get("name") or "")
        if tool_name not in {"capture_screen", "capture_camera", "read_image"}:
            continue
        artifact_id = str(
            result.get("captureId")
            or result.get("attachmentId")
            or (
                result.get("imageArtifact", {}).get("artifact_id")
                if isinstance(result.get("imageArtifact"), Mapping) else ""
            )
            or _stable_id("artifact", tool_name, json.dumps(result, ensure_ascii=False, sort_keys=True)[:1000])
        )
        modality: SourceKind = "attachment_ocr" if tool_name == "read_image" else "screen_ocr"
        submitted_by: Speaker = "user" if tool_name == "read_image" else "system"
        for index, (text, bbox) in enumerate(_ocr_entries(result)):
            source_id = f"artifact:{artifact_id}:ocr:{index}"
            message_id = f"{source_id}:message"
            sources[source_id] = LedgerSource(
                source_id=source_id,
                kind=modality,
                artifact_id=artifact_id,
                submitted_by=submitted_by,
                bbox=bbox,
            )
            messages[message_id] = LedgerMessage(
                message_id=message_id,
                turn_id=artifact_id,
                speaker="unknown",
                text=text,
                source_id=source_id,
                modality=modality,
                created_at=str(result.get("capturedAt") or ""),
            )
    retained_messages = list(messages.values())[-240:]
    quotes = [quote for message in retained_messages for quote in _quote_spans(message)]
    mentions = [
        mention
        for message in retained_messages
        for mention in _message_mentions(
            message,
            [item for item in quotes if item.message_id == message.message_id],
        )
    ]
    return replace(
        ledger,
        sources=tuple(list(sources.values())[-240:]),
        messages=tuple(retained_messages),
        quotes=tuple(quotes[-320:]),
        mentions=tuple(mentions[-640:]),
    )


def extend_with_current_artifacts(
    ledger: DiscourseLedger,
    artifacts: Sequence[Mapping[str, Any]],
) -> DiscourseLedger:
    """Bind newly supplied artifacts as this turn's authoritative object.

    A phrase such as ``看看这个`` normally resolves to the latest chat message.
    When the same turn uploads a new file, that behavior crosses a real task
    boundary: the new artifact is the referent, while prior chat remains only
    background.  This records the boundary in the provider-independent ledger
    instead of relying on prompt wording or a filename-specific heuristic.
    """

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        artifact_id = str(item.get("id") or item.get("attachmentId") or "").strip()
        name = str(item.get("name") or item.get("fileName") or "attachment").strip()
        mime_type = str(
            item.get("mimeType") or item.get("type") or "application/octet-stream"
        ).strip()
        identity = artifact_id or f"{name}\x1f{mime_type}"
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append({
            "id": artifact_id or _stable_id("artifact", name, mime_type),
            "name": name or "attachment",
            "mimeType": mime_type or "application/octet-stream",
        })
    if not normalized:
        return ledger

    group_material = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    group_id = _stable_id("artifact_group", ledger.current_message_id, group_material)
    source_id = f"artifact:{group_id}:current"
    message_id = f"{source_id}:target"
    label = "；".join(
        f"{item['name']}（{item['mimeType']}）" for item in normalized
    )
    sources = {item.source_id: item for item in ledger.sources}
    messages = {item.message_id: item for item in ledger.messages}
    sources[source_id] = LedgerSource(
        source_id=source_id,
        kind="tool_observation",
        artifact_id=group_id,
        submitted_by="user",
    )
    messages[message_id] = LedgerMessage(
        message_id=message_id,
        turn_id=ledger.current_message_id,
        speaker="unknown",
        text=(
            "最近附件任务续接：" if all(item.get("_taskContinuation") for item in artifacts)
            else "本轮新上传附件："
        ) + label,
        source_id=source_id,
        modality="tool_observation",
    )
    retained_messages = list(messages.values())[-240:]
    quotes = [quote for message in retained_messages for quote in _quote_spans(message)]
    mentions = [
        mention
        for message in retained_messages
        for mention in _message_mentions(
            message,
            [item for item in quotes if item.message_id == message.message_id],
        )
    ]
    return replace(
        ledger,
        sources=tuple(list(sources.values())[-240:]),
        messages=tuple(retained_messages),
        quotes=tuple(quotes[-320:]),
        mentions=tuple(mentions[-640:]),
    )


def _latest_prior_message(
    ledger: DiscourseLedger,
    current_message_id: str,
    speaker: Speaker | None = None,
) -> LedgerMessage | None:
    candidates = [
        item for item in ledger.messages
        if item.message_id != current_message_id
        and item.text.strip()
        and (speaker is None or item.speaker == speaker)
        and item.speaker != "system"
    ]
    return candidates[-1] if candidates else None


def _deictic_target_speaker(surface: str, message_speaker: Speaker) -> Speaker | None:
    if surface.startswith("你"):
        return _addressee_for_speaker(message_speaker)
    if surface.startswith("我"):
        return message_speaker
    if surface.startswith(("他", "她")):
        return "third_party"
    return None


def resolve_discourse_references(
    ledger: DiscourseLedger,
    *,
    message_id: str,
) -> ReferenceResolution:
    message = next((item for item in ledger.messages if item.message_id == message_id), None)
    mentions = tuple(item for item in ledger.mentions if item.message_id == message_id)
    links: list[ReferenceLink] = []
    antecedents: list[str] = []
    unresolved: list[str] = []
    ambiguous: list[str] = []
    authoritative_artifact = next((
        item for item in reversed(ledger.messages)
        if item.message_id != message_id
        and item.turn_id == message_id
        and item.modality == "tool_observation"
        and item.source_id.startswith("artifact:")
    ), None)
    for mention in mentions:
        target_message: LedgerMessage | None = None
        target_entity = mention.entity_id
        candidates: tuple[str, ...] = ()
        status = mention.status
        confidence = mention.confidence
        if status == "self_contained":
            continue
        if (
            authoritative_artifact is not None
            and mention.kind in {"deictic", "object_pronoun"}
        ):
            target_message = authoritative_artifact
            target_entity = None
            status = "resolved"
            confidence = 0.99
            antecedents.append(authoritative_artifact.message_id)
        elif target_entity and status == "resolved":
            pass
        elif mention.kind == "deictic" and message is not None:
            target_speaker = _deictic_target_speaker(mention.span.text, message.speaker)
            target_message = _latest_prior_message(
                ledger,
                message_id,
                target_speaker,
            )
            if target_message:
                status = "resolved"
                confidence = 0.93 if target_speaker else 0.82
                antecedents.append(target_message.message_id)
            else:
                status = "unresolved"
        elif mention.kind in {"personal_pronoun", "object_pronoun"}:
            external_entities = tuple(dict.fromkeys(
                item.entity_id
                for item in ledger.mentions
                if item.message_id != message_id
                and item.effective_speaker == "third_party"
                and item.entity_id
            ))
            candidates = external_entities[-4:]
            if len(candidates) == 1:
                target_entity = candidates[0]
                status = "resolved"
                confidence = 0.72
            elif len(candidates) > 1:
                status = "ambiguous"
                confidence = 0.0
            else:
                status = "unresolved"
        elif mention.kind == "reflexive":
            status = "ambiguous"
            candidates = tuple(dict.fromkeys((
                _entity_for_speaker(mention.effective_speaker),
                _entity_for_speaker(_addressee_for_speaker(mention.effective_speaker)),
            )))
        if status == "unresolved":
            unresolved.append(mention.mention_id)
        elif status == "ambiguous":
            ambiguous.append(mention.mention_id)
        links.append(ReferenceLink(
            link_id=_stable_id("ref", mention.mention_id, target_message.message_id if target_message else str(target_entity or "")),
            source_mention_id=mention.mention_id,
            target_message_id=target_message.message_id if target_message else None,
            target_entity_id=target_entity,
            candidate_ids=candidates,
            status=status,
            confidence=confidence,
        ))
    return ReferenceResolution(
        message_id=message_id,
        mentions=mentions,
        links=tuple(links),
        antecedent_message_ids=tuple(dict.fromkeys(antecedents)),
        unresolved_mention_ids=tuple(dict.fromkeys(unresolved)),
        ambiguous_mention_ids=tuple(dict.fromkeys(ambiguous)),
    )


def reference_requires_context(resolution: ReferenceResolution) -> bool:
    return bool(
        resolution.antecedent_message_ids
        or any(
            item.kind in {"deictic", "object_pronoun"}
            and item.status != "self_contained"
            for item in resolution.mentions
        )
        or resolution.unresolved_mention_ids
        or resolution.ambiguous_mention_ids
    )


def render_discourse_context(
    ledger: DiscourseLedger,
    resolution: ReferenceResolution | None = None,
    *,
    max_chars: int = 6000,
) -> str:
    active = resolution or resolve_discourse_references(
        ledger,
        message_id=ledger.current_message_id,
    )
    current = next(
        (item for item in ledger.messages if item.message_id == active.message_id),
        None,
    )
    message_by_id = {item.message_id: item for item in ledger.messages}
    antecedents = [
        message_by_id[item]
        for item in active.antecedent_message_ids
        if item in message_by_id
    ]
    current_quotes = [item for item in ledger.quotes if item.message_id == active.message_id]
    relevant_ocr = [
        item for item in ledger.messages
        if item.modality in {"screen_ocr", "attachment_ocr"}
    ][-6:]
    if not (active.mentions or antecedents or current_quotes or relevant_ocr):
        return ""
    payload = {
        "schema": ledger.schema,
        "participants": {
            USER_ENTITY_ID: "user",
            ASSISTANT_ENTITY_ID: "assistant",
        },
        "currentMessage": current.to_dict() if current else {},
        "antecedentMessages": [item.to_dict() for item in antecedents],
        "currentQuotes": [item.to_dict() for item in current_quotes],
        "currentMentions": [item.to_dict() for item in active.mentions],
        "referenceLinks": [item.to_dict() for item in active.links],
        "visualTextSources": [
            {
                **item.to_dict(),
                "contentAuthorship": "unknown",
            }
            for item in relevant_ocr
        ],
        "unresolvedMentionIds": list(active.unresolved_mention_ids),
        "ambiguousMentionIds": list(active.ambiguous_mention_ids),
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > max_chars:
        payload["visualTextSources"] = payload["visualTextSources"][-2:]
        payload["antecedentMessages"] = payload["antecedentMessages"][-2:]
        for key in ("currentMessage",):
            if isinstance(payload.get(key), dict):
                payload[key]["text"] = str(payload[key].get("text") or "")[:500]
        for key in ("antecedentMessages", "visualTextSources"):
            for item in payload.get(key, []):
                if isinstance(item, dict):
                    item["text"] = str(item.get("text") or "")[:260]
        for item in payload.get("currentQuotes", []):
            if isinstance(item, dict) and isinstance(item.get("span"), dict):
                item["span"]["text"] = str(item["span"].get("text") or "")[:180]
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > max_chars:
        # Keep the JSON structurally valid even under severe prompt pressure.
        # IDs and links carry the binding truth; optional evidence prose can be
        # removed without changing who "我/你" refer to.
        payload["visualTextSources"] = []
        payload["antecedentMessages"] = []
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > max_chars:
        payload["currentQuotes"] = []
        payload["currentMessage"] = {
            key: value
            for key, value in payload.get("currentMessage", {}).items()
            if key != "text"
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "\n".join([
        "[结构化指代账本：高优先级]",
        serialized,
        "participant:user 是用户；participant:assistant 是 AMADEUS。非引用区的第一、第二人称按当前消息 speaker/addressee 绑定。",
        "引号内第一人称必须继承 currentQuotes.quoted_speaker；quoted_speaker=unknown 时保持未知，不能自动变成用户或 AMADEUS。",
        "visualTextSources 的 contentAuthorship 默认 unknown：用户提交图片不等于图片中文字由用户说出。",
        "回答中文与日语发声稿必须使用同一 referenceLinks；翻译只能改变表面词，不能改变‘我/你/第三方’的实体。",
        "声称‘用户说过’或‘我之前说过’时，必须能指向 speaker 对应的具体 antecedent messageId。",
    ])


_ATTRIBUTION_MARKERS: tuple[tuple[str, Speaker], ...] = (
    ("你之前说过", "user"), ("你刚才说过", "user"),
    ("你说过", "user"), ("你之前提过", "user"),
    ("你刚才提过", "user"), ("你提到过", "user"),
    ("你告诉过我", "user"), ("你承认过", "user"),
    ("你表示过", "user"),
    ("我之前说过", "assistant"), ("我刚才说过", "assistant"),
    ("我说过", "assistant"), ("我之前提过", "assistant"),
    ("我刚才提过", "assistant"), ("我提到过", "assistant"),
    ("我告诉过你", "assistant"), ("我承认过", "assistant"),
    ("我表示过", "assistant"),
)
_SENTENCE_ENDINGS = set("。！？!?；;\n")
_CLAIM_FILLERS = set("，,。：:；;！？!?‘’“”\"'（）()[]【】<>《》的了是过曾经之前刚才")


def _sentences_with_offsets(text: str) -> list[tuple[int, str]]:
    sentences: list[tuple[int, str]] = []
    start = 0
    for index, character in enumerate(text):
        if character not in _SENTENCE_ENDINGS:
            continue
        sentence = text[start:index + 1].strip()
        if sentence:
            sentences.append((start, sentence))
        start = index + 1
    trailing = text[start:].strip()
    if trailing:
        sentences.append((start, trailing))
    return sentences


def _claim_comparison_text(text: str) -> str:
    return "".join(
        character.casefold()
        for character in str(text or "")
        if not character.isspace() and character not in _CLAIM_FILLERS
    )


def _claim_overlap(left: str, right: str) -> float:
    normalized_left = _claim_comparison_text(left)
    normalized_right = _claim_comparison_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    shorter, longer = sorted(
        (normalized_left, normalized_right),
        key=len,
    )
    if len(shorter) >= 5 and shorter in longer:
        return 1.0
    matcher = SequenceMatcher(None, normalized_left, normalized_right)
    longest = matcher.find_longest_match().size
    coverage = longest / max(1, len(shorter))
    return max(matcher.ratio(), coverage)


def audit_reply_references(
    reply_text: str,
    ledger: DiscourseLedger | Mapping[str, Any] | None,
) -> ReferenceAudit:
    """Detect high-confidence speaker reversals in explicit reply attributions.

    This is a narrow safety net over the structured pre-generation contract.
    It does not guess referents from topic keywords: a violation is reported
    only when the attributed proposition strongly matches the *other*
    participant's stored message and does not match the claimed participant.
    """

    active = ledger if isinstance(ledger, DiscourseLedger) else DiscourseLedger.from_dict(ledger)
    reply = str(reply_text or "").strip()
    if not reply or not active.messages:
        return ReferenceAudit(valid=True)
    messages_by_speaker = {
        speaker: [
            item for item in active.messages
            if item.speaker == speaker
            and item.message_id != active.current_message_id
            and item.text.strip()
        ][-24:]
        for speaker in ("user", "assistant")
    }
    issues: list[Mapping[str, Any]] = []
    for sentence_start, sentence in _sentences_with_offsets(reply):
        marker_match = next(
            (
                (marker, speaker, sentence.find(marker))
                for marker, speaker in _ATTRIBUTION_MARKERS
                if marker in sentence
            ),
            None,
        )
        if marker_match is None:
            continue
        marker, claimed_speaker, marker_index = marker_match
        proposition = sentence[marker_index + len(marker):].strip(
            " ，,。：:；;！？!?‘’“”\"'"
        )
        if len(_claim_comparison_text(proposition)) < 5:
            continue
        other_speaker: Speaker = (
            "assistant" if claimed_speaker == "user" else "user"
        )

        def best_match(speaker: Speaker) -> tuple[float, LedgerMessage | None]:
            ranked = [
                (_claim_overlap(proposition, item.text), item)
                for item in messages_by_speaker.get(speaker, [])
            ]
            return max(ranked, key=lambda item: item[0], default=(0.0, None))

        claimed_score, claimed_message = best_match(claimed_speaker)
        other_score, other_message = best_match(other_speaker)
        if other_message is None:
            continue
        if other_score < 0.72 or claimed_score >= 0.48 or other_score - claimed_score < 0.28:
            continue
        issues.append({
            "kind": "speaker_reversal",
            "sentenceStart": sentence_start,
            "sentence": sentence,
            "marker": marker,
            "proposition": proposition,
            "claimedSpeaker": claimed_speaker,
            "actualSpeaker": other_speaker,
            "actualMessageId": other_message.message_id,
            "claimedBestMessageId": (
                claimed_message.message_id if claimed_message is not None else ""
            ),
            "actualScore": round(other_score, 4),
            "claimedScore": round(claimed_score, 4),
        })
    return ReferenceAudit(valid=not issues, issues=tuple(issues))


def append_turn_to_ledger(
    ledger: DiscourseLedger | Mapping[str, Any] | None,
    *,
    turn_id: str,
    user_text: str,
    assistant_text: str,
    created_at: str = "",
    user_message_id: str = "",
    assistant_message_id: str = "",
) -> DiscourseLedger:
    base = ledger if isinstance(ledger, DiscourseLedger) else DiscourseLedger.from_dict(ledger)
    messages = list(base.messages)
    for role, text, supplied_message_id in (
        ("user", user_text, user_message_id),
        ("assistant", assistant_text, assistant_message_id),
    ):
        record = _message_record(
            message_id=str(supplied_message_id or f"{turn_id}:{role}"),
            turn_id=turn_id,
            speaker=_speaker(role),
            text=text,
            created_at=created_at,
        )
        if record:
            messages = [item for item in messages if item.message_id != record.message_id]
            messages.append(record)
    rebuilt = build_discourse_ledger(
        recent_turns=(),
        client_messages=tuple(
            {
                "id": item.message_id,
                "turnId": item.turn_id,
                "role": item.speaker,
                "content": item.text,
                "createdAt": item.created_at,
            }
            for item in messages
            if item.speaker in {"user", "assistant"}
        ),
        current_user_message="",
        current_message_id="",
        existing_ledger=replace(base, messages=tuple(messages)),
    )
    current_message_id = f"{turn_id}:assistant"
    rebuilt = replace(rebuilt, current_message_id=current_message_id)
    resolution = resolve_discourse_references(
        rebuilt,
        message_id=current_message_id,
    )
    link_by_id = {
        item.link_id: item
        for item in (*rebuilt.links, *resolution.links)
        if item.link_id
    }
    return replace(rebuilt, links=tuple(link_by_id.values())[-640:])
