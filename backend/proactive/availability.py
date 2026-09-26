"""开口许可评估：模型决定前冻结用户轮次，落地前在同一会话锁下复核。

这里不挑话题或生成语音。新用户轮次、显式暂停会使旧后台决定失效。
许可失败沿用原静默行为，不新增模型重试或统一开启思考。
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping
from agent.proactive_control import OUTREACH_AVAILABILITY_INSTRUCTION
from backend.chat.parsing import parse_json_object
from backend.proactive.rules import proactive_outreach_context_key

@dataclass(frozen=True)
class StateOperations:
    lock: Callable[..., Any]
    read_conversation_context: Callable[..., Any]
    write_conversation_context: Callable[..., Any]
    normalize_proactive_control: Callable[..., Any]
    proactive_control_for_model: Callable[..., Any]
    apply_proactive_directive: Callable[..., Any]

@dataclass(frozen=True)
class ModelOperations:
    configured: Callable[..., Any]
    send_upstream_chat_payload: Callable[..., Any]

class OutreachAvailability:
    def __init__(self, state: StateOperations, model: ModelOperations, *,
                 now: Callable[[], str], write_log: Callable[..., Any]) -> None:
        self.state = state
        self.model = model
        self.now = now
        self.write_log = write_log

    def apply_model_outreach_decision(
        self,
        candidate: Mapping[str, Any], *, context_key: tuple[Any, ...],
        observed_control: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Apply only a structured model decision, without emitting a chat turn."""
        decision = str(candidate.get("outreachDecision") or "").strip().lower()
        reason = str(candidate.get("outreachReason") or "").strip()[:500]
        with self.state.lock():
            context = self.state.read_conversation_context()
            control = self.state.normalize_proactive_control(context.get("proactiveControl"))
            stale = (proactive_outreach_context_key(context) != context_key
                     or control != dict(observed_control))
            if stale or control["status"] == "paused":
                return {"ok": True, "suppressed": True,
                        "reason": "proactive_context_changed", "proactiveControl": control}
            if decision == "speak" and reason:
                self.write_log("proactive_outreach_decision", decision=decision, reason=reason)
                return None
            if decision == "pause" and reason:
                latest_user = next((turn for turn in reversed(context.get("recentTurns") or [])
                                    if isinstance(turn, Mapping) and turn.get("user")), {})
                control = self.state.apply_proactive_directive(
                    context, "pause", user_initiated=False,
                    source_turn_id=str(latest_user.get("turnId") or ""),
                    source_user_message=str(latest_user.get("user") or ""),
                    pause_reason="model_inferred_unavailability",
                )
                self.state.write_conversation_context(context)
            self.write_log("proactive_outreach_decision", decision=decision or "missing",
                             reason=reason, suppressed=True, status=control["status"])
            return {"ok": True, "suppressed": True, "proactiveControl": control,
                    "reason": "model_chose_silence" if decision == "pause" and reason
                    else "proactive_outreach_decision_invalid"}


    def assess_proactive_outreach(
        self,
        settings: dict[str, Any], context: Mapping[str, Any], messages: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Decide availability before choosing topics, observing screens or writing speech."""
        control = self.state.normalize_proactive_control(context.get("proactiveControl"))
        context_key = proactive_outreach_context_key(context)
        if not self.model.configured(settings):
            return self.apply_model_outreach_decision({}, context_key=context_key, observed_control=control)
        model_input = {"currentTime": self.now(), "proactiveControl": self.state.proactive_control_for_model(context),
                       "recentConversation": [{"role": item.get("role"), "content": str(item.get("content") or "")[:1600]}
                                              for item in messages[-14:]]}
        payload = {"model": settings["apiModel"], "temperature": 0, "max_tokens": 220,
                   "messages": [{"role": "system", "content": OUTREACH_AVAILABILITY_INSTRUCTION},
                                {"role": "user", "content": json.dumps(model_input, ensure_ascii=False)}]}
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        try:
            content = self.model.send_upstream_chat_payload(settings, payload, timeout=20, purpose="proactive_availability")
            candidate = parse_json_object(content)
        except Exception as error:
            self.write_log("proactive_availability_failed", errorType=type(error).__name__)
            candidate = {}
        return self.apply_model_outreach_decision(
            candidate if isinstance(candidate, dict) else {},
            context_key=context_key, observed_control=control,
        )
