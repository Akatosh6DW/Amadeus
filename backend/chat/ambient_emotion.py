"""空闲表情 token 的规范化、本地候选和已有模型判断。

它只返回表情控制数据，不生成聊天台词、不改变对话状态；失败时使用既有本地规则。
"""
from __future__ import annotations
from typing import Any, Callable
import json
import re
from backend.common.values import clamp_float, clamp_int
from backend.chat.parsing import parse_json_object
from backend.chat.presentation import ALLOWED_EMOTIONS, ALLOWED_GESTURES, EMOTION_ALIASES, GESTURE_ALIASES
from backend.chat.emotion_repair import EMOTION_SELECTION_RULE

def normalize_emotion_token(value: Any, source: str = "local") -> dict[str, Any]:
    if isinstance(value, str):
        value = parse_json_object(value) or {}
    if not isinstance(value, dict):
        value = {}
    raw_emotion = str(value.get("emotion") or "neutral").strip()
    emotion = raw_emotion if raw_emotion in ALLOWED_EMOTIONS else EMOTION_ALIASES.get(raw_emotion, "neutral")
    raw_gesture = str(value.get("gesture") or "idle").strip()
    gesture = GESTURE_ALIASES.get(raw_gesture, raw_gesture)
    if gesture not in ALLOWED_GESTURES:
        gesture = "idle"
    requested_hold_ms = clamp_int(value.get("holdMs") or value.get("hold_ms"), 0, 120000, 0 if emotion == "neutral" else 45000)
    requested_fade_ms = clamp_int(value.get("fadeMs") or value.get("fade_ms"), 500, 30000, 1200 if emotion == "neutral" else 10000)
    return {
        "ok": True,
        "emotion": emotion,
        "gesture": gesture,
        "emotionIntensity": clamp_float(value.get("emotionIntensity") or value.get("emotion_intensity"), 0.15, 1.25, 0.8 if emotion == "neutral" else 0.9),
        "blushIntensity": clamp_float(value.get("blushIntensity") or value.get("blush"), 0.0, 1.0, 0.78 if emotion == "shy" else 0.88 if emotion == "flustered" else 0.0),
        "eyeContact": clamp_float(value.get("eyeContact"), 0.0, 1.0, 0.42 if emotion == "shy" else 0.31 if emotion == "flustered" else 0.68),
        "gazeX": clamp_float(value.get("gazeX"), -0.6, 0.6, -0.16 if emotion == "shy" else 0.0),
        "gazeY": clamp_float(value.get("gazeY"), -0.6, 0.6, 0.13 if emotion == "shy" else 0.0),
        "tension": clamp_float(value.get("tension"), 0.0, 1.0, 0.24 if emotion == "shy" else 0.0),
        "holdMs": 0 if emotion == "neutral" else max(42000, requested_hold_ms),
        "fadeMs": requested_fade_ms if emotion == "neutral" else max(8000, requested_fade_ms),
        "source": source,
    }


def local_emotion_token(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    text = "\n".join(str(item.get("content") or "") for item in messages[-4:] if isinstance(item, dict))
    current = str(body.get("currentEmotion") or "neutral")
    if current in {"shy", "flustered", "affectionate"}:
        return normalize_emotion_token({"emotion": "neutral", "gesture": "idle", "holdMs": 0, "fadeMs": 1200}, source="local")
    if re.search(r"难受|焦虑|害怕|孤独|睡不着|痛苦|撑不住|累了", text):
        return normalize_emotion_token({"emotion": "concerned", "gesture": "soften", "emotionIntensity": 0.82, "holdMs": 9000, "fadeMs": 3600}, source="local")
    if re.search(r"bug|错误|失败|排查|接口|模型|算法|代码|为什么|原理", text, re.I):
        return normalize_emotion_token({"emotion": "thinking", "gesture": "idle", "emotionIntensity": 0.72, "holdMs": 7000, "fadeMs": 2200}, source="local")
    return normalize_emotion_token({"emotion": "neutral", "gesture": "idle", "holdMs": 0, "fadeMs": 900}, source="local")


class AmbientEmotion:
    def __init__(self, *, settings: Callable[[], dict[str, Any]], configured: Callable[[dict[str, Any]], bool],
                 send_chat: Callable[..., Any]) -> None:
        self.settings = settings
        self.configured = configured
        self.send_chat = send_chat

    def generate_context_emotion_token(self, body: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        if not self.configured(settings):
            return local_emotion_token(body)
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": EMOTION_SELECTION_RULE},
                {"role": "system", "content": (
                    "你是 Amadeus 的空闲表情控制器。只输出 JSON，不输出解释。"
                    "根据最近上下文判断此刻没有新发言时应呈现的 emotion token。"
                    "如果上一段强情绪已经结束，尤其是 shy/flustered/affectionate，不要机械延续，优先回 neutral。"
                    "允许字段：emotion, gesture, emotionIntensity, blushIntensity, eyeContact, gazeX, gazeY, tension, holdMs, fadeMs。"
                    "holdMs 和 fadeMs 用毫秒；没有明确情绪时 emotion=neutral, gesture=idle, holdMs=0。"
                )},
                {"role": "user", "content": json.dumps({
                    "currentEmotion": body.get("currentEmotion") or "neutral",
                    "recentMessages": messages[-8:],
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        try:
            content = self.send_chat(settings, payload, timeout=40, purpose="ambient_emotion")
            return normalize_emotion_token(content, source="api")
        except Exception:
            return local_emotion_token(body)
