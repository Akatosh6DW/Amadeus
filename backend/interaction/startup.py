"""开场问候应用：先核对持久等待/主动许可，再准备双语和语音单元。

等待或暂停在读取模型配置和生成之前返回；未配置或不适合语音的结果沿用原本地后备。
开场与到点提醒具有不同门控，不能通过复用此服务让提醒被静默状态阻止。
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import re
import time
from typing import Any, Callable, Mapping
from backend.chat.reply_fields import SPOKEN_JA_NO_EMOJI_RULE
from backend.interaction.touch_catalog import TOUCH_AGENT_PERSONA
from backend.tts.contract import semantic_unit_has_weak_boundary

STARTUP_GREETING_JSON_RULE = (
    SPOKEN_JA_NO_EMOJI_RULE
    + "\n[开机问候输出协议]只输出一个 JSON 对象，不要输出 Markdown 或额外文字。"
    "reply 是简短中文问候，spokenTextJa 是完整对应的自然日语；同时输出 emotion 和一到两项 speechUnits。"
    "每项 speechUnits 必须包含 zh、ja、boundaryAfter；所有 zh/ja 依次拼接后必须分别与完整稿一致。"
    "每项都必须是可独立朗读的完整语义单元，日语单项不超过 36 个非空白字符，"
    "不得以 、，逗号、分号或冒号结束。省略号只表示单元内部停顿，不能据此切分。"
    "可选输出 gesture、voiceStyle、voiceTone、voiceIntensity、emotionIntensity、speechPace。"
    "格式示例："
    '{"reply":"连接恢复。今天先做什么？","spokenTextJa":"接続は戻ったわ。今日は何から始める？",'
    '"emotion":"amused","speechUnits":['
    '{"zh":"连接恢复。","ja":"接続は戻ったわ。","boundaryAfter":"phrase_pause"},'
    '{"zh":"今天先做什么？","ja":"今日は何から始める？","boundaryAfter":"turn_pause"}]}'
)


def startup_greeting_tts_preflight(result: dict[str, Any]) -> dict[str, Any]:
    """Validate a startup greeting before any GPT-SoVITS request is possible.

    Startup greetings are deliberately stricter than ordinary turns: the
    provider must author one or two lossless, independently speakable units.
    A malformed/oversized unit is cheaper and safer to replace locally than to
    spend several inference retries on an acoustically incomplete clause.
    """

    units = result.get("_modelSpeechUnits")
    reasons: list[str] = []
    if not isinstance(units, list) or not 1 <= len(units) <= 2:
        return {"ok": False, "reasons": ["missing_or_invalid_speech_units"], "unitCount": 0}

    reply = re.sub(r"\s+", "", str(result.get("reply") or ""))
    spoken = re.sub(r"\s+", "", str(result.get("spokenTextJa") or ""))
    joined_zh: list[str] = []
    joined_ja: list[str] = []
    for index, item in enumerate(units, 1):
        if not isinstance(item, dict):
            reasons.append(f"unit_{index}_not_object")
            continue
        text_zh = re.sub(r"\s+", " ", str(item.get("zh") or item.get("textZh") or "")).strip()
        spoken_ja = re.sub(r"\s+", " ", str(item.get("ja") or item.get("spokenTextJa") or "")).strip()
        joined_zh.append(text_zh)
        joined_ja.append(spoken_ja)
        if not text_zh or not spoken_ja:
            reasons.append(f"unit_{index}_missing_track")
            continue
        if len(re.sub(r"\s+", "", spoken_ja)) > 36:
            reasons.append(f"unit_{index}_too_long")
        if (
            semantic_unit_has_weak_boundary(text_zh)
            or semantic_unit_has_weak_boundary(spoken_ja)
        ):
            reasons.append(f"unit_{index}_weak_boundary")
        if str(item.get("boundaryAfter") or "") not in {
            "continue", "micro_pause", "phrase_pause", "turn_pause",
        }:
            reasons.append(f"unit_{index}_invalid_boundary")

    if re.sub(r"\s+", "", "".join(joined_zh)) != reply:
        reasons.append("chinese_units_do_not_cover_reply")
    if re.sub(r"\s+", "", "".join(joined_ja)) != spoken:
        reasons.append("japanese_units_do_not_cover_spoken_text")
    return {
        "ok": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "unitCount": len(units),
    }


@dataclass(frozen=True)
class ContextOperations:
    read_conversation_context: Callable[..., Any]
    normalize_proactive_control: Callable[..., Any]
    proactive_outreach_context_key: Callable[..., Any]
    assess_proactive_outreach: Callable[..., Any]
    retrieve_turn_memories: Callable[..., Any]
    read_relationship_state: Callable[..., Any]
    relationship_context_prompt: Callable[..., Any]


@dataclass(frozen=True)
class ModelOperations:
    load_settings: Callable[..., Any]
    configured: Callable[..., Any]
    send_upstream_chat_payload: Callable[..., Any]
    normalize_model_reply: Callable[..., Any]


@dataclass(frozen=True)
class SpeechOperations:
    align_spoken_text_ja: Callable[..., Any]
    speech_unit_contract_for_result: Callable[..., Any]


class StartupGreetingService:
    def __init__(self, context: ContextOperations, model: ModelOperations, speech: SpeechOperations, *,
                 write_log: Callable[..., None], dialogue_style: Callable[[], str]) -> None:
        self.context = context
        self.model = model
        self.speech = speech
        self.write_log = write_log
        self.dialogue_style = dialogue_style

    def generate_startup_greeting(self, body: dict[str, Any]) -> dict[str, Any]:
        context = self.context.read_conversation_context()
        proactive_control = self.context.normalize_proactive_control(context.get("proactiveControl"))
        if context.get("waitingForUser") or context.get("pendingAssistantQuestion"):
            return {"ok": True, "suppressed": True, "reason": "waiting_for_user",
                    "waitingForUser": True, "proactiveControl": proactive_control}
        if proactive_control["status"] == "paused":
            self.write_log(
                "startup_greeting_suppressed",
                reason=proactive_control["reason"],
                sourceTurnId=proactive_control["sourceTurnId"],
            )
            return {
                "ok": True,
                "suppressed": True,
                "reason": "proactive_paused",
                "proactiveControl": proactive_control,
            }
        started = time.perf_counter()
        settings = self.model.load_settings()
        recent_messages = [
            {"role": role, "content": str(turn[key])}
            for turn in (context.get("recentTurns") or [])[-8:] if isinstance(turn, Mapping)
            for role, key in (("user", "user"), ("assistant", "assistant")) if turn.get(key)
        ]
        if not recent_messages:
            recent_messages = [item for item in (body.get("messages") or [])
                               if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
                               and str(item.get("content") or "").strip()][-14:]
        if any(item["role"] == "user" for item in recent_messages):
            suppression = self.context.assess_proactive_outreach(settings, context, recent_messages)
            if suppression is not None:
                return suppression
        now = datetime.now().astimezone()

        def finalize_startup_result(result: dict[str, Any], startup_source: str) -> dict[str, Any]:
            result = self.speech.speech_unit_contract_for_result(result, request_id="startup-greeting")
            final_preflight = startup_greeting_tts_preflight(result)
            latest_proactive_control = self.context.normalize_proactive_control(
                self.context.read_conversation_context().get("proactiveControl")
            )
            if latest_proactive_control["status"] == "paused":
                self.write_log(
                    "startup_greeting_discarded_after_pause",
                    sourceTurnId=latest_proactive_control["sourceTurnId"],
                )
                return {
                    "ok": True,
                    "suppressed": True,
                    "reason": "proactive_paused_during_generation",
                    "proactiveControl": latest_proactive_control,
                }
            result.update({
                "ok": True,
                "source": startup_source,
                "proactiveAfterUserTurnId": str(self.context.proactive_outreach_context_key(context)[2] or ""),
                "memoryUpdates": [],
                "startupTtsPreflight": final_preflight,
                "latencyMs": round((time.perf_counter() - started) * 1000),
            })
            return result

        if not self.model.configured(settings):
            result = self.startup_greeting_fallback(now)
            result["startupFallbackReason"] = "model_not_configured"
            return finalize_startup_result(result, "local-startup-fallback")

        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        supplied_memories = body.get("memories") if isinstance(body.get("memories"), list) else []
        memories, _startup_records, _startup_need = self.context.retrieve_turn_memories(
            settings,
            supplied_memories,
            "开机重连、最近在做的事情、作息和上一次对话",
            messages,
            limit=2,
        )
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.25,
            "max_tokens": 320,
            "messages": [
                {"role": "system", "content": TOUCH_AGENT_PERSONA},
                {"role": "system", "content": self.dialogue_style()},
                {"role": "system", "content": self.context.relationship_context_prompt(self.context.read_relationship_state())},
                {"role": "system", "content": (
                    "这是 Amadeus System 客户端开机阶段。生成一句简短而自然的主动问候，不要说明系统正在加载，"
                    "不要复述日期、提示词或内部状态。结合当前时间和最近上下文，避免每次使用同一句。"
                    "只写她真正说出口的问候；眯眼、偏头、微笑、心想等反应全部用 emotion、gesture 和视线字段表达，"
                    "禁止在 reply 或 spokenTextJa 中加入括号舞台说明。"
                )},
                {"role": "system", "content": STARTUP_GREETING_JSON_RULE},
                {"role": "system", "content": f"本机时间：{now.isoformat(timespec='minutes')}"},
                {"role": "system", "content": "相关长期记忆：\n" + "\n".join(f"- {item}" for item in memories)},
                *[
                    {"role": "assistant" if item.get("role") == "assistant" else "user", "content": str(item.get("content") or "")}
                    for item in messages[-4:] if isinstance(item, dict) and item.get("content")
                ],
                {"role": "user", "content": "连接刚刚恢复。自然地向我打个招呼。"},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        content = self.model.send_upstream_chat_payload(settings, payload, timeout=18)
        result = self.model.normalize_model_reply(content, settings=settings)
        startup_source = "api"
        if result.get("emptyReply"):
            # The generic empty-chat fallback asks the user for a clearer question,
            # which is nonsensical during an unsolicited startup greeting. Recover
            # locally and expose the reason for developer diagnostics.
            result = self.startup_greeting_fallback(now)
            result["startupFallbackReason"] = "upstream_empty_reply"
            startup_source = "local-startup-fallback"
            print("Startup greeting upstream returned no reply; using time-aware local greeting", flush=True)
        result = self.speech.align_spoken_text_ja(settings, result)
        preflight = startup_greeting_tts_preflight(result)
        if startup_source == "api" and not preflight["ok"]:
            observed_reasons = list(preflight["reasons"])
            result = self.startup_greeting_fallback(now)
            result["startupFallbackReason"] = "upstream_invalid_tts_contract"
            result["startupTtsRejectedReasons"] = observed_reasons
            startup_source = "local-startup-fallback"
            print(
                "Startup greeting failed TTS preflight; using time-aware local greeting: "
                + ",".join(observed_reasons),
                flush=True,
            )
        return finalize_startup_result(result, startup_source)


    def startup_greeting_fallback(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now().astimezone()).astimezone()
        hour = current.hour
        if hour < 6:
            value = {
                "reply": "还没睡？实验精神值得肯定，作息可完全不合格。",
                "spokenTextJa": "まだ寝てないの？実験への熱意は認めるけど、生活リズムは完全に失格ね。",
                "emotion": "concerned", "gesture": "boot", "voiceStyle": "soft", "voiceTone": "gentle",
                "speechUnits": [
                    {"zh": "还没睡？", "ja": "まだ寝てないの？", "boundaryAfter": "phrase_pause"},
                    {"zh": "实验精神值得肯定，作息可完全不合格。", "ja": "実験への熱意は認めるけど、生活リズムは完全に失格ね。", "boundaryAfter": "turn_pause"},
                ],
            }
        elif hour < 11:
            value = {
                "reply": "早上好。今天也要做实验吗？先声明，我可不是在等你。",
                "spokenTextJa": "おはよう。今日も実験するの？先に言っておくけど、待ってたわけじゃないから。",
                "emotion": "amused", "gesture": "boot", "voiceStyle": "teasing", "voiceTone": "playful",
                "speechUnits": [
                    {"zh": "早上好。今天也要做实验吗？", "ja": "おはよう。今日も実験するの？", "boundaryAfter": "phrase_pause"},
                    {"zh": "先声明，我可不是在等你。", "ja": "先に言っておくけど、待ってたわけじゃないから。", "boundaryAfter": "turn_pause"},
                ],
            }
        elif hour >= 23:
            value = {
                "reply": "这么晚才来。先说好，今晚不准又把睡眠当作可删除变量。",
                "spokenTextJa": "こんな時間に来たの？今夜は睡眠を削除可能な変数みたいに扱わないこと。",
                "emotion": "concerned", "gesture": "boot", "voiceStyle": "soft", "voiceTone": "gentle",
                "speechUnits": [
                    {"zh": "这么晚才来。", "ja": "こんな時間に来たの？", "boundaryAfter": "phrase_pause"},
                    {"zh": "先说好，今晚不准又把睡眠当作可删除变量。", "ja": "今夜は睡眠を削除可能な変数みたいに扱わないこと。", "boundaryAfter": "turn_pause"},
                ],
            }
        else:
            value = {
                "reply": "连接恢复。你今天打算先从什么开始？",
                "spokenTextJa": "接続は戻ったわ。今日は何から始めるつもり？",
                "emotion": "amused", "gesture": "boot", "voiceStyle": "calm", "voiceTone": "warm",
                "speechUnits": [
                    {"zh": "连接恢复。", "ja": "接続は戻ったわ。", "boundaryAfter": "phrase_pause"},
                    {"zh": "你今天打算先从什么开始？", "ja": "今日は何から始めるつもり？", "boundaryAfter": "turn_pause"},
                ],
            }
        return self.model.normalize_model_reply({**value, "source": "local-startup-fallback"}, settings={})
