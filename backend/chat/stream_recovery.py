"""快速语义流的有界协议恢复。

本模块只处理原子 JSON 恢复和缺失尾部续写。它复用既有模型、视觉输入和发言
规范化边界，不增加模型调用次数，不重放已经交付的台词，也不提交会话历史。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Protocol

from agent.dialogue_architecture import TurnContract
from agent.fingerprints import runtime_fingerprint
from agent.persona_context import PersonaContextSnapshot
from agent.proactive_control import PROACTIVE_DIRECTIVES
from agent.semantic_recovery import recover_semantic_output
from backend.chat.parsing import parse_json_object
from backend.chat.presentation import ALLOWED_EMOTIONS, speech_language_profile
from backend.chat.reply_fields import normalize_proactive_directive
from backend.chat import stream_utterances
from backend.providers.errors import UpstreamProtocolError
from backend.tts import delivery as speech_delivery
from backend.tts.contract import (
    normalize_speech_plan_units,
    punctuation_fallback_speech_units,
    semantic_unit_has_weak_boundary,
)


class RecoveryRuntimeOperations(Protocol):
    def features(self) -> Any: ...
    def configured(self, *args: Any, **kwargs: Any) -> Any: ...
    def update_progress(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_log(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryContextOperations(Protocol):
    def read_relationship_state(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryModelOperations(Protocol):
    def repair_dialogue_fields_once(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_frozen_reply_emotion_once(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_speech_plan_once(self, *args: Any, **kwargs: Any) -> Any: ...
    def persona_repair_prefix(self, *args: Any, **kwargs: Any) -> Any: ...
    def send_upstream_chat_payload(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryVisionOperations(Protocol):
    def conversation_image_inputs(self, *args: Any, **kwargs: Any) -> Any: ...
    def with_conversation_images(self, *args: Any, **kwargs: Any) -> Any: ...
    def record_persona_model_input(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class StreamRecoveryServices:
    runtime: RecoveryRuntimeOperations
    context: RecoveryContextOperations
    model: RecoveryModelOperations
    vision: RecoveryVisionOperations
    utterances: stream_utterances.StreamUtteranceServices


def _speech_unit_contract_for_result(
    services: StreamRecoveryServices,
    result: dict[str, Any],
    *,
    request_id: str = "",
) -> dict[str, Any]:
    return speech_delivery.speech_unit_contract_for_result(
        result, request_id=request_id, write_log=services.runtime.write_log
    )


def recover_atomic_fast_stream_utterance(
    services: StreamRecoveryServices,
    raw_text: str,
    settings: dict[str, Any],
    *,
    user_message: str,
    request_id: str,
    turn_contract: TurnContract,
    persona_snapshot: PersonaContextSnapshot | None = None,
    recent_messages: list[dict[str, Any]] | None = None,
    relationship_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """从同一次服务商响应恢复轨道对齐的可见发言。

    恢复没有路由权，不能选择工具、研究或新的人格生成。已接受的中文正文保持
    冻结；日语缺失时单独补齐，交付边界缺失时先用确定性的标点回退，再使用隔离
    的语音计划修复。仅丢失元数据绝不能导致整份已接受答案重新生成。
    """
    recovery = recover_semantic_output(
        str(raw_text or ""), allow_plain_text=services.runtime.features().plain_text_salvage_enabled
    )
    if not services.runtime.features().semantic_recovery_v2_enabled or not recovery.accepted:
        return None
    services.runtime.update_progress(
        request_id, "semantic-recovery", "正在从同一次模型响应中恢复完整语义"
    )
    recovered_payload = dict(recovery.payload)
    frozen_reply = str(
        recovered_payload.get("reply") or recovered_payload.get("textZh") or ""
    ).strip()
    spoken_ja = str(
        recovered_payload.get("spokenTextJa") or recovered_payload.get("spoken_text_ja") or ""
    ).strip()
    raw_emotion = str(
        recovered_payload.get("emotion") or recovered_payload.get("expression") or ""
    ).strip()
    raw_segment_count = stream_utterances.valid_speech_segment_count(
        recovered_payload.get("speechSegmentCount")
        if recovered_payload.get("speechSegmentCount") is not None
        else recovered_payload.get("speech_segment_count")
    )
    if not frozen_reply:
        return None
    speech_profile = speech_language_profile(settings)
    requires_spoken = speech_profile["speechLanguage"] == "ja-JP"
    active_recent_messages = list(recent_messages or [])
    active_relationship_state = (
        dict(relationship_state)
        if isinstance(relationship_state, dict)
        else services.context.read_relationship_state()
    )
    missing_fields: list[str] = []
    if requires_spoken and (not spoken_ja):
        missing_fields.append("spokenTextJa")
    recovered_units = recovered_payload.get("_modelSpeechUnits")
    repaired: dict[str, Any] = {
        **recovered_payload,
        "reply": frozen_reply,
        "textZh": frozen_reply,
        "spokenTextJa": spoken_ja,
        "emotion": raw_emotion if raw_emotion in ALLOWED_EMOTIONS else "neutral",
        "spokenTextJaMissingFromModel": requires_spoken and (not bool(spoken_ja)),
        "emotionMissingFromModel": raw_emotion not in ALLOWED_EMOTIONS,
        "emotionResolvedLocally": False,
        "speechSegmentCount": raw_segment_count,
        "speechSegmentCountMissingFromModel": raw_segment_count is None,
    }
    if isinstance(recovered_units, list):
        repaired["_modelSpeechUnits"] = recovered_units
    if missing_fields:
        if (
            not services.runtime.features().field_level_repair_enabled
            or not services.runtime.configured(settings)
        ):
            return None
        try:
            services.runtime.update_progress(
                request_id, "field-repair", "正文已冻结，正在补齐日语发声字段"
            )
            repaired = services.model.repair_dialogue_fields_once(
                settings,
                repaired,
                {"missingFields": missing_fields, "invalidFields": []},
                user_message=user_message,
                recent_messages=active_recent_messages,
                relationship_state=active_relationship_state,
                request_id=request_id,
                persona_snapshot=persona_snapshot,
            )
        except Exception as repair_error:
            services.runtime.write_log(
                "semantic_recovery_field_repair_failed",
                requestId=request_id,
                mode=recovery.mode,
                frozenReplyFingerprint=runtime_fingerprint(frozen_reply, length=32),
                errorType=type(repair_error).__name__,
                error=str(repair_error)[:500],
            )
            return None
        if str(repaired.get("reply") or "").strip() != frozen_reply:
            services.runtime.write_log(
                "semantic_recovery_frozen_reply_violation", requestId=request_id, mode=recovery.mode
            )
            return None
    if repaired.get("emotionMissingFromModel"):
        if services.runtime.configured(settings):
            try:
                services.runtime.update_progress(
                    request_id, "emotion-repair", "正文已冻结，正在补齐表情与语音情绪"
                )
                repaired = services.model.repair_frozen_reply_emotion_once(
                    settings,
                    repaired,
                    user_message=user_message,
                    recent_messages=active_recent_messages,
                    relationship_state=active_relationship_state,
                    request_id=request_id,
                )
                repaired["emotionRecoveryAttempted"] = True
                repaired["emotionRecoverySucceeded"] = True
            except Exception as repair_error:
                repaired["emotionResolvedLocally"] = True
                repaired["emotionRecoveryAttempted"] = True
                repaired["emotionRecoverySucceeded"] = False
                repaired["emotionUnavailableReason"] = "emotion_label_recovery_failed"
                services.runtime.write_log(
                    "semantic_recovery_emotion_repair_failed",
                    requestId=request_id,
                    recoveryMode=recovery.mode,
                    errorType=type(repair_error).__name__,
                    error=str(repair_error)[:500],
                )
        else:
            repaired["emotionResolvedLocally"] = True
            repaired["emotionUnavailableReason"] = "model_not_configured"
    repaired_spoken = str(repaired.get("spokenTextJa") or "").strip()
    repaired_units = normalize_speech_plan_units(
        repaired.get("_modelSpeechUnits"),
        frozen_reply=frozen_reply,
        frozen_spoken=repaired_spoken,
        require_spoken=requires_spoken,
    )
    if repaired_units:
        repaired["speechSegmentCountSource"] = str(
            repaired.get("speechSegmentCountSource") or "recovered_model_semantic_units"
        )
    if not repaired_units:
        repaired_units = punctuation_fallback_speech_units(frozen_reply, repaired_spoken)
        if repaired_units:
            repaired["speechSegmentCountSource"] = "punctuation_plain_text_fallback"
            services.runtime.write_log(
                "semantic_recovery_punctuation_plan",
                requestId=request_id,
                recoveryMode=recovery.mode,
                unitCount=len(repaired_units),
                ellipsisIsBoundary=False,
            )
    if (
        not repaired_units
        and services.runtime.features().field_level_repair_enabled
        and services.runtime.configured(settings)
    ):
        try:
            services.runtime.update_progress(
                request_id, "speech-plan-repair", "正文已冻结，正在补齐语音分段计划"
            )
            repaired_units = services.model.repair_speech_plan_once(
                settings,
                {**repaired, "speechLanguage": speech_profile["speechLanguage"]},
                request_id=request_id,
                persona_snapshot=persona_snapshot,
            )
            repaired["speechPlanRepairAttempted"] = True
            repaired["speechPlanRepairSucceeded"] = True
            repaired["speechSegmentCountSource"] = "speech_plan_field_repair"
        except Exception as repair_error:
            repaired["speechPlanRepairAttempted"] = True
            repaired["speechPlanRepairSucceeded"] = False
            services.runtime.write_log(
                "semantic_recovery_speech_plan_repair_failed",
                requestId=request_id,
                recoveryMode=recovery.mode,
                errorType=type(repair_error).__name__,
                error=str(repair_error)[:500],
            )
    if not repaired_units:
        tracks_are_complete = bool(
            frozen_reply
            and (not requires_spoken or repaired_spoken)
            and (not semantic_unit_has_weak_boundary(frozen_reply))
            and (not requires_spoken or not semantic_unit_has_weak_boundary(repaired_spoken))
        )
        if not tracks_are_complete:
            services.runtime.write_log(
                "semantic_recovery_units_rejected",
                requestId=request_id,
                recoveryMode=recovery.mode,
                chineseCharacters=len(re.sub("\\s+", "", frozen_reply)),
                japaneseCharacters=len(re.sub("\\s+", "", repaired_spoken)),
                reason="accepted_speech_track_is_incomplete",
            )
            return None
        repaired_units = [
            {"textZh": frozen_reply, "spokenTextJa": repaired_spoken, "boundaryAfter": "turn_pause"}
        ]
        repaired["speechSegmentCountSource"] = "atomic_plain_text_fallback"
        services.runtime.write_log(
            "semantic_recovery_atomic_plan",
            requestId=request_id,
            recoveryMode=recovery.mode,
            reason="punctuation_tracks_do_not_align",
        )
    repaired["_modelSpeechUnits"] = repaired_units
    repaired["speechSegmentCount"] = len(repaired_units)
    repaired["speechSegmentCountMissingFromModel"] = False
    payload = {
        **repaired,
        "utteranceId": str(repaired.get("utteranceId") or f"{request_id}:semantic-recovery"),
        "textZh": frozen_reply,
        "covers": list(turn_contract.first_visible_must_cover),
        "standalone": True,
        "semanticRecoveryMode": recovery.mode,
        "plainTextSalvaged": recovery.mode == "plain_text",
        "streamProtocolRecovered": True,
        "speechSegmentCount": stream_utterances.valid_speech_segment_count(
            repaired.get("speechSegmentCount")
        )
        or 1,
        "speechSegmentCountSource": str(
            repaired.get("speechSegmentCountSource") or "atomic_no_model_boundary"
        ),
    }
    normalized = stream_utterances.normalize_streaming_utterance(
        services.utterances,
        payload,
        settings,
        sequence=1,
        user_message=user_message,
        request_id=request_id,
        turn_contract=turn_contract,
        first_visible=True,
        persona_snapshot=persona_snapshot,
    )
    contracted = _speech_unit_contract_for_result(
        services,
        {
            **normalized,
            "_modelSpeechUnits": repaired.get("_modelSpeechUnits") or [],
            "visibleSegmentationSource": str(
                repaired.get("speechSegmentCountSource") or "semantic_unit_field_repair"
            ),
        },
        request_id=request_id,
    )
    contracted_units = contracted.get("speechUnits")
    if not isinstance(contracted_units, list) or not contracted_units:
        return None
    actual_count = len(contracted_units)
    planned_count = actual_count
    segmentation_source = str(
        (contracted.get("speechUnitContract") or {}).get("segmentationSource")
        or "atomic_no_model_boundary"
    )
    utterances: list[dict[str, Any]] = []
    for index, contract_unit in enumerate(contracted_units, 1):
        text_segment = str(contract_unit.get("textZh") or "")
        speech_segment = str(contract_unit.get("spokenTextJa") or "")
        utterance = {
            **normalized,
            "proactiveDirective": normalize_proactive_directive(repaired.get("proactiveDirective")),
            "proactiveDirectiveProvided": str(repaired.get("proactiveDirective") or "")
            .strip()
            .lower()
            in PROACTIVE_DIRECTIVES,
            "utteranceId": (
                str(normalized.get("utteranceId") or f"{request_id}:semantic-recovery")
                if actual_count == 1
                else f"{request_id}:semantic-recovery:{index}"
            )[:80],
            "textZh": text_segment,
            "spokenTextJa": speech_segment,
            "covers": list(turn_contract.first_visible_must_cover) if index == 1 else [],
            "speechSegmentCount": 1,
            "speechSegmentCountSource": segmentation_source,
            "visibleSegmentationSource": segmentation_source,
            "visibleSegmentIndex": index,
            "visibleSegmentCountBefore": planned_count,
            "visibleSegmentCountAfter": actual_count,
            "boundaryAfter": str(contract_unit.get("boundaryAfter") or "continue"),
            **{
                key: (
                    contract_unit.get(key)
                    if contract_unit.get(key) is not None
                    else normalized.get(key)
                )
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
        utterances.append(utterance)
    services.runtime.write_log(
        "semantic_recovery_visible_segments_planned",
        requestId=request_id,
        recoveryMode=recovery.mode,
        segmentationSource=segmentation_source,
        speechSegmentCountBefore=planned_count,
        visibleSegmentCountAfter=actual_count,
        chineseCharacters=len(frozen_reply),
        japaneseCharacters=len(str(normalized.get("spokenTextJa") or "")),
    )
    return utterances


def recover_fast_stream_continuation(
    services: StreamRecoveryServices,
    settings: dict[str, Any],
    user_message: str,
    messages: list[dict[str, Any]],
    accepted_utterances: list[dict[str, Any]],
    *,
    request_id: str,
    starting_sequence: int,
    persona_snapshot: PersonaContextSnapshot | None = None,
    cognitive_body: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """有效流缺少尾部时，只向模型请求尚未显示的续文。"""
    accepted_zh = "\n\n".join(
        (str(item.get("textZh") or "") for item in accepted_utterances)
    ).strip()
    accepted_ja = "\n\n".join(
        (str(item.get("spokenTextJa") or "") for item in accepted_utterances)
    ).strip()
    recovery_rule = '上一轮流式回答已经向用户显示了一部分完整台词，但协议收尾丢失。判断已显示内容是否已经完整回答了用户，而不是仅凭句号判断。只输出 JSON：{"continuationNeeded":true或false,"utterances":[{"textZh":"只包含尚未说出的中文续文","spokenTextJa":"语义完整对应的自然日语","emotion":"标签","emotionIntensity":1.0,"gesture":"标签","voiceStyle":"calm","voiceTone":"neutral","voiceIntensity":"normal","speechPace":"normal","speechSpeed":1.0,"deliveryMode":"normal","blushIntensity":0.0,"eyeContact":0.7,"gazeX":0.0,"gazeY":0.0,"tension":0.0}]}。若已显示内容完整，continuationNeeded=false 且 utterances=[]。若不完整，只补缺失内容，最多三段；每段必须语义完整。禁止从头重答、重复已显示句子、写‘继续说’之类过渡语，禁止工具调用、来源和记忆更新。'
    recent_messages = [
        {
            "role": "assistant" if item.get("role") == "assistant" else "user",
            "content": str(item.get("content") or "")[:4000],
        }
        for item in messages[-8:]
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
    ]
    payload: dict[str, Any] = {
        "model": settings["apiModel"],
        "temperature": 0.35,
        "max_tokens": 1800,
        "messages": [
            *services.model.persona_repair_prefix(persona_snapshot, legacy_system_prompt=True),
            {"role": "system", "content": recovery_rule},
            *recent_messages,
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "originalUserMessage": user_message,
                        "acceptedTextZh": accepted_zh,
                        "acceptedSpokenTextJa": accepted_ja,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    if settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in settings["apiEndpoint"]:
        payload["thinking"] = {"type": "disabled"}
    try:
        image_body = cognitive_body if cognitive_body is not None else {}
        images = services.vision.conversation_image_inputs(image_body, settings)
        outgoing = services.vision.with_conversation_images(
            image_body, payload, images, purpose="chat_fast_stream_recovery"
        )
        services.vision.record_persona_model_input(
            image_body, outgoing, "chat_fast_stream_recovery"
        )
        content = services.model.send_upstream_chat_payload(
            settings,
            outgoing,
            timeout=60,
            request_id=request_id,
            purpose="chat_fast_stream_recovery",
        )
        image_body["_nativeVisualInputIds"] = [item["artifactId"] for item in images]
        parsed = parse_json_object(content) or {}
        raw_items = parsed.get("utterances") if isinstance(parsed.get("utterances"), list) else []
        if not bool(parsed.get("continuationNeeded")) or not raw_items:
            services.runtime.write_log(
                "chat_stream_recovery_not_needed",
                requestId=request_id,
                acceptedUtterances=len(accepted_utterances),
            )
            return []
        accepted_compact = re.sub("\\s+", "", accepted_zh).casefold()
        recovered: list[dict[str, Any]] = []
        for raw_item in raw_items[:3]:
            if not isinstance(raw_item, dict):
                continue
            candidate_text = re.sub("\\s+", " ", str(raw_item.get("textZh") or "")).strip()
            candidate_compact = re.sub("\\s+", "", candidate_text).casefold()
            if (
                not candidate_compact
                or candidate_compact in accepted_compact
                or accepted_compact.endswith(candidate_compact)
            ):
                continue
            normalized = stream_utterances.normalize_streaming_utterance(
                services.utterances,
                raw_item,
                settings,
                sequence=starting_sequence + len(recovered),
                user_message=user_message,
                request_id=request_id,
                persona_snapshot=persona_snapshot,
            )
            normalized["streamRecoveryContinuation"] = True
            recovered.append(normalized)
        services.runtime.write_log(
            "chat_stream_recovery_completed",
            requestId=request_id,
            acceptedUtterances=len(accepted_utterances),
            recoveredUtterances=len(recovered),
        )
        return recovered
    except Exception as error:
        services.runtime.write_log(
            "chat_stream_recovery_failed",
            requestId=request_id,
            acceptedUtterances=len(accepted_utterances),
            errorType=type(error).__name__,
            error=str(error)[:500],
        )
        return []
