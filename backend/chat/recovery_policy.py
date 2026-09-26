"""模型管线恢复的资格判断与重试输入准备。

本模块只读取当前请求已经冻结的状态，构造无人格控制快照，并在获准后生成干净的
重试 body。它不调用模型、不执行工具、不提交历史，也不持有第二份恢复预算。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
import urllib.error

from agent.action_grounding import PreparedTurn
from agent.cognitive_brain import CognitiveProtocolError
from agent.cognitive_commit import CognitiveCommitError
from agent.context_compatibility import ContextCompatibilityError
from agent.pipeline_rescue import IsolatedContextRecovery
from agent.tool_architecture import DEFAULT_TOOL_REGISTRY
from backend.chat.errors import GroundingGateError
from backend.providers.errors import sanitize_provider_error_message
from backend.runtime.requests import RequestValidationError


class RecoveryStateOperations(Protocol):
    def read_action_receipts(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_pending_action(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RecoveryPreparationServices:
    state: RecoveryStateOperations


@dataclass(frozen=True)
class RecoveryPreparationPolicy:
    turn_context_snapshot_owner: object


def pipeline_rescue_case(
    services: RecoveryPreparationServices,
    body: Mapping[str, Any],
    user_message: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    request_id: str,
    route_name: str,
    failure: Exception,
) -> dict[str, Any]:
    """构造不含人格提示和长期记忆的紧凑控制面快照。"""

    prepared = body.get("_preparedActionTurn")
    prepared_payload = prepared.to_dict() if isinstance(prepared, PreparedTurn) else {}
    tool_results = [
        dict(item)
        for item in (
            body.get("_rescueToolResults")
            if isinstance(body.get("_rescueToolResults"), list)
            else []
        )[-12:]
        if isinstance(item, Mapping)
    ]
    receipts = [
        dict(item)
        for item in services.state.read_action_receipts()
        if isinstance(item, Mapping) and str(item.get("requestId") or "") == request_id
    ][:12]
    allowed_tools = []
    for definition in DEFAULT_TOOL_REGISTRY.all():
        if definition.tool_id in {"read_image", "read_attachment"}:
            # 附件读取已经预执行，通用执行器不能再次播放同一动作。
            continue
        allowed_tools.append(
            {
                "name": definition.tool_id,
                "description": definition.description,
                "inputSchema": dict(definition.input_schema),
                "sideEffect": bool(definition.side_effect),
                "permission": definition.permission_key,
                "permissionGranted": bool(
                    not definition.permission_key
                    or body.get("_settingsPermissionSnapshot", {}).get(definition.permission_key)
                ),
                "targetDomains": list(definition.target_domains),
            }
        )
    ordered_inputs = [
        str(item).strip()
        for item in (body.get("userMessages") if isinstance(body.get("userMessages"), list) else [])
        if str(item).strip()
    ] or [str(user_message or "").strip()]
    dialogue = [
        {
            "role": str(item.get("role") or "")[:20],
            "content": str(item.get("content") or item.get("text") or "")[:1000],
        }
        for item in messages[-6:]
        if isinstance(item, Mapping) and str(item.get("role") or "") in {"user", "assistant"}
    ]
    return {
        "requestId": request_id,
        "route": route_name,
        "orderedUserInputs": ordered_inputs[-6:],
        "recentDialogue": dialogue,
        "resolvedTask": dict(body.get("_artifactTaskContext") or {}),
        "preparedAction": prepared_payload,
        "pendingAction": services.state.current_pending_action(),
        "toolResults": tool_results,
        "actionReceipts": receipts,
        "availableTools": allowed_tools,
        "failure": {
            "type": type(failure).__name__,
            "message": sanitize_provider_error_message(failure)[:500],
        },
        "constraints": {
            "maxToolCalls": 2,
            "maxPlannerCalls": 1,
            "maxFinalizeCalls": 1,
            "mustHonorPermissions": True,
            "mustNotRepeatAcceptedSideEffects": True,
        },
    }


def isolated_context_recovery_case(
    services: RecoveryPreparationServices,
    body: Mapping[str, Any],
    user_message: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    request_id: str,
    route_name: str,
    failure: Exception,
) -> dict[str, Any]:
    """冻结活跃近期对话，供无人格任务重建使用。

    这与 ``pipeline_rescue_case`` 使用不同合同：模型看不到工具目录，也没有回答
    或执行动作的权限；它只能识别当前任务以及属于该任务的对话。
    """

    snapshot = body.get("_turnContextSnapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    context = snapshot.get("_contextState")
    context = context if isinstance(context, Mapping) else {}
    dialogue: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def append_message(message_id: str, role: str, content: str) -> None:
        normalized_role = str(role or "").strip().lower()
        normalized_content = str(content or "").strip()
        if normalized_role not in {"user", "assistant"} or not normalized_content:
            return
        normalized_id = str(message_id or "").strip()[:200]
        identity = (
            (normalized_id, normalized_role)
            if normalized_id
            else (
                normalized_role,
                normalized_content,
            )
        )
        if identity in seen:
            return
        seen.add(identity)
        dialogue.append(
            {
                "messageId": normalized_id or f"recent-{len(dialogue) + 1}",
                "role": normalized_role,
                "content": normalized_content[:1600],
            }
        )

    for index, turn in enumerate(context.get("recentTurns", []), 1):
        if not isinstance(turn, Mapping):
            continue
        turn_id = str(turn.get("turnId") or f"turn-{index}")
        append_message(
            str(turn.get("userMessageId") or f"{turn_id}:user"),
            "user",
            str(turn.get("user") or ""),
        )
        append_message(
            str(turn.get("assistantMessageId") or f"{turn_id}:assistant"),
            "assistant",
            str(turn.get("assistant") or ""),
        )
    for index, item in enumerate(messages, 1):
        if not isinstance(item, Mapping):
            continue
        append_message(
            str(
                item.get("messageId")
                or item.get("message_id")
                or item.get("id")
                or f"request-message-{index}"
            ),
            str(item.get("role") or ""),
            str(item.get("content") or item.get("text") or ""),
        )

    current = snapshot.get("currentUser")
    current = current if isinstance(current, Mapping) else {}
    current_message_id = str(
        current.get("messageId") or body.get("currentUserMessageId") or f"{request_id}:user"
    )[:200]
    append_message(current_message_id, "user", user_message)

    # 保留服务端活跃近期窗口中的全部消息。字符上限用于防止异常客户端提交无界
    # 消息数组；需要裁剪时始终从最旧消息开始。
    original_count = len(dialogue)
    while len(dialogue) > 1 and sum(len(item["content"]) for item in dialogue) > 30000:
        dialogue.pop(0)

    receipts = [
        dict(item)
        for item in services.state.read_action_receipts()
        if isinstance(item, Mapping) and str(item.get("requestId") or "") == request_id
    ][:12]
    prepared = body.get("_preparedActionTurn")
    prepared_payload = prepared.to_dict() if isinstance(prepared, PreparedTurn) else {}
    return {
        "requestId": request_id,
        "route": route_name,
        "currentUserMessageId": current_message_id,
        "currentUserMessage": str(user_message or "").strip(),
        "orderedCurrentTurnInputs": [
            str(item).strip()
            for item in (
                body.get("userMessages") if isinstance(body.get("userMessages"), list) else []
            )
            if str(item).strip()
        ]
        or [str(user_message or "").strip()],
        "recentDialogue": dialogue,
        "recentDialogueTrimmedCount": max(0, original_count - len(dialogue)),
        "replyAnchor": dict(snapshot.get("resolvedAnchor") or {}),
        "pendingAction": services.state.current_pending_action(),
        "preparedAction": prepared_payload,
        "actionReceipts": receipts,
        "failedDerivedHypotheses": {
            "artifactTask": dict(body.get("_artifactTaskContext") or {}),
            "contextControl": dict(snapshot.get("_contextControl") or {}),
            "contextResolution": dict(body.get("_contextResolution") or {}),
        },
        "failure": {
            "type": type(failure).__name__,
            "message": sanitize_provider_error_message(failure)[:500],
        },
    }


def prepare_isolated_context_retry_body(
    policy: RecoveryPreparationPolicy,
    body: Mapping[str, Any],
    case: Mapping[str, Any],
    recovery: IsolatedContextRecovery,
) -> dict[str, Any]:
    """保留原始输入和权限边界，构造干净的重试 body。"""

    recovered = deepcopy(dict(body))
    for key in (
        "_artifactTaskContext",
        "_contextResolution",
        "_discourseLedger",
        "_toolPreflight",
        "_turnExecutionPlan",
        "_researchDecision",
        "_epistemicPlan",
        "_capabilityEnvelope",
        "_scheduleDecision",
        "_dignityAssessment",
        "_processedAttachments",
        "_contextPressure",
        "_researchEscalation",
        "_groundingRecoveryAttempted",
        "_backendSingleTurnRecoveryAttempted",
        "_modelPipelineRescueAttempted",
    ):
        recovered.pop(key, None)

    dialogue = [dict(item) for item in case.get("recentDialogue", []) if isinstance(item, Mapping)]
    current_id = str(case.get("currentUserMessageId") or "")
    relevant = {str(item) for item in recovery.relevant_message_ids if str(item)}
    if current_id:
        relevant.add(current_id)
    excluded = {
        str(item) for item in recovery.excluded_message_ids if str(item) and str(item) != current_id
    }
    selected = [
        item
        for item in dialogue
        if str(item.get("messageId") or "") not in excluded
        and (not relevant or str(item.get("messageId") or "") in relevant)
    ]
    if not selected:
        selected = [item for item in dialogue if str(item.get("messageId") or "") not in excluded][
            -6:
        ]

    # 常规流水线必须看到重建后任务所属的对话，不能继续使用导致解释失败的未筛选
    # 数组；本轮 userMessages 和附件保持不变。
    recovered["messages"] = [
        {
            "id": str(item.get("messageId") or ""),
            "role": str(item.get("role") or ""),
            "content": str(item.get("content") or ""),
        }
        for item in selected
    ]
    snapshot = recovered.get("_turnContextSnapshot")
    if isinstance(snapshot, dict):
        snapshot = deepcopy(snapshot)
        original_context = snapshot.get("_contextState")
        original_context = (
            deepcopy(original_context) if isinstance(original_context, Mapping) else {}
        )
        filtered_turns: list[dict[str, Any]] = []
        for turn in original_context.get("recentTurns", []):
            if not isinstance(turn, Mapping):
                continue
            ids = {
                str(turn.get("userMessageId") or ""),
                str(turn.get("assistantMessageId") or ""),
            }
            if ids & excluded:
                continue
            if relevant and not ids & relevant:
                continue
            filtered_turns.append(dict(turn))
        original_context["recentTurns"] = filtered_turns
        snapshot["_contextState"] = original_context
        snapshot["_resolvedContextState"] = deepcopy(original_context)
        snapshot["_contextControl"] = {}
        snapshot["_turnCoordination"] = {
            "utteranceScope": "isolated_context_recovery",
            "pendingActionRelation": recovery.pending_action_relation,
            "contextDirectiveKind": "",
            "pendingActionMutationOwner": "action_state_machine",
            "contextMayMutatePendingAction": False,
        }
        anchor = snapshot.get("resolvedAnchor")
        if isinstance(anchor, Mapping):
            anchor_ids = {str(item) for item in anchor.get("targetMessageIds", []) if str(item)}
            if anchor_ids and (anchor_ids & excluded or relevant and not anchor_ids & relevant):
                snapshot.pop("resolvedAnchor", None)
        recovered["_turnContextSnapshot"] = snapshot
        recovered["_turnContextSnapshotOwner"] = policy.turn_context_snapshot_owner

    recovered["_artifactTaskContext"] = {
        "relation": "unrelated",
        "artifactIds": [],
        "question": recovery.task_goal,
        "visualAction": "reuse",
        "contextSource": "isolated_context_recovery",
        "dialogue": selected,
    }
    recovered["_recoveredTaskContext"] = {
        **recovery.to_dict(),
        "selectedDialogue": selected,
    }
    recovered["_isolatedContextRecoveryAttempted"] = True
    recovered["_turnCoordination"] = {
        "utteranceScope": "isolated_context_recovery",
        "pendingActionRelation": recovery.pending_action_relation,
        "contextDirectiveKind": "",
        "pendingActionMutationOwner": "action_state_machine",
        "contextMayMutatePendingAction": False,
    }
    return recovered


def pipeline_failure_allows_model_rescue(failure: Exception) -> bool:
    """判断一次新的紧凑模型调用是否可能修复当前轮次。

    HTTPError 继承自 URLError，因此必须先检查可恢复的 400。证据门和请求校验错误
    是控制面主动停止，并非编排损坏，不能用第二次模型判断绕过。
    """

    if isinstance(failure, urllib.error.HTTPError):
        return int(failure.code) == 400
    if isinstance(
        failure,
        (
            RequestValidationError,
            GroundingGateError,
            CognitiveProtocolError,
            CognitiveCommitError,
            ContextCompatibilityError,
        ),
    ):
        # 新模型答案无法修复已经失败的持久化提交。
        return False
    if isinstance(
        failure,
        (urllib.error.URLError, TimeoutError, ConnectionError),
    ):
        return False
    return True
