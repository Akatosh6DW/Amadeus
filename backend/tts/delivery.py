"""冻结双语轨道后的语音单元交付与整轮情绪包络。"""

from __future__ import annotations

import re
from typing import Any, Callable, Sequence

from agent.fingerprints import runtime_fingerprint
from backend.chat.presentation import (
    ALLOWED_EMOTIONS,
    EMOTION_ALIASES,
    normalize_speech_language,
    normalize_voice_intensity,
    normalize_voice_tone,
    resolve_voice_intensity,
)
from backend.common.values import clamp_float
from backend.tts.contract import MAX_SPEECH_UNITS


DeliveryLog = Callable[..., None]


def _ignore_log(_event: str, **_details: Any) -> None:
    """供独立调用使用；服务器入口会注入真实日志函数。"""


# 同一轮可以改变情绪色彩，但不能丢失已经建立的激活强度。这是针对模型
# 结构化标签的规则，不从文本或关键词猜情绪；它避免同一回答因逐单元归一化
# 而出现 flustered -> shy -> neutral 这类彼此割裂的听感。
EMOTION_ACTIVATION_LEVEL = {
    "neutral": 0,
    "thinking": 1,
    "focused": 1,
    "melancholic": 1,
    "proud": 1,
    "skeptical": 2,
    "awkward": 2,
    "amused": 2,
    "affectionate": 2,
    "concerned": 2,
    "teasing": 3,
    "shy": 3,
    "protective": 3,
    "annoyed": 4,
    "flustered": 4,
    "excited": 4,
    "startled": 4,
    "laughing": 4,
    "furious": 5,
}


VOICE_INTENSITY_LEVEL = {"subtle": 0, "normal": 1, "strong": 2}


def enforce_turn_emotion_envelope(
    units: Sequence[dict[str, Any]],
    *,
    request_id: str = "",
    write_log: DeliveryLog = _ignore_log,
) -> list[dict[str, Any]]:
    """让同一轮已经建立的情绪激活度只能保持或升高。

    每个单元仍可改变措辞、动作和情绪色彩；真正更平静的反应应放在后续
    对话轮次中，使变化能够被听见并且符合上下文，而不是在同一排队回答中
    途中过早降温。
    """

    adjusted_units = [dict(item) for item in units]
    if len(adjusted_units) < 2:
        return adjusted_units

    anchor: dict[str, Any] | None = None
    anchor_ordinal = 0
    anchor_level = -1
    anchor_effective = 0.0
    adjustments: list[dict[str, Any]] = []
    acoustic_fields = (
        "emotion", "voiceStyle", "voiceTone", "deliveryMode",
    )

    for ordinal, unit in enumerate(adjusted_units, 1):
        raw_emotion = str(unit.get("emotion") or "neutral").strip()
        emotion = (
            raw_emotion
            if raw_emotion in ALLOWED_EMOTIONS
            else EMOTION_ALIASES.get(raw_emotion, "neutral")
        )
        unit["emotion"] = emotion
        emotion_intensity = clamp_float(
            unit.get("emotionIntensity"), 0.2, 2.5, 1.0
        )
        emotion_delta = clamp_float(
            unit.get("emotionDelta"), -0.45, 0.45, 0.0
        )
        effective = clamp_float(
            emotion_intensity + emotion_delta * 0.65,
            0.2,
            2.5,
            1.0,
        )
        level = EMOTION_ACTIVATION_LEVEL.get(emotion, 0)

        if anchor is None:
            anchor = dict(unit)
            anchor_ordinal = ordinal
            anchor_level = level
            anchor_effective = effective
            continue

        original = {
            "emotion": emotion,
            "emotionIntensity": emotion_intensity,
            "voiceStyle": str(unit.get("voiceStyle") or "calm"),
            "voiceTone": str(unit.get("voiceTone") or "neutral"),
            "voiceIntensity": normalize_voice_intensity(unit.get("voiceIntensity")),
        }
        changed = False
        if level < anchor_level:
            for field in acoustic_fields:
                if anchor.get(field) is not None:
                    unit[field] = anchor[field]
            unit["emotionIntensity"] = max(
                emotion_intensity,
                clamp_float(anchor.get("emotionIntensity"), 0.2, 2.5, anchor_effective),
            )
            unit["emotionDelta"] = max(
                emotion_delta,
                clamp_float(anchor.get("emotionDelta"), -0.45, 0.45, 0.0),
            )
            changed = True
        elif effective < anchor_effective:
            # 允许同级或更高等级改变情绪色彩，但声学能量不能跌破本轮包络。
            unit["emotionIntensity"] = max(emotion_intensity, anchor_effective)
            unit["emotionDelta"] = max(emotion_delta, 0.0)
            changed = True

        if changed:
            resolved_intensity, _ = resolve_voice_intensity(
                unit.get("voiceIntensity"),
                clamp_float(unit.get("emotionIntensity"), 0.2, 2.5, anchor_effective),
                clamp_float(unit.get("emotionDelta"), -0.45, 0.45, 0.0),
            )
            anchor_voice_intensity = normalize_voice_intensity(
                anchor.get("voiceIntensity")
            )
            unit["voiceIntensity"] = max(
                (resolved_intensity, anchor_voice_intensity),
                key=lambda item: VOICE_INTENSITY_LEVEL[item],
            )
            unit["emotionEnvelopeAdjusted"] = True
            unit["emotionEnvelopeAnchorOrdinal"] = anchor_ordinal
            adjustments.append({
                "ordinal": ordinal,
                "fromEmotion": original["emotion"],
                "toEmotion": str(unit.get("emotion") or "neutral"),
                "fromVoiceTone": original["voiceTone"],
                "toVoiceTone": str(unit.get("voiceTone") or "neutral"),
                "fromVoiceIntensity": original["voiceIntensity"],
                "toVoiceIntensity": str(unit.get("voiceIntensity") or "normal"),
            })

        current_emotion = str(unit.get("emotion") or "neutral")
        current_level = EMOTION_ACTIVATION_LEVEL.get(current_emotion, 0)
        current_intensity = clamp_float(
            unit.get("emotionIntensity"), 0.2, 2.5, anchor_effective
        )
        current_delta = clamp_float(unit.get("emotionDelta"), -0.45, 0.45, 0.0)
        current_effective = clamp_float(
            current_intensity + current_delta * 0.65, 0.2, 2.5, current_intensity
        )
        if current_level > anchor_level or (
            current_level == anchor_level and current_effective >= anchor_effective
        ):
            anchor = dict(unit)
            anchor_ordinal = ordinal
            anchor_level = current_level
            anchor_effective = current_effective

    if adjustments and request_id:
        write_log(
            "turn_emotion_envelope_applied",
            requestId=request_id,
            policy="non_decreasing_activation",
            unitCount=len(adjusted_units),
            adjustments=adjustments,
        )
    return adjusted_units


def speech_unit_contract_for_result(
    result: dict[str, Any],
    *,
    request_id: str = "",
    write_log: DeliveryLog = _ignore_log,
) -> dict[str, Any]:
    """按模型显式边界装配完整语音单元，否则保持整轮原子交付。

    只有模型提供的 ``_modelSpeechUnits`` 才能建立多单元边界。边界缺失或
    任一语言轨道无法无损覆盖时，完整回答保留为一个原子单元；明确标记的
    纯文本恢复路径可在进入本函数前提供确定性的强标点回退单元。
    """

    updated = dict(result)
    reply = re.sub(
        r"\s+", " ", str(updated.get("reply") or updated.get("textZh") or "")
    ).strip()
    spoken = re.sub(r"\s+", " ", str(updated.get("spokenTextJa") or "")).strip()
    speech_language = normalize_speech_language(updated.get("speechLanguage"))
    # speechText 只是兼容投影，不是第二份事实来源；后续翻译、改写或边界路径
    # 可能替换任一语言轨道，因此每次都从当前冻结轨道重建。
    updated["speechText"] = str(updated.get("reply") or updated.get("textZh") or "") if speech_language == "zh-CN" else str(updated.get("spokenTextJa") or "")
    if not reply or (speech_language == "ja-JP" and not spoken):
        updated["speechUnits"] = []
        updated["speechUnitContract"] = {
            "schemaVersion": 2,
            "expectedUnitCount": 0,
            "allUnitsComplete": False,
            "tailOmitted": False,
            "segmentationSource": "invalid_missing_speech_track",
            "error": "missing_required_speech_track",
        }
        return updated

    def compact(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    source_units = updated.get("_modelSpeechUnits")
    candidates: list[dict[str, Any]] = []
    if isinstance(source_units, list) and 1 <= len(source_units) <= MAX_SPEECH_UNITS:
        for item in source_units:
            if not isinstance(item, dict):
                candidates = []
                break
            text_zh = re.sub(
                r"\s+", " ", str(item.get("textZh") or item.get("zh") or "")
            ).strip()
            spoken_ja = re.sub(
                r"\s+", " ", str(item.get("spokenTextJa") or item.get("ja") or "")
            ).strip()
            if not text_zh or (speech_language == "ja-JP" and not spoken_ja):
                candidates = []
                break
            candidates.append({**item, "textZh": text_zh, "spokenTextJa": spoken_ja})
    candidates_cover_reply = bool(candidates) and compact(
        "".join(str(item["textZh"]) for item in candidates)
    ) == compact(reply)
    candidates_cover_spoken = (
        speech_language != "ja-JP"
        or (
            bool(candidates)
            and compact("".join(str(item["spokenTextJa"]) for item in candidates))
            == compact(spoken)
        )
    )
    if not candidates_cover_reply or not candidates_cover_spoken:
        candidates = [{
            "textZh": reply,
            "spokenTextJa": spoken,
            "boundaryAfter": "turn_pause",
        }]
        segmentation_source = "atomic_no_model_boundary"
    else:
        segmentation_source = str(
            updated.get("visibleSegmentationSource") or "model_semantic_protocol_v3"
        )

    units: list[dict[str, Any]] = []
    for ordinal, candidate in enumerate(candidates, 1):
        text_zh = str(candidate["textZh"])
        spoken_ja = str(candidate.get("spokenTextJa") or "")
        normalized = re.sub(r"\s+", "", f"{text_zh}\u241f{spoken_ja}")
        text_fingerprint = runtime_fingerprint(normalized, length=32)
        stable_seed = f"{request_id}|{ordinal}|{text_fingerprint}"
        unit = {
            **({"_participantSpeechRealized": True} if updated.get("_participantSpeechRealized") is True else {}),
            "unitId": "speech-" + runtime_fingerprint(stable_seed, length=16),
            "ordinal": ordinal,
            "total": len(candidates),
            "textZh": text_zh,
            "spokenTextJa": spoken_ja,
            "normalizedTextFingerprint": text_fingerprint,
            "deliveryRequired": True,
            "unitComplete": True,
            "boundaryAfter": (
                str(candidate.get("boundaryAfter") or "continue")
                if str(candidate.get("boundaryAfter") or "continue")
                in {"continue", "micro_pause", "phrase_pause", "turn_pause"}
                else "continue"
            ),
        }
        for key in (
            "emotion", "emotionIntensity", "voiceStyle", "voiceTone",
            "voiceIntensity", "speechPace", "speechSpeed", "originalAudio",
            "originalReactionId", "gesture", "blushIntensity", "eyeContact",
            "gazeX", "gazeY", "tension", "nicknameReactionEmotionLocked",
        ):
            inherited = candidate.get(key)
            if inherited is None:
                inherited = updated.get(key)
            if inherited is not None:
                unit[key] = inherited
        # 单元覆盖值在浏览器和 TTS 使用前，必须遵守与顶层回复相同的分类契约。
        unit["voiceTone"] = normalize_voice_tone(unit.get("voiceTone"), str(updated.get("voiceTone") or "neutral"))
        unit["voiceIntensity"], _ = resolve_voice_intensity(
            unit.get("voiceIntensity"),
            clamp_float(unit.get("emotionIntensity"), 0.2, 2.5, 1.0),
            clamp_float(unit.get("emotionDelta"), -1.0, 1.0, 0.0),
        )
        units.append(unit)
    units = enforce_turn_emotion_envelope(
        units,
        request_id=request_id,
        write_log=write_log,
    )
    if units and any(item.get("emotionEnvelopeAdjusted") for item in units):
        for key in (
            "emotion", "emotionIntensity", "emotionDelta", "voiceStyle",
            "voiceTone", "voiceIntensity", "deliveryMode",
        ):
            if units[-1].get(key) is not None:
                updated[key] = units[-1][key]
    contract_fingerprint = runtime_fingerprint(
        "|".join(str(item["normalizedTextFingerprint"]) for item in units),
        length=32,
    )
    updated["speechUnits"] = units
    updated["speechUnitContract"] = {
        "schemaVersion": 2,
        "expectedUnitCount": len(units),
        "allUnitsComplete": bool(units),
        "tailOmitted": False,
        "segmentationSource": segmentation_source,
        "punctuationBoundaryInference": (
            segmentation_source == "punctuation_plain_text_fallback"
        ),
        "contractFingerprint": contract_fingerprint,
    }
    updated["visibleSegmentationSource"] = segmentation_source
    updated["speechSegmentCount"] = len(units)
    return updated
