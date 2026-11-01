"""Model-decided schedule intent and deterministic authorization policy.

The language model owns semantic interpretation.  This module owns the small,
typed boundary between that interpretation and local Todo side effects.  It
does not inspect keywords, open SQLite, call tools, or generate user-facing
prose.
"""

from __future__ import annotations

from dataclasses import dataclass
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
        raw_calls = (
            data.get("toolRequests") or data.get("tool_requests")
            or data.get("calls") or ()
        )
        commands: list[TodoCommand] = []
        has_read = False
        if isinstance(raw_calls, Sequence) and not isinstance(raw_calls, (str, bytes)):
            for call in raw_calls[:16]:
                if not isinstance(call, Mapping):
                    continue
                name = str(call.get("name") or call.get("tool") or "").strip()
                args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
                if name in {"todo_search", "todo_list"}:
                    has_read = True
                elif name == "todo_manage":
                    # A trusted structured tool call is already the model's
                    # executable plan.  Older model payloads omitted the
                    # optional write marker; preserve explicit false while
                    # defaulting an absent marker to the existing authorized
                    # tool-call behavior.
                    command_args = dict(args)
                    if "commitIntent" not in command_args and "commit_intent" not in command_args:
                        command_args["commitIntent"] = True
                    commands.append(TodoCommand.from_mapping(command_args))
        if commands:
            # The tool plan is the executable model decision.  Prefer it over
            # an incomplete scheduleIntent (for example one that reports an
            # optional field as missing) while retaining commitIntent as the
            # write-authorization boundary below.
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

        raw_intent = data.get("scheduleIntent") or data.get("schedule_intent")
        if isinstance(raw_intent, Mapping):
            return cls.from_mapping(raw_intent, source="model")
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



def ground_schedule_temporal_precision(
    intent: ScheduleIntent,
    evidence_text: str | Mapping[str, Any],
    *,
    time_source: Mapping[str, Any] | None = None,
    source_messages: Any = None,
    current_message: Mapping[str, Any] | None = None,
) -> ScheduleIntent:
    """Keep a model-selected, typed schedule timing intact.

    ``TodoCommand.from_mapping`` already validates ISO timestamps, dates, time
    specifications, and recurrence fields.  Once that structure exists, the
    model's ``dueAt``/``timeSpec`` is the execution plan.  A verbatim
    ``timeSource.quote`` remains available to callers for provenance diagnostics,
    but this helper must not reinterpret it or turn a valid clock into an
    all-day/clarification result because a local regex cannot parse the phrase.
    The keyword arguments remain for API compatibility with existing callers.
    """

    del evidence_text, time_source, source_messages, current_message
    return intent


SCHEDULE_TRIGGER_RULE = (
    "[可组合的延迟执行能力]\n"
    "系统有持久化定时触发：todo_manage 的 timeSpec.kind=after 可安排若干秒后，"
    "不需要用户到时再发消息。用户让你稍后说话、查询、总结或执行已有能力时，"
    "自主组合创建任务与到点执行：onDue 保存完整任务要求（内容、次数、目标等），"
    "title 是短标题；自动执行任务的 details 与 onDue 是同一份要求的兼容字段，不要分别撰写两套内容。"
    "修改时基于候选 instructions 完整更新 onDue 或 details 中一个即可，保留仍然有效的要求。"
    "不要现在执行，也不要把要求退化成提醒用户自己做。"
    "onDue 在到期时交回正常模型和工具链，不限于念提醒；不要说自己没有定时能力。"
    "本轮认知计划以登记成功为完成标准，到期工作由独立触发执行；登记成功只表示已安排，不代表未来工作已经做完。"
    "‘提醒我做某事’仍是普通提醒，onDue 留空；只有用户明确委托你到点做事才填写。"
    "对纯一次性提醒（成功送达本身就是这项提醒的结果）设置 completionMode=delivery；"
    "对需要主人在提醒后亲自完成的待办设置 completionMode=manual。不要依据标题或关键词猜测，"
    "重复任务始终保留 recurring occurrence 并推进下一次。"
    "后端运行时由后台调度触发，无需聊天界面轮询；后端停止或电脑休眠时不能准点执行，"
    "恢复后合并漏跑，不承诺关机后仍能执行。\n"
)

SCHEDULE_TIME_PROMPT = SCHEDULE_TRIGGER_RULE + (
    "[日程时间决策：语义由你判断，日期和时差由后端计算]\n"
    "先结合本轮时间快照、相邻用户原话和真实待办判断用户指的是哪一次发生；不要只按‘明天’字面加一天。"
    "尤其跨过午夜后尚未睡觉，正在洗漱、准备入睡、要求叫醒等语境中的‘明早/明天早上’，"
    "通常指这次睡醒的最近一个早晨，应选择 next_clock。用户明确说日历日期、隔一天或不是这次睡醒时，"
    "按 calendar 保留其日历意思；不要把所有深夜请求都改成今天。确实无法区分才用 clarification 简短确认日期。\n"
    "新建或修改有钟点的提醒优先用 timeSpec，省略 dueAt；它是同一个 todo_manage 调用的参数，无需额外查询工具："
    "{kind:'next_clock',clock:'HH:MM',reason:'简短时间理解依据'} 表示从本轮快照起最近一次该钟点；"
    "没有上午/下午/晚上限定的‘八点四十’属于 12 小时歧义，不能自行改成 20:40。"
    "此时用 {kind:'next_clock_12h',clock:'08:40',meridiem:'unspecified',reason:'未指明上午或下午'}；"
    "兼容调用也可用 kind:'next_clock' 加 meridiem:'unspecified'。后端以冻结锚点选择今天或次日最近的 08:40/20:40。"
    "用户明确说早上、晚上或给出 24 小时钟点时，直接传对应的 08:40/20:40，不填 unspecified。"
    "{kind:'next_weekday',weekday:1,clock:'09:00'} 表示最近一次周一九点，weekday 为 ISO 星期一1至星期日7。"
    "用户按星期指定首次或每周时间时使用 next_weekday，不要手算日期或时差；每周重复再加 recurrenceRule=weekly。"
    "{kind:'calendar',dayOffset:1,clock:'HH:MM'} 表示日历上的明天，或用 date:'YYYY-MM-DD' 指定明确日期；"
    "{kind:'after',seconds:3600} 表示从本轮快照起一小时后；"
    "{kind:'existing_date',clock:'HH:MM'} 表示保留真实目标任务的当地日期，只修改钟点；真实目标即使是全天任务，也以它的 dueDate 作为当地日期。"
    "timezone 沿用任务/本轮本地时区，用户明确要求其他时区才改变；reason 只简述依据，不写推理过程。\n"
    "已有任务出现 dueAt 或 dueDate 时，用户说‘改成九点’、‘把明天那项改到九点’、‘只改时间/日期不要动’等更新语义，优先用 existing_date 保留该任务当地日期；不要因为九点未写上午/下午就改用 next_clock_12h。"
    "next_clock/next_clock_12h 只用于新建任务或用户明确要求重新选择从当前锚点起的下一次发生；它们不能覆盖更新请求中的已有任务日期。"
    "用户纠正日期或给出新的时间关系时重新选择 calendar/next_clock/after。"
    "没有指定钟点的任务继续用 dueDate 或无日期的全天任务，不填 timeSpec，不猜默认钟点。"
    "scheduleIntent.commands、认知计划与 calls 必须使用同一个 timeSpec；不要再手算另一个 dueAt。\n"
    "成功回复以实际回执 timeFacts 为准，明确说出当地月日和钟点，可同时说今天/明天；"
    "不要只重复‘明天九点’让日期歧义延续。涉及还能睡多久或多久后提醒，使用 timeFacts.secondsUntilDue/"
    "hoursUntilDue；不得另按用户的‘明天’重新心算。保留你自己的人格语气，不需要固定模板。"
)


SCHEDULE_INTENT_PROMPT = (
    "[统一日程语义协议：模型判断，后端授权和执行]\n"
    "scheduleIntent 是兼容旧调用的可选语义字段；具体 calls 存在时，以 calls 作为实际执行计划，"
    "不要因为 scheduleIntent 缺省或只写了可选字段就覆盖、拒绝或重复登记 calls。普通无关对话若输出 scheduleIntent，必须是 "
    '{"kind":"none","relation":"unrelated","authorization":"none",'
    '"commands":[],"missingFields":[],"sourceSpans":[],"summary":"",'
    '"confidence":0.0}。'
    "kind 只能是 none/query/mutation/clarification/cancel；relation 只能是 "
    "new/continue/unrelated/cancel；authorization 只能是 "
    "none/explicit/continued/proposed/prohibited。\n"
    "必须结合完整语义和相邻对话判断，不得用‘今天’‘任务’‘完成’等单词直接触发。"
    "用户明确要求新增、修改、完成、恢复、删除或推迟时，kind=mutation、"
    "authorization=explicit；正在补充一个已记录工作流时用 continued。"
    "修改已有任务的标题、详情、到期执行指令 onDue、时间或重复规则，都使用 update 绑定现有 taskId/targetRef；"
    "到期需要联网、发送消息等动作时，onDue 保存完整执行要求，不能只写到 details 后留空 onDue。"
    "提醒的渠道另用 deliveryTarget 保存：桌面用 {channel:desktop}，QQ 用 "
    "{channel:qq,accountId:实际登录账号,targetType:private或group,targetId:实际收件人或群号}。"
    "用户说通过QQ提醒我时，直接用 {channel:qq,recipient:owner}；宿主根据已认证入口或唯一主人配置解析并保存账号，模型无需抄写号码。"
    "通过 QQ 主人入口创建提醒且未指定其他渠道时，默认回主人的私聊；明确要求桌面时选 desktop。"
    "QQ 账号与目标来自可信入口或 qq_status/qq_identity，不凭昵称猜号码。"
    "不要因为修改内容或重复方式而创建第二个任务。"
    "用户自然反馈自己的对应待办目标已达成，同样属于明确的完成依据，使用 mutation/explicit 并自主调用 complete，"
    "不要求额外的‘标记完成’命令；到场/开始提醒与完成整项工作要按实际目标区分。"
    "仅讨论、假设、建议方案或让你先规划但没有授权写入时用 proposed，禁止调用写工具；"
    "明确否定写入时用 prohibited。\n"
    "commands 使用 todo_manage 的参数结构；一项真实变更对应一个 command。信息足够时直接给出对应 calls，"
    "无需为了登记语义再额外生成一份重复 command。commitIntent 可由模型省略，可信宿主在 calls 边界将缺省值填为 true；"
    "显式 commitIntent=false 表示只提出方案，不执行写入。具体 calls 才是执行决定，scheduleIntent 只作兼容语义描述；"
    "需要检查某个任务过去的到期执行结果时，使用 todo_search 的 includeHistory=true 和可选 historyLimit（1到20），"
    "并绑定 taskId 或明确唯一的 targetRef；不要凭旧对话猜测执行是否成功。"
    "重复任务使用 recurrenceRule 的标准别名 daily、weekly、weekdays、monthly，或使用后端支持的 RFC5545 RRULE；"
    "未要求重复的任务不设重复规则。不要自行改写 RRULE，也不要用不受支持的自然语言值。"
    "用户明确要求暂停或恢复已有任务时，使用 todo_manage action=pause 或 action=resume，"
    "并通过 targetRef/taskId 绑定真实任务；暂停是持久任务状态，不是一次性的 snooze。"
    "只有用户指定具体钟点或上下文已有明确时间时才使用 timeSpec；dueAt 保留兼容明确的带时区 ISO-8601 时间。"
    "裸的 1 至 12 点数字即使有分钟也可能是 12 小时歧义；新建或重新选择下一次发生时必须用 next_clock_12h（或 next_clock + meridiem:'unspecified'），"
    "clock 传 01:00 至 12:59 的规范值；不要根据当前时刻、中文‘今天’或个人习惯暗选上午/下午。"
    "若这是已有任务的更新且用户要求保留日期，先按上面的 existing_date 规则保留该任务日期；不要把更新误当成下一次发生。"
    "若使用了具体钟点，timeSource 可附带 quote、sourceMessageId 和 parsedClock（HH:MM）供溯源诊断；"
    "quote 只记录模型判断所依据的原话，不是执行门禁。即使本地无法解析 quote、来源片段不完整或解析值不一致，"
    "只要 dueAt/timeSpec 已是合法的结构化时间，就保持模型给出的时间，不得改成全天或强行追问。"
    "没有指定具体钟点时，由模型依据语义决定全天任务或仍需澄清的必要字段；不得按关键词擅自补成8点、午夜，"
    "也不要把模型已经给出的合法 dueAt/timeSpec 改写成全天。"
    "全天任务若指定了日期或开始日期，用 dueDate=YYYY-MM-DD 保留日期，否则 dueDate 为空；"
    "dueAt 与 dueDate 不能同时填写。全天普通任务在其发生日期的首次打开应用时提醒；"
    "有 onDue 的全天执行任务在发生日期内由运行中的后端触发，不承诺具体钟点。"
    "重复规则决定后续发生日期；不要把原请求里的早上/下午说成已设置的定时钟点。"
    "重复的全天任务完成后保留当天完成状态，在后端计算的下一次发生日期重新激活；一次性任务完成后不再激活。"
    "信息不足时 kind=clarification，missingFields 写最少缺失字段，不得声称已写入。"
    "查询日程时 kind=query 并调用 todo_search/todo_list。"
    "只有后端返回 accepted actionReceipt 才代表真实完成。"
) + "\n" + SCHEDULE_TIME_PROMPT
