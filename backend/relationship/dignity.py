"""人格边界的状态应用：先读取已持久化状态，再按原模型评估结果更新。

分类失败保持原冷处理反馈；普通 furious 不增加严重侮辱计数。
此层通过关系存储入口读取和原子写入，不建立第二份状态或锁。
"""
from __future__ import annotations
from typing import Any, Callable
from backend.common.values import clamp_float
from backend.chat.reply_fields import normalize_boundary_assessment
from backend.relationship.dignity_rules import dignity_locked_response, furious_cold_shoulder_response

class DignityBoundaryService:
    def __init__(self, *, read_state: Callable[..., Any], write_state: Callable[..., Any],
                 classify: Callable[..., Any], write_log: Callable[..., Any]) -> None:
        self.read_state = read_state
        self.write_state = write_state
        self.classify = classify
        self.write_log = write_log

    def dignity_preflight(
        self,
        settings: dict[str, Any],
        user_message: str,
        recent_messages: list[dict[str, Any]],
        *,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Resolve persistent lock/cold-shoulder state before the main chat call."""

        state = self.read_state()
        if state.get("dignityLocked") or int(state.get("dignityViolationCount") or 0) >= 5:
            if not state.get("dignityLocked"):
                state["dignityLocked"] = True
                state["dignityViolationCount"] = 5
                state = self.write_state(state)
            return {"action": "locked", "state": state, "response": dignity_locked_response(state)}
        if not state.get("coldShoulder"):
            return {"action": "continue", "state": state, "assessment": normalize_boundary_assessment({})}
        try:
            assessment = self.classify(
                settings,
                user_message,
                recent_messages,
                request_id=request_id,
            )
        except Exception as error:
            self.write_log(
                "dignity_preflight_classifier_failed",
                requestId=request_id,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
            return {"action": "cold", "state": state, "response": furious_cold_shoulder_response(state)}
        if assessment["kind"] == "sincere_apology":
            state["coldShoulder"] = False
            state["coldShoulderReason"] = ""
            state["lastBoundaryAssessment"] = "sincere_apology"
            state = self.write_state(state)
            return {"action": "continue", "state": state, "assessment": assessment, "recoveredToAnnoyed": True}
        if assessment["kind"] == "severe_personal_insult":
            return {"action": "continue", "state": state, "assessment": assessment}
        return {"action": "cold", "state": state, "assessment": assessment, "response": furious_cold_shoulder_response(state)}


    def apply_dignity_assessment(
        self,
        result: dict[str, Any],
        preflight: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Persist only model-confirmed severe insults; normal anger never increments."""

        state = self.read_state()
        preflight = preflight if isinstance(preflight, dict) else {}
        assessment = normalize_boundary_assessment(
            preflight.get("assessment")
            if normalize_boundary_assessment(preflight.get("assessment")).get("kind") != "none"
            else result.get("boundaryAssessment")
        )
        state["lastBoundaryAssessment"] = assessment["kind"]
        output = dict(result)
        output["boundaryAssessment"] = assessment
        if assessment["kind"] == "severe_personal_insult":
            state["dignityViolationCount"] = min(5, int(state.get("dignityViolationCount") or 0) + 1)
            state["coldShoulder"] = True
            state["coldShoulderReason"] = "model_confirmed_severe_personal_insult"
            output.update({
                "emotion": "furious",
                "gesture": "look_away",
                "voiceStyle": "sharp",
                "voiceTone": "cold",
                "voiceIntensity": "strong",
                "emotionIntensity": max(1.5, clamp_float(output.get("emotionIntensity"), 0.2, 2.5, 1.5)),
                "deliveryMode": "outburst" if int(state["dignityViolationCount"]) >= 3 else "normal",
                "coldShoulder": True,
                "dignityViolationCount": int(state["dignityViolationCount"]),
            })
            if int(state["dignityViolationCount"]) >= 5:
                state["dignityLocked"] = True
                output = dignity_locked_response(state)
        elif preflight.get("recoveredToAnnoyed"):
            output.update({
                "emotion": "annoyed",
                "gesture": "look_away",
                "voiceStyle": "sharp",
                "voiceTone": "dry",
                "voiceIntensity": "normal",
                "emotionIntensity": min(1.25, max(0.9, clamp_float(output.get("emotionIntensity"), 0.2, 2.5, 1.0))),
                "deliveryMode": "normal",
                "coldShoulder": False,
                "dignityViolationCount": int(state.get("dignityViolationCount") or 0),
                "dignityRecoveryFromColdShoulder": True,
            })
        elif str(output.get("emotion") or "") == "furious" and bool(output.get("coldShoulder")):
            state["coldShoulder"] = True
            state["coldShoulderReason"] = "ordinary_furious_emotion"
            output["dignityViolationCount"] = int(state.get("dignityViolationCount") or 0)
        state = self.write_state(state)
        return output, state
