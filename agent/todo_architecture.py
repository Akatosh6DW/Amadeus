"""Todo command validation and legacy lexical diagnostics for AMADEUS.

Todo state is authoritative in SQLite.  This module deliberately does not open
files, execute reminders, call a model, or render UI. ``classify_*`` remains
for compatibility diagnostics and read-only candidate prefiltering; it has no
authority to route or authorize a write.  New semantic decisions use
``agent.schedule_architecture.ScheduleIntent``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from typing import Any, Literal, Mapping, Sequence


TodoRequestKind = Literal["none", "query", "mutation"]
TodoAction = Literal[
    "list", "create", "update", "complete", "reopen", "delete", "snooze"
]

TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "deleted"})
TODO_PRIORITIES = frozenset({"low", "normal", "high"})
TODO_RECURRENCE_RULES = frozenset({"", "daily"})
TODO_ACTIONS = frozenset({
    "list", "create", "update", "complete", "reopen", "delete", "snooze",
})
TODO_TARGET_STATUSES = frozenset({
    "", "active", "pending", "in_progress", "completed", "deleted", "all",
})

_TODO_NOUN = re.compile(r"待办|任务|todo(?:\s*list)?|日程|提醒|计划事项", re.I)
_TODO_MUTATION = re.compile(
    r"提醒我|到点叫我|记得叫我|加入|添加|新增|创建|记下|安排|设置|"
    r"改到|改成|修改|调整|提前|推迟|延后|稍后提醒|完成了|做完了|打勾|"
    r"重新打开|恢复任务|取消|删除|移除",
    re.I,
)
_TODO_QUERY = re.compile(
    r"(?:还有|有|查看|看看|列出|显示|告诉我).{0,12}(?:待办|任务|日程|安排|计划)|"
    r"(?:待办|任务|日程|安排|计划).{0,12}(?:是什么|有哪些|还有什么|做什么|情况)|"
    r"我(?:今天|明天|上午|下午|晚上|本周|这周)?(?:本来)?(?:要|该|应该|计划)"
    r".{0,10}(?:做什么|干什么|完成什么)",
    re.I,
)
_TODO_NON_COMMITTING = re.compile(
    r"(?:^|[，,。；;\s])(?:先)?(?:别|不要|不用|不必|无需).{0,16}"
    r"(?:提醒|添加|新增|创建|记下|安排|设置|修改|调整|提前|推迟|延后|完成|删除|移除)|"
    r"(?:还没|尚未|并未|没有|未曾|没).{0,12}"
    r"(?:完成|做完|写完|玩完|打完|看完|读完|弄完|搞定|结束|提交)|"
    r"^(?:如果|假如|假设|要是).{0,80}(?:会怎样|会怎么样|怎么样|如何|可以吗|能否|是否)",
    re.I,
)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def classify_todo_request(text: str) -> TodoRequestKind:
    """Classify only the local Todo capability, never public-web freshness.

    False positives on ``query`` are intentionally harmless: they can only load
    the user's local task list.  Mutations still require a model-produced,
    schema-valid command and the side-effect validation gate.
    """

    value = " ".join(str(text or "").split()).strip()
    if not value:
        return "none"
    if _TODO_NON_COMMITTING.search(value):
        return "query" if _TODO_NOUN.search(value) else "none"
    if _TODO_MUTATION.search(value) and (
        _TODO_NOUN.search(value)
        or re.search(r"提醒我|到点叫我|记得叫我|做完了|完成了", value, re.I)
    ):
        return "mutation"
    if _TODO_NOUN.search(value) or _TODO_QUERY.search(value):
        return "query"
    return "none"


def recent_todo_action_context(
    messages: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Return the immediately preceding accepted Todo action, if any.

    Assistant prose is not evidence that a side effect happened. A short
    follow-up such as ``改到两点半`` may inherit a target only from the latest
    assistant message's accepted ``todo_manage`` action receipt.
    """

    for message in reversed(tuple(messages or ())[-8:]):
        if str(message.get("role") or "").strip().lower() != "assistant":
            continue
        containers: list[Mapping[str, Any]] = [message]
        for key in ("meta", "metadata"):
            value = message.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        for container in containers:
            receipt = container.get("actionReceipt")
            if not isinstance(receipt, Mapping):
                continue
            tool = str(
                receipt.get("tool")
                or receipt.get("toolName")
                or container.get("toolUsed")
                or ""
            ).strip()
            status = str(receipt.get("status") or "").strip().lower()
            task_id = str(
                receipt.get("resolvedTarget")
                or receipt.get("taskId")
                or ""
            ).strip()[:100]
            if tool == "todo_manage" and status == "accepted" and task_id:
                return {
                    "taskId": task_id,
                    "actionId": str(receipt.get("actionId") or "").strip()[:100],
                    "taskVersion": _nonnegative_int(
                        receipt.get("taskVersionAfter")
                        or receipt.get("taskVersion")
                        or 0
                    ),
                }
        # Only the nearest assistant turn can establish an implicit target.
        return {}
    return {}


def recent_todo_task_ids(
    messages: Sequence[Mapping[str, Any]] | None,
) -> tuple[str, ...]:
    """Collect authoritative task references carried by recent turn metadata."""

    collected: list[str] = []
    for message in reversed(tuple(messages or ())[-24:]):
        if str(message.get("role") or "").strip().lower() != "assistant":
            continue
        containers: list[Mapping[str, Any]] = [message]
        for key in ("meta", "metadata"):
            value = message.get(key)
            if isinstance(value, Mapping):
                containers.append(value)
        for container in containers:
            todo_context = container.get("todoContext")
            if isinstance(todo_context, Mapping):
                raw_ids = (
                    todo_context.get("recentTaskIds")
                    or todo_context.get("recent_task_ids")
                    or ()
                )
                if isinstance(raw_ids, (list, tuple)):
                    collected.extend(str(item).strip() for item in raw_ids if str(item).strip())
            receipt = container.get("actionReceipt")
            if isinstance(receipt, Mapping) and str(receipt.get("status") or "").lower() == "accepted":
                tool = str(receipt.get("tool") or container.get("toolUsed") or "")
                task_id = str(receipt.get("resolvedTarget") or receipt.get("taskId") or "").strip()
                if tool == "todo_manage" and task_id:
                    collected.append(task_id)
    return tuple(dict.fromkeys(item[:100] for item in collected))[:12]


def classify_todo_request_with_context(
    text: str,
    messages: Sequence[Mapping[str, Any]] | None = None,
) -> TodoRequestKind:
    """Classify direct Todo intent plus an immediate receipt-grounded follow-up."""

    direct = classify_todo_request(text)
    if direct != "none":
        return direct
    value = " ".join(str(text or "").split()).strip()
    if _TODO_MUTATION.search(value) and recent_todo_action_context(messages):
        return "mutation"
    return "none"


def todo_request_targets_recent_receipt(
    text: str,
    messages: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """Whether a mutation explicitly points back to the receipt-bound task."""

    if not recent_todo_action_context(messages):
        return False
    value = " ".join(str(text or "").split()).strip()
    if classify_todo_request(value) == "none" and _TODO_MUTATION.search(value):
        return True
    if classify_todo_request(value) == "mutation" and not _TODO_NOUN.search(value):
        return True
    return bool(re.search(
        r"这(?:个|项)|它|刚才(?:那(?:个|项))?|刚刚(?:那(?:个|项))?|"
        r"上(?:一|面)(?:个|项)?|上述|该(?:任务|待办|日程|提醒)",
        value,
        re.I,
    ))


def todo_mutation_explicitly_requested(
    text: str,
    messages: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    return classify_todo_request_with_context(text, messages) == "mutation"


def todo_mutation_prohibited(text: str) -> bool:
    """Deterministic veto for negated or explicitly hypothetical side effects."""

    value = " ".join(str(text or "").split()).strip()
    return bool(value and _TODO_NON_COMMITTING.search(value))


def parse_due_at(value: Any, *, local_timezone=None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        fallback = local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
        parsed = parsed.replace(tzinfo=fallback)
    return parsed.astimezone(timezone.utc)


def normalized_due_at(value: Any, *, local_timezone=None) -> str:
    parsed = parse_due_at(value, local_timezone=local_timezone)
    return parsed.isoformat(timespec="seconds") if parsed else ""


def clamp_remind_before_minutes(value: Any) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = 0
    return max(0, min(7 * 24 * 60, minutes))


def normalize_priority(value: Any) -> str:
    priority = str(value or "normal").strip().lower()
    return priority if priority in TODO_PRIORITIES else "normal"


@dataclass(frozen=True)
class TodoTargetRef:
    """Model-facing task reference; SQLite remains responsible for resolution."""

    task_id: str = ""
    title: str = ""
    query: str = ""
    status: str = "active"
    due_after: str = ""
    due_before: str = ""
    ordinal: int = 0
    relative: str = ""
    recent_task_ids: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | str | None) -> "TodoTargetRef":
        data: Mapping[str, Any] = (
            value if isinstance(value, Mapping)
            else {"query": str(value or "")}
        )
        status = str(data.get("status") or data.get("scope") or "active").strip().lower()
        if status not in TODO_TARGET_STATUSES:
            status = "active"
        try:
            ordinal = int(data.get("ordinal") or data.get("index") or 0)
        except (TypeError, ValueError):
            ordinal = 0
        recent_raw = data.get("recentTaskIds") or data.get("recent_task_ids") or ()
        recent_ids = tuple(dict.fromkeys(
            str(item).strip()[:100]
            for item in (recent_raw if isinstance(recent_raw, (list, tuple)) else ())
            if str(item).strip()
        ))[:12]
        due_after_input = (
            data.get("dueAfter") if "dueAfter" in data else data.get("due_after")
        )
        due_before_input = (
            data.get("dueBefore") if "dueBefore" in data else data.get("due_before")
        )
        due_after = normalized_due_at(due_after_input)
        due_before = normalized_due_at(due_before_input)
        if due_after_input not in (None, "") and not due_after:
            raise ValueError("todo_invalid_due_after")
        if due_before_input not in (None, "") and not due_before:
            raise ValueError("todo_invalid_due_before")
        return cls(
            task_id=str(data.get("taskId") or data.get("task_id") or "").strip()[:100],
            title=" ".join(str(data.get("title") or "").split()).strip()[:240],
            query=" ".join(str(data.get("query") or data.get("text") or "").split()).strip()[:500],
            status=status,
            due_after=due_after,
            due_before=due_before,
            ordinal=max(0, min(500, ordinal)),
            relative=str(data.get("relative") or data.get("relativeReference") or "").strip()[:80],
            recent_task_ids=recent_ids,
        )

    @property
    def has_selector(self) -> bool:
        return bool(
            self.task_id or self.title or self.query or self.ordinal
            or self.relative or self.recent_task_ids
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "title": self.title,
            "query": self.query,
            "status": self.status,
            "dueAfter": self.due_after,
            "dueBefore": self.due_before,
            "ordinal": self.ordinal,
            "relative": self.relative,
            "recentTaskIds": list(self.recent_task_ids),
        }


@dataclass(frozen=True)
class TodoCommand:
    action: TodoAction
    task_id: str = ""
    title: str = ""
    details: str = ""
    due_at: str = ""
    due_date: str = ""
    timezone_name: str = ""
    remind_before_minutes: int = 0
    priority: str = "normal"
    recurrence_rule: str = ""
    snooze_minutes: int = 0
    snooze_until: str = ""
    scope: str = "active"
    target_ref: TodoTargetRef = TodoTargetRef()
    expected_version: int = 0
    commit_intent: bool = False
    provided_fields: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TodoCommand":
        action = str(value.get("action") or "list").strip().lower()
        if action not in TODO_ACTIONS:
            raise ValueError("invalid_todo_action")
        title = " ".join(str(value.get("title") or "").split()).strip()[:240]
        target_ref = TodoTargetRef.from_mapping(
            value.get("targetRef") or value.get("target_ref")
        )
        task_id = str(
            value.get("taskId") or value.get("task_id") or target_ref.task_id or ""
        ).strip()[:100]
        if action == "create" and not title:
            raise ValueError("todo_title_required")
        if (
            action in {"update", "complete", "reopen", "delete", "snooze"}
            and not task_id
            and not target_ref.has_selector
        ):
            raise ValueError("todo_target_required")
        try:
            snooze_minutes = int(value.get("snoozeMinutes") or value.get("snooze_minutes") or 0)
        except (TypeError, ValueError):
            snooze_minutes = 0
        try:
            expected_version = int(
                value.get("expectedVersion") or value.get("expected_version") or 0
            )
        except (TypeError, ValueError):
            expected_version = 0
        due_input = value.get("dueAt") if "dueAt" in value else value.get("due_at")
        due_at = normalized_due_at(due_input)
        if due_input not in (None, "") and not due_at:
            raise ValueError("todo_invalid_due_at")
        due_date = str(value.get("dueDate", value.get("due_date", "")) or "").strip()
        if due_date:
            try:
                if date.fromisoformat(due_date).isoformat() != due_date:
                    raise ValueError()
            except ValueError:
                raise ValueError("todo_invalid_due_date") from None
        if due_at and due_date:
            raise ValueError("todo_conflicting_due_time")
        recurrence_rule = str(
            value.get("recurrenceRule") or value.get("recurrence_rule") or ""
        ).strip().lower()
        if recurrence_rule == "none":
            recurrence_rule = ""
        if recurrence_rule not in TODO_RECURRENCE_RULES:
            raise ValueError("todo_invalid_recurrence_rule")
        snooze_input = (
            value.get("snoozeUntil")
            if "snoozeUntil" in value else value.get("snooze_until")
        )
        snooze_until = normalized_due_at(snooze_input)
        if snooze_input not in (None, "") and not snooze_until:
            raise ValueError("todo_invalid_snooze_until")
        aliases = {
            "title": ("title",),
            "details": ("details",),
            "dueAt": ("dueAt", "due_at"),
            "dueDate": ("dueDate", "due_date"),
            "timezone": ("timezone", "timezoneName"),
            "remindBeforeMinutes": (
                "remindBeforeMinutes", "remind_before_minutes",
            ),
            "priority": ("priority",),
            "recurrenceRule": ("recurrenceRule", "recurrence_rule"),
            "snoozeMinutes": ("snoozeMinutes", "snooze_minutes"),
            "snoozeUntil": ("snoozeUntil", "snooze_until"),
            "scope": ("scope",),
        }
        provided_fields = tuple(
            public_name
            for public_name, incoming_names in aliases.items()
            if any(incoming_name in value for incoming_name in incoming_names)
        )
        return cls(
            action=action,  # type: ignore[arg-type]
            task_id=task_id,
            title=title,
            details=str(value.get("details") or "").strip()[:4000],
            due_at=due_at,
            due_date=due_date,
            timezone_name=str(
                value.get("timezone") or value.get("timezoneName") or ""
            ).strip()[:80],
            remind_before_minutes=clamp_remind_before_minutes(
                value.get("remindBeforeMinutes", value.get("remind_before_minutes", 0))
            ),
            priority=normalize_priority(value.get("priority")),
            recurrence_rule=recurrence_rule,
            snooze_minutes=max(1, min(30 * 24 * 60, snooze_minutes)) if snooze_minutes else 0,
            snooze_until=snooze_until,
            scope=str(value.get("scope") or "active").strip().lower()[:32],
            target_ref=target_ref,
            expected_version=max(0, expected_version),
            commit_intent=bool(value.get("commitIntent") or value.get("commit_intent")),
            provided_fields=provided_fields,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": self.action,
            "taskId": self.task_id,
            "targetRef": self.target_ref.to_dict(),
            "expectedVersion": self.expected_version,
            "commitIntent": self.commit_intent,
        }
        values = {
            "title": self.title,
            "details": self.details,
            "dueAt": self.due_at,
            "dueDate": self.due_date,
            "timezone": self.timezone_name,
            "remindBeforeMinutes": self.remind_before_minutes,
            "priority": self.priority,
            "recurrenceRule": self.recurrence_rule,
            "snoozeMinutes": self.snooze_minutes,
            "snoozeUntil": self.snooze_until,
            "scope": self.scope,
        }
        if self.action == "create":
            result.update(values)
        else:
            result.update({key: values[key] for key in self.provided_fields})
        return result
