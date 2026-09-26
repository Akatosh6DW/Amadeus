"""主动语音事件与交付回执的持久化边界。

主动事件仍通过 ConversationWriter 写入同一 archive/context 状态；本模块
只拥有事件准入、去重和 delivery acknowledgement 的顺序，不复制 proactive
文本去重或请求生成状态。所有存储和应用动作通过窄端口动态接入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping
import re

from backend.common.values import safe_nonnegative_int
from backend.runtime.requests import RequestValidationError


@dataclass(frozen=True)
class ProactiveEventPorts:
    """主动事件需要的 context、提醒和 turn 写入端口。"""

    lock: Any
    read_context: Callable[[], dict[str, Any]]
    write_context: Callable[[dict[str, Any]], dict[str, Any]]
    normalize_proactive_directive: Callable[[Any], str]
    normalize_proactive_control: Callable[[Any], dict[str, Any]]
    state_database: Callable[[], Any]
    read_todo: Callable[[Any, str], Mapping[str, Any]]
    todo_context_for_task: Callable[[Mapping[str, Any]], dict[str, Any]]
    proactive_outreach_context_key: Callable[[Mapping[str, Any]], tuple[Any, ...]]
    screen_capture_tool_result_from_memory: Callable[[Any], dict[str, Any] | None]
    append_turn: Callable[..., dict[str, Any]]
    load_settings: Callable[[], dict[str, Any]]
    write_log: Callable[..., Any]


class ProactiveConversationEventService:
    """Owns persisted unsolicited assistant event admission and idempotency."""

    def __init__(self, ports: ProactiveEventPorts) -> None:
        self.ports = ports

    def append(self, body: dict[str, Any]) -> dict[str, Any]:
        """Persist unsolicited assistant speech so the user's next reply can answer it."""

        event_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("eventId") or ""))[:100]
        reply = str(body.get("reply") or "").strip()
        proactive_kind = str(body.get("proactiveKind") or "").strip().lower()[:40]
        proactive_phase = str(body.get("proactivePhase") or "").strip().lower()[:40]
        unanswered_count = safe_nonnegative_int(body.get("unansweredCount"))
        silence_limit = safe_nonnegative_int(body.get("silenceLimit"))
        requested_directive = self.ports.normalize_proactive_directive(
            body.get("proactiveDirective")
        )
        terminal_pause_authorized = bool(
            requested_directive in {"pause", "pause_until_next_user_message"}
            and proactive_kind == "sulk"
            and proactive_phase == "sulking"
            and silence_limit in {3, 4}
            and unanswered_count >= silence_limit
        )
        proactive_directive = (
            requested_directive
            if terminal_pause_authorized else "unchanged"
        )
        if not event_id or (not reply and not terminal_pause_authorized):
            raise RequestValidationError(
                "invalid_proactive_event",
                "主动互动记录缺少 eventId 或 reply。",
            )
        with self.ports.lock:
            context = self.ports.read_context()
            proactive_control = self.ports.normalize_proactive_control(
                context.get("proactiveControl")
            )
            reminder_context = None
            if proactive_kind == "todo_reminder":
                with self.ports.state_database() as connection:
                    reminder_row = connection.execute(
                        "SELECT task_id FROM todo_reminders WHERE reminder_id = ? AND status IN ('claimed', 'delivered')",
                        (event_id,),
                    ).fetchone()
                    if reminder_row is not None:
                        reminder_context = self.ports.todo_context_for_task(
                            self.ports.read_todo(connection, str(reminder_row["task_id"]))
                        )
            if proactive_control["status"] == "paused" and reminder_context is None:
                self.ports.write_log(
                    "proactive_event_persistence_discarded_while_paused",
                    proactiveEventId=event_id,
                    sourceTurnId=proactive_control["sourceTurnId"],
                )
                return {
                    "ok": True,
                    "deduplicated": False,
                    "discardedAsPaused": True,
                    "proactiveControl": proactive_control,
                    "pendingAssistantQuestion": context.get("pendingAssistantQuestion", {}),
                }
            event_epoch = safe_nonnegative_int(
                body.get("contextEpoch")
                if body.get("contextEpoch") is not None
                else context.get("conversationEpoch")
            )
            if event_epoch != safe_nonnegative_int(context.get("conversationEpoch")):
                return {
                    "ok": True,
                    "deduplicated": False,
                    "discardedAsStale": True,
                    "conversationEpoch": safe_nonnegative_int(
                        context.get("conversationEpoch")
                    ),
                    "pendingAssistantQuestion": context.get("pendingAssistantQuestion", {}),
                }
            if (
                "afterUserTurnId" in body
                and str(body["afterUserTurnId"] or "")
                != str(self.ports.proactive_outreach_context_key(context)[2] or "")
            ):
                return {
                    "ok": True,
                    "discardedAsStale": True,
                    "waitingForUser": bool(context.get("waitingForUser")),
                    "proactiveControl": proactive_control,
                }
            if terminal_pause_authorized and not reply:
                context["waitingForUser"] = True
                self.ports.write_context(context)
                return {
                    "ok": True,
                    "waitingForUser": True,
                    "proactiveControl": proactive_control,
                }
            known_ids = [
                str(item)[:100]
                for item in context.get("proactiveEventIds", [])
                if str(item).strip()
            ]
            if event_id in known_ids:
                return {
                    "ok": True,
                    "deduplicated": True,
                    "pendingAssistantQuestion": context.get("pendingAssistantQuestion", {}),
                }
            result = {
                "reply": reply[:6000],
                "emotion": str(body.get("emotion") or "neutral")[:40],
                "proactiveDirective": proactive_directive,
            }
            if isinstance(body.get("requiresUserResponse"), bool):
                result.update(
                    requiresUserResponse=body["requiresUserResponse"],
                    requiresUserResponseProvided=True,
                )
            if reminder_context is not None:
                result["todoContext"] = reminder_context
            if terminal_pause_authorized:
                result["_proactivePauseReason"] = "unanswered_sequence_complete"
            elif requested_directive != "unchanged":
                self.ports.write_log(
                    "proactive_terminal_pause_rejected",
                    proactiveEventId=event_id,
                    proactiveKind=proactive_kind,
                    proactivePhase=proactive_phase,
                    unansweredCount=unanswered_count,
                    silenceLimit=silence_limit,
                    requestedDirective=requested_directive,
                )
            observation_result = self.ports.screen_capture_tool_result_from_memory(
                body.get("proactiveObservation")
            )
            if observation_result:
                result.update({
                    "toolUsed": "capture_screen",
                    "toolStatus": "ok",
                    "_conversationToolResults": [observation_result],
                })
            context = self.ports.append_turn(
                "",
                result,
                self.ports.load_settings(),
                {
                    "conversationOrigin": "proactive",
                    "proactiveEventId": event_id,
                    "proactiveKind": proactive_kind,
                    "proactiveSubject": str(body.get("proactiveSubject") or "")[:240],
                    "conversationEpoch": event_epoch,
                },
            )
            known_ids.append(event_id)
            context["proactiveEventIds"] = known_ids[-160:]
            self.ports.write_context(context)
        return {
            "ok": True,
            "deduplicated": False,
            "conversationEpoch": safe_nonnegative_int(context.get("conversationEpoch")),
            "proactiveControl": self.ports.normalize_proactive_control(
                context.get("proactiveControl")
            ),
            "pendingAssistantQuestion": context.get("pendingAssistantQuestion", {}),
            "waitingForUser": bool(
                context.get("waitingForUser") or context.get("pendingAssistantQuestion")
            ),
        }


DELIVERED_TURN_EVENT_STAGES = {
    "turn_envelope_validated",
    "audio_started",
    "audio_failed",
    "text_revealed",
    "text_revealed_after_audio_failure",
}


@dataclass(frozen=True)
class DeliveryEventPorts:
    """交付回执只写当前 conversation context，并记录日志。"""

    lock: Any
    read_context: Callable[[], dict[str, Any]]
    write_context: Callable[[dict[str, Any]], dict[str, Any]]
    now_iso: Callable[[], str]
    fingerprint: Callable[..., str]
    write_log: Callable[..., Any]


class DeliveryEventService:
    """Owns bounded, idempotent delivery acknowledgement events."""

    def __init__(self, ports: DeliveryEventPorts) -> None:
        self.ports = ports

    def record(self, body: dict[str, Any]) -> dict[str, Any]:
        """Persist a bounded delivery acknowledgement without storing speech text."""

        request_id = str(body.get("requestId") or "").strip()[:120]
        turn_id = str(body.get("turnId") or "").strip()[:180]
        utterance_id = str(body.get("utteranceId") or "").strip()[:180]
        stage = str(body.get("stage") or "").strip()
        if not request_id or stage not in DELIVERED_TURN_EVENT_STAGES:
            raise RequestValidationError(
                "invalid_delivery_event",
                "交付回执缺少 requestId 或包含未知阶段。",
            )
        context_epoch = safe_nonnegative_int(body.get("contextEpoch"))
        event_id = "delivery_" + self.ports.fingerprint("\0".join([
            request_id,
            turn_id,
            utterance_id,
            stage,
            str(context_epoch),
        ]), length=24)
        event = {
            "eventId": event_id,
            "requestId": request_id,
            "turnId": turn_id,
            "utteranceId": utterance_id,
            "stage": stage,
            "contextEpoch": context_epoch,
            "at": self.ports.now_iso(),
        }
        with self.ports.lock:
            context = self.ports.read_context()
            active_epoch = safe_nonnegative_int(context.get("conversationEpoch"))
            if context_epoch != active_epoch:
                return {
                    "ok": True,
                    "discardedAsStale": True,
                    "activeContextEpoch": active_epoch,
                }
            existing = [
                item for item in context.get("deliveredTurnEvents", [])
                if isinstance(item, dict)
            ]
            if any(item.get("eventId") == event_id for item in existing):
                return {"ok": True, "deduplicated": True, "event": event}
            existing.append(event)
            context["deliveredTurnEvents"] = existing[-240:]
            context["deliveryGeneration"] = safe_nonnegative_int(
                context.get("deliveryGeneration")
            ) + 1
            self.ports.write_context(context)
        self.ports.write_log("delivered_turn_event", **event)
        return {"ok": True, "event": event}
