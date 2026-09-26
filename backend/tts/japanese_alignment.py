"""中文台词保持冻结，只补齐与之对应的日语发声稿。

本地检查、模型对齐和人称实现沿用原调用顺序。远端修复失败时明确标记语音不可用，
不能用泛化句子替代原台词；配置和原有模型入口通过具体端口提供。
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

from copy import deepcopy
from agent.discourse_reference import DiscourseLedger, render_discourse_context, resolve_discourse_references
from agent.participant_perspective import PARTICIPANT_PERSPECTIVE_RULE
from agent.participant_realization import PARTICIPANT_REALIZATION_RULE, realize_participant_speech, strip_participant_realization_metadata
from backend.chat.presentation import speech_language_profile, sanitize_roleplay_text
from backend.chat.reply_fields import SPOKEN_JA_NO_EMOJI_RULE
from backend.tts.japanese_rules import spoken_japanese_style_needs_repair, spoken_japanese_content_needs_repair, soften_spoken_japanese_style

SPOKEN_JA_ALIGNMENT_RULE = (
    PARTICIPANT_PERSPECTIVE_RULE
    + "\n" + SPOKEN_JA_NO_EMOJI_RULE
    + "\n你负责把 AMADEUS 的中文显示回复改写成自然日语发声稿。"
    "只输出 JSON：{\"spokenTextJa\":\"...\",\"speechSegmentCount\":1}。"
    "要求：1. spokenTextJa 必须完整覆盖 replyZh 的语义，包括吐槽、反问、转折和情绪；"
    "2. 不要只翻译其中的日文假名测试片段；"
    "3. 日语要像日常说出口的话，不要中文词序的硬翻；"
    "4. 可以保留 あいうえお 等用户要求测试的假名序列，但必须同时翻译周围中文含义；"
    "5. 严格使用输入 pronunciationGlossary 中给出的固定读法；其余专有名词优先使用作品、人物或产品的"
    "官方日语名称与读法，不得把中文译名逐字音读，也不要自行杜撰读法。AMADEUS 一律写成‘アマデウス’；"
    "6. 只写真正说出口的日语台词，不要加入括号动作、表情、心理、语气或舞台说明，也不要用引号包住整段台词；"
    "7. 使用聪明克制的年轻女性口语，自称‘私’、称对方‘あなた’，避免君/お前、だぜ、だぞ、かよ和裸命令形；"
    "8. 如果 replyZh 中确实有自然笑声，spokenTextJa 要保留简短笑声；不要把笑声改成括号动作。"
    "9. 文件名、命令、路径、版本号、英文缩写、哈希和测试计数要改写成自然可说出口的日语含义；"
    "不要原样复制 run_xxx.ps1、全大写字母串或“425 passed”之类会造成日英反复切换的技术文本。"
    "能用‘起動スクリプト、開発ツール、四百二十五件合格’等语义概括时优先概括，不要逐字符念代码；"
    "确实需要保留产品名或作品名时，使用日语中自然通行的片假名读法，例如把英文单词作为一个词来读，"
    "不要把五个以上字母的单词机械拆成逐字母读法。最终 spokenTextJa 不得残留拉丁字母、竖线或代码分隔符。"
    "10. speechSegmentCount 是兼容字段，固定填 1。翻译器没有语义分段权，不能根据标点、长度或省略号"
    "新建可见边界；可见分段只能来自上游显式 speechUnits 合同。"
    "11. 若输入含 referenceBindings，中文与日语必须保持同一参与者绑定：participant:user 是用户，"
    "participant:assistant 是 AMADEUS；引号中的第一人称服从 quoted_speaker，OCR 作者 unknown 时不得猜测。"
    "不得在翻译时交换‘我/你’或把第三方改成任一对话参与者。"
    "若提供 participantInterpretation，它是当前轮（含纠错后）的模型理解，帮助沿用正文中的人物关系，"
    "不是额外事实证据。以完整中文正文为表达内容，保持其人物、动作方向和引用层级，不恢复上一版错误稿的关系。"
    "12. 不要解释，不要输出中文。"
)


@dataclass(frozen=True)
class AlignmentOperations:
    configured: Callable[..., Any]
    persona_repair_prefix: Callable[..., Any]
    send_upstream_chat_payload: Callable[..., Any]
    valid_speech_segment_count: Callable[..., Any]
    normalize_spoken_japanese_terms: Callable[..., Any]
    spoken_ja_pronunciation_glossary: Callable[..., Any]
    spoken_japanese_pronunciation_needs_repair: Callable[..., Any]

class JapaneseAlignment:
    def __init__(self, operations: AlignmentOperations) -> None:
        self.operations = operations

    def should_align_spoken_text_ja(self, settings: dict[str, Any], result: dict[str, Any]) -> bool:
        if speech_language_profile(settings)["speechLanguage"] != "ja-JP":
            return False
        if not settings.get("alignSpokenJa", True):
            return False
        if not self.operations.configured(settings):
            return False
        if str(settings.get("ttsProvider") or "").strip().lower() != "gpt-sovits":
            return False
        reply = re.sub(r"\[S\d+\]", "", str(result.get("reply") or ""), flags=re.I).strip()
        spoken = str(result.get("spokenTextJa") or result.get("spoken_text_ja") or "").strip()
        if not reply:
            return False
        # Plain-text recovery has no trustworthy spokenTextJa field. Keep its
        # provenance explicit so even a later compatibility transform cannot make
        # a generic sentence masquerade as a semantically aligned voice line.
        if result.get("spokenTextJaMissingFromModel"):
            return True
        if not spoken:
            return True
        if (
            spoken_japanese_style_needs_repair(spoken)
            or spoken_japanese_content_needs_repair(spoken)
            or self.operations.spoken_japanese_pronunciation_needs_repair(reply, spoken)
        ):
            return True
        reply_units = re.findall(r"[\u3400-\u9fffA-Za-z0-9]", reply)
        spoken_units = re.findall(r"[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9]", spoken)
        # Chinese and Japanese are both compact scripts. A Japanese line shorter
        # than roughly half of the Chinese semantic units is almost always a
        # partial translation, not merely a natural paraphrase.
        if len(spoken_units) < max(7, int(math.ceil(len(reply_units) * 0.55))):
            return True
        if not re.search(r"[\u3040-\u30ff]", spoken):
            return True
        if re.search(r"[\u4e00-\u9fff]{8,}", spoken) and not re.search(r"[ぁ-んァ-ンー]{4,}", spoken):
            return True
        return False


    def align_spoken_text_ja(
        self,
        settings: dict[str, Any],
        result: dict[str, Any],
        *,
        allow_remote_repair: bool = True,
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> dict[str, Any]:
        cleaned_spoken, initial_stage_directions = sanitize_roleplay_text(
            self.operations.normalize_spoken_japanese_terms(result.get("spokenTextJa") or result.get("spoken_text_ja"))
        )
        result["spokenTextJa"] = cleaned_spoken
        if initial_stage_directions:
            result["stageDirectionsRemoved"] = True
        if not self.should_align_spoken_text_ja(settings, result):
            return result
        reply = re.sub(r"\[S\d+\]", "", str(result.get("reply") or ""), flags=re.I).strip()
        previous = str(result.get("spokenTextJa") or "").strip()
        understood_goal = str((result.get("_cognitiveDigest") or {}).get("understoodUserGoal") or "")
        reference_frames = (result.get("_cognitiveDigest") or {}).get("referenceFrames")
        if not allow_remote_repair:
            generated: str | dict[str, Any] = ""
        elif (
            not isinstance(result.get("_referenceLedger"), Mapping)
            and not understood_goal
            and not reference_frames
            and (persona_snapshot is None or not persona_snapshot.uses_hybrid_provider_context)
        ):
            # Preserve the historical helper call shape for independent entrypoints
            # plus off/shadow test or integration overrides.
            generated = self.generate_spoken_text_ja(settings, reply, previous)
        else:
            generated = self.generate_spoken_text_ja(
                settings,
                reply,
                previous,
                persona_snapshot=persona_snapshot,
                reference_ledger=(
                    result.get("_referenceLedger")
                    if isinstance(result.get("_referenceLedger"), Mapping) else None
                ),
                **({"understood_goal": understood_goal} if understood_goal else {}),
                **({"reference_frames": reference_frames} if reference_frames else {}),
            )
        if isinstance(generated, dict):
            fixed = str(generated.get("spokenTextJa") or "")
        else:
            # Keep compatibility with tests and integrations that override the
            # historical string-returning helper.
            fixed = str(generated or "")
        fixed, repaired_stage_directions = sanitize_roleplay_text(self.operations.normalize_spoken_japanese_terms(fixed))
        if not fixed and previous and not result.get("spokenTextJaMissingFromModel"):
            fixed = previous
        fixed = soften_spoken_japanese_style(fixed)
        if spoken_japanese_content_needs_repair(fixed) or spoken_japanese_style_needs_repair(fixed):
            # A generic local sentence is never a translation of the accepted
            # Chinese reply, regardless of whether the bad track was missing or
            # merely unusable.  Fail the speech track explicitly.
            result["spokenTextJaOriginal"] = previous
            result["spokenTextJa"] = ""
            result["spokenTextJaRepaired"] = False
            result["spokenTextJaRepairMode"] = "remote_failed_no_semantic_fallback"
            result["speechUnavailableReason"] = "spoken_alignment_failed"
            result["ttsUnavailableReason"] = "spoken_alignment_failed"
            return result
        if repaired_stage_directions:
            result["stageDirectionsRemoved"] = True
        if not fixed:
            return result
        if fixed != previous:
            result["spokenTextJaOriginal"] = previous
            result["spokenTextJa"] = fixed
            result["spokenTextJaRepaired"] = True
            result.setdefault("spokenTextJaRepairMode", "remote" if allow_remote_repair else "local_fast_fallback")
            result["spokenTextJaMissingFromModel"] = False
        else:
            result["spokenTextJaRepaired"] = False
        return result


    def generate_spoken_text_ja(
        self,
        settings: dict[str, Any],
        reply: str,
        previous: str = "",
        *,
        include_delivery_plan: bool = False,
        persona_snapshot: PersonaContextSnapshot | None = None,
        reference_ledger: Mapping[str, Any] | None = None,
        understood_goal: str = "",
        reference_frames: list[dict[str, Any]] | None = None,
    ) -> str | dict[str, Any]:
        reference_bindings = ""
        if isinstance(reference_ledger, Mapping) and not (understood_goal or reference_frames):
            ledger = DiscourseLedger.from_dict(reference_ledger)
            reference_bindings = render_discourse_context(
                ledger,
                resolve_discourse_references(
                    ledger,
                    message_id=ledger.current_message_id,
                ),
                max_chars=2600,
            )
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.25,
            "messages": [
                *self.operations.persona_repair_prefix(persona_snapshot, legacy_system_prompt=False),
                {"role": "system", "content": SPOKEN_JA_ALIGNMENT_RULE},
                {"role": "system", "content": PARTICIPANT_REALIZATION_RULE + "\n本次只补写spokenTextJa，不输出speechUnits；spokenTextJa内沿用上述人物标识，仍输出原定JSON。"},
                {"role": "user", "content": json.dumps({
                    "replyZh": reply,
                    "previousSpokenTextJa": previous,
                    "pronunciationGlossary": self.operations.spoken_ja_pronunciation_glossary(reply, previous),
                    "referenceBindings": reference_bindings,
                    **({"participantInterpretation": {
                        "source": "current_turn_model_interpretation",
                        "understoodGoal": understood_goal,
                        "referenceFrames": deepcopy(reference_frames or []),
                    }} if understood_goal or reference_frames else {}),
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        try:
            content = self.operations.send_upstream_chat_payload(settings, payload, timeout=90, purpose="spoken_ja_alignment")
            parsed = realize_participant_speech(strip_participant_realization_metadata(parse_json_object(content) or {}))
            spoken = str(parsed.get("spokenTextJa") or "").strip()
            if include_delivery_plan:
                return {
                    "spokenTextJa": spoken,
                    "speechSegmentCount": self.operations.valid_speech_segment_count(
                        (parsed or {}).get("speechSegmentCount")
                    ),
                }
            return spoken
        except Exception:
            return {"spokenTextJa": "", "speechSegmentCount": None} if include_delivery_plan else ""
