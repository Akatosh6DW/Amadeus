"""快速流可见发言的规范化与最终结果投影。

本模块拥有纯字段规则和发言单元结构；关系读取、字段修复及旧回复规范化仍通过
各自领域端口接入。规范化不会提交会话，也不会改变快速流的恢复次数。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Protocol

from agent.dialogue_architecture import TurnContract, TurnPolicy
from agent.participant_realization import realize_participant_speech
from agent.persona_context import PersonaContextSnapshot
from agent.proactive_control import PROACTIVE_DIRECTIVES
from backend.chat.presentation import ALLOWED_EMOTIONS, speech_language_profile
from backend.chat.reply_fields import normalize_proactive_directive, normalize_relation_event
from backend.common.values import clamp_int
from backend.providers.errors import UpstreamProtocolError
from backend.tts.contract import MAX_SPEECH_UNITS


class UtteranceRuntimeOperations(Protocol):
    def configured(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_log(self, *args: Any, **kwargs: Any) -> Any: ...


class UtteranceContextOperations(Protocol):
    def read_relationship_state(self, *args: Any, **kwargs: Any) -> Any: ...


class UtteranceModelOperations(Protocol):
    def repair_dialogue_fields_once(self, *args: Any, **kwargs: Any) -> Any: ...


class UtteranceProjectionOperations(Protocol):
    def normalize_model_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def enforce_nickname_reaction_grounding(self, *args: Any, **kwargs: Any) -> Any: ...


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
    payload = realize_participant_speech(payload)
    text_zh = re.sub("\\s+", " ", str(payload.get("textZh") or "")).strip()
    spoken_ja = re.sub("\\s+", " ", str(payload.get("spokenTextJa") or "")).strip()
    speech_profile = speech_language_profile(settings)
    requires_japanese_track = speech_profile["speechLanguage"] == "ja-JP"
    if not text_zh:
        raise UpstreamProtocolError("streaming utterance requires non-empty Chinese")
    if not spoken_ja and requires_japanese_track and services.runtime.configured(settings):
        try:
            repaired = services.model.repair_dialogue_fields_once(
                settings,
                {
                    "reply": text_zh,
                    "spokenTextJa": "",
                    "emotion": payload.get("emotion") or "neutral",
                    "memoryUpdates": [],
                },
                {"missingFields": ["spokenTextJa"], "invalidFields": []},
                user_message=user_message,
                recent_messages=[],
                relationship_state=services.context.read_relationship_state(),
                request_id=request_id,
                persona_snapshot=persona_snapshot,
            )
            spoken_ja = re.sub("\\s+", " ", str(repaired.get("spokenTextJa") or "")).strip()
            services.runtime.write_log(
                "semantic_stream_spoken_ja_repaired",
                requestId=request_id,
                frozenChineseCharacters=len(text_zh),
            )
        except Exception as repair_error:
            services.runtime.write_log(
                "semantic_stream_spoken_ja_repair_failed",
                requestId=request_id,
                errorType=type(repair_error).__name__,
                error=str(repair_error)[:500],
            )
    if not spoken_ja and requires_japanese_track:
        raise UpstreamProtocolError(
            "streaming utterance is missing Japanese after field-only repair"
        )
    speech_text = text_zh if not requires_japanese_track else spoken_ja
    if len(text_zh) > 900 or len(speech_text) > 1400:
        raise UpstreamProtocolError("streaming utterance exceeded the sentence-level size limit")
    if re.search(
        "https?://|```|toolRequests|memoryUpdates|turn_(?:header|trailer)",
        text_zh + speech_text,
        re.I,
    ):
        raise UpstreamProtocolError("streaming utterance exposed internal or non-speakable content")
    if re.search(
        "(?:但是|不过|因为|所以|而且|如果|除非|虽然|以及|然后|つまり|だから|けれど)\\s*$",
        text_zh + speech_text,
    ):
        raise UpstreamProtocolError("streaming utterance ended as an obvious fragment")
    if requires_japanese_track:
        size_ratio = len(spoken_ja) / max(1, len(text_zh))
        if size_ratio < 0.22 or size_ratio > 5.2:
            raise UpstreamProtocolError("streaming utterance bilingual size ratio is implausible")
    covers_value = payload.get("covers")
    covers = (
        tuple((str(item).strip() for item in covers_value if str(item).strip()))
        if isinstance(covers_value, list)
        else ()
    )
    if first_visible and turn_contract is not None:
        # Semantic Stream V2 不要求模型复述后端合同标识。缺少的 covers 元数据在
        # 此处补齐；回答是否完整仍由最终合同验证器按自然语言内容判断。
        if not covers:
            covers = tuple(turn_contract.first_visible_must_cover)
        missing = [item for item in turn_contract.first_visible_must_cover if item not in covers]
        if missing:
            raise UpstreamProtocolError(
                "first visible utterance does not cover Turn Contract: " + ",".join(missing)
            )
        if payload.get("standalone", True) is not True:
            raise UpstreamProtocolError("first visible utterance is not independently complete")
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
    normalized = services.projection.enforce_nickname_reaction_grounding(
        user_message,
        normalized,
        reaction_key=request_id,
        persona_snapshot=persona_snapshot,
        is_first_visible=first_visible or sequence == 1,
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
        "textZh": str(normalized.get("reply") or text_zh),
        "spokenTextJa": str(normalized.get("spokenTextJa") or spoken_ja),
        "speechText": str(normalized.get("speechText") or speech_text),
        "speechLanguage": str(normalized.get("speechLanguage") or speech_profile["speechLanguage"]),
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
