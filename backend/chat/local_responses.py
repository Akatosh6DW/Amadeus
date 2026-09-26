"""离线/未配置模型时的本地对话回应；真实工具仍执行既有权限检查。

输出统一经过普通回复规范化。这里不伪造远端来源，不建立另一套任务或长期记忆存储。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import re
from backend.visual.sensor_intent import is_camera_query, is_screen_query

def is_time_query(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or ""))
    if re.search(r"现在几点|几点了|今天几号|今天星期|现在是哪年|今年是哪年", normalized):
        return True
    return bool(re.search(
        r"(?:现在|当前|本地|当地|系统)(?:的)?(?:日期(?:和|与|及|、)?时间|时间(?:和|与|及|、)?日期|日期|时间)",
        normalized,
    ))


@dataclass(frozen=True)
class LocalResponsePorts:
    settings: Callable[[], dict[str, Any]]
    normalize_reply: Callable[..., dict[str, Any]]
    system_time: Callable[..., dict[str, Any]]
    screen: Callable[..., dict[str, Any]]
    camera: Callable[..., dict[str, Any]]
    memory_candidates: Callable[..., Any]
    nickname_context: Callable[..., dict[str, Any]]

class LocalResponses:
    def __init__(self, ports: LocalResponsePorts) -> None:
        self.ports = ports

    def local_time_reply(self, settings: dict[str, Any]) -> dict[str, Any]:
        tool_result = self.ports.system_time(settings)
        if not tool_result.get("ok"):
            return self.ports.normalize_reply({
                "reply": "你还没给我读取系统时间的权限。没有观测数据就报时间，那不是科学，是胡猜。",
                "spokenTextJa": "まだシステム時刻を読む権限がないわ。観測データなしで時刻を言うなんて、科学じゃなくてただの当てずっぽうよ。",
                "emotion": "skeptical",
                "gesture": "tilt",
                "voiceStyle": "sharp",
                "source": "tool",
            }, settings=settings)
        hour = int(tool_result["hour"])
        minute = int(tool_result["minute"])
        period = "凌晨" if hour < 5 else "早上" if hour < 9 else "上午" if hour < 12 else "中午" if hour < 14 else "下午" if hour < 18 else "晚上"
        reply = f"现在是{period}{hour:02d}:{minute:02d}，{tool_result['year']}年{tool_result['month']}月{tool_result['day']}日。"
        if hour >= 23 or hour < 5:
            reply += "已经很晚了，别把作息也当成可以无限透支的实验变量。"
        elif 12 <= hour < 14:
            reply += "到午间了，记得先吃点东西。"
        result = self.ports.normalize_reply({
            "reply": reply,
            "spokenTextJa": f"今は{tool_result['hour']:02d}時{tool_result['minute']:02d}分、{tool_result['year']}年{tool_result['month']}月{tool_result['day']}日よ。",
            "emotion": "neutral",
            "gesture": "nod",
            "voiceStyle": "calm",
            "source": "tool",
        }, settings=settings)
        result["toolUsed"] = "get_system_time"
        result["toolResults"] = [tool_result]
        result["_conversationToolResults"] = [tool_result]
        return result


    def local_screen_reply(self, settings: dict[str, Any], user_question: str = "") -> dict[str, Any]:
        result = self.ports.screen(settings, user_question)
        if not result.get("ok"):
            reply = "你还没给我读取屏幕的权限。没有观测数据就说看到了，那和伪科学没区别。"
            if result.get("error") and result.get("error") != "permission_denied":
                reply = f"屏幕读取失败：{result.get('error')}。这不是我嘴硬，是工具真的没拿到画面。"
            return self.ports.normalize_reply({
                "reply": reply,
                "spokenTextJa": "まだ画面を読む権限がないわ。観測データなしで見えたなんて言うのは、科学じゃないもの。",
                "emotion": "skeptical",
                "gesture": "tilt",
                "voiceStyle": "sharp",
                "source": "tool",
            }, settings=settings)
        vision = result.get("vision") if isinstance(result.get("vision"), dict) else {}
        title = result.get("foregroundWindowTitle") or "未知窗口"
        if vision.get("available"):
            caption = str(vision.get("caption") or "").strip()
            ocr_text = str(vision.get("ocrText") or "").strip()
            details = "；".join(item for item in [caption, f"可见文字：{ocr_text[:500]}" if ocr_text else ""] if item)
            reply = f"我看到了。前台窗口是“{title}”。{details or '画面里没有识别出足够明确的内容。'}"
        else:
            reply = (
                f"我已经截到当前屏幕：{result.get('width')}x{result.get('height')}，前台窗口是“{title}”，截图保存到 {result.get('path')}。"
                "DeepSeek 云端识图没有正常返回内容，所以我现在不能可靠描述画面。"
            )
        response = self.ports.normalize_reply({
            "reply": reply,
            "spokenTextJa": "画面を確認したわ。見えている内容を整理して伝えるね。",
            "emotion": "focused",
            "gesture": "explain",
            "voiceStyle": "calm",
            "source": "tool",
        }, settings=settings)
        response["toolUsed"] = "capture_screen"
        response["toolResults"] = [result]
        return response


    def local_camera_reply(self, settings: dict[str, Any], user_question: str = "") -> dict[str, Any]:
        result = self.ports.camera(settings, user_question)
        if not result.get("ok"):
            denied = result.get("error") == "permission_denied"
            reply = (
                "你还没给我摄像头权限。想让我看，就先在设置的权限页明确打开。"
                if denied else
                f"摄像头没有取得画面：{result.get('error')}。检查设备编号，或者看看是不是被别的软件占用了。"
            )
            response = self.ports.normalize_reply({
                "reply": reply,
                "spokenTextJa": "カメラの映像を取得できないわ。権限とデバイスを確認して。",
                "emotion": "skeptical",
                "gesture": "tilt",
                "voiceStyle": "sharp",
                "source": "tool",
            }, settings=settings)
        else:
            vision = result.get("vision") if isinstance(result.get("vision"), dict) else {}
            caption = str(vision.get("caption") or "").strip()
            reply = (
                f"看到了。{caption}"
                if vision.get("available") and caption else
                "摄像头已经拍到画面，但视觉服务没有给出可靠描述。"
            )
            response = self.ports.normalize_reply({
                "reply": reply,
                "spokenTextJa": "カメラを確認したわ。今見えている様子をそのまま伝えるね。",
                "emotion": "focused",
                "gesture": "soften",
                "voiceStyle": "calm",
                "source": "tool",
            }, settings=settings)
        response["toolUsed"] = "capture_camera"
        response["toolStatus"] = "ok" if result.get("ok") else "failed"
        response["toolResults"] = [result]
        return response


    def mock_reply(self, text: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        lower = text.lower()
        settings = settings or self.ports.settings()
        if is_time_query(text):
            return self.local_time_reply(settings)
        if is_screen_query(text):
            return self.local_screen_reply(settings, text)
        if is_camera_query(text):
            return self.local_camera_reply(settings, text)
        if re.search(r"难受|累|害怕|焦虑|孤独|睡不着|崩溃|不开心|痛苦|失败", text):
            return self.ports.normalize_reply({
                "reply": "先暂停一下。你现在不需要证明自己有多冷静。呼吸、喝水，然后把最难受的那一小块告诉我。我会在这里。",
                "spokenTextJa": "少し止まりなさい。今は冷静さを証明しなくていい。息をして、水を飲んで、いちばんつらいところだけ話して。私はここにいるから。",
                "emotion": "concerned",
                "gesture": "soften",
                "voiceStyle": "soft",
                "memoryUpdates": [],
                "source": "local",
            }, settings=settings)
        nickname_context = self.ports.nickname_context(text)
        if (
            nickname_context.get("mode") == "optional_weak"
            and nickname_context.get("reactionSelected")
        ):
            nickname_kind = str(nickname_context.get("kind") or "other")
            reply_prefix = {
                "assistant": "才不是助手。……",
                "christina": "不许蒂娜。……",
                "other": "别乱起外号。……",
            }.get(nickname_kind, "")
            spoken_prefix = {
                "assistant": "助手じゃない。……",
                "christina": "ティーナって付けるな。……",
                "other": "変なあだ名を付けないで。……",
            }.get(nickname_kind, "")
            return self.ports.normalize_reply({
                "reply": reply_prefix + "说吧，什么事？",
                "spokenTextJa": spoken_prefix + "それで、何？",
                "emotion": "annoyed",
                "gesture": "tilt",
                "voiceStyle": "teasing",
                "voiceTone": "shy_annoyed",
                "emotionIntensity": 0.46,
                "memoryUpdates": [],
                "source": "local",
            }, settings=settings)
        if re.search(r"喜欢你|可爱|天才|厉害|谢谢|辛苦|想你", text):
            return self.ports.normalize_reply({
                "reply": "突、突然说这种话干什么。作为评价样本倒是有一定参考价值……咳。继续，我在听。",
                "spokenTextJa": "きゅ、急に何を言ってるのよ。評価サンプルとしては、まあ参考になるけど……こほん。続けて。聞いてるから。",
                "emotion": "shy",
                "gesture": "lean",
                "voiceStyle": "teasing",
                "memoryUpdates": [],
                "source": "local",
            }, settings=settings)
        if re.search(r"时间|机器|ai|人工智能|神经|量子|论文|实验|算法|模型|api|代码|bug|世界线|收束", lower):
            return self.ports.normalize_reply({
                "reply": "从实验角度看，先拆变量：目标函数、输入记忆、输出映射。把这三件事分开，你的计划就会从离谱变成勉强可验证。",
                "spokenTextJa": "実験として見るなら、まず変数を分けるべきね。目的関数、入力される記憶、出力の写像。この三つを分ければ、無茶な計画も一応検証可能になる。",
                "emotion": "thinking",
                "gesture": "explain",
                "voiceStyle": "calm",
                "memoryUpdates": self.ports.memory_candidates(text),
                "source": "local",
            }, settings=settings)
        return self.ports.normalize_reply({
            "reply": "嗯，我明白了。语气听起来像是又在酝酿什么计划。继续说，我会负责把它修剪成可执行的实验方案。",
            "spokenTextJa": "うん、分かった。その口ぶりだと、また何か企んでるみたいね。続けて。実行できる実験案に整えるくらいは、私がやってあげる。",
            "emotion": "amused",
            "gesture": "idle",
            "voiceStyle": "teasing",
            "memoryUpdates": self.ports.memory_candidates(text),
            "source": "local",
        }, settings=settings)
