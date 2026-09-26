"""原声只绑定完全对应的双语台词；普通改写继续走正常语音合成。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable, Mapping
import base64
import mimetypes
from agent.persona_context import PersonaContextSnapshot
from backend.context.nickname_catalog import NICKNAME_REACTION_OBSERVED_PREFIXES

class NicknameAudioGrounding:
    def __init__(self, *, reaction_context: Callable[..., dict[str, Any]], voice_root: Callable[[], Path]) -> None:
        self.reaction_context = reaction_context
        self.voice_root = voice_root

    def enforce_nickname_reaction_grounding(
        self,
        user_message: str,
        result: dict[str, Any],
        *,
        reaction_key: str = "",
        persona_snapshot: PersonaContextSnapshot | None = None,
        is_first_visible: bool = True,
    ) -> dict[str, Any]:
        """只把核验一致的模型台词绑定到对应原声。

        直接称呼来自语义解析器给出的明确称呼片段。本方法不按主题关键词判断
        人称，不重写模型正文；未完全匹配原声文本时仍允许正常 TTS。
        """

        del persona_snapshot
        context = self.reaction_context(str(user_message or ""), reaction_key)
        if not context.get("reactionSelected"):
            # No original reaction was selected, so model-authored banter is just
            # ordinary dialogue. It must never be treated as an audio-grounding
            # failure; there is no source clip to protect in this branch.
            validated = dict(result)
            validated.pop("nicknameReactionGroundingViolation", None)
            validated.pop("originalAudio", None)
            return validated
        reaction = context.get("reaction") if isinstance(context.get("reaction"), dict) else {}
        expected_zh = str(reaction.get("reply") or "").strip()
        expected_ja = str(reaction.get("spokenTextJa") or "").strip()
        expected_kind = str(context.get("kind") or "none")
        style_keys = (
            "emotion", "gesture", "voiceStyle", "voiceTone", "emotionIntensity"
        )
        reaction_style = {
            key: reaction[key]
            for key in style_keys
            if reaction.get(key) is not None
        }

        if not is_first_visible:
            continued = dict(result)
            continued.update(reaction_style)
            continued.update({
                "nicknameReactionExpected": expected_kind,
                "nicknameReactionObserved": "continuation",
                "nicknameReactionId": str(reaction.get("id") or ""),
                "nicknameReactionEmotionLocked": True,
                "nicknameReactionOriginalAudioBound": False,
            })
            continued.pop("nicknameReactionGroundingViolation", None)
            continued.pop("originalAudio", None)
            return continued

        def unit_tracks(item: Mapping[str, Any]) -> tuple[str, str]:
            return (
                str(item.get("textZh") or item.get("zh") or item.get("reply") or "").strip(),
                str(item.get("spokenTextJa") or item.get("ja") or "").strip(),
            )

        model_units = result.get("_modelSpeechUnits")
        first_unit = (
            model_units[0]
            if isinstance(model_units, list) and model_units and isinstance(model_units[0], dict)
            else result
        )
        observed_zh, observed_ja = unit_tracks(first_unit)
        exact = observed_zh == expected_zh and observed_ja == expected_ja
        validated = dict(result)
        validated["nicknameReactionExpected"] = expected_kind
        validated["nicknameReactionObserved"] = expected_kind if exact else "missing_or_modified"
        validated["nicknameReactionId"] = str(reaction.get("id") or "")
        if not exact:
            observed_kind = next((
                kind
                for kind, prefixes in NICKNAME_REACTION_OBSERVED_PREFIXES.items()
                if any(observed_zh.startswith(prefix) or observed_ja.startswith(prefix) for prefix in prefixes)
            ), "")
            if observed_kind == expected_kind:
                # The response contains a real model-authored reaction to the
                # parsed vocative, but it is not the exact transcript of the
                # original clip. Keep the valid answer and synthesize this text
                # normally; never bind mismatched source audio, and never turn an
                # optional persona flourish into a whole-turn 503.
                validated.update(reaction_style)
                validated.update({
                    "nicknameReactionObserved": "model_paraphrase",
                    "nicknameReactionEmotionLocked": True,
                    "nicknameReactionOriginalAudioBound": False,
                })
                validated.pop("nicknameReactionGroundingViolation", None)
                validated.pop("originalAudio", None)
                return validated
            # Exact transcript matching guards only original-audio binding. A
            # different or omitted persona reaction remains valid model dialogue
            # and is synthesized with TTS. Fixed phrase inventories must not decide
            # whether the entire turn can be delivered.
            validated.update({
                "nicknameReactionObserved": "model_variant",
                "nicknameReactionOriginalAudioBound": False,
            })
            validated.pop("nicknameReactionGroundingViolation", None)
            validated.pop("originalAudio", None)
            return validated

        audio_relative = str(reaction.get("audio") or "").strip()
        original_audio: dict[str, Any] | None = None
        if audio_relative:
            audio_path = (self.voice_root() / audio_relative).resolve()
            try:
                audio_path.relative_to(self.voice_root())
            except ValueError:
                audio_path = Path()
            if audio_path.is_file():
                audio_bytes = audio_path.read_bytes()
                original_audio = {
                    "id": str(reaction.get("id") or ""),
                    "voiceId": audio_path.stem.split("_nickname", 1)[0],
                    "contentType": mimetypes.guess_type(audio_path.name)[0] or "audio/wav",
                    "audioBase64": base64.b64encode(audio_bytes).decode("ascii"),
                    "bytes": len(audio_bytes),
                }
            else:
                validated["originalReactionAudioMissing"] = True

        if isinstance(model_units, list) and model_units and isinstance(model_units[0], dict):
            bound_units = [dict(item) if isinstance(item, dict) else item for item in model_units]
            first_bound = dict(bound_units[0])
            first_bound.update(reaction_style)
            if original_audio is not None:
                first_bound["originalAudio"] = original_audio
            first_bound["originalReactionId"] = str(reaction.get("id") or "")
            first_bound["nicknameReactionEmotionLocked"] = True
            bound_units[0] = first_bound
            for index in range(1, len(bound_units)):
                if not isinstance(bound_units[index], dict):
                    continue
                continued_unit = dict(bound_units[index])
                continued_unit.update(reaction_style)
                continued_unit["originalReactionId"] = str(reaction.get("id") or "")
                continued_unit["nicknameReactionEmotionLocked"] = True
                continued_unit.pop("originalAudio", None)
                bound_units[index] = continued_unit
            validated["_modelSpeechUnits"] = bound_units
        else:
            validated.update(reaction_style)
            if original_audio is not None:
                validated["originalAudio"] = original_audio
        validated.update(reaction_style)
        validated["nicknameReactionEmotionLocked"] = True
        validated["nicknameReactionOriginalAudioBound"] = original_audio is not None
        return validated
