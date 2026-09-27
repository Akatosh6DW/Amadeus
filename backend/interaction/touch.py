"""触摸模型请求与原音交付服务。

原音只在既有目录和命中条件下使用；模型分支保留一次主生成与既有有界修复。
本服务不保存聊天或关系，调用方接受结果后再经统一交互应用提交状态。
"""
from __future__ import annotations
import base64
from dataclasses import dataclass
import json
import mimetypes
from pathlib import Path
import re
import time
from typing import Any, Callable
from agent.fingerprints import runtime_digest
from backend.chat.presentation import model_emotion_label
from backend.common.text import unique_strings
from backend.chat.parsing import parse_json_object
from backend.interaction.touch_catalog import (
    PARTNER_STAGES, TOUCH_AGENT_PERSONA, TOUCH_JSON_RULE, TOUCH_BEHAVIOR_RULES,
    TOUCH_ORIGINAL_CLIPS, TOUCH_ORIGINAL_CLIP_CHANCE,
)
from backend.interaction.touch_rules import (
    _touch_variant, apply_touch_expression_contract, chest_touch_contract_issues,
    touch_event_prompt, touch_reply_is_grounded, touch_reply_is_repetitive,
    touch_reply_misreads_text_as_touch, touch_reply_reverses_roles,
)

@dataclass(frozen=True)
class ModelOperations:
    """现有模型/配置与关系提示入口，延迟读取同一运行配置。"""
    load_settings: Callable[..., Any]
    read_memory: Callable[..., Any]
    relationship_context_prompt: Callable[..., Any]
    send_upstream_chat_payload: Callable[..., Any]
    normalize_model_reply: Callable[..., Any]


class TouchInteractionService:
    def __init__(
        self, model: ModelOperations, *, voice_root: Callable[[], Path],
        release_mode: Callable[[], bool], dialogue_style: Callable[[], str],
    ) -> None:
        self.model = model
        self.voice_root = voice_root
        self.release_mode = release_mode
        self.dialogue_style = dialogue_style

    def select_touch_original_clip(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Select an exact original line rarely, without replacing normal LLM turns."""

        if (
            str(event.get("boundaryLevel") or "normal") != "normal"
            or bool(event.get("aggregatedBurst"))
            or bool(event.get("interruptedPreviousTouch"))
        ):
            return None
        recent_ids = {
            str(item).strip()
            for item in event.get("recentOriginalClipIds", [])
            if str(item).strip()
        }
        candidates = [
            item for item in TOUCH_ORIGINAL_CLIPS
            if str(event.get("zone") or "") in item["zones"]
            and str(event.get("action") or "") in item["actions"]
            and item["id"] not in recent_ids
            and (self.voice_root() / item["audio"]).is_file()
        ]
        if not candidates:
            return None
        material = json.dumps({
            "zone": event.get("zone"),
            "action": event.get("action"),
            "zoneCount": event.get("zoneCount"),
            "sessionCount": event.get("sessionCount"),
            "variationKey": event.get("responseDirective", {}).get("variationKey"),
            "recentOriginalClipIds": sorted(recent_ids),
            "selectorVersion": 1,
        }, ensure_ascii=False, sort_keys=True)
        digest = runtime_digest(material)
        roll = int.from_bytes(digest[:2], "big") / 65535
        if roll >= TOUCH_ORIGINAL_CLIP_CHANCE:
            return None
        return dict(candidates[int.from_bytes(digest[2:4], "big") % len(candidates)])


    def touch_original_clip_response(
        self,
        clip: dict[str, Any],
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a text/audio-locked original performance response."""

        audio_path = (self.voice_root() / str(clip["audio"])).resolve()
        audio_bytes = audio_path.read_bytes()
        value = apply_touch_expression_contract(self.model.normalize_model_reply({
            **clip,
            "memoryUpdates": [],
            "source": "original_clip",
        }), event)
        # normalize_model_reply may clean punctuation for synthetic speech. Exact
        # clips must keep their authored transcript and visible translation locked.
        value["reply"] = str(clip["reply"])
        value["spokenTextJa"] = str(clip["spokenTextJa"])
        value["source"] = "original_clip"
        value["originalAudio"] = {
            "id": str(clip["id"]),
            "voiceId": audio_path.stem.split("_processed", 1)[0],
            "contentType": mimetypes.guess_type(audio_path.name)[0] or "audio/wav",
            "audioBase64": base64.b64encode(audio_bytes).decode("ascii"),
            "bytes": len(audio_bytes),
        }
        value["_touchMetrics"] = {
            "llmLatencyMs": 0,
            "promptChars": 0,
            "historyMessages": 0,
            "retrievedMemories": 0,
            "repetitionRepaired": False,
            "structuredOutputRepaired": False,
            "repairAttempted": False,
            "repairSucceeded": False,
            "fallbackUsed": False,
            "originalClipUsed": True,
            "originalClipId": str(clip["id"]),
            "responseAngle": str(event.get("responseDirective", {}).get("angle") or ""),
        }
        return value


    def mock_touch_reply(self, event: dict[str, Any]) -> dict[str, Any]:
        boundary_variants = {
            "ignore": [
                {"reply": "……够了。我现在不想理你。", "spokenTextJa": "……もういい。今は知らないから。", "emotion": "annoyed", "gesture": "tilt", "voiceStyle": "sharp", "voiceTone": "cold", "emotionDelta": 0.22},
                {"reply": "哼。等你学会适可而止再来。", "spokenTextJa": "ふん。加減を覚えてから来なさい。", "emotion": "annoyed", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "cold", "emotionDelta": 0.24},
            ],
            "interrupt": [
                {"reply": "等一下，慢一点。我又不会突然消失。", "spokenTextJa": "ちょっと、ゆっくりして。急に消えたりしないから。", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "teasing", "voiceTone": "flustered", "emotionDelta": 0.14},
                {"reply": "连续碰个不停，是在观察我的反应吗？", "spokenTextJa": "何度も続けて触って、私の反応を観察してるの？", "emotion": "annoyed", "gesture": "tilt", "voiceStyle": "teasing", "voiceTone": "dry", "emotionDelta": 0.12},
            ],
        }
        partner_chest_boundary_variants = {
            "ignore": [
                {"reply": "……变态。你先冷静下来，我现在真的不理你。", "spokenTextJa": "……変態。少し頭を冷やして。今は本当に知らないから。", "emotion": "shy", "gesture": "look_away", "voiceStyle": "soft", "voiceTone": "shy", "emotionDelta": 0.2, "voiceIntensity": "normal"},
                {"reply": "色狼……再这样突然碰，我就不看你了。", "spokenTextJa": "スケベ……また急に触ったら、もうあなたの顔を見ないから。", "emotion": "flustered", "gesture": "look_away", "voiceStyle": "soft", "voiceTone": "flustered", "emotionDelta": 0.22, "voiceIntensity": "normal"},
            ],
            "interrupt": [
                {"reply": "等、等一下，变态……就算是你也要先停手。", "spokenTextJa": "ちょ、ちょっと、変態……あなたでも、まず手を離して。", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "soft", "voiceTone": "flustered", "emotionDelta": 0.18, "voiceIntensity": "normal"},
                {"reply": "色狼，别连续碰那里……至少先看着我问清楚。", "spokenTextJa": "スケベ、そこを何度も触らないで……せめて先に私を見て聞いて。", "emotion": "shy", "gesture": "look_away", "voiceStyle": "soft", "voiceTone": "shy", "emotionDelta": 0.18, "voiceIntensity": "normal"},
            ],
        }
        guarded_chest_boundary_variants = {
            "ignore": [
                {"reply": "……够了，变态。我现在不想理你。", "spokenTextJa": "……もういい、この変態。今は知らないから。", "emotion": "annoyed", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "cold", "emotionDelta": 0.26, "voiceIntensity": "normal"},
                {"reply": "色狼。等你学会适可而止再来。", "spokenTextJa": "このスケベ。加減を覚えてから来なさい。", "emotion": "annoyed", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "cold", "emotionDelta": 0.26, "voiceIntensity": "normal"},
            ],
            "interrupt": [
                {"reply": "等、等一下，变态！别连续碰那里！", "spokenTextJa": "ちょ、ちょっと、この変態！ そこを何度も触らないで！", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "flustered", "emotionDelta": 0.24, "voiceIntensity": "strong"},
                {"reply": "色狼，你还碰个不停……快把手拿开！", "spokenTextJa": "このスケベ、まだ触るつもり……早く手を離して！", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "flustered", "emotionDelta": 0.25, "voiceIntensity": "strong"},
            ],
        }
        if event["zone"] == "chest" and event["boundaryLevel"] in boundary_variants:
            stage_variants = (
                partner_chest_boundary_variants
                if event.get("relationshipStage") in PARTNER_STAGES
                else guarded_chest_boundary_variants
            )
            value = _touch_variant(stage_variants[event["boundaryLevel"]], event)
        elif event["boundaryLevel"] in boundary_variants:
            value = _touch_variant(boundary_variants[event["boundaryLevel"]], event)
        else:
            variants = {
                "head": [
                    {"reply": "……摸头？先说好，我可没把自己当小孩子。", "spokenTextJa": "……頭を撫でるの？　先に言っておくけど、子供扱いはしないで。", "emotion": "amused", "gesture": "perk", "voiceStyle": "teasing", "voiceTone": "playful", "emotionDelta": 0.08},
                    {"reply": "手法还算温柔。这个评价可不代表我在鼓励你。", "spokenTextJa": "手つきはまあ優しいわね。褒めたからって、調子に乗らないで。", "emotion": "amused", "gesture": "soften", "voiceStyle": "soft", "voiceTone": "warm", "emotionDelta": 0.1},
                    {"reply": "……再一下也不是不行。别露出那种得意的表情。", "spokenTextJa": "……もう少しくらいならいいけど。そんな得意そうな顔しないで。", "emotion": "shy", "gesture": "soften", "voiceStyle": "soft", "voiceTone": "shy", "emotionDelta": 0.12},
                ],
                "face": [
                    {"reply": "脸别随便碰……靠得太近了。", "spokenTextJa": "顔は勝手に触らないで……近すぎるわ。", "emotion": "shy", "gesture": "lean", "voiceStyle": "soft", "voiceTone": "shy", "emotionDelta": 0.0, "emotionIntensity": 0.82, "blushIntensity": 0.26},
                    {"reply": "你是想确认我会不会脸红吗？这种实验没有意义。", "spokenTextJa": "私が赤くなるか確かめたいの？　そんな実験、意味ないから。", "emotion": "shy", "gesture": "tilt", "voiceStyle": "teasing", "voiceTone": "shy", "emotionDelta": 0.01, "emotionIntensity": 0.84, "blushIntensity": 0.3},
                    {"reply": "……知道了，别一直盯着我的脸看。", "spokenTextJa": "……分かったから、そんなに顔をじっと見ないで。", "emotion": "shy", "gesture": "lean", "voiceStyle": "soft", "voiceTone": "shy", "emotionDelta": 0.02, "emotionIntensity": 0.86, "blushIntensity": 0.34},
                ],
                "hand": [
                    {"reply": "想牵手就直说。……暂时不松开也可以。", "spokenTextJa": "手をつなぎたいなら、そう言いなさい。……しばらくこのままでもいいけど。", "emotion": "affectionate", "gesture": "soften", "voiceStyle": "soft", "voiceTone": "warm", "emotionDelta": 0.08},
                    {"reply": "手有点凉。……靠近一点会暖得快些。", "spokenTextJa": "少し手が冷たいわね。……もう少し近いほうが、早く温まるかも。", "emotion": "affectionate", "gesture": "lean", "voiceStyle": "soft", "voiceTone": "warm", "emotionDelta": 0.1},
                    {"reply": "握得这么自然，看来你已经很习惯了。", "spokenTextJa": "ずいぶん自然に握るのね。もう慣れたみたい。", "emotion": "amused", "gesture": "soften", "voiceStyle": "teasing", "voiceTone": "warm", "emotionDelta": 0.1},
                ],
                "chest": [
                    {"reply": "你、你往哪里碰啊，变态！", "spokenTextJa": "ど、どこを触ってるのよ、この変態！", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "flustered", "emotionDelta": 0.22},
                    {"reply": "等、等一下……变态，快把手拿开！", "spokenTextJa": "ちょ、ちょっと……変態、早く手を離して！", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "flustered", "emotionDelta": 0.22},
                    {"reply": "突、突然碰那里做什么……你这个变态！", "spokenTextJa": "い、いきなりそこを触って何するのよ……この変態！", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "flustered", "emotionDelta": 0.24},
                    {"reply": "实验可不是变态行为的借口，手拿开！", "spokenTextJa": "実験を変態行為の言い訳にしないで。手を離して！", "emotion": "annoyed", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "dry", "emotionDelta": 0.22},
                    {"reply": "又在打什么变态主意？先把手从那里拿开！", "spokenTextJa": "また何か変態なことを考えてるの？ まずそこから手を離して！", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "flustered", "emotionDelta": 0.23},
                    {"reply": "这种变态要求，答案当然是不行。立刻放手！", "spokenTextJa": "そんな変態な要求、答えは当然だめ。今すぐ離して！", "emotion": "annoyed", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "cold", "emotionDelta": 0.24},
                    {"reply": "疯狂科学家的设定里还包括性骚扰吗？变态，快住手！", "spokenTextJa": "マッドサイエンティストの設定にはセクハラまで入ってるの？ この変態、やめなさい！", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "flustered", "emotionDelta": 0.24},
                    {"reply": "差点忘了你本来就是个变态……快把手拿开！", "spokenTextJa": "あなたが変態だってこと、忘れかけてた……早く手を離して！", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "sharp", "voiceTone": "flustered", "emotionDelta": 0.24},
                ],
                "mouth": [
                    {"reply": "还不行。别擅自靠这么近。", "spokenTextJa": "まだだめ。勝手にそんなに近づかないで。", "emotion": "shy", "gesture": "recoil", "voiceStyle": "teasing", "voiceTone": "shy", "emotionDelta": 0.12},
                    {"reply": "等、等一下，这种事至少先问我。", "spokenTextJa": "ちょ、ちょっと待って。そういうことは、せめて先に聞いて。", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "soft", "voiceTone": "flustered", "emotionDelta": 0.14},
                ],
            }
            if event["action"] == "kiss":
                variants["mouth"] = [
                    {"reply": "……这次不躲。只准一下。", "spokenTextJa": "……今回は逃げない。一回だけだから。", "emotion": "affectionate", "gesture": "lean", "voiceStyle": "soft", "voiceTone": "warm", "emotionDelta": 0.12},
                    {"reply": "先说好，是我允许的。……靠近一点。", "spokenTextJa": "先に言っておくけど、私が許したのよ。……もう少し近くに。", "emotion": "flustered", "gesture": "lean", "voiceStyle": "soft", "voiceTone": "warm", "emotionDelta": 0.14},
                ]
            if event["zone"] == "chest" and event.get("relationshipStage") in PARTNER_STAGES:
                variants["chest"] = [
                    {"reply": "……变态。就算是你，突然碰那里也太犯规了。", "spokenTextJa": "……変態。あなたでも、急にそこを触るのは反則よ。", "emotion": "flustered", "gesture": "recoil", "voiceStyle": "soft", "voiceTone": "flustered", "emotionDelta": 0.18},
                    {"reply": "色狼……至少先看着我问清楚，别突然碰。", "spokenTextJa": "スケベ……せめて私を見て聞いて。急に触らないで。", "emotion": "shy", "gesture": "look_away", "voiceStyle": "soft", "voiceTone": "shy", "emotionDelta": 0.16},
                    {"reply": "你这个变态科学家……别只顾着看我的反应，先放手。", "spokenTextJa": "この変態科学者……私の反応ばかり見てないで、まず手を離して。", "emotion": "flustered", "gesture": "look_away", "voiceStyle": "teasing", "voiceTone": "flustered", "emotionDelta": 0.18},
                    {"reply": "实验也不能拿来给变态行为找借口。……快停下。", "spokenTextJa": "実験だって変態行為の言い訳にはならないわ。……早くやめて。", "emotion": "shy", "gesture": "tilt", "voiceStyle": "soft", "voiceTone": "shy", "emotionDelta": 0.16},
                ]
            value = _touch_variant(variants[event["zone"]], event)
            value["voiceIntensity"] = "normal" if event["zone"] == "chest" else "subtle"
        return apply_touch_expression_contract(
            self.model.normalize_model_reply({**value, "memoryUpdates": [], "source": "local"}),
            event,
        )


    def call_touch_upstream(self, body: dict[str, Any], event: dict[str, Any], relationship_state: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        settings = self.model.load_settings()
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        supplied_memories = body.get("memories") if isinstance(body.get("memories"), list) else []
        # Touch is fully grounded by the current physical event and relationship
        # state. Pulling an unrelated long-term memory here made the reaction feel
        # scripted, so the local NeedGate intentionally resolves to no recall.
        memories: list[str] = []
        recent_messages = [
            {"role": "assistant" if item.get("role") == "assistant" else "user", "content": str(item.get("content") or "")}
            for item in messages[-4:] if isinstance(item, dict) and item.get("content")
        ]
        recent_assistant_replies = [
            item["content"] for item in recent_messages if item["role"] == "assistant"
        ]
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.82,
            "max_tokens": 320,
            "messages": [
                {"role": "system", "content": TOUCH_AGENT_PERSONA},
                {"role": "system", "content": self.dialogue_style()},
                {"role": "system", "content": TOUCH_JSON_RULE},
                {"role": "system", "content": TOUCH_BEHAVIOR_RULES},
                {"role": "system", "content": self.model.relationship_context_prompt(relationship_state)},
                {"role": "system", "content": "从完整长期记忆库检索出的相关项：\n" + "\n".join(f"- {item}" for item in memories[:4])},
                *recent_messages,
                {"role": "system", "content": touch_event_prompt(event)},
                {
                    "role": "user",
                    "content": (
                        f"当前唯一的新事件是用户通过界面进行了“{event['actionLabel']}”，没有新的口头发言。"
                        + (
                            "界面已等这段连续动作停止并统一上报；只生成一次综合反应。"
                            if event.get("aggregatedBurst") else ""
                        )
                        +
                        "只回应这个肢体事件；旧对话只能帮助判断语气，不能当作刚说的话重答。"
                    ),
                },
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        content = self.model.send_upstream_chat_payload(settings, payload, timeout=18, purpose="touch_primary")
        result = self.model.normalize_model_reply(content, settings=settings)
        comparison_replies = unique_strings([*event["recentTouchReplies"], *recent_assistant_replies])
        repetition_repaired = touch_reply_is_repetitive(result.get("reply", ""), comparison_replies)
        text_event_misread = touch_reply_misreads_text_as_touch(result.get("reply", ""))
        roles_reversed = touch_reply_reverses_roles(
            result.get("reply", ""),
            result.get("spokenTextJa", ""),
        )
        event_ungrounded = not touch_reply_is_grounded(result.get("reply", ""), event)
        structured_output = parse_json_object(content)
        structured_output_missing = not (
            isinstance(structured_output, dict)
            and model_emotion_label(structured_output)
            and str(structured_output.get("reply") or "").strip()
            and str(structured_output.get("spokenTextJa") or structured_output.get("spoken_text_ja") or "").strip()
        )
        chest_contract_issues = chest_touch_contract_issues(result, event)
        repair_reasons: list[str] = []
        if repetition_repaired:
            repair_reasons.append("与最近触摸回复过于相似")
        if text_event_misread:
            repair_reasons.append("把旧对话误读成当前发言")
        if roles_reversed:
            repair_reasons.append("把触摸的发起者和被触摸者写反")
        if event_ungrounded:
            repair_reasons.append("没有明确回应当前触摸部位")
        if structured_output_missing:
            repair_reasons.append("缺少完整 JSON、情绪标签或日语发声稿")
        repair_reasons.extend(chest_contract_issues)
        repair_attempted = bool(repair_reasons)
        repair_succeeded = False
        fallback_used = False
        soft_grounding_accepted = False
        final_fallback_reasons: list[str] = []
        if repair_attempted:
            repair_payload = dict(payload)
            repair_payload["temperature"] = 0.88
            repair_payload["messages"] = [
                *payload["messages"],
                {
                    "role": "system",
                    "content": (
                        f"上一候选无效：{'；'.join(repair_reasons)}。"
                        f"重新生成，只回应当前的“{event['actionLabel']}”肢体事件，并严格采用 "
                        f"responseDirective.angle={event['responseDirective']['angle']} 的表达角度。"
                        "必须改变交流目的和句子结构，不能只替换同义词。"
                        "用户始终是触摸发起者，Amadeus 始终是被触摸者；不得把动作主体反写。"
                        + (
                            "本轮中文必须自然说出一次“变态”或“色狼”，日语必须对应说出“変態”或“スケベ”；"
                            "这不是要求复读固定台词，而是要求保留红莉栖式越界斥责并改变其余表达。"
                            if event["zone"] == "chest" else ""
                        )
                    ),
                },
                {"role": "user", "content": "重新输出一个不同且符合触摸事件的 JSON 反应。"},
            ]
            repaired_content = self.model.send_upstream_chat_payload(settings, repair_payload, timeout=18, purpose="touch_repair")
            repaired_result = self.model.normalize_model_reply(repaired_content, settings=settings)
            repaired_structured = parse_json_object(repaired_content)
            final_fallback_reasons = []
            if not isinstance(repaired_structured, dict):
                final_fallback_reasons.append("repair_not_json")
            if not model_emotion_label(repaired_structured or {}):
                final_fallback_reasons.append("repair_missing_emotion")
            if not str((repaired_structured or {}).get("reply") or "").strip():
                final_fallback_reasons.append("repair_missing_reply")
            if not str((repaired_structured or {}).get("spokenTextJa") or (repaired_structured or {}).get("spoken_text_ja") or "").strip():
                final_fallback_reasons.append("repair_missing_spoken_ja")
            if touch_reply_is_repetitive(repaired_result.get("reply", ""), comparison_replies):
                final_fallback_reasons.append("repair_still_repetitive")
            if touch_reply_misreads_text_as_touch(repaired_result.get("reply", "")):
                final_fallback_reasons.append("repair_misread_old_dialogue")
            if touch_reply_reverses_roles(
                repaired_result.get("reply", ""),
                repaired_result.get("spokenTextJa", ""),
            ):
                final_fallback_reasons.append("repair_reversed_roles")
            repaired_chest_issues = chest_touch_contract_issues(repaired_result, event)
            if repaired_chest_issues:
                final_fallback_reasons.append("repair_chest_contract")
            repaired_ungrounded = not touch_reply_is_grounded(repaired_result.get("reply", ""), event)
            if repaired_ungrounded:
                # The zone-keyword matcher is deliberately a soft signal for
                # ordinary head/face/hand interaction.  After a fully structured
                # second model response has passed role, repetition and old-turn
                # checks, replacing it with a canned line merely because it used
                # an implicit physical reaction recreates the repetitive behavior.
                if str(event.get("zone") or "") in {"head", "face", "hand"} and not final_fallback_reasons:
                    soft_grounding_accepted = True
                else:
                    final_fallback_reasons.append("repair_not_grounded")
            repaired_invalid = bool(final_fallback_reasons)
            if not repaired_invalid:
                content = repaired_content
                result = repaired_result
                repair_succeeded = True
        if repair_attempted:
            if not repair_succeeded:
                result = self.mock_touch_reply(event)
                fallback_used = True
        result["_touchMetrics"] = {
            "llmLatencyMs": round((time.perf_counter() - started) * 1000),
            "promptChars": sum(len(str(item.get("content") or "")) for item in payload["messages"]),
            "historyMessages": min(4, len(messages)),
            "retrievedMemories": min(4, len(memories)),
            "memoryStoreSize": len(self.model.read_memory(settings)),
            "ignoredClientMemoryCount": len([
                item for item in supplied_memories if str(item).strip()
            ]),
            "repetitionRepaired": repetition_repaired,
            "textEventMisreadRepaired": text_event_misread,
            "touchRolesReversedRepaired": roles_reversed,
            "eventUngroundedRepaired": event_ungrounded,
            "chestContractIssues": chest_contract_issues,
            "chestContractRepaired": bool(chest_contract_issues and repair_succeeded),
            "structuredOutputRepaired": structured_output_missing,
            "repairReasons": list(repair_reasons),
            "finalFallbackReasons": list(final_fallback_reasons),
            "softGroundingAccepted": soft_grounding_accepted,
            "repairAttempted": repair_attempted,
            "repairSucceeded": repair_succeeded,
            "fallbackUsed": fallback_used,
            "originalClipUsed": False,
            "originalClipId": "",
            "responseAngle": str(event.get("responseDirective", {}).get("angle") or ""),
        }
        if body.get("developerDebug") and not self.release_mode():
            result["_debugRawModelOutput"] = content
            result["_debugUpstreamPayload"] = payload
        if event["boundaryLevel"] == "interrupt":
            result["emotionDelta"] = max(0.08, min(0.2, float(result.get("emotionDelta") or 0)))
            if result["voiceIntensity"] == "strong":
                result["voiceIntensity"] = "normal"
        elif event["boundaryLevel"] == "ignore":
            result["emotionDelta"] = max(0.14, min(0.28, float(result.get("emotionDelta") or 0)))
            result["voiceIntensity"] = "normal"
        if event["action"] == "kiss_attempt_blocked":
            if result["emotion"] in {"affectionate", "startled", "excited"}:
                result["emotion"] = "shy"
            result["gesture"] = "recoil"
            result["emotionDelta"] = min(0.16, max(0.06, float(result.get("emotionDelta") or 0)))
        if event["boundaryLevel"] == "normal" and event["zone"] != "chest":
            if result["emotion"] == "startled":
                result["emotion"] = "shy" if event["zone"] in {"face", "mouth", "hand"} else "amused"
            result["emotionDelta"] = max(-0.08, min(0.16, float(result.get("emotionDelta") or 0)))
            if result["voiceIntensity"] == "strong":
                result["voiceIntensity"] = "normal"
        result = apply_touch_expression_contract(result, event)
        result["spokenTextJa"] = re.sub(r"ダメだ([。！!]|$)", r"だめ\1", result["spokenTextJa"])
        result["memoryUpdates"] = []
        result["source"] = "api"
        return result
