"""将模型工具请求校验为保序执行计划；不执行工具或持久化结果。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from typing import Any, Callable, Mapping, Sequence
import uuid

from agent.cognitive_runtime import task_tool_violations
from agent.cognitive_tools import canonical_tool_arguments, tool_step_binding
from agent.tool_arguments import tool_argument_error
from agent.tool_architecture import (
    ToolRegistry,
    TurnPreflight,
    build_turn_preflight,
    validate_tool_choice,
)
from agent.turn_runtime import ToolResultEnvelope, utc_now_iso
from backend.tools.trust import TrustDecision, evaluate_tool_trust, normalize_trust_context


SENSOR_TOOL_NAMES = frozenset({"capture_screen", "capture_camera"})
TOOL_AUTHORIZATIONS = frozenset({"none", "read", "write", "observe", "navigate"})
# 已持久化的审计/协议标识，模块迁出后仍沿用原值。
AUDIT_EXECUTOR_ID = "server.execute_agent_tools"


def _record_policy_check(
    check_id: str,
    *,
    blocked: bool,
    request_id: str,
    reason: str,
    logger: Callable[..., Any],
) -> dict[str, Any]:
    """Record a registry decision when the host's check registry is present.

    The protocol branch can be tested without the optional registry module, so
    its absence is diagnostic-only.  The registry never replaces the real
    permission or executor checks below.
    """

    try:
        from agent.check_policy import Action, record
    except ImportError:
        return {}
    try:
        return record(
            check_id,
            Action.BLOCK_TOOL if blocked else Action.LOG,
            logger=logger,
            request_id=request_id,
            reason=reason[:240],
            decisive=False,
            strict=False,
        )
    except Exception:
        # Policy diagnostics cannot turn a valid tool decision into a runtime
        # failure; the host executor remains authoritative.
        return {}


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


def canonical_model_tool_call(request: Mapping[str, Any] | None) -> bool:
    """Return whether *request* is a current structured model call.

    The request array is already the model's typed choice by the time it
    reaches validation.  Requiring the model to repeat that choice in a
    second ``selection.toolAuthorization`` object made ordinary calls fail
    after the semantic protocol had already selected them.  A normalized
    call carries an ``args`` mapping. Optional audit reasons and selection
    metadata must not change whether the requested operation exists.
    """

    return bool(
        isinstance(request, Mapping)
        and str(request.get("name") or request.get("tool") or "").strip()
        and isinstance(request.get("args"), Mapping)
    )


def canonical_model_authorization_allows(
    request: Mapping[str, Any] | None,
    expected: str,
) -> bool:
    """A current call is intent; selection metadata is not host authority.

    Keep the compatibility parameter for old callers. Real permissions and
    active task restrictions are enforced once at execution, regardless of
    what the model wrote in a redundant authorization field.
    """
    return canonical_model_tool_call(request)


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


def _todo_commit_intent(args: Mapping[str, Any]) -> bool:
    """Read the structured command's intent marker, never natural language."""

    return args.get("commitIntent") is True or args.get("commit_intent") is True


def commit_intent_is_true(args: Mapping[str, Any] | None) -> bool:
    """Check the adapter's typed commit marker without truthiness coercion."""

    return bool(isinstance(args, Mapping) and _todo_commit_intent(args))


def schedule_policy_allows_todo_write(
    decision: Mapping[str, Any] | None,
) -> bool:
    """Return whether a supplied schedule decision authorizes a write.

    An absent decision is the legacy/direct-tool route and remains eligible
    for the command's own ``commitIntent`` check.  A present decision must be
    an executing, allowed policy; wording in ``user_question`` is never read.
    """

    if decision is None:
        return True
    if bool(decision.get("omitted")):
        return False
    policy = decision.get("policy")
    return bool(
        isinstance(policy, Mapping)
        and str(policy.get("action") or "") == "execute"
        and policy.get("allowed")
    )


def todo_write_is_structurally_authorized(
    decision: Mapping[str, Any] | None,
    args: Mapping[str, Any],
) -> bool:
    """Require typed schedule policy plus command intent when supplied.

    A direct structured tool call without a schedule decision remains
    compatible with the existing tool route and only needs its command's
    ``commitIntent``.  Once a schedule decision exists, ``omitted`` or a
    non-executing policy carries no write authority.  The user question is
    deliberately absent from this function.
    """

    return schedule_policy_allows_todo_write(decision) and _todo_commit_intent(args)


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
    trust_context: Mapping[str, Any] | None = None,
) -> ToolExecutionPlan:
    """先完成整批策略和逐项校验，再交给执行层处理副作用。

    浏览器待续回调可能清除 pending action，只在浏览器分支且前面的
    显式授权未命中时惰性调用；校验顺序本身也是行为契约。
    """

    decision = schedule_decision
    normalized_trust_context = normalize_trust_context(trust_context)

    seen: set[str] = set()
    active_preflight = preflight or build_turn_preflight(user_question)
    plan = ToolExecutionPlan()
    progress_started = False

    def append_preexecution_rejection(
        *,
        name: str,
        tool_call_id: str,
        error: str,
        reason: str,
        violations: Sequence[str] = (),
    ) -> None:
        """Keep one concrete failure beside the request that caused it."""

        rejected: dict[str, Any] = {
            "name": name,
            "ok": False,
            "error": error,
            "reason": reason,
            "toolCallId": tool_call_id,
            "attempted": False,
        }
        if violations:
            rejected["violations"] = list(violations)
        rejected["_toolResultEnvelope"] = ToolResultEnvelope.from_legacy_result(
            rejected,
            turn_id=f"{request_id}:turn" if request_id else "",
            started_at=utc_now_iso(),
            provenance={"executor": AUDIT_EXECUTOR_ID, "validation": {"allowed": False, "reason": reason}},
        ).to_dict()
        write_log(
            "tool_result_received",
            requestId=request_id,
            toolCallId=tool_call_id,
            toolName=name,
            ok=False,
            error=error,
            validationReason=reason,
        )
        plan.operations.append({"kind": "result", "result": rejected})
    # 一整批待办共享模型所见的后端时间，且时间不进入模型可伪造的参数。
    time_anchor = None
    if settings.get("permissionReadSystemTime") and any(item.get("name") == "todo_manage" for item in requests):
        clock_iso = str((time_context or {}).get("iso") or "")
        time_anchor = datetime.fromisoformat(clock_iso) if clock_iso else datetime.now().astimezone()
        if time_anchor.tzinfo is None:
            raise ValueError("schedule_time_anchor_must_have_timezone")
    for request in requests:
        if not isinstance(request, dict):
            continue
        name = str(request.get("name") or "").strip()
        args = dict(request.get("args")) if isinstance(request.get("args"), dict) else {}
        # The selected call is the execution decision. Older adapters still
        # require this transport flag; fill it at the host boundary rather
        # than asking the model to register the same decision twice.
        if canonical_model_tool_call(request) and name in {
            "todo_manage", "bookmark_manage", "qq_manage", "qq_space_write",
            "qq_remember_alias", "qq_person_memory", "voice_configure",
        }:
            args.setdefault("commitIntent", args.pop("commit_intent", True))
        signature = tool_request_signature({"name": name, "args": args})
        operation_identity = signature
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
        request_violations = task_tool_violations(
            cognitive_state or {}, [request], registry,
        )
        if request_violations:
            append_preexecution_rejection(
                name=name,
                tool_call_id=tool_call_id,
                error="cognitive_task_constraint",
                reason="cognitive_task_constraint",
                violations=request_violations,
            )
            continue
        sensor_explicit = (
            name in SENSOR_TOOL_NAMES
            and (
                structured_sensor_authorized(request)
                or canonical_model_authorization_allows(request, "observe")
            )
        )
        current_model_call = canonical_model_tool_call(request)
        explicit = bool(
            sensor_explicit
            or (
                name not in SENSOR_TOOL_NAMES
                and name in active_preflight.mandatory_capabilities
            )
            or (
                name == "open_browser"
                and (
                    canonical_model_authorization_allows(request, "navigate")
                    or structured_browser_authorized(request)
                    or bool(args.get("bookmarkId"))
                )
            )
            or (
                name in {"list_directory", "read_text_file", "create_directory", "write_text_file"}
                and (
                    canonical_model_authorization_allows(
                        request,
                        "read" if name in {"list_directory", "read_text_file"} else "write",
                    )
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
                name == "qq_identity"
                and (current_model_call or structured_tool_authorization(request) in {"read", "write"})
            )
            or (
                name == "qq_media"
                and args.get("action") == "search"
                and (current_model_call or structured_tool_authorization(request) in {"read", "write"})
            )
            or (
                name == "todo_manage"
                and (
                    (
                        todo_write_is_structurally_authorized(decision, args)
                        and (
                            not isinstance(request.get("selection"), Mapping)
                            or structured_tool_authorization(request) == "write"
                        )
                    )
                    or (
                        canonical_model_authorization_allows(request, "write")
                        and _todo_commit_intent(args)
                    )
                )
            )
            or (
                name in {"bookmark_manage", "qq_manage", "qq_space_write", "qq_remember_alias", "qq_person_memory", "voice_configure"}
                and _todo_commit_intent(args)
                and (
                    canonical_model_authorization_allows(request, "write")
                    or structured_tool_authorization(request) == "write"
                )
            )
        )
        request_preflight = active_preflight
        if sensor_explicit:
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
        argument_error = tool_argument_error(name, args) if validation.allowed else None
        trust_decision = (
            evaluate_tool_trust(name, args, normalized_trust_context)
            if validation.allowed and argument_error is None
            else TrustDecision("allow", "validation_rejected_before_trust")
        )
        permission_check = _record_policy_check(
            "permission.executor",
            blocked=not validation.allowed,
            request_id=request_id,
            reason=validation.reason,
            logger=write_log,
        )
        trust_check = _record_policy_check(
            "permission.untrusted_target",
            blocked=trust_decision.action != "allow",
            request_id=request_id,
            reason=trust_decision.reason,
            logger=write_log,
        )
        write_log(
            "tool_choice_validated",
            requestId=request_id,
            toolCallId=tool_call_id,
            toolName=name,
            validation=validation.to_dict(),
            perceptionTarget=request_preflight.perception.target_domain,
            trustDecision=trust_decision.to_dict(),
            permissionCheck=permission_check,
            trustCheck=trust_check,
        )
        if validation.allowed and argument_error is None and trust_decision.action == "allow":
            if not progress_started:
                update_progress(request_id, "tool-executing", "正在执行本轮已验证的工具调用")
                progress_started = True
            if sensor_explicit and visual_tool_matches_perception_target(
                name, request_preflight.perception.target_domain,
            ):
                plan.validated_observation_targets[tool_call_id] = request_preflight.perception.target_domain
            operation = {
                "kind": "execute", "name": name, "args": args,
                "toolCallId": tool_call_id, "timeAnchor": time_anchor,
                "hostTrust": {
                    "schema": normalized_trust_context['schema'],
                    "decision": trust_decision.to_dict(),
                    "untrustedInput": normalized_trust_context['tainted'],
                },
            }
            if name in {'qq_history', 'qq_manage', 'qq_identity', 'qq_remember_alias', 'qq_person_memory', 'qq_media'}:
                target = ((cognitive_state or {}).get('task') or {}).get('target')
                targets = target if isinstance(target, list) else [target]
                account = next((item.get('account') for item in targets if isinstance(item, Mapping)
                                and item.get('type') == args.get('targetType')
                                and str(item.get('id')) == str(args.get('targetId'))), None)
                if account:
                    operation['qqAccount'] = str(account)
            if name in {'qq_space', 'qq_space_write'} and normalized_trust_context.get('qzoneAccount'):
                operation['qqAccount'] = normalized_trust_context['qzoneAccount']
            plan.operations.append(operation)
            continue
        error_name = (
            "tool_arguments_invalid" if argument_error else
            "target_confirmation_required" if trust_decision.action == "confirm" else
            "unknown_delivery_not_retryable"
            if trust_decision.action == "block"
            and trust_decision.reason == "unknown_delivery_not_retryable" else
            "trust_policy_blocked" if trust_decision.action == "block" else
            "permission_denied" if validation.reason.startswith("permission_denied:") else
            "tool_choice_rejected"
        )
        rejection_reason = (
            trust_decision.reason
            if trust_decision.action in {"confirm", "block"}
            else "Correct the fields listed in argumentError; the tool was not executed."
            if argument_error
            else validation.reason
        )
        rejected = {
            "name": name,
            "ok": False,
            "error": error_name,
            "reason": rejection_reason,
            "requiresClarification": (
                True if trust_decision.action == "confirm" else validation.requires_clarification
            ),
            "toolCallId": tool_call_id,
            "attempted": False,
        }
        if argument_error:
            rejected.update(argumentError=argument_error, attempted=False, requiresClarification=False)
        if validation.reason.startswith("permission_denied:"):
            rejected["permission"] = validation.reason.split(":", 1)[1]
        if trust_decision.action in {"confirm", "block"}:
            rejected["trustDecision"] = trust_decision.to_dict()
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
            trustDecision=trust_decision.to_dict(),
        )
        plan.operations.append({"kind": "result", "result": rejected})
    return plan
