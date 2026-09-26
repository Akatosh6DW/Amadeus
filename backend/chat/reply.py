"""模型回复归一化：编排字段规则，运行时状态由调用方显式提供。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping

from agent.attachment_editing import attachment_request_stopped
from agent.dialogue_waiting import question_relation, synchronize_response_waiting
from agent.participant_realization import realize_participant_speech
from agent.proactive_control import PROACTIVE_DIRECTIVES
from backend.common.values import clamp_float, clamp_int
from backend.tts.contract import MAX_SPEECH_UNITS

from .parsing import (
    extract_complete_json_string_field,
    looks_like_incomplete_structured_reply,
    parse_json_object,
)
from .presentation import (
    ALLOWED_EMOTIONS,
    ALLOWED_GESTURES,
    EMOTION_ALIASES,
    GESTURE_ALIASES,
    SPEECH_PACE_SPEED,
    fallback_spoken_text_ja,
    model_emotion_label,
    normalize_speech_pace,
    normalize_voice_tone,
    resolve_voice_intensity,
    roleplay_visual_hints,
    sanitize_roleplay_text,
    speech_language_profile,
)
from .reply_fields import (
    memory_candidates_from_model,
    normalize_boundary_assessment,
    normalize_claim_patches,
    normalize_proactive_directive,
    normalize_relation_event,
)


EMPTY_REPLY_FALLBACK = "我好像没太理解你具体想说什么，可以再解释一下吗？"


def normalize_model_reply(
    value: Any,
    settings: dict[str, Any] | None = None,
    *,
    default_settings: dict[str, Any],
    configured: Callable[[dict[str, Any] | None], bool],
    normalize_spoken_japanese_terms: Callable[[Any], str],
) -> dict[str, Any]:
    """按原模型契约归一化回复，保持字段处理顺序与缺失标记语义。

    先恢复结构并实现参与者语句，再取有序语音单元和外层缺失标记；
    嵌套 JSON 仅覆盖原逻辑指定的字段，不能统一重算所有标记。
    默认配置和两处运行时行为由 server 提供；日语回调仍会收到空值，
    由回调在非空文本时才读取词音文件。configured 只在原来的语音
    回退和 source 回退位置调用。
    """
    structured_output_recovered = False
    structured_output_invalid = False
    structured_output_missing = False
    if isinstance(value, str):
        original = value
        value = parse_json_object(value)
        if value is None:
            structured_output_missing = True
            if looks_like_incomplete_structured_reply(original):
                recovered_reply = extract_complete_json_string_field(original, "reply")
                structured_output_recovered = bool(recovered_reply)
                structured_output_invalid = True
                value = {"reply": recovered_reply}
            else:
                value = {"reply": original}
    if not isinstance(value, dict):
        value = {}
    value = realize_participant_speech(value)
    value = synchronize_response_waiting(value)
    semantic_kind = str(value.get("semanticKind") or value.get("kind") or "").strip().lower()
    if semantic_kind in {"clarification", "clarify"}:
        # A clarification is still one visible bilingual utterance. Project
        # compact V3 fields into the established internal delivery model.
        clarification_zh = str(value.get("zh") or value.get("textZh") or "").strip()
        clarification_ja = str(value.get("ja") or value.get("spokenTextJa") or "").strip()
        if clarification_zh and not isinstance(value.get("speechUnits"), list):
            value = {
                **value,
                "speechUnits": [{
                    "zh": clarification_zh,
                    "ja": clarification_ja,
                    "boundaryAfter": "turn_pause",
                }],
                "requiresUserResponse": True,
                "expectedResponseType": "free_text",
            }
    proactive_directive_provided = (
        str(value.get("proactiveDirective") or "").strip().lower()
        in PROACTIVE_DIRECTIVES
    )
    proactive_directive = normalize_proactive_directive(
        value.get("proactiveDirective")
    )
    raw_model_speech_units: list[dict[str, Any]] = []
    raw_units_value = (
        value.get("speechUnits")
        if isinstance(value.get("speechUnits"), list)
        else value.get("utterances")
    )
    if isinstance(raw_units_value, list):
        parsed_units = [dict(item) for item in raw_units_value if isinstance(item, dict)]
        raw_model_speech_units = (
            parsed_units
            if len(parsed_units) == len(raw_units_value)
            and 1 <= len(parsed_units) <= MAX_SPEECH_UNITS
            else []
        )
        if raw_model_speech_units and not str(value.get("reply") or "").strip():
            joined_reply = "\n\n".join(
                str(item.get("zh") or item.get("textZh") or item.get("reply") or "").strip()
                for item in raw_model_speech_units
            ).strip()
        else:
            joined_reply = ""
        if raw_model_speech_units and not str(value.get("spokenTextJa") or "").strip():
            joined_spoken = "\n\n".join(
                str(item.get("ja") or item.get("spokenTextJa") or "").strip()
                for item in raw_model_speech_units
            ).strip()
        else:
            joined_spoken = ""
        if joined_reply or joined_spoken:
            value = {
                **value,
                **({"reply": joined_reply} if joined_reply else {}),
                **({"spokenTextJa": joined_spoken} if joined_spoken else {}),
            }
    elif isinstance(value.get("_modelSpeechUnits"), list):
        parsed_units = [
            dict(item) for item in value["_modelSpeechUnits"] if isinstance(item, dict)
        ]
        raw_model_speech_units = (
            parsed_units
            if len(parsed_units) == len(value["_modelSpeechUnits"])
            and 1 <= len(parsed_units) <= MAX_SPEECH_UNITS
            else []
        )
    requires_user_response_provided = (
        bool(value.get("requiresUserResponseProvided"))
        if isinstance(value.get("requiresUserResponseProvided"), bool)
        else isinstance(value.get("requiresUserResponse"), bool)
    )
    requires_user_response = (
        bool(value.get("requiresUserResponse"))
        if requires_user_response_provided else False
    )
    expected_response_type = str(value.get("expectedResponseType") or "").strip()
    if expected_response_type not in {"free_text", "confirmation", "browser_target", "choice"}:
        expected_response_type = ""
    completed_acts_value = value.get("completedActs")
    completed_acts = (
        [
            str(item).strip()
            for item in completed_acts_value
            if str(item).strip()
        ]
        if isinstance(completed_acts_value, (list, tuple))
        else []
    )
    standalone_provided = isinstance(value.get("standalone"), bool)
    depends_on_prefix_provided = isinstance(
        value.get("dependsOnUndeliveredPrefix"),
        bool,
    )
    turn_complete_provided = isinstance(value.get("turnComplete"), bool)
    nested_reply = value.get("reply")
    if isinstance(nested_reply, str):
        nested_value = parse_json_object(nested_reply)
        if isinstance(nested_value, dict) and isinstance(nested_value.get("reply"), str):
            # 嵌套 JSON 只重算原逻辑指定的部分标记；其他标记已取自外层模型响应。
            # Some OpenAI-compatible providers occasionally JSON-encode the
            # whole structured response into the reply field. Decode that
            # transport wrapper instead of showing raw JSON to the user.
            value = synchronize_response_waiting({**value, **nested_value})
            proactive_directive_provided = (
                str(value.get("proactiveDirective") or "").strip().lower()
                in PROACTIVE_DIRECTIVES
            )
            proactive_directive = normalize_proactive_directive(
                value.get("proactiveDirective")
            )
            requires_user_response_provided = (
                bool(value.get("requiresUserResponseProvided"))
                if isinstance(value.get("requiresUserResponseProvided"), bool)
                else isinstance(value.get("requiresUserResponse"), bool)
            )
            requires_user_response = (
                bool(value.get("requiresUserResponse"))
                if requires_user_response_provided else False
            )
            expected_response_type = str(
                value.get("expectedResponseType") or ""
            ).strip()
            if expected_response_type not in {
                "free_text", "confirmation", "browser_target", "choice",
            }:
                expected_response_type = ""
            completed_acts_value = value.get("completedActs")
            completed_acts = (
                [
                    str(item).strip()
                    for item in completed_acts_value
                    if str(item).strip()
                ]
                if isinstance(completed_acts_value, (list, tuple))
                else []
            )
            standalone_provided = isinstance(value.get("standalone"), bool)
            depends_on_prefix_provided = isinstance(
                value.get("dependsOnUndeliveredPrefix"),
                bool,
            )
            turn_complete_provided = isinstance(value.get("turnComplete"), bool)
    emotion_missing_from_model = not bool(model_emotion_label(value))
    raw_reply = str(value.get("reply") or "").strip()
    reply, reply_stage_directions = sanitize_roleplay_text(raw_reply)
    raw_spoken_text_ja = normalize_spoken_japanese_terms(value.get("spokenTextJa") or value.get("spoken_text_ja"))
    spoken_text_ja, spoken_stage_directions = sanitize_roleplay_text(raw_spoken_text_ja)
    spoken_text_ja_missing_from_model = not bool(spoken_text_ja)
    visual_hints = roleplay_visual_hints(reply_stage_directions + spoken_stage_directions)
    raw_emotion = str(value.get("emotion") or value.get("expression") or "neutral").strip()
    emotion = raw_emotion if raw_emotion in ALLOWED_EMOTIONS else EMOTION_ALIASES.get(raw_emotion, "neutral")
    if emotion == "neutral" and visual_hints.get("emotion"):
        emotion = str(visual_hints["emotion"])
    raw_gesture = str(value.get("gesture") or value.get("motion") or "idle").strip().casefold()
    gesture = GESTURE_ALIASES.get(raw_gesture, raw_gesture)
    if gesture == "idle" and visual_hints.get("gesture"):
        gesture = str(visual_hints["gesture"])
    if gesture not in ALLOWED_GESTURES:
        gesture = "idle"
    memory_update_keys = ("memoryUpdates", "memory_candidates", "memoryCandidates")
    memory_updates_provided = (
        bool(value.get("memoryUpdatesProvided"))
        if isinstance(value.get("memoryUpdatesProvided"), bool)
        else any(key in value for key in memory_update_keys)
    )
    raw_memory_action = str(value.get("memoryAction") or "").strip().lower()
    memory_action_provided = (
        bool(value.get("memoryActionProvided"))
        if isinstance(value.get("memoryActionProvided"), bool)
        else raw_memory_action in {"none", "save"}
    )
    memory_action = raw_memory_action if raw_memory_action in {"none", "save"} else "none"
    raw_memory_candidates = value.get("memoryUpdates")
    if not isinstance(raw_memory_candidates, list):
        raw_memory_candidates = value.get("memory_candidates") or value.get("memoryCandidates")
    structured_memory_candidates = memory_candidates_from_model(raw_memory_candidates)
    memories = [item["content"] for item in structured_memory_candidates]
    relation_event_provided = (
        bool(value.get("relationEventProvided"))
        if isinstance(value.get("relationEventProvided"), bool)
        else "relationEvent" in value
    )
    relation_event = normalize_relation_event(value.get("relationEvent"))
    memory_update_types = {
        str(item.get("content") or "").strip(): str(item.get("type") or "").strip()
        for item in (raw_memory_candidates if isinstance(raw_memory_candidates, list) else [])
        if isinstance(item, dict)
        and str(item.get("content") or "").strip()
        and str(item.get("type") or "").strip() in {
            "user_memory", "task_state", "correction", "episode",
            "shared_reference", "relationship_state",
        }
    }
    empty_reply = not reply
    if not reply:
        reply = EMPTY_REPLY_FALLBACK
    if not spoken_text_ja:
        needs_semantic_spoken_alignment = bool(
            configured(settings)
            and str((settings or {}).get("ttsProvider") or "").strip().lower() == "gpt-sovits"
        )
        if not needs_semantic_spoken_alignment:
            spoken_text_ja = fallback_spoken_text_ja(reply)
    # The legacy scalar count carries no boundary information and therefore
    # cannot authorize segmentation.  Only explicit ordered units do.
    model_speech_segment_count = (
        len(raw_model_speech_units) if raw_model_speech_units else None
    )
    speech_segment_count_missing_from_model = (
        not raw_model_speech_units
    )
    speech_segment_count = (
        model_speech_segment_count or 1
    )
    speech_pace = normalize_speech_pace(value.get("speechPace") or value.get("speech_pace"), value.get("speechSpeed"))
    speech_speed = clamp_float(value.get("speechSpeed"), 0.62, 1.45, SPEECH_PACE_SPEED[speech_pace])
    voice_style = str(value.get("voiceStyle") or "calm")
    delivery_mode = str(value.get("deliveryMode") or value.get("delivery_mode") or "normal").strip().lower()
    delivery_mode = "outburst" if delivery_mode == "outburst" else "normal"
    emotion_intensity = clamp_float(
        value.get("emotionIntensity") or value.get("emotion_intensity") or visual_hints.get("emotionIntensity"),
        0.2,
        2.5 if delivery_mode == "outburst" else 1.25,
        1.0,
    )
    emotion_delta = clamp_float(
        value.get("emotionDelta") if value.get("emotionDelta") is not None else value.get("emotion_delta"),
        -0.45,
        0.45,
        0.0,
    )
    voice_intensity, voice_intensity_source = resolve_voice_intensity(
        value.get("voiceIntensity") if value.get("voiceIntensity") is not None else value.get("voice_intensity"),
        emotion_intensity,
        emotion_delta,
    )
    requested_hold_ms = clamp_int(value.get("holdMs") or value.get("hold_ms"), 0, 120000, 0 if emotion == "neutral" else 45000)
    requested_fade_ms = clamp_int(value.get("fadeMs") or value.get("fade_ms"), 500, 30000, 1200 if emotion == "neutral" else 10000)
    hold_ms = 0 if emotion == "neutral" else max(42000, requested_hold_ms)
    fade_ms = requested_fade_ms if emotion == "neutral" else max(8000, requested_fade_ms)
    voice_tone = normalize_voice_tone(value.get("voiceTone") or value.get("voice_tone"), voice_style)
    if emotion == "shy" and voice_tone not in {"shy", "shy_annoyed"}:
        voice_tone = "shy"
        if voice_style == "calm":
            voice_style = "soft"
    elif emotion == "flustered" and voice_tone != "flustered":
        voice_tone = "flustered"
        if voice_style == "calm":
            voice_style = "energetic"
    elif emotion == "startled":
        voice_tone = "startled"
        if voice_style == "calm":
            voice_style = "energetic"
    elif emotion == "laughing":
        voice_tone = "laughing"
    elif emotion == "furious":
        voice_tone = "cold"
        gesture = "look_away"
        voice_style = "sharp"
        voice_intensity = "strong" if delivery_mode == "outburst" else voice_intensity
    speech_profile = speech_language_profile(settings or default_settings)
    speech_text = reply if speech_profile["speechLanguage"] == "zh-CN" else spoken_text_ja
    if attachment_request_stopped(value):
        # The model's terminal capability decision is authoritative even when
        # it omitted the redundant waiting flag or used a rhetorical question.
        requires_user_response = False
        requires_user_response_provided = True
        expected_response_type = ""
    return {
        **({"_participantSpeechRealized": True} if value.get("_participantSpeechRealized") is True else {}),
        "reply": reply,
        "spokenTextJa": spoken_text_ja,
        "speechText": speech_text,
        "speechLanguage": speech_profile["speechLanguage"],
        "emotion": emotion,
        "gesture": gesture,
        "voiceStyle": voice_style,
        "voiceTone": voice_tone,
        "voiceIntensity": voice_intensity,
        "voiceIntensitySource": voice_intensity_source,
        "speechPace": speech_pace,
        "speechSpeed": speech_speed,
        "speechSegmentCount": speech_segment_count,
        "speechSegmentCountMissingFromModel": speech_segment_count_missing_from_model,
        "speechSegmentCountSource": (
            "model_semantic_units"
            if raw_model_speech_units
            else "legacy_model_count"
            if model_speech_segment_count is not None
            else "missing_pending_alignment"
        ),
        "emotionIntensity": emotion_intensity,
        "emotionDelta": emotion_delta,
        "blushIntensity": clamp_float(value.get("blushIntensity") or value.get("blush") or visual_hints.get("blushIntensity"), 0.0, 1.0, 0.78 if emotion == "shy" else 0.88 if emotion == "flustered" else 0.0),
        "eyeContact": clamp_float(value.get("eyeContact") if value.get("eyeContact") is not None else visual_hints.get("eyeContact"), 0.0, 1.0, 0.18 if emotion == "furious" else 0.42 if emotion == "shy" else 0.31 if emotion == "flustered" else 0.68),
        "gazeX": clamp_float(value.get("gazeX") if value.get("gazeX") is not None else visual_hints.get("gazeX"), -0.6, 0.6, 0.5 if emotion == "furious" else -0.16 if emotion == "shy" else -0.24 if emotion == "flustered" else 0.0),
        "gazeY": clamp_float(value.get("gazeY") if value.get("gazeY") is not None else visual_hints.get("gazeY"), -0.6, 0.6, -0.04 if emotion == "furious" else 0.13 if emotion == "shy" else 0.18 if emotion == "flustered" else 0.0),
        "tension": clamp_float(value.get("tension") if value.get("tension") is not None else visual_hints.get("tension"), 0.0, 1.0, 0.92 if emotion == "furious" else 0.55 if emotion == "annoyed" else 0.0),
        "deliveryMode": delivery_mode,
        "outburst": delivery_mode == "outburst",
        "coldShoulder": bool(value.get("coldShoulder") or value.get("cold_shoulder")),
        "boundaryAssessment": normalize_boundary_assessment(
            value.get("boundaryAssessment") or value.get("boundary_assessment")
        ),
        "holdMs": hold_ms,
        "fadeMs": fade_ms,
        "memoryUpdates": memories[:4] if isinstance(memories, list) else [],
        "memoryUpdatesProvided": memory_updates_provided,
        "memoryAction": memory_action,
        "memoryActionProvided": memory_action_provided,
        "_memoryCandidates": structured_memory_candidates[:4],
        "_memoryUpdateTypes": memory_update_types,
        "relationEvent": relation_event,
        "relationEventProvided": relation_event_provided,
        "source": value.get("source") or ("api" if configured(settings) else "local"),
        "emptyReply": empty_reply,
        "structuredOutputRecovered": structured_output_recovered,
        "structuredOutputInvalid": structured_output_invalid,
        "structuredOutputMissingFromModel": structured_output_missing,
        "spokenTextJaMissingFromModel": spoken_text_ja_missing_from_model,
        "emotionMissingFromModel": emotion_missing_from_model,
        "stageDirectionsRemoved": bool(reply_stage_directions or spoken_stage_directions),
        "requiresUserResponse": requires_user_response,
        "requiresUserResponseProvided": requires_user_response_provided,
        "semanticKind": str(value.get("semanticKind") or value.get("kind") or semantic_kind).strip().lower()[:40],
        "outcome": deepcopy(value.get("outcome")) if isinstance(value.get("outcome"), Mapping) else None,
        "questionRelation": question_relation(value.get("questionRelation")),
        "proactiveDirective": proactive_directive,
        "proactiveDirectiveProvided": proactive_directive_provided,
        "expectedResponseType": expected_response_type,
        "clarificationActionId": str(value.get("clarificationActionId") or "")[:100],
        "completedActs": list(dict.fromkeys(completed_acts))[:12],
        "completedActsProvided": isinstance(completed_acts_value, (list, tuple)),
        "standalone": value.get("standalone") is True,
        "standaloneProvided": standalone_provided,
        "dependsOnUndeliveredPrefix": bool(value.get("dependsOnUndeliveredPrefix")),
        "dependsOnUndeliveredPrefixProvided": depends_on_prefix_provided,
        "turnComplete": value.get("turnComplete") is True,
        "turnCompleteProvided": turn_complete_provided,
        "_fileOutputs": deepcopy(value.get("fileOutputs", value.get("_fileOutputs", []))),
        "attachmentAction": deepcopy(value.get("attachmentAction")) if isinstance(value.get("attachmentAction"), Mapping) else None,
        "_modelSpeechUnits": raw_model_speech_units,
        "claimPatches": normalize_claim_patches(value.get("claimPatches")),
    }
