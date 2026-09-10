"""Model-decided schedule intent and deterministic authorization policy.

The language model owns semantic interpretation.  This module owns the small,
typed boundary between that interpretation and local Todo side effects.  It
does not inspect keywords, open SQLite, call tools, or generate user-facing
prose.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal, Mapping, Sequence

from agent.todo_architecture import TodoCommand


ScheduleIntentKind = Literal["none", "query", "mutation", "clarification", "cancel"]
ScheduleRelation = Literal["new", "continue", "unrelated", "cancel"]
ScheduleAuthorization = Literal[
    "none", "explicit", "continued", "proposed", "prohibited"
]
SchedulePolicyAction = Literal[
    "none", "read", "execute", "collect_details", "confirm", "cancel", "reject"
]

SCHEDULE_INTENT_KINDS = frozenset({
    "none", "query", "mutation", "clarification", "cancel",
})
SCHEDULE_RELATIONS = frozenset({"new", "continue", "unrelated", "cancel"})
SCHEDULE_AUTHORIZATIONS = frozenset({
    "none", "explicit", "continued", "proposed", "prohibited",
})


def _bounded_strings(value: Any, *, limit: int, width: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(
        " ".join(str(item or "").split()).strip()[:width]
        for item in value
        if " ".join(str(item or "").split()).strip()
    ))[:limit]


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class ScheduleIntent:
    """One model-produced interpretation of the current schedule activity."""

    kind: ScheduleIntentKind = "none"
    relation: ScheduleRelation = "unrelated"
    authorization: ScheduleAuthorization = "none"
    commands: tuple[TodoCommand, ...] = ()
    missing_fields: tuple[str, ...] = ()
    source_spans: tuple[str, ...] = ()
    summary: str = ""
    confidence: float = 0.0
    source: str = "model"

    @classmethod
    def none(cls, *, source: str = "model") -> "ScheduleIntent":
        return cls(source=source)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        source: str = "model",
    ) -> "ScheduleIntent":
        data = value if isinstance(value, Mapping) else {}
        kind = str(data.get("kind") or "none").strip().lower()
        relation = str(data.get("relation") or (
            "unrelated" if kind == "none" else "new"
        )).strip().lower()
        authorization = str(data.get("authorization") or "none").strip().lower()
        if kind not in SCHEDULE_INTENT_KINDS:
            raise ValueError("invalid_schedule_intent_kind")
        if relation not in SCHEDULE_RELATIONS:
            raise ValueError("invalid_schedule_relation")
        if authorization not in SCHEDULE_AUTHORIZATIONS:
            raise ValueError("invalid_schedule_authorization")
        raw_commands = data.get("commands")
        commands: list[TodoCommand] = []
        if isinstance(raw_commands, Sequence) and not isinstance(raw_commands, (str, bytes)):
            for raw_command in raw_commands[:12]:
                if not isinstance(raw_command, Mapping):
                    raise ValueError("invalid_schedule_command")
                commands.append(TodoCommand.from_mapping(raw_command))
        return cls(
            kind=kind,  # type: ignore[arg-type]
            relation=relation,  # type: ignore[arg-type]
            authorization=authorization,  # type: ignore[arg-type]
            commands=tuple(commands),
            missing_fields=_bounded_strings(
                data.get("missingFields") or data.get("missing_fields"),
                limit=12,
                width=80,
            ),
            source_spans=_bounded_strings(
                data.get("sourceSpans") or data.get("source_spans"),
                limit=8,
                width=240,
            ),
            summary=" ".join(str(data.get("summary") or "").split()).strip()[:500],
            confidence=_confidence(data.get("confidence")),
            source=str(source or "model")[:40],
        )

    @classmethod
    def from_model_payload(cls, payload: Mapping[str, Any] | None) -> "ScheduleIntent":
        """Read the explicit semantic field, with tool calls as migration input.

        The compatibility branch interprets structured Todo tool calls only; it
        never infers schedule activity from user text.
        """

        data = payload if isinstance(payload, Mapping) else {}
        raw_intent = data.get("scheduleIntent") or data.get("schedule_intent")
        if isinstance(raw_intent, Mapping):
            return cls.from_mapping(raw_intent, source="model")

        raw_calls = (
            data.get("toolRequests") or data.get("tool_requests")
            or data.get("calls") or ()
        )
        if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
            return cls.none(source="missing")
        commands: list[TodoCommand] = []
        has_read = False
        for call in raw_calls[:16]:
            if not isinstance(call, Mapping):
                continue
            name = str(call.get("name") or call.get("tool") or "").strip()
            args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
            if name in {"todo_search", "todo_list"}:
                has_read = True
            elif name == "todo_manage":
                commands.append(TodoCommand.from_mapping(args))
        if commands:
            return cls(
                kind="mutation",
                relation="new",
                authorization=(
                    "explicit" if all(command.commit_intent for command in commands)
                    else "none"
                ),
                commands=tuple(commands),
                confidence=0.7,
                source="structured_tool_calls",
            )
        if has_read:
            return cls(
                kind="query",
                relation="new",
                authorization="none",
                confidence=0.7,
                source="structured_tool_calls",
            )
        return cls.none(source="missing")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "relation": self.relation,
            "authorization": self.authorization,
            "commands": [command.to_dict() for command in self.commands],
            "missingFields": list(self.missing_fields),
            "sourceSpans": list(self.source_spans),
            "summary": self.summary,
            "confidence": self.confidence,
            "source": self.source,
        }


@dataclass(frozen=True)
class SchedulePolicyDecision:
    action: SchedulePolicyAction
    allowed: bool
    requires_user_response: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "allowed": self.allowed,
            "requiresUserResponse": self.requires_user_response,
            "reason": self.reason,
        }


def decide_schedule_policy(intent: ScheduleIntent) -> SchedulePolicyDecision:
    """Authorize a typed intent without re-interpreting natural language."""

    if intent.kind == "none" or intent.relation == "unrelated":
        return SchedulePolicyDecision("none", True, reason="no_schedule_activity")
    if intent.kind == "cancel" or intent.relation == "cancel":
        return SchedulePolicyDecision("cancel", True, reason="schedule_activity_cancelled")
    if intent.authorization == "prohibited":
        return SchedulePolicyDecision("reject", False, reason="schedule_write_prohibited")
    if intent.kind == "query":
        return SchedulePolicyDecision("read", True, reason="schedule_read_requested")
    if intent.kind == "clarification" or intent.missing_fields:
        return SchedulePolicyDecision(
            "collect_details", True, True, "schedule_details_missing"
        )
    if intent.kind != "mutation":
        return SchedulePolicyDecision("none", True, reason="no_schedule_mutation")
    if intent.authorization == "proposed":
        return SchedulePolicyDecision(
            "confirm", True, True, "schedule_plan_requires_confirmation"
        )
    if intent.authorization not in {"explicit", "continued"}:
        return SchedulePolicyDecision(
            "confirm", False, True, "schedule_write_not_authorized"
        )
    if not intent.commands:
        return SchedulePolicyDecision(
            "collect_details", True, True, "schedule_commands_missing"
        )
    if any(not command.commit_intent for command in intent.commands):
        return SchedulePolicyDecision(
            "reject", False, True, "schedule_command_missing_commit_intent"
        )
    return SchedulePolicyDecision("execute", True, reason="schedule_write_authorized")


_EXACT_CLOCK_EVIDENCE = re.compile(
    r"(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d|"
    r"(?:[01]?\d|2[0-3]|[零〇一二两三四五六七八九十]{1,3})\s*(?:点|时)"
    r"(?:\s*(?:半|一刻|三刻|[0-5]?\d\s*分))?|"
    r"\d+\s*(?:分钟|小时)后|(?:正午|中午十二点|午夜|零点)",
    re.I,
)


def ground_schedule_temporal_precision(
    intent: ScheduleIntent,
    evidence_text: str,
) -> ScheduleIntent:
    """Reject an invented wall-clock time without changing schedule intent.

    This is a validation boundary, not an intent classifier: it only examines
    already model-authorized daily commands and can downgrade execution to a
    clarification.  It can never create a schedule activity or authorize a write.
    """

    daily_timed_commands = tuple(
        command for command in intent.commands
        if command.recurrence_rule == "daily" and bool(command.due_at)
    )
    if not daily_timed_commands or _EXACT_CLOCK_EVIDENCE.search(
        str(evidence_text or "")
    ):
        return intent
    return ScheduleIntent(
        kind="clarification",
        relation=intent.relation,
        authorization=intent.authorization,
        commands=(),
        missing_fields=tuple(dict.fromkeys((
            *intent.missing_fields,
            "exactClockTime",
        ))),
        source_spans=intent.source_spans,
        summary=intent.summary,
        confidence=intent.confidence,
        source="temporal_grounding_guard",
    )


SCHEDULE_INTENT_PROMPT = (
    "[统一日程语义协议：模型判断，后端授权和执行]\n"
    "每次输出顶层 scheduleIntent。普通无关对话必须是 "
    '{"kind":"none","relation":"unrelated","authorization":"none",'
    '"commands":[],"missingFields":[],"sourceSpans":[],"summary":"",'
    '"confidence":0.0}。'
    "kind 只能是 none/query/mutation/clarification/cancel；relation 只能是 "
    "new/continue/unrelated/cancel；authorization 只能是 "
    "none/explicit/continued/proposed/prohibited。\n"
    "必须结合完整语义和相邻对话判断，不得用‘今天’‘任务’‘完成’等单词直接触发。"
    "用户明确要求新增、修改、完成、恢复、删除或推迟时，kind=mutation、"
    "authorization=explicit；正在补充一个已记录工作流时用 continued。"
    "用户自然反馈自己的对应待办目标已达成，同样属于明确的完成依据，使用 mutation/explicit 并自主调用 complete，"
    "不要求额外的‘标记完成’命令；到场/开始提醒与完成整项工作要按实际目标区分。"
    "仅讨论、假设、建议方案或让你先规划但没有授权写入时用 proposed，禁止调用写工具；"
    "明确否定写入时用 prohibited。\n"
    "commands 使用 todo_manage 的参数结构；一项真实变更对应一个 command，写操作必须 "
    "commitIntent=true。信息足够时，同一个输出必须同时给出 kind=tool_use 和对应 calls；"
    "每天重复的任务用 recurrenceRule=daily；未要求重复的任务不设重复规则。"
    "只有用户指定具体钟点或上下文已有明确时间时，dueAt 才设为带时区 ISO-8601 时间。"
    "没有指定具体钟点（包括只说早上、下午）时，直接建立全天任务，dueAt 为空，"
    "不得猜成8点或午夜，也不要为此追问时间或填写 exactClockTime。"
    "全天任务若指定了日期或开始日期，用 dueDate=YYYY-MM-DD 保留日期，否则 dueDate 为空；"
    "dueAt 与 dueDate 不能同时填写。全天任务在每天首次打开应用时提醒，"
    "创建成功后向用户说明首次打开时提醒，不要把原请求里的早上/下午说成已设置的定时提醒。"
    "每日重复的全天任务完成后当天保持完成，次日重新激活；一次性任务完成后不再激活。"
    "信息不足时 kind=clarification，missingFields 写最少缺失字段，不得声称已写入。"
    "查询日程时 kind=query 并调用 todo_search/todo_list。"
    "只有后端返回 accepted actionReceipt 才代表真实完成。"
)
