"""快速流可见发言的规范化与最终结果投影。

本模块拥有纯字段规则和发言单元结构；协议与文本交付通过共享边界接入，缺失的
语音/情绪字段只做本地降级。规范化不会提交会话，也不会改变快速流的恢复次数。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Protocol

from agent.dialogue_architecture import TurnContract, TurnPolicy
from agent.participant_realization import realize_participant_speech
from agent.persona_context import PersonaContextSnapshot
from agent.proactive_control import PROACTIVE_DIRECTIVES
from backend.chat.presentation import speech_language_profile
from backend.chat.reply_fields import normalize_proactive_directive, normalize_relation_event
from backend.chat.delivery import require_delivery
from backend.common.values import clamp_int
from backend.providers.errors import UpstreamProtocolError
from backend.providers.reply_protocol import unwrap_reply_envelope
from backend.tts.contract import MAX_SPEECH_UNITS


# This is a transport/resource guard for one stream unit.  It is intentionally
# much larger than a conversational sentence limit: wording, topic coverage,
# and bilingual length are presentation diagnostics, not protocol failures.
MAX_STREAM_UTTERANCE_FIELD_CHARACTERS = 50_000


class UtteranceRuntimeOperations(Protocol):
    def configured(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_log(self, *args: Any, **kwargs: Any) -> Any: ...


class UtteranceContextOperations(Protocol):
    def read_relationship_state(self, *args: Any, **kwargs: Any) -> Any: ...


class UtteranceModelOperations(Protocol):
    """Reserved for native stream parsing; presentation fields never call a model."""


class UtteranceProjectionOperations(Protocol):
    def normalize_model_reply(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class StreamUtteranceServices:
    runtime: UtteranceRuntimeOperations
    context: UtteranceContextOperations
    model: UtteranceModelOperations
    projection: UtteranceProjectionOperations


def valid_speech_segment_count(value: Any) -> int | None:
    """只接受语音合同允许范围内的整数分段数。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= MAX_SPEECH_UNITS else None


def normalize_streaming_utterance(
    services: StreamUtteranceServices,
    payload: dict[str, Any],
    settings: dict[str, Any],
    *,
    sequence: int,
    user_message: str = "",
    request_id: str = "",
    turn_contract: TurnContract | None = None,
    first_visible: bool = False,
    persona_snapshot: PersonaContextSnapshot | None = None,
) -> dict[str, Any]:
    """规范单个可见发言单元，并在放行前补齐必需语音轨道。"""
    # A provider may JSON-encode the semantic envelope once more inside the
    # visible reply field.  Unwrap only a complete, contract-shaped mapping;
    # URLs, code fences and field names in ordinary prose remain untouched.
    payload = unwrap_reply_envelope(payload)
    payload = realize_participant_speech(payload)
    # ``textZh`` is the canonical display/history track.  Keep its source
    # characters and line breaks intact; only the speech projection below may
    # normalize whitespace.
    text_zh = str(payload.get("textZh") or "")
    # regex-purpose: projection (speech whitespace only; canonical text is untouched)
    spoken_ja = re.sub("\\s+", " ", str(payload.get("spokenTextJa") or "")).strip()
    speech_profile = speech_language_profile(settings)
    requires_japanese_track = speech_profile["speechLanguage"] == "ja-JP"
    if not text_zh.strip():
        raise UpstreamProtocolError("streaming utterance requires non-empty Chinese")
    # The first visible segment crosses the shared delivery boundary before it
    # can enter the stream buffer.  Topic/field wording remains ordinary text;
    # only an actual private span can stop delivery here.  Missing speech is a
    # presentation degradation and must not hide the Chinese answer.
    delivery_probe = require_delivery(
        {
            "reply": text_zh,
            "spokenTextJa": spoken_ja,
            "emotion": payload.get("emotion") or "neutral",
            "gesture": payload.get("gesture") or "none",
        },
        settings=settings,
        persona_snapshot=persona_snapshot,
        logger=getattr(services.runtime, "write_log", None),
        request_id=request_id,
    )
    speech_unavailable_reason = str(
        delivery_probe.get("speechUnavailableReason") or ""
    ).strip()
    # regex-purpose: projection (speech whitespace only; canonical text is untouched)
    spoken_ja = re.sub(
        "\\s+",
        " ",
        str(delivery_probe.get("spokenTextJa") or spoken_ja),
    ).strip()
    if not spoken_ja and requires_japanese_track:
        speech_unavailable_reason = speech_unavailable_reason or "optional_speech_field_missing"
    if (
        len(text_zh) > MAX_STREAM_UTTERANCE_FIELD_CHARACTERS
        or len(spoken_ja) > MAX_STREAM_UTTERANCE_FIELD_CHARACTERS
    ):
        raise UpstreamProtocolError("streaming utterance exceeded the transport resource limit")
    speech_quality_warnings: list[str] = []
    contract_warnings: list[str] = []
    covers_value = payload.get("covers")
    covers = (
        tuple((str(item).strip() for item in covers_value if str(item).strip()))
        if isinstance(covers_value, list)
        else ()
    )
    if first_visible and turn_contract is not None:
        # These are useful diagnostics for the final contract audit.  They do
        # not describe a transport failure and therefore cannot hide the first
        # visible answer.
        missing = [item for item in turn_contract.first_visible_must_cover if item not in covers]
        if missing:
            contract_warnings.append("turn_contract_coverage_unverified")
        if payload.get("standalone", True) is not True:
            contract_warnings.append("first_visible_segment_not_standalone")
    normalized = services.projection.normalize_model_reply(
        {
            **(
                {"_participantSpeechRealized": True}
                if payload.get("_participantSpeechRealized") is True
                else {}
            ),
            "reply": text_zh,
            "spokenTextJa": spoken_ja,
            "proactiveDirective": payload.get("proactiveDirective"),
            "emotion": payload.get("emotion"),
            "emotionIntensity": payload.get("emotionIntensity"),
            "gesture": payload.get("gesture"),
            "voiceStyle": payload.get("voiceStyle"),
            "voiceTone": payload.get("voiceTone"),
            "voiceIntensity": payload.get("voiceIntensity"),
            "speechPace": payload.get("speechPace"),
            "speechSpeed": payload.get("speechSpeed"),
            "deliveryMode": payload.get("deliveryMode"),
            "blushIntensity": payload.get("blushIntensity"),
            "eyeContact": payload.get("eyeContact"),
            "gazeX": payload.get("gazeX"),
            "gazeY": payload.get("gazeY"),
            "tension": payload.get("tension"),
            "memoryUpdates": [],
            "speechSegmentCount": valid_speech_segment_count(payload.get("speechSegmentCount"))
            or 1,
        },
        settings=settings,
    )
    normalized = dict(normalized) if isinstance(normalized, Mapping) else {"reply": text_zh}
    normalized_text_zh = text_zh
    # regex-purpose: projection (speech whitespace only; canonical text is untouched)
    normalized_spoken_ja = re.sub(
        "\\s+", " ", str(normalized.get("spokenTextJa") or spoken_ja)
    ).strip()
    if requires_japanese_track and normalized_spoken_ja:
        size_ratio = len(normalized_spoken_ja) / max(1, len(normalized_text_zh))
        if size_ratio < 0.22 or size_ratio > 5.2:
            # Keep the canonical text, annotate the quality issue, and mute
            # only the unreliable optional speech track.
            speech_quality_warnings.append("speech_length_ratio_unreliable")
            speech_unavailable_reason = speech_unavailable_reason or "speech_length_ratio_unreliable"
            normalized_spoken_ja = ""
    if speech_unavailable_reason:
        normalized["spokenTextJa"] = ""
        normalized["speechText"] = normalized_text_zh
        normalized["textOnlyDelivery"] = True
        normalized["speechUnavailableReason"] = speech_unavailable_reason
        normalized["ttsUnavailableReason"] = speech_unavailable_reason
    speech_text = (
        normalized_text_zh
        if not requires_japanese_track or not normalized_spoken_ja
        else normalized_spoken_ja
    )
    visible_spoken_ja = (
        ""
        if speech_unavailable_reason
        else str(normalized.get("spokenTextJa") or normalized_spoken_ja)
    )
    return {
        **(
            {"_participantSpeechRealized": True}
            if normalized.get("_participantSpeechRealized") is True
            else {}
        ),
        "utteranceId": str(payload.get("utteranceId") or f"u{sequence}")[:80],
        "proactiveDirective": normalize_proactive_directive(payload.get("proactiveDirective")),
        "proactiveDirectiveProvided": str(payload.get("proactiveDirective") or "").strip().lower()
        in PROACTIVE_DIRECTIVES,
        "textZh": text_zh,
        "spokenTextJa": visible_spoken_ja,
        "speechText": str(normalized.get("speechText") or speech_text),
        "speechLanguage": str(normalized.get("speechLanguage") or speech_profile["speechLanguage"]),
        "textOnlyDelivery": bool(normalized.get("textOnlyDelivery")),
        "speechUnavailableReason": str(normalized.get("speechUnavailableReason") or ""),
        "ttsUnavailableReason": str(normalized.get("ttsUnavailableReason") or ""),
        "speechQualityWarnings": list(dict.fromkeys(speech_quality_warnings)),
        "streamContractWarnings": list(dict.fromkeys(contract_warnings)),
        "originalAudio": (
            dict(normalized["originalAudio"])
            if isinstance(normalized.get("originalAudio"), dict)
            else None
        ),
        "originalReactionId": str(normalized.get("nicknameReactionId") or ""),
        "nicknameReactionOriginalAudioBound": bool(
            normalized.get("nicknameReactionOriginalAudioBound")
        ),
        "nicknameReactionGroundingViolation": bool(
            normalized.get("nicknameReactionGroundingViolation")
        ),
        "nicknameReactionEmotionLocked": bool(normalized.get("nicknameReactionEmotionLocked")),
        "boundaryAfter": (
            str(payload.get("boundaryAfter") or "continue")
            if str(payload.get("boundaryAfter") or "continue")
            in {"continue", "micro_pause", "phrase_pause", "turn_pause"}
            else "continue"
        ),
        "covers": list(dict.fromkeys(covers)),
        "standalone": payload.get("standalone", True) is True,
        "semanticRecoveryMode": str(payload.get("semanticRecoveryMode") or ""),
        "plainTextSalvaged": bool(payload.get("plainTextSalvaged")),
        "streamProtocolRecovered": bool(payload.get("streamProtocolRecovered")),
        "dialogueFieldRepairSucceeded": bool(payload.get("dialogueFieldRepairSucceeded")),
        "dialogueFieldRepairFields": (
            list(payload.get("dialogueFieldRepairFields"))
            if isinstance(payload.get("dialogueFieldRepairFields"), list)
            else []
        ),
        "emotionMissingFromModel": bool(
            payload.get("emotionMissingFromModel") or normalized.get("emotionMissingFromModel")
        ),
        "emotionLabelRecoveredByModel": bool(payload.get("emotionLabelRecoveredByModel")),
        "speechSegmentCount": int(normalized.get("speechSegmentCount") or 1),
        "speechSegmentCountSource": str(
            payload.get("speechSegmentCountSource") or "stream_utterance"
        ),
        "visibleSegmentationSource": str(payload.get("visibleSegmentationSource") or ""),
        "visibleSegmentCountBefore": clamp_int(
            payload.get("visibleSegmentCountBefore"),
            1,
            MAX_SPEECH_UNITS,
            int(normalized.get("speechSegmentCount") or 1),
        ),
        "visibleSegmentCountAfter": clamp_int(
            payload.get("visibleSegmentCountAfter"), 1, MAX_SPEECH_UNITS, 1
        ),
        **{
            key: normalized.get(key)
            for key in (
                "emotion",
                "emotionIntensity",
                "gesture",
                "voiceStyle",
                "voiceTone",
                "voiceIntensity",
                "speechPace",
                "speechSpeed",
                "deliveryMode",
                "blushIntensity",
                "eyeContact",
                "gazeX",
                "gazeY",
                "tension",
            )
        },
    }


def fast_stream_combined_result(
    utterances: list[dict[str, Any]],
    trailer: dict[str, Any],
    turn_policy: TurnPolicy,
    *,
    first_utterance_at: float,
    stream_started_at: float,
    parser_errors: list[str],
    protocol_recovered: bool = False,
    turn_contract: TurnContract | None = None,
) -> dict[str, Any]:
    """组合已验收发言；即使尾部元数据缺失，也不能丢弃已经交付的单元。"""
    if not utterances:
        raise UpstreamProtocolError("stream ended without a valid utterance")
    last = utterances[-1]
    requires_response_provided = (
        bool(trailer.get("requiresUserResponseProvided"))
        if isinstance(trailer.get("requiresUserResponseProvided"), bool)
        else isinstance(trailer.get("requiresUserResponse"), bool)
    )
    memory_updates_provided = (
        bool(trailer.get("memoryUpdatesProvided"))
        if isinstance(trailer.get("memoryUpdatesProvided"), bool)
        else isinstance(trailer.get("memoryUpdates"), list)
    )
    relation_event_provided = (
        bool(trailer.get("relationEventProvided"))
        if isinstance(trailer.get("relationEventProvided"), bool)
        else isinstance(trailer.get("relationEvent"), Mapping)
    )
    return {
        "reply": "\n\n".join((str(item["textZh"]) for item in utterances)),
        'imageIds': trailer.get('imageIds') if isinstance(trailer.get('imageIds'), list) else [],
        'galleryAccount': str(trailer.get('galleryAccount') or ''),
        "spokenTextJa": "\n\n".join((str(item["spokenTextJa"]) for item in utterances)),
        "_modelSpeechUnits": [
            {
                "textZh": str(item.get("textZh") or ""),
                "spokenTextJa": str(item.get("spokenTextJa") or ""),
                "emotion": str(item.get("emotion") or "neutral"),
                "emotionIntensity": item.get("emotionIntensity"),
                "voiceStyle": item.get("voiceStyle"),
                "voiceTone": item.get("voiceTone"),
                "voiceIntensity": item.get("voiceIntensity"),
                "speechPace": item.get("speechPace"),
                "speechSpeed": item.get("speechSpeed"),
                "textOnlyDelivery": bool(item.get("textOnlyDelivery")),
                "speechUnavailableReason": str(item.get("speechUnavailableReason") or ""),
                "ttsUnavailableReason": str(item.get("ttsUnavailableReason") or ""),
                "boundaryAfter": str(item.get("boundaryAfter") or "continue"),
            }
            for item in utterances
        ],
        "semanticRecoveryMode": str(last.get("semanticRecoveryMode") or ""),
        "plainTextSalvaged": bool(last.get("plainTextSalvaged")),
        **{
            key: last.get(key)
            for key in (
                "emotion",
                "emotionIntensity",
                "gesture",
                "voiceStyle",
                "voiceTone",
                "voiceIntensity",
                "speechPace",
                "speechSpeed",
                "deliveryMode",
                "blushIntensity",
                "eyeContact",
                "gazeX",
                "gazeY",
                "tension",
            )
        },
        "finalEmotion": trailer.get("finalEmotion") or last.get("emotion"),
        "finalEmotionIntensity": (
            trailer.get("finalEmotionIntensity")
            if trailer.get("finalEmotionIntensity") is not None
            else last.get("emotionIntensity")
        ),
        "emotionDelta": trailer.get("emotionDelta") or 0.0,
        "coldShoulder": bool(trailer.get("coldShoulder")),
        "memoryUpdates": (
            trailer.get("memoryUpdates") if isinstance(trailer.get("memoryUpdates"), list) else []
        ),
        "memoryUpdatesProvided": memory_updates_provided,
        "relationEvent": normalize_relation_event(trailer.get("relationEvent")),
        "relationEventProvided": relation_event_provided,
        "knowledgeConfidence": str(trailer.get("knowledgeConfidence") or "medium"),
        "knowledgeConfidenceProvided": bool(trailer.get("knowledgeConfidenceProvided")),
        "researchNeeded": False,
        "requiresUserResponse": (
            bool(trailer.get("requiresUserResponse")) if requires_response_provided else False
        ),
        "requiresUserResponseProvided": requires_response_provided,
        "proactiveDirective": normalize_proactive_directive(trailer.get("proactiveDirective")),
        "proactiveDirectiveProvided": str(trailer.get("proactiveDirective") or "").strip().lower()
        in PROACTIVE_DIRECTIVES,
        "completedActs": (
            trailer.get("completedActs") if isinstance(trailer.get("completedActs"), list) else []
        ),
        "completedActsProvided": isinstance(trailer.get("completedActs"), list),
        "standalone": trailer.get("standalone") is True,
        "standaloneProvided": isinstance(trailer.get("standalone"), bool),
        "dependsOnUndeliveredPrefix": bool(trailer.get("dependsOnUndeliveredPrefix")),
        "dependsOnUndeliveredPrefixProvided": isinstance(
            trailer.get("dependsOnUndeliveredPrefix"), bool
        ),
        "turnComplete": trailer.get("turnComplete") is True,
        "turnCompleteProvided": isinstance(trailer.get("turnComplete"), bool),
        "expectedResponseType": (
            str(trailer.get("expectedResponseType") or "")
            if str(trailer.get("expectedResponseType") or "")
            in {"free_text", "confirmation", "browser_target", "choice"}
            else ""
        ),
        "clarificationActionId": str(trailer.get("clarificationActionId") or "")[:100],
        "scheduleIntent": (
            dict(trailer.get("scheduleIntent"))
            if isinstance(trailer.get("scheduleIntent"), Mapping)
            else {}
        ),
        "speechSegmentCount": len(utterances),
        "visibleSegmentationSource": str(
            last.get("visibleSegmentationSource")
            or ("model_semantic_stream" if len(utterances) > 1 else "single_visible_utterance")
        ),
        "source": "api-stream",
        "streamProtocolRecovered": protocol_recovered,
        **({"streamProtocolIssue": "missing_turn_trailer"} if protocol_recovered else {}),
        "_chatMetrics": {
            "route": "fast_chat",
            "replyMode": turn_policy.reply_mode,
            "actionBudget": turn_policy.action_budget,
            "turnPolicyReason": turn_policy.reason,
            "turnPolicy": turn_policy.to_dict(),
            "streamedUtteranceCount": len(utterances),
            "timeToFirstUtteranceMs": (
                round(max(0.0, first_utterance_at - stream_started_at) * 1000)
                if first_utterance_at
                else 0
            ),
            "streamParserErrors": list(parser_errors),
            "streamProtocolRecovered": protocol_recovered,
            "semanticRecoveryMode": str(last.get("semanticRecoveryMode") or ""),
            "plainTextSalvaged": bool(last.get("plainTextSalvaged")),
            "visibleSegmentCount": len(utterances),
            "visibleSegmentationSource": str(
                last.get("visibleSegmentationSource")
                or ("model_semantic_stream" if len(utterances) > 1 else "single_visible_utterance")
            ),
        },
    }
