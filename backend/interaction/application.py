"""触摸交互的完整应用流程，HTTP 只负责接收和返回 JSON。

先冻结会话身份与读取关系，再选择原音/模型/本地反馈；冷淡阻断不进入生成或保存。
已接受结果按原顺序更新关系并追加触摸会话，不写长期记忆，旧会话身份由存储层核验。
"""
from __future__ import annotations
from dataclasses import dataclass
import time
from typing import Any, Callable
from backend.interaction.touch_rules import normalize_touch_event, touch_event_prompt

@dataclass(frozen=True)
class GenerationOperations:
    """按既有优先级提供原音、模型或本地反馈。"""

    select_touch_original_clip: Callable[..., Any]
    touch_original_clip_response: Callable[..., Any]
    call_touch_upstream: Callable[..., Any]
    mock_touch_reply: Callable[..., Any]


@dataclass(frozen=True)
class StateOperations:
    """使用同一会话身份和关系仓储，不另存用户状态。"""

    touch_conversation_context: Callable[..., Any]
    read_relationship_state: Callable[..., Any]
    update_relationship_state: Callable[..., Any]
    append_touch_conversation_reply: Callable[..., Any]


class TouchApplication:
    def __init__(
        self, generation: GenerationOperations, state: StateOperations, *,
        blocked_response: Callable[..., Any], align_spoken: Callable[..., Any],
        load_settings: Callable[..., Any], configured: Callable[..., bool],
        release_mode: Callable[[], bool], write_log: Callable[..., None],
    ) -> None:
        self.generation = generation
        self.state = state
        self.blocked_response = blocked_response
        self.align_spoken = align_spoken
        self.load_settings = load_settings
        self.configured = configured
        self.release_mode = release_mode
        self.write_log = write_log

    def execute(self, body: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        touch_context = self.state.touch_conversation_context(body)
        relationship_before = self.state.read_relationship_state()
        event = normalize_touch_event(body, relationship_before)
        if relationship_before.get("coldShoulder"):
            result = self.blocked_response(relationship_before)
            result.update({
                "relationshipTone": relationship_before.get("stage", "close_companion"),
                "touchZone": event["zone"],
                "touchAction": event["action"],
                "latencyMs": round((time.time() - started) * 1000),
                "touchMetrics": {"blockedByColdShoulder": True},
            })
            self.write_log(
                "touch_blocked_by_cold_shoulder",
                zone=event["zone"],
                action=event["action"],
            )
            return result
        original_clip = self.generation.select_touch_original_clip(event)
        if original_clip is not None:
            result = self.generation.touch_original_clip_response(original_clip, event)
        else:
            result = self.generation.call_touch_upstream(body, event, relationship_before) if self.configured() else self.generation.mock_touch_reply(event)
        raw_model_output = result.pop("_debugRawModelOutput", "")
        upstream_payload = result.pop("_debugUpstreamPayload", None)
        touch_metrics = result.pop("_touchMetrics", {})
        touch_metrics.setdefault(
            "responseAngle",
            str(event.get("responseDirective", {}).get("angle") or ""),
        )
        self.write_log(
            "touch_response_generated",
            zone=event["zone"],
            action=event["action"],
            responseAngle=str(event.get("responseDirective", {}).get("angle") or "")[:160],
            repetitionRepaired=bool(touch_metrics.get("repetitionRepaired")),
            structuredOutputRepaired=bool(touch_metrics.get("structuredOutputRepaired")),
            fallbackUsed=bool(touch_metrics.get("fallbackUsed")),
            repairReasons=list(touch_metrics.get("repairReasons") or []),
            finalFallbackReasons=list(touch_metrics.get("finalFallbackReasons") or []),
            softGroundingAccepted=bool(touch_metrics.get("softGroundingAccepted")),
            originalClipUsed=bool(touch_metrics.get("originalClipUsed")),
            originalClipId=str(touch_metrics.get("originalClipId") or ""),
        )
        # 原音无需文字语音对齐；其余结果保留原对齐入口及远程修复许可。
        # 这里只协调交付，不另写一份语言修复策略或增加新的重试。
        if not result.get("originalAudio"):
            result = self.align_spoken(self.load_settings(), result, allow_remote_repair=True)
        interaction_text = (
            f"用户通过界面完成了一段{event['actionLabel']}；这些动作已在停止点击后合并为一次互动。"
            if event.get("aggregatedBurst")
            else {
                "head": "用户通过界面摸了摸头。",
                "hand": "用户通过界面牵住了手。",
                "face": "用户通过界面轻触脸颊。",
                "mouth": "用户尝试了亲吻互动。",
                "chest": "用户进行了越界的胸口触摸。",
            }[event["zone"]]
        )
        relationship = self.state.update_relationship_state(interaction_text, result)
        if body.get("developerDebug") and not self.release_mode():
            result["debugInteraction"] = {
                "normalizedEvent": event,
                "hiddenPrompt": touch_event_prompt(event),
                "upstreamPayload": upstream_payload,
                "rawModelOutput": raw_model_output or "(local fallback: no upstream raw output)",
                "normalizedResult": {
                    key: value for key, value in result.items()
                    if not key.startswith("_")
                },
                "relationshipBefore": relationship_before,
                "relationshipAfter": relationship,
                "metrics": touch_metrics,
            }
        result.update({
            "ok": True,
            "source": result.get("source") or ("api" if self.configured() else "local"),
            "interactionMode": (
                "original_clip"
                if result.get("originalAudio")
                else "hidden_agent_event" if self.configured() else "local_fallback"
            ),
            "relationshipTone": relationship.get("stage", "close_companion"),
            "touchZone": event["zone"],
            "touchAction": event["action"],
            "memoryUpdates": [],
            "latencyMs": round((time.time() - started) * 1000),
            "touchMetrics": touch_metrics,
        })
        self.state.append_touch_conversation_reply(interaction_text, result, touch_context)
        return result
