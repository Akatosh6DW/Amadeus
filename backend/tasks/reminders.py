"""已授权定时提醒的租约和回复装配。

先在同一数据库事务中准备全天提醒并认领到期租约，再进行可选模型表达。
quiet/waiting 不取消提醒；生成后重新读取主动状态用于交付，不能使用旧快照解禁闲聊。
失败保留既有本地提醒文案，最终 ack 由客户端交付结果决定。
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from typing import Any, Callable
from todo_store import prepare_all_day_reminders, claim_due_reminder
from backend.chat.reply_fields import SPOKEN_JA_NO_EMOJI_RULE


def scheduled_delivery_status(result: dict[str, Any], model_status: str) -> str:
    """An earlier model success cannot certify a later local fallback reply."""
    if (result.get("emptyReply") or result.get("source") == "local-empty-recovery"
            or result.get("fullReplyRepairSucceeded") is False
            or (result.get("speechUnitContract") or {}).get("allUnitsComplete") is False):
        return "failed"
    if result.get("repairedEmptyReply"):
        # The replacement was generated after the original outcome validation.
        return "unknown"
    return model_status

@dataclass(frozen=True)
class StateOperations:
    state_database: Callable[..., Any]
    read_conversation_context: Callable[..., Any]
    normalize_proactive_control: Callable[..., Any]
    todo_context_for_task: Callable[..., Any]


@dataclass(frozen=True)
class ModelOperations:
    load_settings: Callable[..., Any]
    configured: Callable[..., Any]
    send_upstream_chat_payload: Callable[..., Any]
    normalize_model_reply: Callable[..., Any]
    align_spoken_text_ja: Callable[..., Any]
    read_proactive_persona_prompt: Callable[..., Any]
    execute_scheduled_task: Callable[..., Any] | None = None


class ReminderService:
    def __init__(self, state: StateOperations, model: ModelOperations, *,
                 write_log: Callable[..., None], dialogue_style: Callable[[], str]) -> None:
        self.state = state
        self.model = model
        self.write_log = write_log
        self.dialogue_style = dialogue_style

    def generate_todo_reminder(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Lease an authorized reminder independently of unsolicited-chat silence."""

        del body
        with self.state.state_database() as connection:
            prepare_all_day_reminders(connection)
            reminder = claim_due_reminder(connection)
        if not reminder:
            return {"ok": True, "reminder": None}

        task = reminder.get("task") if isinstance(reminder.get("task"), dict) else {}
        title = str(task.get("title") or "这项任务").strip()
        kind = str(reminder.get("kind") or "due")
        due_at = str(task.get("dueAt") or "")
        fallback_reply = (
            f"“{title}”快到时间了。先把手头这一小段收好，准备开始吧。"
            if kind == "upcoming"
            else f"“{title}”到时间了。别忘了现在处理一下。"
        )
        fallback_ja = (
            f"「{title}」の時間が近いわ。そろそろ準備しなさい。"
            if kind == "upcoming"
            else f"「{title}」の時間よ。忘れずに今やりなさい。"
        )
        if kind == "all_day":
            fallback_reply = f"今天还有一项全天待办：“{title}”。做完后告诉我就好。"
            fallback_ja = f"今日の終日タスクに「{title}」があるわ。終わったら教えてね。"
        result: dict[str, Any] = {
            "reply": fallback_reply,
            "spokenTextJa": fallback_ja,
            "emotion": "concerned",
            "gesture": "explain",
            "voiceStyle": "gentle",
            "voiceTone": "warm",
            "emotionIntensity": 0.62,
            "blushIntensity": 0.0,
            "eyeContact": 0.86,
            "gazeX": 0.0,
            "gazeY": 0.0,
            "tension": 0.18,
        }
        settings = self.model.load_settings()
        if task.get("onDue") and kind != "upcoming":
            result = self._execute_scheduled(reminder, settings, result)
        elif self.model.configured(settings):
            rule = "\n".join([
                "你要生成一条 Amadeus 主动任务提醒。只使用输入 JSON 中的任务事实，不联网、不补充外部事实。",
                "简短自然、有牧濑红莉栖式的关心和轻微督促，但不要羞辱、威胁或编造用户状态。",
                "upcoming 表示快到时间，due 表示已到时间，snoozed 表示用户推迟后的新提醒时间已到。",
                "all_day 表示今天首次打开应用的全天待办提醒，没有指定钟点；不要说到点、逾期或编造具体时间。",
                "只输出 JSON，字段必须包含 reply、spokenTextJa、emotion、gesture、voiceStyle、voiceTone、emotionIntensity、blushIntensity、eyeContact、gazeX、gazeY、tension。",
                "reply 是中文口语；spokenTextJa 是语义完整的日语口语。两者只包含说出口的话。",
                SPOKEN_JA_NO_EMOJI_RULE,
            ])
            payload: dict[str, Any] = {
                "model": settings["apiModel"],
                "temperature": 0.55,
                "max_tokens": 360,
                "messages": [
                    {"role": "system", "content": self.model.read_proactive_persona_prompt()},
                    {"role": "system", "content": self.dialogue_style()},
                    {"role": "system", "content": rule},
                    {"role": "user", "content": json.dumps({
                        "reminderKind": kind,
                        "taskTitle": title,
                        "taskDetails": str(task.get("details") or "")[:800],
                        "dueAt": due_at,
                        "dueDate": str(task.get("dueDate") or ""),
                        "recurrenceRule": str(task.get("recurrenceRule") or ""),
                        "scheduledAt": str(reminder.get("scheduledAt") or ""),
                    }, ensure_ascii=False)},
                ],
            }
            if settings.get("useResponseFormat"):
                payload["response_format"] = {"type": "json_object"}
            if "deepseek.com" in settings["apiEndpoint"]:
                payload["thinking"] = {"type": "disabled"}
            try:
                content = self.model.send_upstream_chat_payload(
                    settings,
                    payload,
                    timeout=35,
                    request_id=str(reminder.get("reminderId") or ""),
                    purpose="todo_reminder_generation",
                )
                candidate = self.model.normalize_model_reply(content, settings=settings)
                if str(candidate.get("reply") or "").strip():
                    result = self.model.align_spoken_text_ja(settings, candidate)
            except Exception as error:
                self.write_log(
                    "todo_reminder_generation_fallback",
                    reminderId=str(reminder.get("reminderId") or ""),
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                )

        latest_proactive_control = self.state.normalize_proactive_control(
            self.state.read_conversation_context().get("proactiveControl")
        )
        self.write_log(
            "todo_reminder_ready",
            reminderId=str(reminder.get("reminderId") or ""),
            proactiveStatus=latest_proactive_control["status"],
            deliveryPriority="scheduled_reminder",
        )

        result.update({
            "ok": True,
            "source": "todo-reminder",
            "deliveryPriority": "scheduled_reminder",
            "interactionKind": "proactive",
            "proactiveEventId": str(reminder.get("reminderId") or ""),
            "proactiveKind": "todo_reminder",
            "proactiveSubject": title[:240],
            "reminderId": str(reminder.get("reminderId") or ""),
            "reminderKind": kind,
            "task": task,
            "todoContext": self.state.todo_context_for_task(task),
            "memoryUpdates": [],
        })
        return result

    def _execute_scheduled(self, reminder: dict[str, Any], settings: dict[str, Any],
                           fallback: dict[str, Any]) -> dict[str, Any]:
        """Execute once per occurrence; speech delivery retries reuse the result.

        The existing reminder payload stores a started marker before tool use.
        A crash with unknown external outcome must not blindly run tools again.
        """
        ident = reminder["reminderId"]
        with self.state.state_database() as connection:
            row = connection.execute("SELECT payload_json FROM todo_reminders WHERE reminder_id=?", (ident,)).fetchone()
            payload = json.loads(row["payload_json"] or "{}")
            execution = payload.get("execution") or {}
            if execution.get("result"):
                return dict(execution["result"])
            if execution.get("status") == "started":
                return {**fallback, "reply": "这项定时任务已经开始执行，但暂时无法确认结果，我不会重复执行。",
                        "spokenTextJa": "この予約タスクは実行を開始したけれど、結果を確認できないわ。重複実行はしない。",
                        "scheduledExecutionStatus": "unknown"}
            if not self.model.configured(settings) or self.model.execute_scheduled_task is None:
                return {**fallback, "reply": "定时任务到时间了，但模型尚未配置，暂时无法执行。",
                        "spokenTextJa": "予約の時間だけれど、モデルが未設定なので実行できないわ。",
                        "scheduledExecutionStatus": "failed"}
            payload["execution"] = {"status": "started"}
            connection.execute("UPDATE todo_reminders SET payload_json=? WHERE reminder_id=?",
                               (json.dumps(payload, ensure_ascii=False), ident))
        self.write_log("scheduled_task_started", reminderId=ident, taskId=reminder["task"]["taskId"])
        try:
            result = self.model.execute_scheduled_task(reminder, settings)
            if not isinstance(result, dict) or not str(result.get("reply") or "").strip():
                raise ValueError("scheduled_task_empty_reply")
            # The normal tool loop owns execution/permission/outcome validation.
            # Only its delivered reply is cached, not private model context.
            result = {key: value for key, value in result.items() if not key.startswith("_")}
            result["scheduledExecutionStatus"] = scheduled_delivery_status(
                result, str(result.get("scheduledExecutionStatus") or "unknown"))
        except Exception as error:
            self.write_log("scheduled_task_failed", reminderId=ident, errorType=type(error).__name__)
            result = {**fallback, "reply": "定时任务已触发，但执行没有正常结束。可能已有部分操作完成，我不会自动重做。",
                      "spokenTextJa": "予約タスクは起動したけれど、正常に終了しなかったわ。一部の操作が完了した可能性があるので、自動では繰り返さない。",
                      "scheduledExecutionStatus": "failed"}
        # A timed event does not represent the user returning to conversation.
        result.update(proactiveDirective="none", proactiveDirectiveProvided=False,
                      requiresUserResponse=False, requiresUserResponseProvided=True, expectedResponseType="")
        result.pop("proactiveControl", None)
        with self.state.state_database() as connection:
            payload["execution"] = {"status": "finished", "result": result}
            connection.execute("UPDATE todo_reminders SET payload_json=? WHERE reminder_id=?",
                               (json.dumps(payload, ensure_ascii=False), ident))
        self.write_log("scheduled_task_finished", reminderId=ident, status=result["scheduledExecutionStatus"])
        return result
