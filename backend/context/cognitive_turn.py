"""认知轮次提示、修复上下文与最终结果投影。

本模块把请求 body 中已经冻结的认知状态投影为模型提示和交付账务。它不读取
会话文件、不调用模型，也不提交 SQLite；主动联系和 Todo 的当前状态由对应
领域端口按原时机提供。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping, Protocol, Sequence

from agent.cognitive_brain import cognitive_prompt
from agent.cognitive_context import effective_context_controls
from agent.cognitive_runtime import compact_packet
from agent.cognitive_state import stage_revision as stage_cognitive_revision
from agent.cognitive_tools import project_tool_plan_receipts
from agent.dialogue_waiting import (
    question_relation,
    refresh_waiting_context,
    synchronize_response_waiting,
    waiting_packet,
)
from agent.schedule_architecture import SCHEDULE_TIME_PROMPT
from agent.tool_architecture import DEFAULT_TOOL_REGISTRY
from backend.chat.upstream_pipeline import tool_result_was_attempted
from backend.context.turn_snapshot import cognitive_turn_state, frozen_turn_context_state


class CognitiveTurnProactiveProjection(Protocol):
    def for_model(self, context: Mapping[str, Any]) -> dict[str, Any]: ...


class CognitiveTurnTodoProjection(Protocol):
    def reconciliation(self, *args: Any, **kwargs: Any) -> Any: ...
    def prompt(self, value: Any) -> str: ...


@dataclass(frozen=True)
class CognitiveTurnServices:
    proactive: CognitiveTurnProactiveProjection
    todos: CognitiveTurnTodoProjection


@dataclass(frozen=True)
class CognitiveTurnPolicy:
    time_context_owner: object
    session_started_local: datetime
    schedule_time_prompt: str = SCHEDULE_TIME_PROMPT


def compact_execution_history(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """暴露失败轮次的进度，但不能把旧工作恢复为当前任务。"""

    history = []
    for row in rows:
        item = {
            key: deepcopy(row[key])
            for key in ("turnId", "userRequest", "createdAt", "answerCommitted")
            if key in row
        }
        item["executionPlans"] = []
        for task in row.get("executionPlans", []):
            steps = task.get("plan") or []
            item["executionPlans"].append(
                {
                    "taskId": task.get("taskId"),
                    "planRevision": task.get("planRevision"),
                    "goal": str(task.get("goal") or "")[:800],
                    "role": "execution_history_only",
                    "continuationRequiresCurrentUserIntent": True,
                    "steps": [
                        {
                            "stepId": step.get("stepId"),
                            "name": step.get("name"),
                            "status": step.get("status"),
                            "receiptId": step.get("receiptId"),
                            "receiptIds": step.get("receiptIds", []),
                            "argumentsExcerpt": json.dumps(
                                step.get("args") or {}, ensure_ascii=False
                            )[:400],
                        }
                        for step in steps[:16]
                    ],
                    "stepsNotShown": max(0, len(steps) - 16),
                }
            )
        history.append(item)
    return history


def cognitive_turn_prompt(
    services: CognitiveTurnServices,
    policy: CognitiveTurnPolicy,
    body: Mapping[str, Any],
    *,
    analysis_only: bool = False,
) -> str:
    """从本轮冻结状态构造唯一认知决策包。"""

    state = cognitive_turn_state(body)
    snapshot = body.get("_turnContextSnapshot")
    if not state or not isinstance(snapshot, Mapping):
        return ""
    state = project_tool_plan_receipts(state, body.get("_cognitiveEvidence") or {})
    packet = compact_packet(
        state,
        snapshot,
        evidence=body.get("_cognitiveEvidence") or {},
        selected_adaptation_ids=body.get("_cognitiveSelectedAdaptationIds"),
    )
    if isinstance(body.get("_currentStateEvidenceRequirement"), Mapping):
        packet["currentStateEvidenceRequirement"] = deepcopy(
            body["_currentStateEvidenceRequirement"]
        )
    packet["artifactCandidates"] = deepcopy(
        body.get("_cognitiveArtifactCandidates") or []
    )
    packet["executionHistory"] = compact_execution_history(
        body.get("_cognitiveExecutionHistory") or []
    )
    packet["failedRequests"] = deepcopy(body.get("_cognitiveFailedRequests") or [])
    if body.get("_timeContextOwner") is policy.time_context_owner:
        packet["scheduleTimeAnchor"] = deepcopy(body.get("_timeContext") or {})
    packet["proactiveControl"] = services.proactive.for_model(
        frozen_turn_context_state(body) or {}
    )
    packet["dialogueWaiting"] = waiting_packet(
        refresh_waiting_context(
            deepcopy(dict(frozen_turn_context_state(body) or {})),
            now=datetime.now(timezone.utc),
            session_id=policy.session_started_local.isoformat(),
        )
    )
    recent = (frozen_turn_context_state(body) or {}).get("recentTurns", [])
    packet["traceCandidates"] = [
        {
            "turnId": str(item.get("turnId") or ""),
            "user": str(item.get("user") or "")[:240],
            "assistant": str(item.get("assistant") or "")[:320],
        }
        for item in recent[-8:]
        if isinstance(item, Mapping)
    ]
    known_turns = {item["turnId"] for item in packet["traceCandidates"]}
    packet["traceCandidates"].extend(
        {
            "turnId": item["turnId"],
            "user": item["userRequest"][:240],
            "answerCommitted": item["answerCommitted"],
        }
        for item in packet["executionHistory"]
        if item["turnId"] not in known_turns
    )
    pending = (frozen_turn_context_state(body) or {}).get("pendingAction")
    if isinstance(pending, Mapping) and pending:
        packet["pendingAction"] = dict(pending)
        if str(pending.get("actionId") or "") in (
            body.get("_cognitiveOverlay") or {}
        ).get("invalidatedPendingActionIds", []):
            packet["pendingAction"]["status"] = "invalidated_by_current_task_revision"
    prompt = (
        cognitive_prompt(packet, analysis_only=True)
        if analysis_only
        else cognitive_prompt(packet)
    )
    prompt = prompt.replace("\n", "\n" + policy.schedule_time_prompt + "\n", 1)
    if isinstance(body, dict):
        # Todo 状态必须紧邻实际决策包，并位于旧轨迹之后。工具执行后重新读取，
        # 避免把已经完成的任务从缓存的工具前快照再次交给模型。
        if any(
            (entry.get("receipt") or {}).get("name") == "todo_manage"
            and not entry.get("historical")
            for entry in (body.get("_cognitiveEvidence") or {}).values()
        ):
            body.pop("_todoReconciliation", None)
        todo_context = services.todos.reconciliation(
            body,
            str(packet.get("currentUserRequest") or ""),
            body.get("messages") or [],
        )
        todo_prompt = services.todos.prompt(todo_context)
        if todo_prompt:
            prompt += "\n" + todo_prompt
    return prompt


def cognitive_tool_execution_policy(body: Mapping[str, Any]) -> dict[str, Any]:
    """给校验器暴露去重事实，不新增意图判断规则。"""

    attempts = []
    for ref, entry in (body.get("_cognitiveEvidence") or {}).items():
        if not isinstance(entry, Mapping) or entry.get("historical"):
            continue
        receipt = entry.get("receipt")
        if (
            not isinstance(receipt, Mapping)
            or not receipt.get("_cognitiveRequestSignature")
            or not tool_result_was_attempted(receipt)
        ):
            continue
        attempts.append(
            {
                "evidenceRef": ref,
                "toolCallId": receipt.get("toolCallId"),
                "name": receipt.get("name"),
                "canonicalRequest": receipt["_cognitiveRequestSignature"],
                "ok": receipt.get("ok"),
                "error": receipt.get("error"),
                "sameRequestAlreadyAttempted": True,
            }
        )
    return {
        "attempts": attempts,
        "rules": [
            "当前回执中的相同工具和规范化参数不能重发，包括失败的尝试；历史回执不作为本轮重复判定。",
            "浏览器支持认知协议定义的显式 retryOf/actionGroupId 重试；其他工具需要有理由的替代参数或不同证据来源。",
            "工具失败不自动意味着需要重试。依据本轮用户目标决定如实报告、提出替代计划或澄清；不要为重试而随意修改查询。",
            "决定如实报告时使用最终 turn，并在 outcome 用 tool_completion/resultStatus=failed 引用对应失败证据；不能声称拿到了正文。",
            "需要替代操作时按真实目标修订任务计划，然后交正常链路执行；失败记录保留，替代计划不是成功证据。",
        ],
    }


def messages_with_repaired_context(
    body: Mapping[str, Any],
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """替换后端生成的旧提示块，并保留所有原始对话消息。"""

    refreshed = body.get("_cognitiveRefreshedContext")
    route = body.get("_cognitiveRepairRoute") or {}
    if not isinstance(refreshed, Mapping) and not route:
        return list(messages)
    # 这些前缀只标识后端生成的提示块，不是理解用户意图的语言模式。原始对话
    # 绝不能按这些前缀过滤。
    prefixes = ("[认知与任务修正协议]",)
    if isinstance(refreshed, Mapping):
        prefixes += (
            "[长期记忆检索]",
            "[持久会话上下文]",
            "[当前会话短期上下文",
            "[本轮重新检索的上下文数据]",
        )
    if route:
        prefixes += (
            "[统一工具选择前置]",
            "[Capability Envelope：",
            "[后端确定性短期指代解析：",
            "[Turn Contract：",
            "[Claim-Level Fact Policy：",
            "[最近附件任务续接：",
            "[本轮新附件指代绑定：",
            "[修复后的执行上下文]",
        )
    active = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and str(message.get("content") or "").startswith(prefixes)
        )
    ]
    if isinstance(refreshed, Mapping):
        active.append(
            {
                "role": "system",
                "content": "[本轮重新检索的上下文数据]\n"
                + json.dumps(refreshed, ensure_ascii=False),
            }
        )
    if route:
        state = cognitive_turn_state(body)
        active.append(
            {
                "role": "system",
                "content": "[修复后的执行上下文]\n"
                + json.dumps(
                    {
                        "currentUserRequest": str(
                            (
                                (body.get("_turnContextSnapshot") or {}).get(
                                    "currentUserMessage"
                                )
                                or {}
                            ).get("text")
                            or body.get("userMessage")
                            or ""
                        ),
                        "task": state.get("task"),
                        "references": state.get("references"),
                        "contextControls": effective_context_controls(state),
                        "capabilityIndex": [
                            {
                                "id": item.tool_id,
                                "description": item.description,
                                "inputSchema": dict(item.input_schema),
                                "permissionKey": item.permission_key,
                            }
                            for item in DEFAULT_TOOL_REGISTRY.all()
                        ],
                        "repair": route.get("analysis"),
                        "toolExecutionPolicy": cognitive_tool_execution_policy(body),
                    },
                    ensure_ascii=False,
                )
                + "\n以上来自已通过结构校验的本轮私有状态，优先于旧任务推断。工具权限仍由后端逐次校验。历史原话可用于审计，已被排除的记录不能重新作为当前指代；被拒绝的引用不能重新生效。",
            }
        )
    return active


def bind_cognitive_result(body: Mapping[str, Any], result: dict[str, Any]) -> None:
    """把本轮认知覆盖层、核验结果和审计轨迹绑定到交付候选。"""

    snapshot = body.get("_turnContextSnapshot") or {}
    overlay = body.get("_cognitiveOverlay")
    if not isinstance(overlay, Mapping):
        state = cognitive_turn_state(body)
        if not state:
            return
        overlay = stage_cognitive_revision(
            state,
            {},
            turn_id=str(snapshot.get("currentTurnId") or body.get("requestId") or ""),
            root_snapshot_id=str(snapshot.get("rootSnapshotId") or ""),
            user_message=str((snapshot.get("currentUserMessage") or {}).get("text") or ""),
        )
    decision = body.get("_cognitiveDecision") or {}
    result["_cognitiveOverlay"] = deepcopy(dict(overlay))
    if isinstance(body.get("_cognitiveOutcome"), Mapping):
        result["_cognitiveOutcome"] = deepcopy(body["_cognitiveOutcome"])
        result["_cognitiveOutcomeVerification"] = deepcopy(
            body.get("_cognitiveOutcomeVerification") or {}
        )
    result["questionRelation"] = question_relation(
        decision.get("questionRelation")
    ) or question_relation((overlay.get("directive") or {}).get("questionRelation"))
    result.update(synchronize_response_waiting(result))
    result["_cognitiveDigest"] = {
        "turnId": snapshot.get("currentTurnId"),
        "rootSnapshotId": snapshot.get("rootSnapshotId"),
        "understoodUserGoal": str(decision.get("understoodGoal") or "")[:800],
        "referenceFrames": deepcopy(decision.get("referenceFrames") or []),
        "userRequest": str(
            (snapshot.get("currentUserMessage") or {}).get("text") or ""
        )[:800],
        "interactionAct": decision.get("interactionAct", "CHAT"),
        "actions": [
            {
                "tool": item.get("name"),
                "status": "success" if item.get("ok") else "failed",
            }
            for item in result.get("_conversationToolResults", [])
            if isinstance(item, Mapping)
        ],
        "answerCommitments": [str(result.get("reply") or "")[:1200]],
    }
    result["_cognitiveTrace"] = {
        "turnId": snapshot.get("currentTurnId"),
        "rootSnapshotId": snapshot.get("rootSnapshotId"),
        "contextSelection": deepcopy(body.get("_contextResolution") or {}),
        "taskInterpretation": deepcopy(decision),
        "toolPlan": deepcopy(body.get("_turnExecutionPlan") or {}),
        "toolReceipts": deepcopy(result.get("_conversationToolResults") or []),
        "evidenceUsed": list((body.get("_cognitiveEvidence") or {}).keys()),
        "outcome": deepcopy(body.get("_cognitiveOutcome") or {}),
        "attachmentAction": deepcopy(result.get("attachmentAction")),
        "finalAnswer": str(result.get("reply") or ""),
        "repair": deepcopy(body.get("_cognitiveRepairRoute") or {}),
    }
