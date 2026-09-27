"""保持已验收台词不变，有限修复发声字段与分段计划。

字段修复不能重新回答用户；分段修复必须无损覆盖冻结的双语轨道。
复用现有 finish-reason ContextVar，不能另建共享状态或增加恢复预算。
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import math
import re
from typing import Any, Callable, Mapping
from agent.persona_context import PersonaContextSnapshot
from backend.chat.parsing import parse_json_object
from backend.providers.errors import UpstreamProtocolError

from agent.fingerprints import runtime_fingerprint
from backend.chat.presentation import sanitize_roleplay_text
from backend.chat.reply_fields import SPOKEN_JA_NO_EMOJI_RULE
from backend.tts.japanese_rules import dialogue_field_repair_token_budget, soften_spoken_japanese_style

@dataclass(frozen=True)
class FieldRepairOperations:
    persona_repair_prefix: Callable[..., Any]
    spoken_ja_pronunciation_glossary: Callable[..., Any]
    write_server_log: Callable[..., Any]
    send_upstream_chat_payload: Callable[..., Any]
    normalize_spoken_japanese_terms: Callable[..., Any]
    should_align_spoken_text_ja: Callable[..., Any]
    normalize_speech_plan_units: Callable[..., Any]
    normalize_speech_language: Callable[..., Any]

class SpeechFieldRepair:
    def __init__(self, operations: FieldRepairOperations, *, finish_reason: Any, max_units: Callable[[], int]) -> None:
        self.operations = operations
        self.finish_reason = finish_reason
        self.max_units = max_units

    def repair_dialogue_fields_once(
        self,
        settings: dict[str, Any],
        result: dict[str, Any],
        validation: dict[str, Any],
        *,
        user_message: str,
        recent_messages: list[dict[str, Any]],
        relationship_state: dict[str, Any],
        request_id: str,
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> dict[str, Any]:
        """Repair only the speech-critical field while freezing accepted prose."""

        # Preserve the public call shape for existing callers, but do not give a
        # speech-only repair permission to reinterpret dialogue history or the
        # relationship state.
        del recent_messages, relationship_state

        frozen_reply = str(result.get("reply") or "").strip()
        frozen_fingerprint = runtime_fingerprint(frozen_reply, length=32)
        missing = [str(item) for item in validation.get("missingFields", [])]
        invalid = [str(item) for item in validation.get("invalidFields", [])]
        requested = list(dict.fromkeys([
            *(["spokenTextJa"] if "spokenTextJa" in {*missing, *invalid} else []),
        ]))
        if not requested:
            return dict(result)
        repair_max_tokens = dialogue_field_repair_token_budget(frozen_reply, requested)
        response_shape: dict[str, Any] = {field: "..." for field in requested}
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.18,
            "max_tokens": repair_max_tokens,
            "messages": [
                *self.operations.persona_repair_prefix(persona_snapshot, legacy_system_prompt=False),
                {"role": "system", "content": (
                    "你是 Amadeus 的字段修复器，不重新回答用户，也不改写中文台词。"
                    "只输出一个 JSON 对象，只填写 requestedFields 指定的字段；即使你认为中文可以改进也不得输出 reply。"
                    "spokenTextJa 必须完整对应 frozenReply 的全部语义和情绪，使用自然年轻女性日语口语及官方专名读法。"
                    "不要输出 emotion、speechUnits、gesture 或其他未请求字段。"
                    + "\n" + SPOKEN_JA_NO_EMOJI_RULE
                )},
                {"role": "user", "content": json.dumps({
                    "currentUserMessage": str(user_message or ""),
                    "frozenReply": frozen_reply,
                    "frozenSpokenTextJa": str(result.get("spokenTextJa") or ""),
                    "frozenReplyFingerprint": frozen_fingerprint,
                    "requestedFields": requested,
                    "missingFields": missing,
                    "invalidFields": invalid,
                    "responseShape": response_shape,
                    "pronunciationGlossary": self.operations.spoken_ja_pronunciation_glossary(
                        frozen_reply,
                        str(result.get("spokenTextJa") or ""),
                    ),
                }, ensure_ascii=False)},
            ],
        }
        self.operations.write_server_log(
            "dialogue_field_repair_request",
            requestId=request_id,
            requestedFields=requested,
            maxTokens=repair_max_tokens,
            frozenReplyChars=len(frozen_reply),
        )
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        self.finish_reason.set("")
        content = self.operations.send_upstream_chat_payload(
            settings,
            payload,
            timeout=45,
            request_id=request_id,
            purpose="dialogue_field_repair",
        )
        repair_finish_reason = self.finish_reason.get()
        parsed = parse_json_object(content)
        repair_json_complete = isinstance(parsed, dict)
        self.operations.write_server_log(
            "dialogue_field_repair_response",
            requestId=request_id,
            requestedFields=requested,
            maxTokens=repair_max_tokens,
            finishReason=repair_finish_reason,
            jsonComplete=repair_json_complete,
        )
        if not isinstance(parsed, dict):
            raise UpstreamProtocolError("dialogue field repair returned no JSON object")
        repaired = dict(result)
        if "spokenTextJa" in requested:
            spoken, _ = sanitize_roleplay_text(
                self.operations.normalize_spoken_japanese_terms(parsed.get("spokenTextJa"))
            )
            spoken = soften_spoken_japanese_style(spoken)
            spoken_probe = {
                **repaired,
                "spokenTextJa": spoken,
                "spokenTextJaMissingFromModel": False,
                "speechSegmentCountMissingFromModel": False,
            }
            if not spoken or self.operations.should_align_spoken_text_ja(settings, spoken_probe):
                raise UpstreamProtocolError("dialogue field repair returned unusable spokenTextJa")
            repaired["spokenTextJa"] = spoken
            repaired["spokenTextJaMissingFromModel"] = False
            repaired["spokenTextJaRepaired"] = True
            repaired["spokenTextJaRepairMode"] = "unified_field_repair"
        repaired["reply"] = frozen_reply
        repaired["dialogueFieldRepairSucceeded"] = True
        repaired["dialogueFieldRepairFrozenReplyFingerprint"] = frozen_fingerprint
        repaired["dialogueFieldRepairFields"] = requested
        repaired["dialogueFieldRepairMaxTokens"] = repair_max_tokens
        repaired["dialogueFieldRepairFinishReason"] = repair_finish_reason
        repaired["dialogueFieldRepairJsonComplete"] = repair_json_complete
        if persona_snapshot is not None:
            repaired["personaContextSnapshotId"] = persona_snapshot.snapshot_id
        return repaired


    def repair_speech_plan_once(
        self,
        settings: dict[str, Any],
        result: dict[str, Any],
        *,
        request_id: str,
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> list[dict[str, Any]]:
        """Repair only delivery boundaries while both accepted tracks stay frozen."""

        frozen_reply = str(result.get("reply") or result.get("textZh") or "").strip()
        frozen_spoken = str(result.get("spokenTextJa") or "").strip()
        speech_language = self.operations.normalize_speech_language(result.get("speechLanguage"))
        require_spoken = speech_language == "ja-JP"
        if not frozen_reply or (require_spoken and not frozen_spoken):
            raise UpstreamProtocolError("speech plan repair requires complete frozen tracks")

        reply_fingerprint = runtime_fingerprint(frozen_reply, length=32)
        spoken_fingerprint = runtime_fingerprint(frozen_spoken, length=32)
        max_tokens = max(
            384,
            min(
                1800,
                256 + 2 * len(re.sub(r"\s+", "", frozen_reply + frozen_spoken)),
            ),
        )
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.12,
            "max_tokens": max_tokens,
            "messages": [
                *self.operations.persona_repair_prefix(persona_snapshot, legacy_system_prompt=False),
                {"role": "system", "content": (
                    "你是 Amadeus 的语音交付计划修复器。正文与日语发声稿已经验收并冻结，"
                    "不得改写、概括、补充或重新回答用户。只输出一个 JSON 对象，且只能包含 speechUnits。"
                    f"speechUnits 必须有一到 {self.max_units()} 项，每项只含 zh、ja、boundaryAfter；"
                    "zh 依次拼接必须与 frozenReply 完全一致，ja 依次拼接必须与 frozenSpokenTextJa 完全一致。"
                    "每项必须是能够独立发声的完整语段，不得以逗号、冒号、连接词或从句悬空结束。"
                    "省略号只表示句中停顿，不能单独作为切分边界。"
                )},
                {"role": "user", "content": json.dumps({
                    "frozenReply": frozen_reply,
                    "frozenSpokenTextJa": frozen_spoken,
                    "frozenReplyFingerprint": reply_fingerprint,
                    "frozenSpokenTextJaFingerprint": spoken_fingerprint,
                    "responseShape": {
                        "speechUnits": [{
                            "zh": "冻结中文的连续片段",
                            "ja": "冻结日语的对应连续片段",
                            "boundaryAfter": "phrase_pause 或 turn_pause",
                        }],
                    },
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        self.operations.write_server_log(
            "speech_plan_repair_request",
            requestId=request_id,
            frozenReplyFingerprint=reply_fingerprint,
            frozenSpokenTextJaFingerprint=spoken_fingerprint,
            chineseCharacters=len(re.sub(r"\s+", "", frozen_reply)),
            japaneseCharacters=len(re.sub(r"\s+", "", frozen_spoken)),
            maxTokens=max_tokens,
        )
        self.finish_reason.set("")
        content = self.operations.send_upstream_chat_payload(
            settings,
            payload,
            timeout=45,
            request_id=request_id,
            purpose="speech_plan_repair",
        )
        finish_reason = self.finish_reason.get()
        parsed = parse_json_object(content)
        if not isinstance(parsed, dict) or set(parsed) != {"speechUnits"}:
            raise UpstreamProtocolError("speech plan repair returned fields outside speechUnits")
        units = self.operations.normalize_speech_plan_units(
            parsed.get("speechUnits"),
            frozen_reply=frozen_reply,
            frozen_spoken=frozen_spoken,
            require_spoken=require_spoken,
        )
        self.operations.write_server_log(
            "speech_plan_repair_response",
            requestId=request_id,
            accepted=bool(units),
            unitCount=len(units),
            finishReason=finish_reason,
            jsonComplete=isinstance(parsed, dict),
        )
        if not units:
            raise UpstreamProtocolError("speech plan repair did not losslessly cover frozen tracks")
        return units
