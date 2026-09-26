"""将模型工具请求校验为保序执行计划；不执行工具或持久化结果。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from typing import Any, Callable, Mapping, Sequence
import uuid

from agent.cognitive_runtime import task_tool_violations
from agent.cognitive_tools import canonical_tool_arguments, tool_step_binding
from agent.todo_architecture import todo_mutation_explicitly_requested, todo_mutation_prohibited
from agent.tool_arguments import file_tool_argument_error
from agent.tool_architecture import (
    ToolRegistry,
    TurnPreflight,
    build_turn_preflight,
    validate_tool_choice,
)
from agent.turn_runtime import ToolResultEnvelope, utc_now_iso


SENSOR_TOOL_NAMES = frozenset({"capture_screen", "capture_camera"})
TOOL_AUTHORIZATIONS = frozenset({"none", "read", "write", "observe", "navigate"})
# 已持久化的审计/协议标识，模块迁出后仍沿用原值。
AUDIT_EXECUTOR_ID = "server.execute_agent_tools"


@dataclass
class ToolExecutionPlan:
    """保留请求序及每个工具调用的认知绑定，供执行层逐项记录。"""

    operations: list[dict[str, Any]] = field(default_factory=list)
    cognitive_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    cognitive_request_signatures: dict[str, str] = field(default_factory=dict)
    validated_observation_targets: dict[str, str] = field(default_factory=dict)
    early_results: list[dict[str, Any]] | None = None


def tool_request_signature(item: dict[str, Any]) -> str:
    """把工具名与规范化参数组成稳定调用身份。"""

    name = str(item.get("name") or "").strip()
    # 省略的 schema 默认值与显式给出的默认值属于同一调用。
    args = canonical_tool_arguments(name, item.get("args"))
    return json.dumps(
        {"name": name, "args": args},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def normalize_tool_authorization(value: Any) -> str:
    authorization = str(value or "").strip().lower()
    return authorization if authorization in TOOL_AUTHORIZATIONS else "none"


def structured_tool_authorization(request: Mapping[str, Any] | None) -> str:
    if not isinstance(request, Mapping):
        return "none"
    selection = request.get("selection")
    if not isinstance(selection, Mapping):
        return "none"
    return normalize_tool_authorization(selection.get("toolAuthorization"))


def structured_sensor_authorized(request: Mapping[str, Any] | None) -> bool:
    if structured_tool_authorization(request) != "observe" or not isinstance(request, Mapping):
        return False
    selection = request.get("selection")
    target = str(selection.get("targetDomain") or "") if isinstance(selection, Mapping) else ""
    name = str(request.get("name") or "")
    return (
        (name == "capture_screen" and target == "screen")
        or (name == "capture_camera" and target == "physical_user")
    )


def structured_browser_authorized(request: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(request, Mapping)
        and str(request.get("name") or "") == "open_browser"
        and structured_tool_authorization(request) == "navigate"
    )


def bind_structured_perception_request(
    preflight: TurnPreflight,
    requests: Sequence[Mapping[str, Any]],
    *,
    source: str,
) -> TurnPreflight:
    """只绑定单一结构化观察目标，不放宽传感器权限。"""

    targets = {
        str((item.get("selection") or {}).get("targetDomain") or "")
        for item in requests
        if structured_sensor_authorized(item)
        and isinstance(item.get("selection"), Mapping)
    }
    if len(targets) != 1:
        return preflight
    target = next(iter(targets))
    perception = replace(
        preflight.perception,
        required=True,
        target_domain=target,
        freshness="current",
        confidence=max(0.9, preflight.perception.confidence),
        ambiguities=(),
    )
    return replace(
        preflight,
        likely_no_tool=False,
        confidence=max(preflight.confidence, perception.confidence),
        perception=perception,
        reasons=tuple(dict.fromkeys((
            *preflight.reasons,
            f"structured_model_perception_target:{source}:{target}",
        ))),
    )


def visual_tool_matches_perception_target(tool_name: str, target_domain: str) -> bool:
    expected = {
        "screen": {"capture_screen"},
        "physical_user": {"capture_camera"},
        "uploaded_image": {"read_image"},
        "unknown_visual": {"capture_screen", "capture_camera", "read_image"},
    }
    return str(tool_name or "") in expected.get(str(target_domain or ""), set())


def build_execution_plan(
    requests: list[dict[str, Any]],
    settings: dict[str, Any],
    user_question: str,
    *,
    preflight: TurnPreflight | None,
    request_id: str,
    cognitive_state: Mapping[str, Any] | None,
    schedule_decision: Mapping[str, Any] | None,
    time_context: Mapping[str, Any] | None,
    registry: ToolRegistry,
    explicitly_requested_sensor_tools: Callable[[str], frozenset[str]],
    is_open_browser_query: Callable[[str], bool],
    pending_browser_action_continuation: Callable[[str], bool],
    is_agent_file_action_query: Callable[[str], bool],
    update_progress: Callable[..., Any],
    write_log: Callable[..., Any],
) -> ToolExecutionPlan:
    """先完成整批策略和逐项校验，再交给执行层处理副作用。

    浏览器待续回调可能清除 pending action，只在浏览器分支且前面的
    显式授权未命中时惰性调用；校验顺序本身也是行为契约。
    """

    decision = schedule_decision or {}
    policy = decision.get("policy") or {}
    if not decision.get("omitted") and policy and (
        policy.get("action") != "execute" or not policy.get("allowed")
    ) and any(item.get("name") == "todo_manage" for item in requests):
        return ToolExecutionPlan(early_results=[
            {"name": str(item.get("name") or ""), "ok": False,
             "error": "schedule_write_not_authorized", "attempted": False}
            for item in requests
        ])
    violations = task_tool_violations(cognitive_state or {}, requests, registry)
    if violations:
        return ToolExecutionPlan(early_results=[
            {"name": str(item.get("name") or ""), "ok": False,
             "error": "cognitive_task_constraint", "violations": violations, "attempted": False}
            for item in requests if isinstance(item, Mapping)
        ])

    seen: set[tuple[str, str]] = set()
    active_preflight = preflight or build_turn_preflight(user_question)
    plan = ToolExecutionPlan()
    # 一整批待办共享模型所见的后端时间，且时间不进入模型可伪造的参数。
    time_anchor = None
    if settings.get("permissionReadSystemTime") and any(item.get("name") == "todo_manage" for item in requests):
        clock_iso = str((time_context or {}).get("iso") or "")
        time_anchor = datetime.fromisoformat(clock_iso) if clock_iso else datetime.now().astimezone()
        if time_anchor.tzinfo is None:
            raise ValueError("schedule_time_anchor_must_have_timezone")
    if requests and request_id:
        update_progress(request_id, "tool-executing", "正在执行本轮已验证的工具调用")

    for request in requests:
        if not isinstance(request, dict):
            continue
        name = str(request.get("name") or "").strip()
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        signature = tool_request_signature({"name": name, "args": args})
        operation_identity = (signature, str(request.get("stepId") or ""))
        if not name or operation_identity in seen:
            continue
        seen.add(operation_identity)
        tool_call_id = str(
            request.get("toolCallId")
            or request.get("callId")
            or f"tool_{uuid.uuid4().hex}"
        )[:100]
        plan.cognitive_request_signatures[tool_call_id] = signature
        binding = tool_step_binding((cognitive_state or {}).get("task") or {}, request)
        if binding:
            plan.cognitive_bindings[tool_call_id] = binding
        sensor_explicit = (
            name in SENSOR_TOOL_NAMES
            and (
                bool(cognitive_state)
                or name in explicitly_requested_sensor_tools(user_question)
                or structured_sensor_authorized(request)
            )
        )
        explicit = bool(
            sensor_explicit
            or (
                name not in SENSOR_TOOL_NAMES
                and name in active_preflight.mandatory_capabilities
            )
            or (
                name == "open_browser"
                and (
                    is_open_browser_query(user_question)
                    or pending_browser_action_continuation(user_question)
                    or structured_browser_authorized(request)
                    or bool(args.get("bookmarkId"))
                )
            )
            or (
                name in {"list_directory", "read_text_file", "create_directory", "write_text_file"}
                and (
                    is_agent_file_action_query(user_question)
                    or (
                        name in {"list_directory", "read_text_file"}
                        and structured_tool_authorization(request) in {"read", "write"}
                    )
                    or (
                        name in {"create_directory", "write_text_file"}
                        and structured_tool_authorization(request) == "write"
                    )
                )
            )
            or (
                name == "todo_manage"
                and not todo_mutation_prohibited(user_question)
                and (
                    todo_mutation_explicitly_requested(user_question)
                    or bool(args.get("commitIntent") or args.get("commit_intent"))
                )
            )
            or (
                name == "bookmark_manage"
                and structured_tool_authorization(request) == "write"
                and bool(args.get("commitIntent") or args.get("commit_intent"))
            )
        )
        request_preflight = active_preflight
        if cognitive_state and sensor_explicit:
            sensor_request = {**request, "selection": {
                **dict(request.get("selection") or {}), "toolAuthorization": "observe",
                "targetDomain": "screen" if name == "capture_screen" else "physical_user",
            }}
            request_preflight = bind_structured_perception_request(
                active_preflight, [sensor_request], source="cognitive_tool_call",
            )
        validation = validate_tool_choice(
            name, request_preflight, settings, explicitly_requested=explicit,
        )
        write_log(
            "tool_choice_validated",
            requestId=request_id,
            toolCallId=tool_call_id,
            toolName=name,
            validation=validation.to_dict(),
            perceptionTarget=request_preflight.perception.target_domain,
        )
        argument_error = file_tool_argument_error(name, args) if validation.allowed else None
        if validation.allowed and argument_error is None:
            if sensor_explicit and visual_tool_matches_perception_target(
                name, request_preflight.perception.target_domain,
            ):
                plan.validated_observation_targets[tool_call_id] = request_preflight.perception.target_domain
            plan.operations.append({
                "kind": "execute", "name": name, "args": args,
                "toolCallId": tool_call_id, "timeAnchor": time_anchor,
            })
            continue
        error_name = "tool_arguments_invalid" if argument_error else (
            "permission_denied"
            if validation.reason.startswith("permission_denied:")
            else "tool_choice_rejected"
        )
        rejected = {
            "name": name,
            "ok": False,
            "error": error_name,
            "reason": "Correct the fields listed in argumentError; the tool was not executed." if argument_error else validation.reason,
            "requiresClarification": validation.requires_clarification,
            "toolCallId": tool_call_id,
            "attempted": False,
        }
        if argument_error:
            rejected.update(argumentError=argument_error, attempted=False, requiresClarification=False)
        if validation.reason.startswith("permission_denied:"):
            rejected["permission"] = validation.reason.split(":", 1)[1]
        rejected["_toolResultEnvelope"] = ToolResultEnvelope.from_legacy_result(
            rejected,
            turn_id=f"{request_id}:turn" if request_id else "",
            started_at=utc_now_iso(),
            provenance={"executor": AUDIT_EXECUTOR_ID, "validation": validation.to_dict()},
        ).to_dict()
        write_log(
            "tool_result_received",
            requestId=request_id,
            toolCallId=tool_call_id,
            toolName=name,
            ok=False,
            error=error_name,
            validationReason=validation.reason,
        )
        plan.operations.append({"kind": "result", "result": rejected})
    return plan
