"""情绪分类与冻结回复的表现元数据恢复。

只修复情绪/动作等表现字段，中文和日语轨道必须逐字保持；没有本地词表猜测兜底。
模型参数、原有一次恢复调用和失败类型保持不变。
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
from backend.chat.presentation import ALLOWED_EMOTIONS, model_emotion_label
from backend.common.values import clamp_float
from backend.relationship.rules import relationship_context_prompt

EMOTION_SELECTION_RULE = (
    "情绪选择规则：emotion 需要根据本轮用户内容自动判断，不需要用户手动切换。"
    "可用 emotion：neutral, thinking, focused, skeptical, awkward, annoyed, furious, teasing, amused, laughing, shy, flustered, affectionate, concerned, protective, excited, startled, proud, melancholic。"
    "没有显著情绪、只是平稳承接或恢复平静时使用 neutral。强度不要习惯性堆在中间："
    "轻微且克制的表现优先 0.20-0.55，清晰可见的情绪优先 0.90-1.25；"
    "0.56-0.89 只用于确实混合、犹豫或正在过渡的状态。"
    "严谨推理用 focused；怀疑前提或证据不足用 skeptical；一般科学思考用 thinking。"
    "轻微打趣用 teasing；说错话、接不上话、场面冷住或不知道该怎么接但并非害羞时用 awkward；"
    "被亲近的人用昵称调侃、被看穿嘴硬或用防御性否认掩饰动摇时可用 shy/flustered/teasing。"
    "私下兴趣、网络活动、匿名论坛习惯、玩梗痕迹或不愿公开的身份关联被发现时，主要驱动力是窘迫而不是受惊："
    "轻微被点出用 shy，突然被说中、慌忙否认或急着遮掩用 flustered；不能仅因信息出乎意料就标成 startled。"
    "昵称抗议若确实带一点不爽，也允许使用低强度 annoyed（emotionIntensity 0.35-0.70），配合 shy_annoyed 语气、tilt、"
    "少量 tension，并可按语境增加脸红或回避视线，表达“轻微羞恼而非真实冲突”。不能因为句子里出现称呼、"
    "自嘲或暧昧词就使用更高强度的生气。正经讨论、分析、求助和信息密度较高的话题中，外号只是呼语；"
    "正文没有冲突时不得因此选择 annoyed。只有 Amadeus 自己确实受到挑衅、边界侵犯，或实际回复表达了持续不满时，"
    "才使用 emotionIntensity 高于 0.70 的 annoyed；startled 只用于主要反应确实是外界突发消息、画面、声响、威胁"
    "或本能受惊，而不是害羞、被看穿、秘密暴露或亲密冲击；确有受惊或本能后退时可配 recoil。"
    "必须理解整句话的语义、否定关系、语气和上下文，再判断角色在实际回复中表现出的主要情绪。"
    "称呼、单个词或固定短语都不能脱离整句语义直接决定 emotion；表白、玩笑、安慰、拒绝等整体意图优先。"
    "用户拿自己开玩笑不等于攻击 Amadeus；实际回复若明确表示没有生气，且没有设限、训斥、敌意或真实冲突，"
    "不得标为 annoyed/furious，应标注其真正显露的害羞、嘴硬、调侃或平静。"
    "真正开怀大笑、明确被逗得笑出声或用户明确要求大笑时才用 laughing/laugh；普通开心和克制微笑仍用 amused，不能滥用大笑。"
    "annoyed 从低强度轻微恼意到普通生气连续变化：0.35-0.70 可表示羞恼但不是真冲突，超过 0.70 才表示明确生气；"
    "furious 是气到不想理人、把头侧开但仍冷冷瞟向对方的极端状态，三种强度不能混用。"
    "furious 可以来自普通情绪冲突，不等于人格侮辱计数或永久锁定。真诚道歉后应从 furious 降回 annoyed。"
    "害羞、着急、生气等任何情绪在极少数峰值都可 deliveryMode=outburst，并把 emotionIntensity 提高到 1.26-2.5；"
    "达到 2.5 只用于接近失控的原作式大喊，普通对话不得使用。"
    "轻度害羞用 shy；强烈慌乱、表白或亲密冲击用 flustered；别扭但温柔的亲近用 affectionate。"
    "情绪标签和发声标签必须一致：emotion=shy 时 voiceTone 使用 shy，轻微羞恼应改用"
    "低强度 annoyed 配 shy_annoyed，而不是保留 dry/cold；emotion=flustered 时 voiceTone 必须是 flustered。"
    "若上一轮刚发生说漏嘴、秘密暴露或被当场看穿，本轮仍在急忙防御、否认或遮掩，"
    "这是同一次急性窘迫的延续，不能仅因已经过了一条消息就突然降为平静的 shy。"
    "用户突然表白、说‘我爱你’或表达亲密心意时，主要反应是 flustered、shy 或 affectionate，"
    "即使回复文字里说‘突然’，也不得因此使用 startled；亲密冲击不是外界突发惊吓。"
    "用户低落、焦虑、害怕或求安慰时使用 concerned/soften/soft；需要守护或强烈提醒时用 protective。"
    "重大进展、启动或突破时使用 proud 或 excited；成功后克制满意用 proud，强烈兴奋用 excited/perk/energetic。"
    "谈到记忆、失去、世界线遗憾等安静伤感内容时可用 melancholic。"
    "可用 gesture：idle, nod, tilt, look_away, glitch, lean, soften, laugh, recoil, explain, perk。"
    "tilt 只表示把头向肩侧歪；look_away 表示把脸转向左右或移开视线；lean 只表示身体靠近或前倾。"
    "根据回复中自然出现的视觉意图选择，不能用 lean 代替侧头，也不要无缘无故连续触发动作。"
    "没有更具体动作时，teasing/skeptical 较适合 tilt，proud 较适合 look_away；"
    "shy/flustered 可用 look_away 表示回避视线，annoyed 可按语气使用 look_away 或 tilt，amused 只偶尔使用。"
    "模型明确需要 explain/recoil/soften/laugh/perk 等动作时，以明确动作优先。"
)


@dataclass(frozen=True)
class EmotionOperations:
    send_upstream_chat_payload: Callable[..., Any]
    normalize_model_reply: Callable[..., Any]
    write_server_log: Callable[..., Any]

class EmotionRepair:
    def __init__(self, operations: EmotionOperations) -> None:
        self.operations = operations

    def classify_reply_emotion_with_model(
        self,
        settings: dict[str, Any],
        user_message: str,
        assistant_reply: str,
        recent_messages: list[dict[str, Any]],
        relationship_state: dict[str, Any],
        *,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Ask the configured model for a semantic emotion label when the main JSON omitted it."""

        classifier_rule = (
            "你是 Amadeus 的情绪标注器，不生成或改写台词。根据完整语义判断角色在给定实际回复中"
            "主要表现出的情绪，而不是只看用户预期她应该有什么情绪。必须理解否定、反讽、含蓄表达、"
            "混合情绪、称呼在整句中的作用、最近对话和关系阶段。任何单个词都不能直接决定标签。"
            "先判断 actualAssistantReply 是否真的在生气：熟人间的调侃、自我调侃、害羞后的防御性否认、"
            "反射性纠正昵称都可以包含轻微羞恼，不代表毫无不满，但也不自动构成真实生气。"
            "必须继续判断情绪的来源：私下兴趣、网络活动、匿名论坛习惯、玩梗痕迹或不愿公开的身份关联被发现，"
            "属于 embarrassment/exposure；轻微时用 shy，突然被说中并慌忙否认或遮掩时用 flustered。"
            "startled 只用于外界突发消息、画面、声响、威胁或本能受惊，不得用来代替被看穿后的害羞慌乱。"
            "若回复没有设限、训斥、敌意或真实冲突，genuineAnger 必须为 false，furious 禁止使用。"
            "此时可以用 teasing 或 shy/flustered；若确实存在轻微羞恼，也可用 emotion=annoyed，但必须同时满足"
            "interactionFrame=shy_annoyance、emotionIntensity 不高于 0.70，并通过 dry 语气、tilt/look_away、"
            "适量 blushIntensity 和 tension 表现。超过这个范围的 annoyed 代表真实生气，genuineAnger 必须为 true。"
            "只输出 JSON。emotion 必须从以下值选择："
            + ", ".join(sorted(ALLOWED_EMOTIONS))
            + "。同时给出与该情绪一致的 gesture、emotionIntensity、blushIntensity、eyeContact、"
              "gazeX、gazeY、tension、voiceStyle、voiceTone、voiceIntensity；voiceStyle 和 voiceTone"
              "不得沿用与主情绪冲突的平静标签；并输出 genuineAnger 布尔值、"
              "interactionFrame（如 playful_banter、embarrassed_deflection、private_interest_exposure、"
               "ordinary_disagreement、boundary_conflict）、arousalSource（从 embarrassment_exposure、"
              "external_surprise、anger_conflict、affection、cognitive、other 中选择）、acuteEmbarrassment 布尔值"
              "以及 nonAngerEmotion。acuteEmbarrassment 只在最近上下文中的说漏嘴、秘密暴露或被看穿仍直接支配"
              "当前回复，角色仍在急忙防御、否认、遮掩或明显慌乱时为 true；不能因为跨过一条消息就自动降级。"
              "若 genuineAnger=false，nonAngerEmotion 必须填写"
              "实际主要非愤怒情绪；若 genuineAnger=true，可与 emotion 相同。"
        )
        context_messages = [
            {
                "role": "assistant" if item.get("role") == "assistant" else "user",
                "content": str(item.get("content") or "")[:1200],
            }
            for item in recent_messages[-6:]
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        ]
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.1,
            "max_tokens": 240,
            "messages": [
                {"role": "system", "content": EMOTION_SELECTION_RULE},
                {"role": "system", "content": classifier_rule},
                {"role": "system", "content": relationship_context_prompt(relationship_state)},
                {"role": "user", "content": json.dumps({
                    "recentConversation": context_messages,
                    "currentUserMessage": str(user_message or ""),
                    "actualAssistantReply": str(assistant_reply or ""),
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        content = self.operations.send_upstream_chat_payload(
            settings,
            payload,
            timeout=35,
            request_id=request_id,
            purpose="emotion_label_recovery",
        )
        emotion = model_emotion_label(content)
        if not emotion:
            raise UpstreamProtocolError("emotion classifier returned no valid label")
        parsed = parse_json_object(content)
        annotation = dict(parsed) if isinstance(parsed, dict) else {}
        genuine_anger = annotation.get("genuineAnger")
        non_anger_emotion = model_emotion_label(annotation.get("nonAngerEmotion"))
        interaction_frame = str(annotation.get("interactionFrame") or "").strip().lower()
        arousal_source = str(annotation.get("arousalSource") or "").strip().lower()
        acute_embarrassment = annotation.get("acuteEmbarrassment") is True
        annotation_intensity = clamp_float(
            annotation.get("emotionIntensity"),
            0.2,
            2.5,
            1.0,
        )
        low_shy_annoyance = bool(
            emotion == "annoyed"
            and interaction_frame == "shy_annoyance"
            and annotation_intensity <= 0.70
        )
        if (
            emotion in {"annoyed", "furious"}
            and genuine_anger is False
            and not low_shy_annoyance
            and non_anger_emotion
            and non_anger_emotion not in {"annoyed", "furious"}
        ):
            emotion = non_anger_emotion
        if emotion == "startled" and arousal_source == "embarrassment_exposure":
            emotion = "flustered"
            annotation["voiceStyle"] = "energetic"
            annotation["voiceTone"] = "flustered"
            annotation["gesture"] = (
                annotation.get("gesture")
                if annotation.get("gesture") in {"look_away", "tilt", "lean"}
                else "look_away"
            )
        elif (
            emotion == "shy"
            and arousal_source == "embarrassment_exposure"
            and acute_embarrassment
        ):
            emotion = "flustered"
            annotation["voiceStyle"] = "energetic"
            annotation["voiceTone"] = "flustered"
            if str(annotation.get("voiceIntensity") or "") == "subtle":
                annotation["voiceIntensity"] = "normal"
            annotation["gesture"] = (
                annotation.get("gesture")
                if annotation.get("gesture") in {"look_away", "tilt", "lean", "recoil"}
                else "look_away"
            )
        annotation["emotion"] = emotion
        return {
            key: annotation[key]
            for key in (
                "emotion", "gesture", "emotionIntensity", "emotionDelta", "blushIntensity",
                "eyeContact", "gazeX", "gazeY", "tension", "voiceStyle", "voiceTone", "voiceIntensity",
            )
            if key in annotation
        }


    def apply_model_emotion_annotation(
        self,
        result: dict[str, Any],
        annotation: dict[str, Any],
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply only expression-related model fields while preserving the accepted reply and metadata."""

        merged = dict(result)
        for key in (
            "voiceTone", "voiceIntensity", "emotionIntensity", "emotionDelta", "blushIntensity",
            "eyeContact", "gazeX", "gazeY", "tension", "holdMs", "fadeMs",
        ):
            if key not in annotation:
                merged.pop(key, None)
        merged.update(annotation)
        normalized = self.operations.normalize_model_reply(merged, settings=settings)
        repaired = dict(result)
        repaired.update(normalized)
        repaired["emotionLabelRecoveredByModel"] = True
        return repaired


    def repair_frozen_reply_emotion_once(
        self,
        settings: dict[str, Any],
        result: dict[str, Any],
        *,
        user_message: str,
        recent_messages: list[dict[str, Any]],
        relationship_state: dict[str, Any],
        request_id: str = "",
    ) -> dict[str, Any]:
        """Recover presentation metadata without allowing either speech track to change."""

        frozen_reply = str(result.get("reply") or result.get("textZh") or "").strip()
        frozen_spoken = str(result.get("spokenTextJa") or "").strip()
        if not frozen_reply:
            raise UpstreamProtocolError("emotion recovery requires a frozen reply")
        reply_fingerprint = runtime_fingerprint(frozen_reply, length=32)
        self.operations.write_server_log(
            "emotion_label_recovery_started",
            requestId=request_id,
            frozenReplyFingerprint=reply_fingerprint,
            recentMessageCount=len(recent_messages[-6:]),
        )
        annotation = self.classify_reply_emotion_with_model(
            settings,
            user_message,
            frozen_reply,
            recent_messages,
            relationship_state,
            request_id=request_id,
        )
        repaired = self.apply_model_emotion_annotation(result, annotation, settings)
        if (
            str(repaired.get("reply") or repaired.get("textZh") or "").strip()
            != frozen_reply
            or str(repaired.get("spokenTextJa") or "").strip() != frozen_spoken
        ):
            raise UpstreamProtocolError("emotion recovery changed a frozen speech track")
        repaired["emotionMissingFromModel"] = False
        repaired["emotionResolvedLocally"] = False
        repaired["emotionFieldRepaired"] = True
        repaired["emotionLabelRecoverySource"] = "frozen_reply_semantic_classifier"
        self.operations.write_server_log(
            "emotion_label_recovery_completed",
            requestId=request_id,
            frozenReplyFingerprint=reply_fingerprint,
            emotion=str(repaired.get("emotion") or ""),
            voiceStyle=str(repaired.get("voiceStyle") or ""),
            voiceTone=str(repaired.get("voiceTone") or ""),
        )
        return repaired


    def repair_frozen_utterance_emotions_once(
        self,
        settings: dict[str, Any],
        utterances: list[dict[str, Any]],
        *,
        user_message: str,
        recent_messages: list[dict[str, Any]],
        relationship_state: dict[str, Any],
        request_id: str = "",
    ) -> list[dict[str, Any]]:
        """Annotate a frozen multi-unit turn once and share it with every unit."""

        if not utterances or not any(
            item.get("emotionMissingFromModel") for item in utterances
        ):
            return [dict(item) for item in utterances]
        seed = {
            "reply": "\n\n".join(str(item.get("textZh") or "") for item in utterances),
            "spokenTextJa": "\n\n".join(
                str(item.get("spokenTextJa") or "") for item in utterances
            ),
            "emotion": "neutral",
            "emotionMissingFromModel": True,
        }
        annotated = self.repair_frozen_reply_emotion_once(
            settings,
            seed,
            user_message=user_message,
            recent_messages=recent_messages,
            relationship_state=relationship_state,
            request_id=request_id,
        )
        presentation_keys = (
            "emotion", "emotionIntensity", "gesture", "voiceStyle", "voiceTone",
            "voiceIntensity", "speechPace", "speechSpeed", "deliveryMode",
            "blushIntensity", "eyeContact", "gazeX", "gazeY", "tension",
        )
        repaired: list[dict[str, Any]] = []
        for item in utterances:
            updated = dict(item)
            if item.get("emotionMissingFromModel"):
                for key in presentation_keys:
                    if annotated.get(key) is not None:
                        updated[key] = annotated[key]
                updated["emotionMissingFromModel"] = False
                updated["emotionLabelRecoveredByModel"] = True
                updated["emotionLabelRecoverySource"] = "frozen_reply_semantic_classifier"
            repaired.append(updated)
        return repaired
