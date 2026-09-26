"""模型管线故障的有界恢复执行。

本模块拥有两类恢复的模型请求、一次隔离重入，以及最多一轮工具救援和最终合成。
恢复预算仍保存在原请求 body；取消、工具权限、真实回执和最终持久化由现有 owner
通过分域端口提供，执行器本身不保存会话状态。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol, Sequence

from agent.cognitive_runtime import collect_evidence
from agent.pipeline_rescue import (
    ISOLATED_CONTEXT_RECOVERY_SYSTEM_PROMPT,
    PIPELINE_RESCUE_FINALIZE_PROMPT,
    PIPELINE_RESCUE_SYSTEM_PROMPT,
    IsolatedContextRecovery,
    PipelineRescuePlan,
    normalize_isolated_context_recovery,
    normalize_pipeline_rescue_plan,
)
from agent.tool_architecture import DEFAULT_TOOL_REGISTRY
from backend.chat import upstream_pipeline
from backend.chat.parsing import parse_json_object
from backend.providers.errors import UpstreamProtocolError


class RecoveryExecutionRuntimeOperations(Protocol):
    def features(self) -> Any: ...
    def configured(self, *args: Any, **kwargs: Any) -> Any: ...
    def cancelled(self, *args: Any, **kwargs: Any) -> Any: ...
    def update_progress(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_log(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryExecutionModelOperations(Protocol):
    def send_chat(self, *args: Any, **kwargs: Any) -> Any: ...
    def request_isolated(self, *args: Any, **kwargs: Any) -> Any: ...
    def request_pipeline_plan(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryExecutionPreparationOperations(Protocol):
    def failure_allows(self, *args: Any, **kwargs: Any) -> Any: ...
    def isolated_case(self, *args: Any, **kwargs: Any) -> Any: ...
    def prepare_isolated_retry(self, *args: Any, **kwargs: Any) -> Any: ...
    def pipeline_case(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryExecutionResultOperations(Protocol):
    def normalize_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def isolated_clarification(self, *args: Any, **kwargs: Any) -> Any: ...
    def pipeline_result_from_plan(self, *args: Any, **kwargs: Any) -> Any: ...
    def seal_result(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryExecutionPipelineOperations(Protocol):
    def process_chat_request(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryExecutionToolOperations(Protocol):
    def normalize_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def cognitive_execution_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def constrain_model_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def body_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...
    def execute_agent_tools(self, *args: Any, **kwargs: Any) -> Any: ...
    def turn_time_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def cognitive_turn_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def enforce_external_action_receipt(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RecoveryExecutionServices:
    runtime: RecoveryExecutionRuntimeOperations
    model: RecoveryExecutionModelOperations
    preparation: RecoveryExecutionPreparationOperations
    results: RecoveryExecutionResultOperations
    pipeline: RecoveryExecutionPipelineOperations
    tools: RecoveryExecutionToolOperations


def request_isolated_context_recovery(
    services: RecoveryExecutionServices,
    settings: dict[str, Any],
    case: Mapping[str, Any],
    *,
    request_id: str,
) -> IsolatedContextRecovery | None:
    """请求一次无人格上下文重建，不授予回答或工具执行权。"""

    payload: dict[str, Any] = {
        "model": settings["apiModel"],
        "temperature": 0.0,
        "max_tokens": 900,
        "messages": [
            {"role": "system", "content": ISOLATED_CONTEXT_RECOVERY_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(case, ensure_ascii=False)},
        ],
    }
    if settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in str(settings.get("apiEndpoint") or ""):
        payload["thinking"] = {"type": "disabled"}
    content = services.model.send_chat(
        settings,
        payload,
        timeout=35,
        request_id=request_id,
        purpose="isolated_context_recovery",
    )
    message_ids = [
        str(item.get("messageId") or "")
        for item in case.get("recentDialogue", [])
        if isinstance(item, Mapping) and str(item.get("messageId") or "")
    ]
    return normalize_isolated_context_recovery(
        parse_json_object(content),
        allowed_message_ids=message_ids,
    )


def isolated_context_clarification_result(
    services: RecoveryExecutionServices,
    body: dict[str, Any],
    settings: dict[str, Any],
    user_message: str,
    recovery: IsolatedContextRecovery,
    *,
    request_id: str,
    failure: Exception,
) -> dict[str, Any]:
    """把重建模型确认的缺失输入投影为可持久化澄清结果。"""

    result = services.results.normalize_reply(
        {
            "reply": recovery.clarification_zh or "我好像没太理解你具体指什么，可以再解释一下吗？",
            "spokenTextJa": "ごめん、少し意味をつかみきれなかったわ。もう少し説明してくれる？",
            "emotion": "concerned",
            "gesture": "idle",
            "voiceStyle": "calm",
            "memoryUpdates": [],
            "requiresUserResponse": True,
            "expectedResponseType": "free_text",
            "completedActs": ["request_missing_input"],
            "standalone": True,
            "dependsOnUndeliveredPrefix": False,
            "turnComplete": True,
            "source": "isolated-context-clarification",
        },
        settings=settings,
    )
    result.update(
        {
            "ok": True,
            "requestId": request_id,
            "isolatedContextRecovery": True,
            "pipelineFailureType": type(failure).__name__,
            "contextRecoveryDecision": recovery.decision,
            "contextRecoveryDiagnosis": recovery.diagnosis,
            "contextRecoveryMissing": list(recovery.missing_context),
            "proactiveDirective": "unchanged",
            "proactiveDirectiveProvided": True,
        }
    )
    return services.results.seal_result(
        body,
        user_message,
        result,
        settings=settings,
        request_id=request_id,
        delivery_path="isolated_context_clarification",
    )


def run_isolated_context_recovery(
    services: RecoveryExecutionServices,
    body: dict[str, Any],
    settings: dict[str, Any],
    user_message: str,
    messages: list[dict[str, Any]],
    *,
    request_id: str,
    route_name: str,
    failure: Exception,
    started_at: float,
) -> dict[str, Any] | None:
    """无人格重建当前任务，再以筛选后的同一请求重入常规流水线。"""

    if (
        not services.runtime.features().model_pipeline_rescue_enabled
        or not services.runtime.configured(settings)
        or body.get("_isolatedContextRecoveryAttempted")
        or services.runtime.cancelled(request_id)
        or not services.preparation.failure_allows(failure)
    ):
        return None
    body["_isolatedContextRecoveryAttempted"] = True
    case = services.preparation.isolated_case(
        body,
        user_message,
        messages,
        request_id=request_id,
        route_name=route_name,
        failure=failure,
    )
    services.runtime.update_progress(
        request_id,
        "recovering",
        "刚才的上下文没有接稳，正在只根据近期原始对话重新理清当前任务",
    )
    try:
        recovery = services.model.request_isolated(
            settings,
            case,
            request_id=request_id,
        )
        if recovery is None:
            raise UpstreamProtocolError("isolated context recovery returned invalid JSON")
        services.runtime.write_log(
            "isolated_context_recovery_resolved",
            requestId=request_id,
            decision=recovery.decision,
            taskGoal=recovery.task_goal[:500],
            relevantMessageIds=list(recovery.relevant_message_ids),
            excludedMessageIds=list(recovery.excluded_message_ids),
            missingContext=list(recovery.missing_context),
            pendingActionRelation=recovery.pending_action_relation,
            confidence=recovery.confidence,
            diagnosis=recovery.diagnosis,
            recentDialogueCount=len(case.get("recentDialogue", [])),
            recentDialogueTrimmedCount=int(case.get("recentDialogueTrimmedCount") or 0),
        )
        if recovery.decision == "abort":
            return None
        if recovery.decision == "ask_user":
            return services.results.isolated_clarification(
                body,
                settings,
                user_message,
                recovery,
                request_id=request_id,
                failure=failure,
            )

        retry_body = services.preparation.prepare_isolated_retry(body, case, recovery)
        services.runtime.update_progress(
            request_id,
            "model-generating",
            "已经理清当前任务，正在回到正常回答流程",
        )
        result = services.pipeline.process_chat_request(
            retry_body,
            settings,
            request_id=request_id,
            started_at=started_at,
            preflight=None,
            persona_snapshot=None,
        )
        result.update(
            {
                "isolatedContextRecovery": True,
                "contextRecoveryDecision": recovery.decision,
                "contextRecoveryTaskGoal": recovery.task_goal,
                "contextRecoveryRelevantMessageIds": list(recovery.relevant_message_ids),
                "contextRecoveryExcludedMessageIds": list(recovery.excluded_message_ids),
                "contextRecoveryDiagnosis": recovery.diagnosis,
                "pipelineFailureType": type(failure).__name__,
            }
        )
        return result
    except Exception as recovery_error:
        services.runtime.write_log(
            "isolated_context_recovery_failed",
            requestId=request_id,
            initialErrorType=type(failure).__name__,
            recoveryErrorType=type(recovery_error).__name__,
            recoveryError=str(recovery_error)[:500],
        )
        return None


def request_pipeline_rescue_plan(
    services: RecoveryExecutionServices,
    settings: dict[str, Any],
    case: Mapping[str, Any],
    *,
    request_id: str,
    finalize: bool = False,
) -> PipelineRescuePlan | None:
    """请求一次受限救援计划或执行后的最终合成。"""

    payload: dict[str, Any] = {
        "model": settings["apiModel"],
        "temperature": 0.0,
        "max_tokens": 900 if finalize else 1100,
        "messages": [
            {
                "role": "system",
                "content": (
                    PIPELINE_RESCUE_FINALIZE_PROMPT if finalize else PIPELINE_RESCUE_SYSTEM_PROMPT
                ),
            },
            {
                "role": "user",
                "content": json.dumps(case, ensure_ascii=False),
            },
        ],
    }
    if settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in settings["apiEndpoint"]:
        payload["thinking"] = {"type": "disabled"}
    content = services.model.send_chat(
        settings,
        payload,
        timeout=40,
        request_id=request_id,
        purpose="pipeline_rescue_finalize" if finalize else "pipeline_rescue_plan",
    )
    return normalize_pipeline_rescue_plan(parse_json_object(content))


def pipeline_rescue_result_from_plan(
    services: RecoveryExecutionServices,
    plan: PipelineRescuePlan,
    settings: dict[str, Any],
    body: dict[str, Any],
    user_message: str,
    *,
    request_id: str,
    failure: Exception,
    tool_results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any] | None:
    """把救援计划和真实工具回执投影为统一恢复结果。"""

    if plan.decision == "abort" and not plan.reply_zh:
        return None
    if not plan.reply_zh:
        return None
    result = services.results.normalize_reply(
        {
            "reply": plan.reply_zh,
            "spokenTextJa": plan.spoken_ja,
            "emotion": "concerned",
            "gesture": "idle",
            "voiceStyle": "calm",
            "memoryUpdates": [],
            "requiresUserResponse": plan.requires_user_response,
            "expectedResponseType": plan.expected_response_type,
            "completedActs": ["complete_requested_task"],
            "standalone": True,
            "dependsOnUndeliveredPrefix": False,
            "turnComplete": True,
            "source": "model-pipeline-rescue",
        },
        settings=settings,
    )
    normalized_results = [dict(item) for item in tool_results if isinstance(item, Mapping)]
    if normalized_results:
        result = services.tools.enforce_external_action_receipt(result, normalized_results)
        result["toolResults"] = normalized_results
        result["toolUsed"] = ",".join(
            dict.fromkeys(
                str(item.get("name") or "")
                for item in normalized_results
                if str(item.get("name") or "")
            )
        )
        result["toolStatus"] = (
            "failed" if any(not item.get("ok") for item in normalized_results) else "ok"
        )
        receipts = upstream_pipeline.action_receipts_from_results(normalized_results)
        if receipts:
            result["actionReceipt"] = receipts[-1]
            result["actionReceipts"] = receipts
    result.update(
        {
            "ok": True,
            "requestId": request_id,
            "modelPipelineRescue": True,
            "pipelineFailureType": type(failure).__name__,
            "rescueDecision": plan.decision,
            "rescueDiagnosis": plan.diagnosis,
            "proactiveDirective": "unchanged",
            "proactiveDirectiveProvided": True,
        }
    )
    return services.results.seal_result(
        body,
        user_message,
        result,
        settings=settings,
        request_id=request_id,
        delivery_path="model_pipeline_rescue",
    )


def run_model_pipeline_rescue(
    services: RecoveryExecutionServices,
    body: dict[str, Any],
    settings: dict[str, Any],
    user_message: str,
    messages: list[dict[str, Any]],
    *,
    request_id: str,
    route_name: str,
    failure: Exception,
) -> dict[str, Any] | None:
    """执行一次有界的规划、工具和最终合成救援循环。"""

    if (
        not services.runtime.features().model_pipeline_rescue_enabled
        or not services.runtime.configured(settings)
        or body.get("_modelPipelineRescueAttempted")
        or services.runtime.cancelled(request_id)
        or not services.preparation.failure_allows(failure)
    ):
        return None
    body["_modelPipelineRescueAttempted"] = True
    body["_settingsPermissionSnapshot"] = {
        definition.permission_key: bool(settings.get(definition.permission_key))
        for definition in DEFAULT_TOOL_REGISTRY.all()
        if definition.permission_key
    }
    case = services.preparation.pipeline_case(
        body,
        user_message,
        messages,
        request_id=request_id,
        route_name=route_name,
        failure=failure,
    )
    services.runtime.update_progress(
        request_id,
        "recovering",
        "正常处理链路未能收尾，正在用精简任务快照重新规划一次",
    )
    try:
        plan = services.model.request_pipeline_plan(
            settings,
            case,
            request_id=request_id,
        )
        if plan is None:
            raise UpstreamProtocolError("pipeline rescue returned no valid plan")
        services.runtime.write_log(
            "pipeline_rescue_plan_created",
            requestId=request_id,
            decision=plan.decision,
            diagnosis=plan.diagnosis,
            requestedTools=[item.get("name") for item in plan.calls],
        )
        if plan.decision != "execute_tools":
            return services.results.pipeline_result_from_plan(
                plan,
                settings,
                body,
                user_message,
                request_id=request_id,
                failure=failure,
            )

        raw_requests = services.tools.normalize_tool_requests(
            {
                "toolRequests": list(plan.calls),
                "toolAuthorization": plan.tool_authorization,
                "targetDomain": plan.target_domain,
            }
        )
        execution_state = services.tools.cognitive_execution_state(body)
        constrained = services.tools.constrain_model_tool_requests(
            raw_requests,
            user_message,
            (
                body.get("_contextResolution")
                if isinstance(body.get("_contextResolution"), dict)
                else {}
            ),
            messages,
            body.get("_scheduleDecision"),
            cognitive_state=execution_state,
            evidence=body.get("_cognitiveEvidence") or {},
        )
        active_preflight = services.tools.body_tool_preflight(
            body,
            user_message,
            messages,
            settings=settings,
        )
        tool_results = (
            services.tools.execute_agent_tools(
                constrained,
                settings,
                user_message,
                preflight=active_preflight,
                request_id=request_id,
                cognitive_state=execution_state,
                schedule_decision=body.get("_scheduleDecision"),
                time_context=services.tools.turn_time_context(body, settings),
            )
            if constrained
            else [
                {
                    "name": "pipeline_rescue",
                    "ok": False,
                    "error": "proposed_tool_calls_rejected_by_runtime",
                    "attempted": False,
                }
            ]
        )
        body.setdefault("_rescueToolResults", []).extend(deepcopy(tool_results))
        body.setdefault("_cognitiveEvidence", {}).update(collect_evidence(tool_results))
        if execution_state:
            progressed = services.tools.cognitive_execution_state(body)
            services.tools.cognitive_turn_state(body)["task"] = deepcopy(progressed["task"])
        final_case = {
            **case,
            "rescuePlan": plan.to_dict(),
            "executedToolResults": tool_results,
            "instruction": "根据真实结果完成本轮；不得再调用工具。",
        }
        final_plan = services.model.request_pipeline_plan(
            settings,
            final_case,
            request_id=request_id,
            finalize=True,
        )
        if final_plan is None or final_plan.decision == "execute_tools":
            raise UpstreamProtocolError("pipeline rescue finalizer returned no final answer")
        return services.results.pipeline_result_from_plan(
            final_plan,
            settings,
            body,
            user_message,
            request_id=request_id,
            failure=failure,
            tool_results=tool_results,
        )
    except Exception as rescue_error:
        services.runtime.write_log(
            "pipeline_rescue_failed",
            requestId=request_id,
            initialErrorType=type(failure).__name__,
            rescueErrorType=type(rescue_error).__name__,
            rescueError=str(rescue_error)[:500],
        )
        return None
