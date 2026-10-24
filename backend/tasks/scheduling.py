"""日程工作流的应用编排边界。

``schedule_service`` 仍拥有 SQLite workflow 状态迁移，
``agent.schedule_architecture`` 仍拥有 ScheduleIntent 与 policy 规则；本模块
只组织模型协议恢复、证据后的再判定和执行回执闭环。模型传输、权限提示、
解析、日志与同库连接通过窄端口提供，避免任务服务反向导入 server。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping, Sequence

from agent.schedule_architecture import (
    SCHEDULE_INTENT_PROMPT,
    ScheduleIntent,
    decide_schedule_policy,
    ground_schedule_temporal_precision,
)
from backend.providers.errors import UpstreamProtocolError
from backend.tools.todo_actions import public_todo_candidate
from schedule_service import (
    apply_schedule_transition,
    current_schedule_workflow,
    mark_schedule_execution,
    schedule_workflow_prompt,
)


@dataclass(frozen=True)
class ScheduleServicePorts:
    """日程应用服务所需的 state DB、模型和诊断端口。"""

    state_database: Callable[[], Any]
    send_request: Callable[..., str]
    send_brain: Callable[..., str]
    permission_context_prompt: Callable[..., str]
    parse_json: Callable[[str], dict[str, Any] | None]
    write_log: Callable[..., Any]


class ScheduleWorkflowService:
    """Owns typed schedule protocol recovery and workflow execution reconciliation."""

    def __init__(self, ports: ScheduleServicePorts) -> None:
        self.ports = ports

    def read_current_schedule_workflow(self) -> dict[str, Any]:
        """Return the durable schedule workflow without consulting chat prose."""

        with self.ports.state_database() as connection:
            return current_schedule_workflow(connection)

    def current_schedule_workflow_prompt(self) -> str:
        return schedule_workflow_prompt(self.read_current_schedule_workflow())

    def apply_model_schedule_payload(
        self,
        payload: Mapping[str, Any] | None,
        *,
        request_id: str,
        user_message: str,
    ) -> dict[str, Any]:
        """Validate and persist a model semantic decision, never a prose guess."""

        try:
            intent = ScheduleIntent.from_model_payload(payload)
        except ValueError as error:
            self.ports.write_log(
                "schedule_intent_rejected",
                requestId=str(request_id)[:120],
                error=str(error)[:160],
            )
            return {
                "valid": False,
                "error": str(error),
                "intent": ScheduleIntent.none(source="invalid").to_dict(),
                "policy": {
                    "action": "reject",
                    "allowed": False,
                    "requiresUserResponse": True,
                    "reason": "invalid_schedule_intent",
                },
                "workflow": {},
            }
        grounded_intent = ground_schedule_temporal_precision(intent, user_message)
        if grounded_intent != intent:
            self.ports.write_log(
                "schedule_temporal_precision_rejected",
                requestId=str(request_id)[:120],
                originalKind=intent.kind,
                recurrenceRules=[
                    command.recurrence_rule for command in intent.commands
                ],
                missingFields=list(grounded_intent.missing_fields),
            )
            intent = grounded_intent
        # During the migration window, older providers may omit the new field.
        # Missing data has no authority to create, suspend, resume, or cancel state.
        if intent.source == "missing":
            return {
                "valid": True,
                "omitted": True,
                "intent": intent.to_dict(),
                "policy": {},
                "workflow": self.read_current_schedule_workflow(),
            }
        policy = decide_schedule_policy(intent)
        with self.ports.state_database() as connection:
            workflow = apply_schedule_transition(
                connection,
                intent,
                policy,
                request_id=request_id,
                user_message=user_message,
            )
        result = {
            "valid": True,
            "omitted": False,
            "intent": intent.to_dict(),
            "policy": policy.to_dict(),
            "workflow": workflow,
        }
        self.ports.write_log(
            "schedule_intent_transitioned",
            requestId=str(request_id)[:120],
            intentKind=intent.kind,
            relation=intent.relation,
            authorization=intent.authorization,
            policyAction=policy.action,
            workflowId=str(workflow.get("workflowId") or ""),
            workflowStatus=str(workflow.get("status") or ""),
            commandCount=len(intent.commands),
            source=intent.source,
        )
        return result

    def recover_schedule_intent_with_model(
        self,
        settings: Mapping[str, Any],
        user_message: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_id: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Recover the typed schedule field after a provider protocol fallback.

        This is intentionally model-decided.  It runs only when the primary output
        omitted ``scheduleIntent`` and never infers schedule activity from keywords.
        """

        workflow_prompt = self.current_schedule_workflow_prompt()
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.1,
            "max_tokens": 1500,
            "messages": [
                {"role": "system", "content": SCHEDULE_INTENT_PROMPT},
                {
                    "role": "system",
                    "content": self.ports.permission_context_prompt(
                        dict(settings), body=body,
                    ),
                },
                {"role": "system", "content": (
                    "主回答因为上游协议退化而没有留下 scheduleIntent。现在只做一次独立语义判定；"
                    "不要写角色回复，不要依据单个词触发。输出唯一 JSON 对象，顶层只能有 scheduleIntent。"
                    "todo_manage 支持 recurrenceRule=daily、weekly、weekdays、monthly 以及后端接受的 RFC5545 RRULE；"
                    "用户要求暂停或恢复现有任务时使用 action=pause 或 action=resume，并保留目标绑定。"
                    "没有明确钟点时创建全天任务，dueAt为空，明确的日期保留到dueDate；"
                    "不要索要 exactClockTime，也不要把 recurrenceRule 当成缺失能力。"
                )},
                *([{"role": "system", "content": workflow_prompt}] if workflow_prompt else []),
                *[
                    {
                        "role": "assistant" if item.get("role") == "assistant" else "user",
                        "content": str(item.get("content") or ""),
                    }
                    for item in messages[-8:]
                    if isinstance(item, Mapping) and str(item.get("content") or "").strip()
                ],
                {"role": "user", "content": str(user_message or "")},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in str(settings.get("apiEndpoint") or ""):
            payload["thinking"] = {"type": "disabled"}
        content = self.ports.send_request(
            dict(settings),
            payload,
            timeout=60,
            request_id=request_id,
            purpose="schedule_intent_recovery",
        )
        parsed = self.ports.parse_json(content)
        if not isinstance(parsed, dict) or not isinstance(
            parsed.get("scheduleIntent") or parsed.get("schedule_intent"),
            Mapping,
        ):
            raise UpstreamProtocolError("schedule intent recovery returned no typed intent")
        return parsed

    def resolve_model_schedule_decision(
        self,
        payload: Mapping[str, Any] | None,
        settings: Mapping[str, Any],
        user_message: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        request_id: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply a typed decision, recovering only a missing protocol field."""

        decision = self.apply_model_schedule_payload(
            payload,
            request_id=request_id,
            user_message=user_message,
        )
        if not decision.get("omitted") or isinstance(
            (payload or {}).get("cognitive"), Mapping
        ):
            return decision
        try:
            recovered = self.recover_schedule_intent_with_model(
                settings,
                user_message,
                messages,
                request_id=request_id,
                body=body,
            )
        except Exception as error:
            # A missing protocol field carries no write authority. If the bounded
            # semantic recovery also fails, preserve the conversational answer and
            # keep Todo side effects disabled instead of aborting an unrelated turn.
            # Downstream action-receipt grounding still rejects any false claim that
            # a schedule mutation was completed.
            decision["scheduleProtocolRecoveryFailed"] = True
            decision["recoveryError"] = str(error)[:240]
            self.ports.write_log(
                "schedule_intent_protocol_recovery_failed",
                requestId=str(request_id)[:120],
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
            return decision
        decision = self.apply_model_schedule_payload(
            recovered,
            request_id=request_id,
            user_message=user_message,
        )
        decision["recoveredFromMissingProtocol"] = True
        self.ports.write_log(
            "schedule_intent_protocol_recovered",
            requestId=str(request_id)[:120],
            intentKind=str((decision.get("intent") or {}).get("kind") or "none"),
            policyAction=str((decision.get("policy") or {}).get("action") or "none"),
        )
        return decision

    def repair_reply_for_recovered_schedule_decision(
        self,
        settings: Mapping[str, Any],
        base_messages: Sequence[Mapping[str, Any]],
        prior_content: str,
        schedule_decision: Mapping[str, Any] | None,
        *,
        request_id: str,
        body: dict[str, Any] | None = None,
        user_message: str = "",
    ) -> str:
        """Render a recovered typed decision without inventing a schedule write."""

        decision = schedule_decision if isinstance(schedule_decision, Mapping) else {}
        policy = decision.get("policy") if isinstance(decision.get("policy"), Mapping) else {}
        intent = decision.get("intent") if isinstance(decision.get("intent"), Mapping) else {}
        needs_typed_repair = bool(decision.get("recoveredFromMissingProtocol")) or (
            str(intent.get("source") or "") == "temporal_grounding_guard"
        ) or (body is not None and set(intent.get("missingFields") or []) == {"exactClockTime"})
        if not needs_typed_repair or not policy.get("requiresUserResponse"):
            return prior_content
        recover_all_day = bool(
            body is not None
            and set(intent.get("missingFields") or []) == {"exactClockTime"}
        )
        repair_instruction = (
            SCHEDULE_INTENT_PROMPT + "\n上一版擅自补了具体钟点。用户没有指定钟点并不缺信息："
            "请依据原请求重新生成全天任务的结构化日程及工具调用，dueAt为空，保留明确日期和重复规则。"
            "使用 REPLACE_TOOL_PLAN.value=[{stepId,name,args}] 更新当前尚未执行的认知计划，"
            "calls 的 name/args 必须与新计划一致；不要询问几点，也不要在收到执行回执前声称已创建。"
            "沿用本轮 JSON 协议。\n被拒绝的日程判定："
            if recover_all_day else
            "上一版回复缺少结构化日程字段，独立语义判定已经完成。"
            "严格依据下面的日程判定重写面向用户的最终回复；不得改变判定、不得调用工具、"
            "不得声称已经记住、写入、安排或设置提醒。只询问 missingFields 中仍缺少的最少信息，"
            "并用自然的人格口吻明确说明当前尚未写入。若上一版包含本轮要求的原作绰号反应，"
            "保留该反应并继续完成日程追问。仍按本轮既有 JSON 回复协议输出。\n日程判定："
        )
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.35,
            "max_tokens": 1800,
            "messages": [
                *[dict(item) for item in base_messages],
                {"role": "assistant", "content": str(prior_content or "")},
                {"role": "system", "content": (
                    repair_instruction + json.dumps(
                        decision,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in str(settings.get("apiEndpoint") or ""):
            payload["thinking"] = {"type": "disabled"}
        if recover_all_day:
            repaired = self.ports.send_brain(
                body,
                dict(settings),
                payload,
                timeout=60,
                request_id=request_id,
                purpose="schedule_all_day_repair",
            )
        else:
            repaired = self.ports.send_request(
                dict(settings),
                payload,
                timeout=60,
                request_id=request_id,
                purpose="schedule_reply_repair",
            )
        parsed = self.ports.parse_json(repaired)
        if not isinstance(parsed, dict):
            raise UpstreamProtocolError("schedule reply repair returned no JSON object")
        if recover_all_day:
            body["_scheduleDecision"] = self.apply_model_schedule_payload(
                parsed,
                request_id=request_id,
                user_message=user_message,
            )
        self.ports.write_log(
            "schedule_reply_repaired_from_typed_decision",
            requestId=str(request_id)[:120],
            missingFields=list((decision.get("intent") or {}).get("missingFields") or []),
            policyAction=str(policy.get("action") or ""),
        )
        return repaired

    def reconsider_schedule_intent_after_tool_evidence(
        self,
        settings: Mapping[str, Any],
        user_message: str,
        messages: Sequence[Mapping[str, Any]],
        schedule_decision: Mapping[str, Any] | None,
        tool_results: Sequence[Mapping[str, Any]],
        *,
        request_id: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Let the model revise a schedule decision after authoritative Todo reads."""

        decision = schedule_decision if isinstance(schedule_decision, Mapping) else {}
        intent = decision.get("intent") if isinstance(decision.get("intent"), Mapping) else {}
        if str(intent.get("kind") or "") not in {"clarification", "mutation"}:
            return None
        todo_reads = [
            dict(item) for item in tool_results
            if isinstance(item, Mapping)
            and str(item.get("name") or "") in {"todo_search", "todo_list"}
            and bool(item.get("ok"))
        ]
        if not todo_reads:
            return None
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.1,
            "max_tokens": 1800,
            "messages": [
                {"role": "system", "content": SCHEDULE_INTENT_PROMPT},
                {
                    "role": "system",
                    "content": self.ports.permission_context_prompt(
                        dict(settings), body=body,
                    ),
                },
                {"role": "system", "content": (
                    "你正在对同一轮日程活动做证据后的第二次语义判定。此前本地日程读取已经返回；"
                    "结合用户原话、相邻对话、原判定和真实候选，输出唯一 JSON，顶层只能有 scheduleIntent。"
                    "不得用关键词或相似度自行授权，也不得写角色回复。用户原话若明确要求完成、修改、删除、"
                    "暂停、恢复、推迟或新增，并且读取结果已经唯一确定目标，就应生成带 targetRef.taskId 和 "
                    "commitIntent=true 的 mutation command；只有目标仍不唯一或确实缺少执行字段时才 clarification。"
                    "用户只是查询、讨论、假设或明确禁止写入时，绝不能生成写命令。\n"
                    "原判定：" + json.dumps(decision, ensure_ascii=False, separators=(",", ":")) + "\n"
                    "本地日程证据：" + json.dumps(todo_reads, ensure_ascii=False, separators=(",", ":"))
                )},
                *[
                    {
                        "role": "assistant" if item.get("role") == "assistant" else "user",
                        "content": str(item.get("content") or ""),
                    }
                    for item in messages[-8:]
                    if isinstance(item, Mapping) and str(item.get("content") or "").strip()
                ],
                {"role": "user", "content": str(user_message or "")},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in str(settings.get("apiEndpoint") or ""):
            payload["thinking"] = {"type": "disabled"}
        content = self.ports.send_request(
            dict(settings),
            payload,
            timeout=60,
            request_id=request_id,
            purpose="schedule_evidence_reconsideration",
        )
        parsed = self.ports.parse_json(content)
        if not isinstance(parsed, dict) or not isinstance(
            parsed.get("scheduleIntent") or parsed.get("schedule_intent"), Mapping
        ):
            raise UpstreamProtocolError(
                "schedule evidence reconsideration returned no typed intent"
            )
        revised = self.apply_model_schedule_payload(
            parsed,
            request_id=request_id,
            user_message=user_message,
        )
        revised["reconsideredAfterTodoEvidence"] = True
        self.ports.write_log(
            "schedule_intent_reconsidered_after_todo_evidence",
            requestId=str(request_id)[:120],
            previousKind=str(intent.get("kind") or ""),
            revisedKind=str((revised.get("intent") or {}).get("kind") or ""),
            policyAction=str((revised.get("policy") or {}).get("action") or ""),
            todoReadCount=len(todo_reads),
        )
        return revised

    def finalize_schedule_workflow_from_results(
        self,
        schedule_decision: Mapping[str, Any] | None,
        tool_results: Sequence[Mapping[str, Any]] | None,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        decision = schedule_decision if isinstance(schedule_decision, Mapping) else {}
        policy = decision.get("policy") if isinstance(decision.get("policy"), Mapping) else {}
        workflow = decision.get("workflow") if isinstance(decision.get("workflow"), Mapping) else {}
        workflow_id = str(workflow.get("workflowId") or "")
        if not workflow_id or str(policy.get("action") or "") != "execute":
            return dict(workflow)
        attempts = [
            item for item in (tool_results or ())
            if isinstance(item, Mapping) and item.get("name") == "todo_manage"
        ]
        if not attempts:
            return dict(workflow)
        all_accepted = all(
            item.get("ok")
            and isinstance(item.get("actionReceipt"), Mapping)
            and item["actionReceipt"].get("status") == "accepted"
            for item in attempts
        )
        error = next(
            (str(item.get("error") or "") for item in attempts if not item.get("ok")),
            "",
        )
        needs_clarification = any(bool(item.get("requiresClarification")) for item in attempts)
        execution_context = {
            "toolResults": [
                {
                    "error": str(item.get("error") or "")[:160],
                    "requiresClarification": bool(item.get("requiresClarification")),
                    "candidates": [
                        public_todo_candidate(candidate)
                        for candidate in (
                            item.get("candidates")
                            if isinstance(item.get("candidates"), list)
                            else []
                        )[:8]
                        if isinstance(candidate, Mapping)
                    ],
                }
                for item in attempts
                if not item.get("ok")
            ],
        }
        with self.ports.state_database() as connection:
            updated = mark_schedule_execution(
                connection,
                workflow_id,
                status=(
                    "completed"
                    if all_accepted
                    else "collecting_details"
                    if needs_clarification
                    else "failed"
                ),
                request_id=request_id,
                error=error,
                execution_context=execution_context,
            )
        self.ports.write_log(
            "schedule_workflow_finalized",
            requestId=request_id,
            workflowId=workflow_id,
            status=str(updated.get("status") or ""),
            attemptCount=len(attempts),
            acceptedCount=sum(1 for item in attempts if item.get("ok")),
            error=error[:160],
        )
        return updated
