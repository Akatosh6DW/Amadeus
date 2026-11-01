"""模型驱动工具查询循环。

本模块只持有一次请求中的临时循环状态。对话正文、认知状态、工具回执和
持久化仍由各自现有 owner 管理，并通过按领域分组的端口注入。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
import time
import uuid
from typing import Any, Mapping, Protocol, Sequence

from agent.cognitive_brain import CognitiveProtocolError
from agent.cognitive_runtime import collect_evidence
from agent.cognitive_tools import project_tool_plan_receipts, tool_step_binding
from agent.dialogue_architecture import (
    bind_current_visual_observation_contract,
    policy_prompt,
)
from agent.epistemic_architecture import knowledge_confidence_gate_decision
from agent.research_architecture import EpistemicPlan, capability_envelope
from agent.research_budget import (
    select_tool_budget, sync_tool_usage, record_tool_attempts,
    tool_budget_exhausted, tool_budget_prompt,
)
from agent.tool_architecture import bind_required_capabilities, DEFAULT_TOOL_REGISTRY
from agent.visual_grounding import (
    has_current_visual_evidence as has_typed_visual_evidence,
    visual_evidence_prompt,
)
from backend.chat.parsing import parse_json_object
from backend.context.prompts import core_dialogue_turn_contract_prompt
from backend.context.tool_evidence import tool_context_prompt
from backend.research.intent import model_web_research_requested
from backend.research.planning import normalize_web_research_plan, web_research_anchor
from backend.tools.outcomes import effective_tool_results
from backend.tools.validation import (
    bind_structured_perception_request,
    tool_request_signature,
)


_TERMINAL_OUTCOME_STATUSES = frozenset(
    {"completed", "partial", "needs_input", "failed"}
)
_TERMINAL_FORMAT_REPAIR_LIMIT = 1
_EXPLICIT_ACTION_FAILURE_STATUSES = frozenset({"failed", "rejected"})


def _tool_result_was_attempted(item: Mapping[str, Any]) -> bool:
    """Keep execution identity separate from admission feedback.

    ``attempted=false`` is an explicit pre-execution rejection.  It may be
    retried after the model repairs structured arguments/authorization, while
    an adapter result without that marker remains a real or unknown attempt
    and therefore keeps the normal idempotency barrier.
    """

    return item.get("attempted") is not False


def _tool_request_retry_signature(item: Mapping[str, Any]) -> str:
    """Identify an admission retry, including structured selection metadata.

    The canonical tool signature intentionally describes executable arguments;
    ``selection`` is authorization metadata and is therefore not part of the
    executed side-effect identity.  It still matters when deciding whether a
    rejected request made progress, so a correction from ``none`` to ``write``
    must not be mistaken for the exact same failed request.
    """

    selection = item.get("selection")
    selection_payload = dict(selection) if isinstance(selection, Mapping) else {}
    return (
        f"{tool_request_signature(dict(item))}|selection="
        f"{json.dumps(selection_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
    )


def _action_receipt(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the host-owned action receipt, if this result has one."""

    receipt = item.get("actionReceipt")
    return receipt if isinstance(receipt, Mapping) else None


def _action_id(item: Mapping[str, Any]) -> str:
    """Read a retry identity only from a structured host field."""

    receipt = _action_receipt(item)
    value = receipt.get("actionId") if receipt is not None else item.get("actionId")
    return str(value or "").strip()


def _explicit_action_failure(item: Mapping[str, Any]) -> bool:
    """Identify a retryable failure without interpreting free-form error text.

    An execution barrier remains in place for every attempted result.  This
    predicate is deliberately narrower: only a host status that explicitly
    says ``failed``/``rejected`` may create a retry candidate.  An action
    identity is checked by the caller, so transport-unknown and bare error
    strings cannot authorize a resend.
    """

    if not _tool_result_was_attempted(item) or item.get("ok") is True:
        return False
    receipt = _action_receipt(item)
    if receipt is not None:
        return str(receipt.get("status") or "").strip().lower() in _EXPLICIT_ACTION_FAILURE_STATUSES
    return any(
        str(item.get(field) or "").strip().lower() in _EXPLICIT_ACTION_FAILURE_STATUSES
        for field in ("status", "executionStatus", "deliveryState")
    )


def _retry_relation_declared(item: Mapping[str, Any]) -> bool:
    selection = item.get("selection")
    return isinstance(selection, Mapping) and selection.get("actionRelation") == "retry"


def _retry_target_action_id(item: Mapping[str, Any]) -> str:
    """Get a model-declared retry target, requiring the structured relation."""

    if not _retry_relation_declared(item):
        return ""
    selection = item.get("selection")
    selection_target = str(selection.get("retryOfActionId") or "").strip()
    args = item.get("args")
    args_target = str(args.get("_retryOfActionId") or "").strip() if isinstance(args, Mapping) else ""
    # Browser constraints copy the target into transport args.  If both
    # representations exist, a mismatch is not a legal retry identity.
    if selection_target and args_target and selection_target != args_target:
        return ""
    return selection_target or args_target


def _plan_revision(item: Mapping[str, Any]) -> str | None:
    """Read the current cognitive plan revision from known structured fields."""

    binding = item.get("_cognitiveStepBinding")
    if isinstance(binding, Mapping) and binding.get("planRevision") is not None:
        value = binding.get("planRevision")
    else:
        value = item.get("planRevision", item.get("_cognitivePlanRevision"))
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _request_result_pair_indices(
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> list[tuple[int, Mapping[str, Any], Mapping[str, Any]]]:
    """Pair tool requests with their receipts without relying on list order."""

    request_rows = [item for item in requests if isinstance(item, Mapping)]
    result_rows = [item for item in results if isinstance(item, Mapping)]
    unused = set(range(len(request_rows)))
    by_call_id: dict[str, list[int]] = {}
    by_signature: dict[str, list[int]] = {}
    for index, request in enumerate(request_rows):
        call_id = str(request.get("toolCallId") or request.get("callId") or "").strip()
        if call_id:
            by_call_id.setdefault(call_id, []).append(index)
        signature = tool_request_signature(dict(request))
        by_signature.setdefault(signature, []).append(index)

    pairs: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    for result in result_rows:
        index: int | None = None
        result_call_id = str(result.get("toolCallId") or result.get("callId") or "").strip()
        if result_call_id:
            index = next(
                (candidate for candidate in by_call_id.get(result_call_id, ()) if candidate in unused),
                None,
            )
        stored_signature = str(result.get("_cognitiveRequestSignature") or "").strip()
        if index is None and stored_signature:
            index = next(
                (candidate for candidate in by_signature.get(stored_signature, ()) if candidate in unused),
                None,
            )
        if index is None:
            result_name = str(result.get("name") or "").strip()
            candidates = [candidate for candidate in unused
                          if str(request_rows[candidate].get("name") or "").strip() == result_name]
            # A name alone cannot identify which of several writes was rejected.
            # Keep ambiguous outcomes behind the execution barrier.
            index = candidates[0] if len(candidates) == 1 else None
        if index is None:
            continue
        unused.remove(index)
        pairs.append((index, request_rows[index], result))
    return pairs


def _request_result_pairs(
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Return matched request/receipt rows for admission diagnostics."""

    return [
        (request, result)
        for _, request, result in _request_result_pair_indices(requests, results)
    ]


def _attempted_request_signatures(
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Keep attempted or receipt-missing calls behind the idempotency barrier.

    Only a matched receipt with the explicit boolean ``attempted=false`` proves
    that no execution occurred.  A missing receipt is an unknown outcome and
    must therefore remain blocked, including when only part of a batch has a
    receipt.
    """

    request_rows = [item for item in requests if isinstance(item, Mapping)]
    pairs = _request_result_pair_indices(request_rows, results)
    matched_indexes = {index for index, _, _ in pairs}
    signatures = {
        tool_request_signature(dict(request))
        for _, request, result in pairs
        if _tool_result_was_attempted(result)
        and str(request.get("name") or "").strip()
    }
    signatures.update(
        tool_request_signature(dict(request))
        for index, request in enumerate(request_rows)
        if index not in matched_indexes
        and str(request.get("name") or "").strip()
    )
    return signatures


def _rejected_request_retry_signatures(
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Track exact admission failures without treating them as execution."""

    return {
        _tool_request_retry_signature(request)
        for request, result in _request_result_pairs(requests, results)
        if not _tool_result_was_attempted(result)
        and str(request.get("name") or "").strip()
    }


def _rejected_request_plan_revisions(
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, set[str]]:
    """Remember which plan revision produced an explicit pre-execution reject."""

    revisions: dict[str, set[str]] = {}
    for request, result in _request_result_pairs(requests, results):
        if _tool_result_was_attempted(result):
            continue
        revision = _plan_revision(result) or _plan_revision(request)
        if revision is None or not str(request.get("name") or "").strip():
            continue
        revisions.setdefault(_tool_request_retry_signature(request), set()).add(revision)
    return revisions


def _retryable_failure_identities(
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    """Pair explicit failed actions with their host-issued action IDs."""

    return {
        (tool_request_signature(dict(request)), action_id)
        for request, result in _request_result_pairs(requests, results)
        if _explicit_action_failure(result)
        for action_id in (_action_id(result),)
        if action_id
    }


def _successful_request_signatures(
    requests: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Return calls with a success receipt, which always outrank old failures."""

    signatures: set[str] = set()
    for request, result in _request_result_pairs(requests, results):
        if not _tool_result_was_attempted(result):
            continue
        receipt = _action_receipt(result)
        if result.get("ok") is True or (
            receipt is not None
            and str(receipt.get("status") or "").strip().lower() == "accepted"
        ):
            signatures.add(tool_request_signature(dict(request)))
    return signatures


def _request_can_retry_failed_action(
    request: Mapping[str, Any],
    retryable_failure_identities: set[tuple[str, str]],
    successful_signatures: set[str] | None = None,
) -> bool:
    """Allow only an explicit retry linked to a known failed action."""

    signature = tool_request_signature(dict(request))
    if signature in (successful_signatures or set()):
        return False
    target = _retry_target_action_id(request)
    return bool(target and (signature, target) in retryable_failure_identities)


def _read_only_request(request: Mapping[str, Any]) -> bool:
    definition = DEFAULT_TOOL_REGISTRY.get(str(request.get("name") or ""))
    return bool(definition and definition.read_only)


def _record_filtered_calls(body: dict[str, Any], proposed, retained) -> None:
    """A filtered candidate is not a forever-pending execution step."""
    retained_signatures = {tool_request_signature(dict(item)) for item in retained}
    overlay = body.get("_cognitiveOverlay")
    state = (overlay or {}).get("state") if isinstance(overlay, dict) else None
    if not isinstance(state, dict):
        return
    receipts = []
    for item in proposed:
        signature = tool_request_signature(dict(item))
        if signature in retained_signatures:
            continue
        binding = tool_step_binding(state.get("task") or {}, item)
        if not binding:
            continue
        receipts.append({"name": item.get("name"), "ok": False, "attempted": False,
            "error": "request_filtered", "reason": "该候选未进入执行，请依据现有工具结果继续处理",
            "toolCallId": "filtered_" + uuid.uuid4().hex,
            "_cognitiveStepBinding": binding, "_cognitiveRequestSignature": signature})
    if receipts:
        body["_cognitiveEvidence"] = {**(body.get("_cognitiveEvidence") or {}), **collect_evidence(receipts)}
        overlay["state"] = project_tool_plan_receipts(state, body["_cognitiveEvidence"])


def _eligible_followup_requests(
    proposed: Sequence[Mapping[str, Any]],
    executed_signatures: set[str],
    rejected_retry_signatures: set[str],
    *,
    retryable_failure_identities: set[tuple[str, str]] | None = None,
    successful_signatures: set[str] | None = None,
    rejected_plan_revisions: Mapping[str, set[str]] | None = None,
    current_plan_revision: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter executed calls and no-progress repeats separately.

    A repaired request may keep the same executable arguments while changing
    structured authorization, so it is eligible when its retry signature
    differs from the prior rejection.  An unchanged rejection is returned in
    ``repeated_rejections`` so the caller can terminate through its existing
    bounded duplicate path.
    """

    eligible: list[dict[str, Any]] = []
    repeated_rejections: list[dict[str, Any]] = []
    retryable_failure_identities = retryable_failure_identities or set()
    successful_signatures = successful_signatures or set()
    rejected_plan_revisions = rejected_plan_revisions or {}
    current_revision = _plan_revision({"planRevision": current_plan_revision})
    for item in proposed:
        if not isinstance(item, Mapping):
            continue
        request = dict(item)
        signature = tool_request_signature(request)
        retry_is_legal = _request_can_retry_failed_action(
            request,
            retryable_failure_identities,
            successful_signatures,
        )
        retry_declared = _retry_relation_declared(request)
        retry_signature = _tool_request_retry_signature(request)
        if retry_signature in rejected_retry_signatures:
            prior_revisions = rejected_plan_revisions.get(retry_signature, set())
            if current_revision is not None and prior_revisions and current_revision not in prior_revisions:
                eligible.append(request)
            else:
                repeated_rejections.append(request)
            continue
        # A retry declaration without a matching failed action is not a new
        # call.  This keeps unknown/successful/missing receipts behind the
        # execution barrier even if the model supplied a plausible-looking ID.
        if signature in successful_signatures:
            continue
        if signature in executed_signatures:
            # Re-reading after a timeout has no duplicate side effect. Writes
            # still require a definite failed receipt before replay.
            if retry_is_legal or _read_only_request(request):
                eligible.append(request)
            continue
        if retry_declared and not retry_is_legal:
            # Changed arguments describe a new operation, even when the model
            # kept retry metadata from its previous failed attempt.
            request["selection"] = {**dict(request.get("selection") or {}), "actionRelation": "new"}
            request["selection"].pop("retryOfActionId", None)
            request["args"] = dict(request.get("args") or {})
            request["args"].pop("_retryOfActionId", None)
        eligible.append(request)
    return eligible, repeated_rejections


def _terminal_tool_response_issue(
    content: Any,
    *,
    require_outcome: bool,
    allow_legacy_reply: bool = False,
) -> str | None:
    """Return a structural terminal-contract issue without changing content.

    The cognitive transport remains the authority for semantic/state validation.
    This small check only prevents a tool-loop terminal path from handing a
    second tool decision (or an outcome-less cognitive response) back to the
    upstream pipeline after the loop has been closed.
    """

    value = parse_json_object(content) if isinstance(content, str) else None
    if not isinstance(value, Mapping):
        return "terminal_response_not_json_object"
    kind = str(value.get("kind") or "").strip().lower()
    if require_outcome and kind != "turn":
        return "terminal_response_kind_must_be_turn"
    if not require_outcome and kind in {"tool_use", "tool_request"}:
        return "terminal_response_contains_tool_requests"
    requests = value.get("calls") or value.get("toolRequests") or []
    if not isinstance(requests, list):
        return "terminal_response_tool_requests_must_be_list"
    if requests:
        return "terminal_response_contains_tool_requests"
    if require_outcome:
        outcome = value.get("outcome")
        if not isinstance(outcome, Mapping):
            return "terminal_response_outcome_missing"
        if str(outcome.get("status") or "").strip().lower() not in _TERMINAL_OUTCOME_STATUSES:
            return "terminal_response_outcome_status_invalid"
    elif kind != "turn":
        legacy_visible = any(
            isinstance(value.get(field), str) and value[field].strip()
            for field in ("reply", "spokenTextJa", "speechText")
        )
        speech_units = value.get("speechUnits") or value.get("utterances")
        if isinstance(speech_units, list):
            legacy_visible = legacy_visible or any(
                isinstance(item, Mapping)
                and any(
                    isinstance(item.get(field), str) and item[field].strip()
                    for field in ("zh", "textZh", "reply", "ja", "spokenTextJa")
                )
                for item in speech_units
            )
        if not allow_legacy_reply or kind or not legacy_visible:
            return "terminal_response_kind_must_be_turn"
    return None


class ModelPort(Protocol):
    """模型传输及模型输入装配所需入口。"""

    def send_conversation_brain_payload(
        self,
        body: dict[str, Any],
        settings: Mapping[str, Any],
        payload: dict[str, Any],
        **request: Any,
    ) -> str: ...

    def send_upstream_chat_payload(
        self,
        settings: Mapping[str, Any],
        payload: dict[str, Any],
        **request: Any,
    ) -> str: ...

    def finish_reason(self) -> str: ...

    def conversation_image_inputs(
        self,
        body: dict[str, Any],
        settings: Mapping[str, Any],
    ) -> list[dict[str, Any]]: ...

    def with_conversation_images(
        self,
        body: dict[str, Any],
        payload: dict[str, Any],
        images: Sequence[Mapping[str, Any]],
        **request: Any,
    ) -> dict[str, Any]: ...

    def record_persona_model_input(
        self,
        body: dict[str, Any],
        payload: Mapping[str, Any],
        purpose: str,
    ) -> None: ...

    def finalize_preexecuted_tool_result(
        self,
        settings: dict[str, Any],
        **request: Any,
    ) -> tuple[str, str, int]: ...


class ToolPort(Protocol):
    """工具准入、执行、回执投影与证据提示入口。"""

    def normalize_tool_requests(self, value: Any) -> list[dict[str, Any]]: ...

    def constrain_model_tool_requests(
        self,
        requests: list[dict[str, Any]],
        user_message: str,
        *context: Any,
        **state: Any,
    ) -> list[dict[str, Any]]: ...

    def preexecuted_attachment_request_satisfied(
        self,
        request: Mapping[str, Any],
        results: Sequence[Mapping[str, Any]],
    ) -> bool: ...

    def tool_result_request_signature(self, result: Mapping[str, Any]) -> str: ...

    def constrain_attachment_sensor_requests(
        self,
        requests: Sequence[dict[str, Any]],
        user_message: str,
        **state: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]: ...

    def execute_agent_tools(
        self,
        requests: Sequence[dict[str, Any]],
        settings: Mapping[str, Any],
        user_message: str,
        **state: Any,
    ) -> list[dict[str, Any]]: ...

    def tool_grounding_instruction(
        self,
        results: list[dict[str, Any]],
    ) -> str: ...

    def visual_relationship_reaction_instruction(
        self,
        results: list[dict[str, Any]],
        relationship_state: Mapping[str, Any],
    ) -> str: ...

    def turn_time_context(
        self,
        body: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ResearchPort(Protocol):
    """公开信息研究循环与受验证浏览动作入口。"""

    def run_research_query_loop(self, *args: Any, **kwargs: Any) -> Any: ...

    def browser_action_args_from_tool_results(
        self,
        user_message: str,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def browser_args_with_usage_tracking(
        self,
        args: dict[str, Any],
        **state: Any,
    ) -> dict[str, Any]: ...

    def tool_open_browser(
        self,
        settings: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def emit_research_bridge_once(self, plan: Mapping[str, Any]) -> None: ...


class SchedulePort(Protocol):
    """日程意图、Todo 续接与证据后重评入口。"""

    def resolve_model_schedule_decision(self, *args: Any, **kwargs: Any) -> Any: ...

    def repair_reply_for_recovered_schedule_decision(
        self,
        settings: Mapping[str, Any],
        base_messages: Sequence[Mapping[str, Any]],
        content: str,
        decision: Any,
        **state: Any,
    ) -> str: ...

    def body_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...

    def merge_schedule_tool_requests(
        self,
        requests: Sequence[Mapping[str, Any]],
        decision: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]: ...

    def normalize_todo_continuation_decision(self, value: Any) -> str: ...

    def reconsider_schedule_intent_after_tool_evidence(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any: ...

    def schedule_tool_requests(
        self,
        decision: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]: ...


class CognitivePlanPort(Protocol):
    """认知计划、能力包络和 prepared turn 持久化入口。"""

    def cognitive_turn_state(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def cognitive_execution_state(
        self,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def current_task_semantic_request(
        self,
        body: Mapping[str, Any],
        user_message: str,
    ) -> str: ...

    def finalize_turn_execution_plan(self, *args: Any, **kwargs: Any) -> Any: ...

    def capability_with_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...

    def synchronize_prepared_epistemic_plan(
        self,
        prepared: Any,
        decision: Any,
    ) -> tuple[Any, Any]: ...

    def persist_prepared_turn(self, prepared: Any) -> None: ...

    def bind_reused_tool_steps(self, *args: Any, **kwargs: Any) -> None: ...


class RequestPolicyPort(Protocol):
    """请求预算、输出规范和 Turn Contract 提示入口。"""

    def pressure_managed_prompt(
        self,
        messages: Sequence[dict[str, Any]],
        settings: Mapping[str, Any],
        **budget: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]: ...

    def normalize_model_reply(
        self,
        value: Any,
        **context: Any,
    ) -> dict[str, Any]: ...

    def fast_stream_token_budget(self, turn_policy: Any) -> int: ...


class EventPort(Protocol):
    """请求日志与进度事件入口。"""

    def write_server_log(self, event: str, **fields: Any) -> None: ...

    def update_chat_request_progress(
        self,
        request_id: str,
        stage: str,
        detail: str,
    ) -> Any: ...


@dataclass(frozen=True)
class ToolLoopServices:
    """按领域分组的工具循环依赖。"""

    model: ModelPort
    tools: ToolPort
    research: ResearchPort
    schedule: SchedulePort
    cognitive_plan: CognitivePlanPort
    request_policy: RequestPolicyPort
    events: EventPort


@dataclass(frozen=True)
class ToolLoopContext:
    """进入模型工具循环时冻结的请求上下文。

    ``body`` 仍是唯一的认知请求状态 owner；这里仅保存同一对象的引用，
    不复制、不提交历史，也不创建进程级可变状态。
    """

    settings: dict[str, Any]
    body: dict[str, Any]
    base_messages: list[dict[str, Any]]
    content: str
    context_resolution: dict[str, Any]
    user_message: str
    messages: Sequence[Mapping[str, Any]]
    turn_policy: Any
    file_output_expected: bool
    interpretation: Any
    contract: Any
    request_id: str
    tool_preflight: Any
    research_decision: Any
    prepared_action_turn: Any
    capability: Any
    semantic_task_request: str
    answer_finish_reason: str
    context_pressure_metrics: dict[str, Any]
    research_review_ms: int
    research_bridge_emitted: bool
    todo_continuation_transition: Any
    pre_tool_results: list[dict[str, Any]]
    inferred_requests: list[dict[str, Any]]
    current_attachments: Sequence[Mapping[str, Any]]
    attachment_prompt: str
    has_bound_attachment: bool
    preexecution_tool_rounds: int
    existing_tool_rounds: int
    continuation_tool_rounds: int
    continuation: Mapping[str, Any] | None
    pending_todo_before: Mapping[str, Any]
    deferred_browser_requests: Sequence[Mapping[str, Any]]
    pending_browser_action: Mapping[str, Any]
    relationship_state: Any
    progress_callback: Any | None = None
    persona_snapshot: Any | None = None


@dataclass(frozen=True)
class ToolLoopResult:
    """循环结束后供 ``call_upstream`` 后半程继续使用的显式状态。"""

    content: str
    finish_reason: str
    executed_results: list[dict[str, Any]]
    requested_tools: list[dict[str, Any]]
    parsed: dict[str, Any]
    rounds: int
    stop_reason: str
    model_ms: int
    research_review_ms: int
    research_bridge_emitted: bool
    todo_continuation_transition: Any
    semantic_task_request: str
    capability: Any
    research_decision: Any
    prepared_action_turn: Any
    tool_preflight: Any
    turn_contract: Any
    context_pressure_metrics: dict[str, Any]
    uncertainty_triggered_web: bool


def finalize_preexecuted_tool_result(
    services: ToolLoopServices,
    settings: dict[str, Any],
    *,
    base_messages: list[dict[str, Any]],
    primary_content: str,
    executed_results: list[dict[str, Any]],
    duplicate_requests: list[dict[str, Any]],
    context_resolution: dict[str, Any],
    user_message: str,
    turn_policy: Any,
    file_output_expected: bool,
    interpretation: Any,
    contract: Any,
    request_id: str,
    cognitive_body: dict[str, Any] | None = None,
    source_metadata: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[str, str, int]:
    """把已执行的重复工具请求收束成一次最终回复。"""

    write_server_log = services.events.write_server_log
    tool_grounding_instruction = services.tools.tool_grounding_instruction
    pressure_managed_prompt = services.request_policy.pressure_managed_prompt
    fast_stream_token_budget = services.request_policy.fast_stream_token_budget
    update_chat_request_progress = services.events.update_chat_request_progress
    cognitive_turn_state = services.cognitive_plan.cognitive_turn_state
    bind_reused_tool_steps = services.cognitive_plan.bind_reused_tool_steps
    conversation_image_inputs = services.model.conversation_image_inputs
    with_conversation_images = services.model.with_conversation_images
    record_persona_model_input = services.model.record_persona_model_input
    send_conversation_brain_payload = services.model.send_conversation_brain_payload
    send_upstream_chat_payload = services.model.send_upstream_chat_payload

    write_server_log(
        "preexecuted_duplicate_tool_finalize",
        requestId=request_id,
        tools=[str(item.get("name") or "") for item in duplicate_requests],
        resultStatuses=[{
            "name": str(item.get("name") or ""),
            "ok": bool(item.get("ok")),
            "error": str(item.get("error") or "")[:120],
        } for item in executed_results if isinstance(item, Mapping)],
    )
    tool_context = tool_context_prompt(
        executed_results,
        context_resolution,
        max_chars=turn_policy.tool_evidence_max_chars,
        model_owned=bool(cognitive_turn_state(cognitive_body or {})),
    )
    visual_context = visual_evidence_prompt(
        executed_results,
        user_question=interpretation.semantic_text or user_message,
    )
    grounding = tool_grounding_instruction(executed_results)
    terminal_messages = [
        *base_messages,
        {"role": "assistant", "content": primary_content},
        {"role": "system", "content": tool_context},
        *([{"role": "system", "content": visual_context}] if visual_context else []),
        *([{"role": "system", "content": grounding}] if grounding else []),
        {"role": "system", "content": (
            "你刚请求的工具已经在本轮执行，禁止重复输出 tool_use。"
            "请依据已返回的真实结果，按照 semantic-v3 输出 kind='turn'；"
            "成功时直接回答；失败或权限不足时简短说明影响当前答案的具体缺口。"
            "只有完成当前操作确实需要用户补充输入或处理权限时，才给一个必要下一步；"
            "联网取证失败时不要展开检索流程、建议用户自行查找或追问是否继续搜索。"
            "不得把工具请求 JSON 当作面向用户的回复，也不得编造成功。"
        )},
        {"role": "system", "content": core_dialogue_turn_contract_prompt(
            interpretation,
            contract,
            model_owned=bool(cognitive_body and cognitive_turn_state(cognitive_body)),
        )},
    ]
    max_tokens = (
        6000
        if file_output_expected
        else min(6_000, fast_stream_token_budget(turn_policy) + 240)
    )
    terminal_messages, _ = pressure_managed_prompt(
        terminal_messages,
        settings,
        reserved_output_tokens=max_tokens,
        request_id=request_id,
        stage="chat_preexecuted_duplicate_finalize",
        source_metadata=source_metadata,
    )
    payload: dict[str, Any] = {
        "model": settings["apiModel"],
        "temperature": 0.5,
        "max_tokens": max_tokens,
        "messages": terminal_messages,
    }
    if settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in settings["apiEndpoint"]:
        payload["thinking"] = {"type": "disabled"}
    update_chat_request_progress(
        request_id,
        "model-synthesizing",
        "工具已经执行，正在根据真实结果形成回答",
    )
    started = time.perf_counter()
    if cognitive_body is not None and cognitive_turn_state(cognitive_body):
        state = cognitive_turn_state(cognitive_body)
        bind_reused_tool_steps(cognitive_body, state, duplicate_requests, executed_results)
        cognitive_body["_cognitiveEvidence"] = {
            **(cognitive_body.get("_cognitiveEvidence") or {}),
            **collect_evidence(executed_results),
        }
        cognitive_body["_cognitiveToolLoopTerminal"] = True
        try:
            content = send_conversation_brain_payload(
                cognitive_body,
                settings,
                payload,
                timeout=90,
                request_id=request_id,
                purpose="chat_tool_preexecuted_duplicate_finalize",
            )
        finally:
            cognitive_body.pop("_cognitiveToolLoopTerminal", None)
    else:
        image_body = cognitive_body if cognitive_body is not None else {"requestId": request_id}
        image_body["_cognitiveEvidence"] = {
            **(image_body.get("_cognitiveEvidence") or {}),
            **collect_evidence(executed_results),
        }
        images = conversation_image_inputs(image_body, settings)
        outgoing = with_conversation_images(
            image_body,
            payload,
            images,
            purpose="chat_preexecuted_duplicate_finalize",
        )
        record_persona_model_input(
            image_body,
            outgoing,
            "chat_preexecuted_duplicate_finalize",
        )
        content = send_upstream_chat_payload(
            settings,
            outgoing,
            timeout=90,
            request_id=request_id,
            purpose="chat_preexecuted_duplicate_finalize",
        )
        image_body["_nativeVisualInputIds"] = [item["artifactId"] for item in images]
    return (
        content,
        services.model.finish_reason(),
        round((time.perf_counter() - started) * 1000),
    )


def run_model_tool_loop(
    context: ToolLoopContext,
    services: ToolLoopServices,
) -> ToolLoopResult:
    """执行一次有界模型工具循环，并显式返回后续阶段需要的状态。"""

    settings = context.settings
    body = context.body
    base_messages = context.base_messages
    content = context.content
    context_resolution = context.context_resolution
    user_message = context.user_message
    messages = context.messages
    turn_policy = context.turn_policy
    file_output_expected = context.file_output_expected
    utterance_interpretation = context.interpretation
    turn_contract = context.contract
    request_id = context.request_id
    tool_preflight = context.tool_preflight
    research_decision = context.research_decision
    prepared_action_turn = context.prepared_action_turn
    capability = context.capability
    semantic_task_request = context.semantic_task_request
    answer_finish_reason = context.answer_finish_reason
    context_pressure_metrics = context.context_pressure_metrics
    research_review_ms = context.research_review_ms
    research_bridge_emitted = context.research_bridge_emitted
    todo_continuation_transition = context.todo_continuation_transition
    pre_tool_results = context.pre_tool_results
    inferred_requests = context.inferred_requests
    current_attachments = context.current_attachments
    attachment_prompt = context.attachment_prompt
    has_bound_attachment = context.has_bound_attachment
    preexecution_tool_rounds = context.preexecution_tool_rounds
    existing_tool_rounds = context.existing_tool_rounds
    continuation_tool_rounds = context.continuation_tool_rounds
    continuation = context.continuation
    pending_todo_before = context.pending_todo_before
    deferred_browser_requests = context.deferred_browser_requests
    pending_browser_action = context.pending_browser_action
    relationship_state = context.relationship_state
    progress_callback = context.progress_callback

    send_conversation_brain_payload = services.model.send_conversation_brain_payload
    send_upstream_chat_payload = services.model.send_upstream_chat_payload
    normalize_tool_requests = services.tools.normalize_tool_requests
    constrain_model_tool_requests = services.tools.constrain_model_tool_requests
    preexecuted_attachment_request_satisfied = (
        services.tools.preexecuted_attachment_request_satisfied
    )
    tool_result_request_signature = services.tools.tool_result_request_signature
    constrain_attachment_sensor_requests = (
        services.tools.constrain_attachment_sensor_requests
    )
    execute_agent_tools = services.tools.execute_agent_tools
    tool_grounding_instruction = services.tools.tool_grounding_instruction
    visual_relationship_reaction_instruction = (
        services.tools.visual_relationship_reaction_instruction
    )
    turn_time_context = services.tools.turn_time_context
    run_research_query_loop = services.research.run_research_query_loop
    browser_action_args_from_tool_results = (
        services.research.browser_action_args_from_tool_results
    )
    browser_args_with_usage_tracking = (
        services.research.browser_args_with_usage_tracking
    )
    tool_open_browser = services.research.tool_open_browser
    emit_research_bridge_once = services.research.emit_research_bridge_once
    resolve_model_schedule_decision = (
        services.schedule.resolve_model_schedule_decision
    )
    repair_reply_for_recovered_schedule_decision = (
        services.schedule.repair_reply_for_recovered_schedule_decision
    )
    body_tool_preflight = services.schedule.body_tool_preflight
    merge_schedule_tool_requests = services.schedule.merge_schedule_tool_requests
    normalize_todo_continuation_decision = (
        services.schedule.normalize_todo_continuation_decision
    )
    reconsider_schedule_intent_after_tool_evidence = (
        services.schedule.reconsider_schedule_intent_after_tool_evidence
    )
    schedule_tool_requests = services.schedule.schedule_tool_requests
    cognitive_turn_state = services.cognitive_plan.cognitive_turn_state
    cognitive_execution_state = services.cognitive_plan.cognitive_execution_state
    current_task_semantic_request = (
        services.cognitive_plan.current_task_semantic_request
    )
    finalize_turn_execution_plan = (
        services.cognitive_plan.finalize_turn_execution_plan
    )
    capability_with_tool_preflight = (
        services.cognitive_plan.capability_with_tool_preflight
    )
    synchronize_prepared_epistemic_plan = (
        services.cognitive_plan.synchronize_prepared_epistemic_plan
    )
    persist_prepared_turn = services.cognitive_plan.persist_prepared_turn
    pressure_managed_prompt = services.request_policy.pressure_managed_prompt
    normalize_model_reply = services.request_policy.normalize_model_reply
    fast_stream_token_budget = services.request_policy.fast_stream_token_budget
    write_server_log = services.events.write_server_log
    update_chat_request_progress = services.events.update_chat_request_progress

    def _finalize_preexecuted_tool_result(*args: Any, **kwargs: Any) -> tuple[str, str, int]:
        return services.model.finalize_preexecuted_tool_result(*args, **kwargs)

    tool_loop_stop_reason = "no_tool_request"
    parsed = parse_json_object(content) or {}
    select_tool_budget(body, body.get("_toolEscalation"), settings)
    select_tool_budget(body, parsed, settings)
    sync_tool_usage(body, pre_tool_results)
    write_server_log(
        "dialogue_protocol_output_parsed",
        requestId=request_id,
        kind=str(parsed.get("kind") or "legacy_or_unknown")[:40],
        keys=sorted(str(key)[:80] for key in parsed.keys())[:30],
        speechUnitCount=len(parsed.get("speechUnits") or [])
        if isinstance(parsed.get("speechUnits"), list) else 0,
        toolCallCount=len(parsed.get("calls") or parsed.get("toolRequests") or [])
        if isinstance(parsed.get("calls") or parsed.get("toolRequests") or [], list) else 0,
        hasLegacyReply=bool(str(parsed.get("reply") or "").strip()),
        hasLegacySpokenText=bool(str(parsed.get("spokenTextJa") or "").strip()),
    )
    if not isinstance(body.get("_scheduleDecision"), Mapping):
        body["_scheduleDecision"] = resolve_model_schedule_decision(
            parsed,
            settings,
            user_message=user_message,
            messages=messages,
            request_id=request_id,
            body=body,
        )
    if continuation is None:
        content = repair_reply_for_recovered_schedule_decision(
            settings,
            base_messages,
            content,
            body.get("_scheduleDecision"),
            request_id=request_id,
            body=body,
            user_message=user_message,
        )
    parsed = parse_json_object(content) or {}
    if isinstance(body.get("_scheduleDecision"), Mapping):
        tool_preflight = body_tool_preflight(
            body,
            user_message,
            messages,
            reply_mode=turn_policy.reply_mode,
            speech_act=utterance_interpretation.primary_speech_act,
        )
    model_tool_requests = merge_schedule_tool_requests(
        normalize_tool_requests(parsed),
        body.get("_scheduleDecision"),
    )
    uncertainty_triggered_web = False
    atomic_epistemic_plan = EpistemicPlan.from_dict(
        prepared_action_turn.epistemic_plan
    )
    atomic_confidence_gate = knowledge_confidence_gate_decision(
        atomic_epistemic_plan.fact_requirements if atomic_epistemic_plan else (),
        parsed.get("knowledgeConfidence", parsed.get("knowledge_confidence")),
        confidence_provided=(
            str(
                parsed.get(
                    "knowledgeConfidence",
                    parsed.get("knowledge_confidence", ""),
                )
                or ""
            ).strip().lower()
            in {"high", "medium", "low"}
        ),
        research_policy=research_decision.policy,
        web_permission=bool(settings.get("permissionWebSearch")),
    )
    write_server_log(
        "atomic_knowledge_confidence_gate_decision",
        requestId=request_id,
        planId=(atomic_epistemic_plan.plan_id if atomic_epistemic_plan else ""),
        modelConfidence=str(
            parsed.get("knowledgeConfidence", parsed.get("knowledge_confidence", ""))
            or ""
        )[:16],
        decision=atomic_confidence_gate.to_dict(),
    )
    if (
        continuation is None
        and not cognitive_turn_state(body)
        and
        not pre_tool_results
        and research_decision.policy != "forbidden"
        and (
            model_web_research_requested(parsed, user_message)
            or atomic_confidence_gate.status == "RESEARCH_ESCALATION"
        )
        and not any(item.get("name") in {"web_search", "read_web_page"} for item in model_tool_requests)
    ):
        model_tool_requests.append({"name": "web_search", "args": {}})
        uncertainty_triggered_web = True
    requested_tools = constrain_model_tool_requests(
        model_tool_requests,
        user_message,
        context_resolution,
        messages,
        body.get("_scheduleDecision"),
        cognitive_state=cognitive_execution_state(body), evidence=body.get("_cognitiveEvidence") or {},
    )
    if research_decision.policy == "forbidden":
        requested_tools = [
            item
            for item in requested_tools
            if item.get("name") not in {"web_search", "read_web_page"}
        ]
    _record_filtered_calls(body, model_tool_requests, requested_tools)
    todo_already_executed = any(
        item.get("name") == "todo_manage" and item.get("ok")
        for item in pre_tool_results
        if isinstance(item, Mapping)
    )
    todo_already_requested = any(
        item.get("name") == "todo_manage" for item in requested_tools
    )
    if (
        "todo_manage" in tool_preflight.mandatory_capabilities
        and not cognitive_turn_state(body)
        and not todo_already_executed
        and not todo_already_requested
    ):
        # 必需副作用不能由文字回答代替。在接受最终回复前只给模型一次有界
        # 机会补出符合结构的命令，最后仍以真实回执为准。
        obligation_payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.1,
            "max_tokens": 1800,
            "messages": [
                *base_messages,
                {"role": "assistant", "content": content},
                {"role": "system", "content": (
                    "本轮 todo_manage 是必需能力，但你上一条没有发出可执行的工具请求。"
                    "现在只输出一个 JSON 对象，字段仅为 toolRequests、continuationDecision、pendingCommand。"
                    "continuationDecision 只能是 execute、needs_input、unrelated、cancel。"
                    "若存在[待续接的 Todo 操作]，先结合 originalUserRequest 与用户当前消息判断是否属于同一次操作；"
                    "强相关且信息够用时为 execute 并生成 todo_manage，强相关但仍缺关键信息时为 needs_input，"
                    "明确转向无关主题时为 unrelated，明确取消时为 cancel。"
                    "若不存在待续接操作，本轮已由系统确认用户明确要求 Todo 写入；信息不足只能选 needs_input，不能选 unrelated。"
                    "根据完整语义生成 todo_manage；不得用‘今晚’、‘任务’等单个词作为触发依据。实际写操作必须设置 commitIntent=true。"
                    "命名任务使用 targetRef，不得编造 taskId；时间必须是带时区的 ISO-8601。"
                    "若用户信息不足，toolRequests 为空，并在 pendingCommand 中仅保留已明确知道的安全字段；不得声称执行。"
                    "不要声称已经执行，也不要输出面向用户的回复。"
                )},
            ],
        }
        if settings.get("useResponseFormat"):
            obligation_payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            obligation_payload["thinking"] = {"type": "disabled"}
        obligation_payload["messages"], _ = pressure_managed_prompt(
            obligation_payload["messages"],
            settings,
            reserved_output_tokens=int(obligation_payload["max_tokens"]),
            request_id=request_id,
            stage="todo_required_tool_recovery",
            source_metadata=messages,
        )
        recovered_content = send_upstream_chat_payload(
            settings,
            obligation_payload,
            timeout=90,
            request_id=request_id,
            purpose="todo_required_tool_recovery",
        )
        recovered_payload = parse_json_object(recovered_content) or {}
        recovered_requests = constrain_model_tool_requests(
            normalize_tool_requests(recovered_payload),
            user_message,
            context_resolution,
            messages,
            body.get("_scheduleDecision"),
            cognitive_state=cognitive_execution_state(body), evidence=body.get("_cognitiveEvidence") or {},
        )
        continuation_decision = normalize_todo_continuation_decision(
            recovered_payload.get("continuationDecision")
        )
        if any(item.get("name") == "todo_manage" for item in recovered_requests):
            continuation_decision = "execute"
        elif continuation_decision == "execute":
            # 没有一条结构有效的命令时，"execute" 不代表已经执行。
            continuation_decision = "needs_input"
        elif not continuation_decision:
            # 兼容旧模型只返回 toolRequests 的格式。空恢复表示已授权操作仍缺
            # 信息，不能据此把文字回答当成写入完成。
            continuation_decision = "needs_input"
        if (
            not pending_todo_before
            and continuation_decision in {"unrelated", "cancel"}
        ):
            # 此分支只会因当前轮本身要求 Todo 写入而进入；没有更早的续接操作
            # 可供模型拒绝或取消。
            continuation_decision = "needs_input"
        if pending_todo_before or continuation_decision == "needs_input":
            todo_continuation_transition = {
                "decision": continuation_decision,
                "pendingCommand": (
                    dict(recovered_payload.get("pendingCommand"))
                    if isinstance(recovered_payload.get("pendingCommand"), Mapping)
                    else {}
                ),
            }
        requested_tools.extend(
            item for item in recovered_requests
            if item.get("name") == "todo_manage"
        )
        write_server_log(
            "todo_required_tool_recovery",
            requestId=request_id,
            recovered=any(
                item.get("name") == "todo_manage" for item in recovered_requests
            ),
            continuationDecision=continuation_decision,
            pendingActionId=str(pending_todo_before.get("actionId") or ""),
        )
    # 只有完全相同调用的回执才能证明它已执行；推断结果和执行前拒绝不能占用
    # 修正后调用的身份。
    preexecution_requests = [*inferred_requests, *requested_tools]
    preexecution_retryable_failures = _retryable_failure_identities(
        preexecution_requests,
        pre_tool_results,
    )
    preexecution_successes = _successful_request_signatures(
        preexecution_requests,
        pre_tool_results,
    )
    already_executed_signatures = {
        signature
        for item in pre_tool_results
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
        for signature in (tool_result_request_signature(item),)
        if signature and not (_read_only_request(item) and item.get("ok") is False)
    }
    duplicate_preexecuted_requests = [
        item for item in requested_tools
        if tool_request_signature(item) in already_executed_signatures
        and not _request_can_retry_failed_action(
            item,
            preexecution_retryable_failures,
            preexecution_successes,
        )
    ]
    preexecuted_attachment_requests = [
        item for item in requested_tools
        if preexecuted_attachment_request_satisfied(item, pre_tool_results)
    ]
    if preexecuted_attachment_requests:
        requested_tools = [
            item for item in requested_tools
            if item not in preexecuted_attachment_requests
        ]
        duplicate_preexecuted_requests.extend(preexecuted_attachment_requests)
        write_server_log(
            "preexecuted_attachment_request_satisfied",
            requestId=request_id,
            requestedTools=[
                str(item.get("name") or "")
                for item in preexecuted_attachment_requests
            ],
            receiptTools=[
                str(item.get("name") or "")
                for item in pre_tool_results
                if isinstance(item, Mapping) and item.get("ok")
            ],
        )
    if already_executed_signatures:
        requested_tools = [
            item
            for item in requested_tools
            if tool_request_signature(item) not in already_executed_signatures
            or _request_can_retry_failed_action(
                item,
                preexecution_retryable_failures,
                preexecution_successes,
            )
        ]
    requested_tools, rejected_requested_sensors = constrain_attachment_sensor_requests(
        requested_tools,
        user_message,
        has_bound_attachment=has_bound_attachment,
    )
    tool_preflight = bind_structured_perception_request(
        tool_preflight,
        requested_tools,
        source="query_loop_tool_request",
    )
    body["_toolPreflight"] = tool_preflight
    for item in rejected_requested_sensors:
        write_server_log(
            "attachment_sensor_request_rejected",
            requestId=request_id,
            toolName=str(item.get("name") or ""),
            source="query_loop_tool_request",
            reason="current_attachment_is_authoritative_target",
        )
    prior_tool_rounds = (
        max(existing_tool_rounds, continuation_tool_rounds)
        if continuation is not None
        else max(existing_tool_rounds, preexecution_tool_rounds)
    )
    executed_tool_results = list(pre_tool_results)
    second_llm_ms = 0

    def request_terminal_tool_synthesis(
        previous_content: str,
        *,
        reason: str,
    ) -> tuple[str, int]:
        """Close a cognitive tool turn after no further tool call is legal."""

        terminal_payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.5,
            "max_tokens": 6000 if file_output_expected else min(
                6_000,
                fast_stream_token_budget(turn_policy) + 240,
            ),
            "messages": [
                *base_messages,
                {"role": "assistant", "content": previous_content},
                {"role": "system", "content": tool_context_prompt(
                    executed_tool_results,
                    context_resolution,
                    max_chars=turn_policy.tool_evidence_max_chars,
                 model_owned=bool(cognitive_turn_state(body)))},
                *([{"role": "system", "content": visual_evidence_prompt(
                    executed_tool_results,
                    user_question=semantic_task_request,
                )}] if visual_evidence_prompt(
                    executed_tool_results,
                    user_question=semantic_task_request,
                ) else []),
                *(
                    [{"role": "system", "content": tool_grounding_instruction(
                        executed_tool_results
                    )}]
                    if tool_grounding_instruction(executed_tool_results)
                    else []
                ),
                {"role": "system", "content": (
                    "工具循环现在必须结束，原因是 " + reason + "。"
                    "不得再输出 tool_use、calls 或 toolRequests。请依据所有已返回证据按照 semantic-v3 输出 kind='turn'；"
                    "证据足够就直接回答；证据不足时仍须输出 kind='turn'，并在 outcome.status 使用 partial 或 failed，"
                    "自然说明具体不确定处或当前缺口。"
                    "不要编造工具结果，也不要复述调度过程。"
                    "工具调用计划不等于执行证据。没有实际工具结果时，只能说明尚未执行或未获得状态；"
                    "不得声称查询失败、网关异常或回执为空。permission_denied 表示本地权限未开启，"
                    "不是外部连接失败；按真实错误和已执行结果说明下一步。"
                )},
                {"role": "system", "content": core_dialogue_turn_contract_prompt(
                    utterance_interpretation,
                    turn_contract,
                    model_owned=bool(cognitive_turn_state(body)),
                )},
            ],
        }
        if settings.get("useResponseFormat"):
            terminal_payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            terminal_payload["thinking"] = {"type": "disabled"}
        terminal_payload["messages"], _ = pressure_managed_prompt(
            terminal_payload["messages"],
            settings,
            reserved_output_tokens=int(terminal_payload["max_tokens"]),
            request_id=request_id,
            stage=f"chat_tool_finalize_{reason}",
            source_metadata=messages,
        )
        terminal_started = time.perf_counter()
        body["_cognitiveEvidence"] = {**(body.get("_cognitiveEvidence") or {}), **collect_evidence(executed_tool_results)}
        body["_cognitiveToolLoopTerminal"] = True
        cognitive_terminal = bool(cognitive_turn_state(body))
        terminal_repair_attempts = 0
        terminal_issue: str | None = None
        try:
            while True:
                try:
                    terminal_content = send_conversation_brain_payload(
                        body,
                        settings,
                        terminal_payload,
                        timeout=90,
                        request_id=request_id,
                        purpose=f"chat_tool_query_loop_finalize_{reason}",
                    )
                except CognitiveProtocolError as error:
                    terminal_issue = "terminal_model_validation_failed"
                    if terminal_repair_attempts >= _TERMINAL_FORMAT_REPAIR_LIMIT:
                        raise CognitiveProtocolError(
                            "tool_loop_terminal_synthesis_aborted: "
                            + str(error)[:1200]
                        ) from error
                else:
                    terminal_issue = _terminal_tool_response_issue(
                        terminal_content,
                        require_outcome=cognitive_terminal,
                        allow_legacy_reply=not cognitive_terminal,
                    )
                    if terminal_issue is None:
                        break
                    if terminal_repair_attempts >= _TERMINAL_FORMAT_REPAIR_LIMIT:
                        raise CognitiveProtocolError(
                            "tool_loop_terminal_synthesis_aborted: "
                            + terminal_issue
                        )
                terminal_repair_attempts += 1
                write_server_log(
                    "tool_query_loop_terminal_format_repair",
                    requestId=request_id,
                    reason=reason,
                    attempt=terminal_repair_attempts,
                    issue=terminal_issue,
                )
                terminal_payload["messages"] = [
                    *terminal_payload["messages"],
                    {"role": "system", "content": (
                        "[终止格式修复] 上一个终止候选没有通过收尾合同。"
                        "工具预算已经耗尽，本次只允许输出一个最终 JSON 对象；不得输出 kind='tool_use'，"
                        "不得给出 calls 或 toolRequests，也不得要求后端再执行工具。"
                        "必须输出 kind='turn'。若当前证据不足，outcome.status 必须是 partial 或 failed，"
                        "并如实保留已知部分和未决缺口；不要编造证据或把未执行操作写成完成。"
                    )},
                ]
                terminal_payload["messages"], _ = pressure_managed_prompt(
                    terminal_payload["messages"],
                    settings,
                    reserved_output_tokens=int(terminal_payload["max_tokens"]),
                    request_id=request_id,
                    stage=f"chat_tool_finalize_repair_{reason}",
                    source_metadata=messages,
                )
        finally:
            body.pop("_cognitiveToolLoopTerminal", None)
        return (
            terminal_content,
            round((time.perf_counter() - terminal_started) * 1000),
        )

    initial_tool_use_rejected = (
        bool(cognitive_turn_state(body))
        and str(parsed.get("kind") or "").strip().lower() == "tool_use"
        and not requested_tools
        and not duplicate_preexecuted_requests
    )
    if initial_tool_use_rejected:
        content, terminal_ms = request_terminal_tool_synthesis(
            content,
            reason="requested_tools_rejected",
        )
        second_llm_ms += terminal_ms
        answer_finish_reason = services.model.finish_reason()
        tool_loop_stop_reason = "requested_tools_rejected"

    if duplicate_preexecuted_requests and not requested_tools:
        tool_loop_stop_reason = "preexecuted_duplicate_finalized"
        content, answer_finish_reason, second_llm_ms = _finalize_preexecuted_tool_result(
            settings,
            base_messages=base_messages,
            primary_content=content,
            executed_results=executed_tool_results,
            duplicate_requests=duplicate_preexecuted_requests,
            context_resolution=context_resolution,
            user_message=user_message,
            turn_policy=turn_policy,
            file_output_expected=file_output_expected,
            interpretation=utterance_interpretation,
            contract=turn_contract,
            request_id=request_id,
            cognitive_body=body,
            source_metadata=messages,
        )
    initial_budget_reason = tool_budget_exhausted(body, requested_tools) if requested_tools else ""
    if initial_budget_reason:
        content, terminal_ms = request_terminal_tool_synthesis(content, reason=initial_budget_reason)
        second_llm_ms += terminal_ms
        answer_finish_reason = services.model.finish_reason()
        tool_loop_stop_reason = initial_budget_reason
        requested_tools = []
    if requested_tools:
        requested_web_capability = any(
            item.get("name") in {"web_search", "read_web_page"}
            for item in requested_tools
        )
        model_selected_plan = finalize_turn_execution_plan(
            body,
            user_message=user_message,
            decision=research_decision,
            preflight=tool_preflight,
            tool_requests=requested_tools,
            settings=settings,
            has_attachments=bool(current_attachments or attachment_prompt),
            model_research_escalation=requested_web_capability,
            model_tool_escalation=True,
        )
        tool_preflight = bind_required_capabilities(
            tool_preflight,
            model_selected_plan.required_capabilities,
            source="model_selected_turn_execution_plan",
        )
        body["_toolPreflight"] = tool_preflight
        research_decision = replace(
            research_decision,
            policy=model_selected_plan.research_policy,
            reasons=tuple(dict.fromkeys((
                *research_decision.reasons,
                "model_tool_request_finalized_by_turn_execution_plan",
            ))),
            plan_revision=max(
                research_decision.plan_revision + 1,
                model_selected_plan.revision,
            ),
        )
        prepared_action_turn, research_decision = synchronize_prepared_epistemic_plan(
            prepared_action_turn,
            research_decision,
        )
        capability = capability_envelope(
            turn_policy,
            research_decision,
            web_permission=bool(settings.get("permissionWebSearch")),
        )
        capability = capability_with_tool_preflight(
            capability,
            tool_preflight,
            settings,
            extra_tool_ids=[
                str(item.get("name") or "")
                for item in requested_tools
                if isinstance(item, dict)
            ],
        )
        capability = replace(
            capability,
            research_policy=model_selected_plan.research_policy,
            required_tools=model_selected_plan.required_capabilities,
            max_tool_rounds=model_selected_plan.max_tool_rounds,
            tool_policy="required",
        )
        prepared_action_turn = replace(
            prepared_action_turn,
            research_decision=research_decision.to_dict(),
            capability_envelope=capability.to_dict(),
        )
        body["_preparedActionTurn"] = prepared_action_turn
        persist_prepared_turn(prepared_action_turn)
        first_pass_progress_zh = re.sub(
            r"\s+", " ", str(parsed.get("reply") or "")
        ).strip()
        first_pass_progress_ja = re.sub(
            r"\s+", " ", str(parsed.get("spokenTextJa") or "")
        ).strip()
        progress_claims_completed_work = bool(re.search(
            r"(?:已经|刚刚)?(?:查到|搜到|检索到|核对到|打开了|看到了)|"
            r"(?:搜索|检索|查阅|资料|来源|网页)(?:结果|显示|表明|证明)|"
            r"\[(?:S|来源)\s*\d+\]|https?://",
            first_pass_progress_zh,
            re.I,
        ))
        requested_has_todo_tool = any(
            item.get("name") in {"todo_search", "todo_manage"}
            for item in requested_tools
        )
        if (
            callable(progress_callback)
            and first_pass_progress_zh
            and first_pass_progress_ja
            and not progress_claims_completed_work
            and not requested_has_todo_tool
        ):
            progress_payload = normalize_model_reply(
                {
                    **parsed,
                    "reply": first_pass_progress_zh,
                    "spokenTextJa": first_pass_progress_ja,
                    "memoryUpdates": [],
                },
                settings=settings,
                persona_snapshot=context.persona_snapshot,
            )
            progress_event = {
                "utteranceId": f"tool-progress-{uuid.uuid4().hex[:10]}",
                "progressKind": (
                    "research_bridge"
                    if any(
                        item.get("name") in {"web_search", "read_web_page"}
                        for item in requested_tools
                    )
                    else "tool_progress"
                ),
                "progressOnly": True,
                "textZh": str(progress_payload.get("reply") or first_pass_progress_zh),
                "spokenTextJa": str(
                    progress_payload.get("spokenTextJa") or first_pass_progress_ja
                ),
                "emotion": str(progress_payload.get("emotion") or "focused"),
                "emotionIntensity": progress_payload.get("emotionIntensity", 0.68),
                "standalone": True,
                "dependsOnResearchEvidence": False,
            }
            if progress_callback(progress_event) is not False:
                body["_semanticToolProgressProvided"] = True
                research_bridge_emitted = True
        update_chat_request_progress(request_id, "tool-executing", "模型请求补充证据，正在执行工具")
        requested_web_tools = [
            item
            for item in requested_tools
            if item.get("name") in {"web_search", "read_web_page"}
        ]
        requested_non_web_tools = [
            item
            for item in requested_tools
            if item.get("name") not in {"web_search", "read_web_page"}
        ]
        requested_has_visual_tool = any(
            item.get("name") in {"capture_screen", "capture_camera"}
            for item in requested_non_web_tools
        )
        tool_results = (
            execute_agent_tools(
                requested_non_web_tools,
                settings,
                user_message,
                preflight=tool_preflight,
                request_id=request_id,
                cognitive_state=cognitive_execution_state(body),
                schedule_decision=body.get("_scheduleDecision"),
                time_context=turn_time_context(body, settings),
                evidence_context=body,
            )
            if requested_has_visual_tool else execute_agent_tools(
                requested_non_web_tools,
                settings,
                user_message,
                preflight=tool_preflight,
                request_id=request_id,
                cognitive_state=cognitive_execution_state(body),
                schedule_decision=body.get("_scheduleDecision"),
                time_context=turn_time_context(body, settings),
                evidence_context=body,
            )
        )
        tool_results = effective_tool_results(tool_results)
        requested_read_tools = [
            item for item in requested_web_tools if item.get("name") == "read_web_page"
        ]
        if requested_read_tools:
            tool_results.extend(execute_agent_tools(
                requested_read_tools,
                settings,
                user_message,
                preflight=tool_preflight,
                request_id=request_id,
                cognitive_state=cognitive_execution_state(body),
                schedule_decision=body.get("_scheduleDecision"),
                time_context=turn_time_context(body, settings),
                evidence_context=body,
            ))
        requested_web_queries = [
            item for item in requested_web_tools if item.get("name") == "web_search"
        ]
        if requested_web_queries and cognitive_turn_state(body):
            # 检索证据返回同一个 ConversationBrain，由它继续阅读或追加查询，
            # 不再引入第二个语义研究规划器。
            tool_results.extend(execute_agent_tools(
                requested_web_queries, settings, user_message, preflight=tool_preflight,
                request_id=request_id, cognitive_state=cognitive_execution_state(body),
                schedule_decision=body.get("_scheduleDecision"),
                time_context=turn_time_context(body, settings),
                evidence_context=body,
            ))
        elif requested_web_queries:
            model_research_plan = normalize_web_research_plan(
                {
                    "needsWeb": True,
                    "resolvedQuestion": context_resolution.get("resolvedQuestion") or user_message,
                    "queries": [
                        {
                            "query": item.get("args", {}).get("query"),
                            "purpose": "补充模型判断仍缺少的公开事实",
                        }
                        for item in requested_web_queries
                    ],
                    "answerRequirements": ["综合检索证据后直接回答当前问题"],
                    "questionAnalysis": {
                        "intent": "执行主模型明确选择的公开信息检索",
                        "subject": web_research_anchor(
                            user_message,
                            str(context_resolution.get("resolvedQuestion") or user_message),
                        ),
                        "factsToVerify": ["主模型在本轮声明缺少的公开事实"],
                    },
                },
                user_message,
                context_resolution,
                messages,
            )
            if research_decision.source_policy_id:
                model_research_plan["sourcePolicyId"] = (
                    research_decision.source_policy_id
                )
            model_research_plan["entityTerms"] = list(
                research_decision.unknown_entities
            )
            emit_research_bridge_once(model_research_plan)
            model_research_result, model_research_plan, _, model_review_ms = (
                run_research_query_loop(
                    settings,
                    user_message,
                    messages,
                    model_research_plan,
                    context_resolution,
                    research_decision,
                    capability,
                    request_id=request_id,
                    cognitive_body=body,
                )
            )
            research_review_ms += model_review_ms
            tool_results.append(model_research_result)
            if deferred_browser_requests and model_research_result.get("ok"):
                # 显式打开动作若起初无法解析，主模型可随后选择联网查询。查询
                # 返回已验证目标后，在同一轮完成原副作用，避免保留过期拒绝。
                browser_args = browser_action_args_from_tool_results(
                    user_message,
                    [model_research_result],
                )
                if browser_args:
                    browser_args = browser_args_with_usage_tracking(
                        browser_args,
                        user_message=user_message,
                        request_id=request_id,
                        fallback_alias=str(pending_browser_action.get("targetHint") or ""),
                    )
                    browser_args["actionId"] = str(
                        pending_browser_action.get("actionId") or ""
                    )
                    browser_args["_actionGroupId"] = str(pending_browser_action.get("actionGroupId") or pending_browser_action.get("actionId") or "")
                    executed_tool_results = [
                        item for item in executed_tool_results
                        if not (
                            item.get("name") == "open_browser"
                            and item.get("error") in {
                                "no_verified_source_to_open",
                                "unresolved_browser_target",
                                "missing_url",
                            }
                        )
                    ]
                    tool_results.append(tool_open_browser(settings, browser_args))
        revised_schedule = reconsider_schedule_intent_after_tool_evidence(
            settings,
            user_message,
            messages,
            body.get("_scheduleDecision"),
            tool_results,
            request_id=request_id,
            body=body,
        )
        if isinstance(revised_schedule, Mapping):
            body["_scheduleDecision"] = dict(revised_schedule)
            tool_preflight = body_tool_preflight(
                body,
                user_message,
                messages,
                reply_mode=turn_policy.reply_mode,
                speech_act=utterance_interpretation.primary_speech_act,
            )
            known_schedule_signatures = {
                tool_request_signature(request)
                for request in [*inferred_requests, *requested_tools]
                if isinstance(request, Mapping)
            }
            evidence_schedule_requests = [
                item for item in schedule_tool_requests(revised_schedule)
                if str(item.get("name") or "") == "todo_manage"
                and tool_request_signature(item) not in known_schedule_signatures
            ]
            if evidence_schedule_requests:
                tool_results.extend(execute_agent_tools(
                    evidence_schedule_requests,
                    settings,
                    user_message,
                    preflight=tool_preflight,
                    request_id=request_id,
                    cognitive_state=cognitive_execution_state(body),
                    schedule_decision=body.get("_scheduleDecision"),
                    time_context=turn_time_context(body, settings),
                evidence_context=body,
                ))
                requested_tools.extend(evidence_schedule_requests)
        executed_tool_results = effective_tool_results([
            *executed_tool_results,
            *tool_results,
        ])
        record_tool_attempts(body, tool_results)
        if has_typed_visual_evidence(tool_results):
            turn_contract = bind_current_visual_observation_contract(
                turn_contract,
                semantic_text=semantic_task_request,
            )
            body["_cognitiveTurnContract"] = turn_contract
            visual_plan = EpistemicPlan.from_dict(prepared_action_turn.epistemic_plan)
            if visual_plan is not None:
                visual_plan = visual_plan.with_current_visual_observation(
                    subject=semantic_task_request,
                )
                prepared_action_turn = replace(
                    prepared_action_turn,
                    epistemic_plan=visual_plan.to_dict(),
                    epistemic_state=visual_plan.epistemic_state().to_dict(),
                )
                body["_preparedActionTurn"] = prepared_action_turn
        final_payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.55,
            "max_tokens": 6000 if file_output_expected else min(
                6_000,
                fast_stream_token_budget(turn_policy) + 240,
            ),
            "messages": [
                *base_messages,
                {"role": "assistant", "content": content},
                {"role": "system", "content": tool_context_prompt(
                    tool_results,
                    context_resolution,
                    max_chars=turn_policy.tool_evidence_max_chars,
                 model_owned=bool(cognitive_turn_state(body)))},
                *([{"role": "system", "content": visual_evidence_prompt(
                    tool_results,
                    user_question=semantic_task_request,
                )}] if visual_evidence_prompt(tool_results, user_question=semantic_task_request) else []),
                *([{"role": "system", "content": tool_grounding_instruction(tool_results)}] if tool_grounding_instruction(tool_results) else []),
                *([{"role": "system", "content": visual_relationship_reaction_instruction(tool_results, relationship_state)}]
                  if not (body.get('_turnPersonaContext') or {}).get('independentPersona')
                  and visual_relationship_reaction_instruction(tool_results, relationship_state)
                  and not has_typed_visual_evidence(tool_results) else []),
                {"role": "system", "content": (
                    "[工具返回后的认知续写] 依据已返回的执行状态与正文更新当前判断。"
                    "足以回答时输出 kind='turn'、cognitive、outcome 和 speechUnits，outcome.taskStatus 可省略；实际工具回执记录任务进度。"
                    "仍需不同证据时可 kind='tool_use'，但不要重发已经成功的动作。"
                    "执行失败按真实回执报告原因，不能把所有失败解释为权限不足。"
                    if cognitive_turn_state(body) else
                    "本轮工具结果已经返回。先判断是否足以完成 Turn Contract；足够时按照 semantic-v3 "
                    "输出 kind='turn'、emotion、proactiveDirective、scheduleIntent 和 speechUnits，"
                    "不要重复输出 reply/spokenTextJa 或运行状态。"
                    "若仍缺少一项确实必要的不同工具证据，可以输出 kind='tool_use' 和 calls，"
                    "但不得重复完全相同的工具与参数，也不得无依据地改写参数碰运气。"
                    "如果回执明确 attempted=false 且指出缺少参数，可依据原请求补齐参数后调用同一工具完成任务；"
                    "这不需要再次询问已经明确的授权。不要只说稍后补发或重试却结束本轮。"
                    "发送结果未知、正在发送或已经成功时不得重发。"
                    "工具失败或权限不足时要如实说明；只有完成当前操作确实依赖用户处理权限时，"
                    "才提出一个必要动作。联网取证失败不应变成长篇流程说明、让用户自行搜索或询问是否继续。"
                )},
                {"role": "system", "content": policy_prompt(turn_policy)},
                {"role": "system", "content": core_dialogue_turn_contract_prompt(
                    utterance_interpretation,
                    turn_contract,
                    model_owned=bool(cognitive_turn_state(body)),
                )},
            ],
        }
        if body.get("_researchBudget") or body.get("_toolBudget"):
            final_payload["messages"].append({"role": "system", "content": tool_budget_prompt(body)})
        if settings.get("useResponseFormat"):
            final_payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            final_payload["thinking"] = {"type": "disabled"}
        final_payload["messages"], context_pressure_metrics = pressure_managed_prompt(
            final_payload["messages"],
            settings,
            reserved_output_tokens=int(final_payload["max_tokens"]),
            request_id=request_id,
            stage="chat_tool_followup",
            source_metadata=messages,
        )
        update_chat_request_progress(request_id, "model-synthesizing", "工具结果已返回，正在综合证据形成回答")
        second_started = time.perf_counter()
        body["_cognitiveEvidence"] = {**(body.get("_cognitiveEvidence") or {}), **collect_evidence(executed_tool_results or tool_results)}
        content = send_conversation_brain_payload(
            body, settings, final_payload, timeout=90, request_id=request_id, purpose="chat_tool_followup"
        )
        semantic_task_request = current_task_semantic_request(body, user_message)
        answer_finish_reason = services.model.finish_reason()
        second_llm_ms = round((time.perf_counter() - second_started) * 1000)
        executed_request_signatures = {
            signature
            for item in executed_tool_results
            if isinstance(item, Mapping)
            for signature in (tool_result_request_signature(item),)
            if signature
        }
        executed_request_signatures.update(
            _attempted_request_signatures(
                [*inferred_requests, *requested_tools],
                [*pre_tool_results, *tool_results],
            )
        )
        retryable_failure_identities = _retryable_failure_identities(
            [*inferred_requests, *requested_tools],
            [*pre_tool_results, *tool_results],
        )
        successful_request_signatures = _successful_request_signatures(
            [*inferred_requests, *requested_tools],
            [*pre_tool_results, *tool_results],
        )
        rejected_request_retry_signatures = _rejected_request_retry_signatures(
            [*inferred_requests, *requested_tools],
            [*pre_tool_results, *tool_results],
        )
        rejected_request_plan_revisions = _rejected_request_plan_revisions(
            [*inferred_requests, *requested_tools],
            [*pre_tool_results, *tool_results],
        )
        tool_rounds = prior_tool_rounds + 1
        body["_cognitiveToolRoundsUsed"] = tool_rounds

        tool_loop_stop_reason = "model_returned_final"
        while True:
            followup_parsed = parse_json_object(content) or {}
            select_tool_budget(body, followup_parsed, settings)
            sync_tool_usage(body, executed_tool_results)
            proposed_followup_requests = constrain_model_tool_requests(
                normalize_tool_requests(followup_parsed),
                user_message,
                context_resolution,
                messages,
                body.get("_scheduleDecision"),
                cognitive_state=cognitive_execution_state(body), evidence=body.get("_cognitiveEvidence") or {},
            )
            if research_decision.policy == "forbidden":
                proposed_followup_requests = [
                    item
                    for item in proposed_followup_requests
                    if item.get("name") not in {"web_search", "read_web_page"}
                ]
            proposed_followup_requests, rejected_followup_sensors = (
                constrain_attachment_sensor_requests(
                    proposed_followup_requests,
                    user_message,
                    has_bound_attachment=has_bound_attachment,
                )
            )
            tool_preflight = bind_structured_perception_request(
                tool_preflight,
                proposed_followup_requests,
                source="query_loop_followup",
            )
            body["_toolPreflight"] = tool_preflight
            for item in rejected_followup_sensors:
                write_server_log(
                    "attachment_sensor_request_rejected",
                    requestId=request_id,
                    toolName=str(item.get("name") or ""),
                    source="query_loop_followup",
                    reason="current_attachment_is_authoritative_target",
                )
            _record_filtered_calls(body, normalize_tool_requests(followup_parsed), proposed_followup_requests)
            if not proposed_followup_requests:
                if cognitive_turn_state(body) and str(followup_parsed.get("kind") or "") == "tool_use":
                    content, terminal_ms = request_terminal_tool_synthesis(content, reason="requested_tools_rejected")
                    second_llm_ms += terminal_ms
                    tool_loop_stop_reason = "requested_tools_rejected"
                else:
                    tool_loop_stop_reason = "model_returned_final"
                break
            current_execution_state = cognitive_execution_state(body)
            current_plan_revision = (
                (current_execution_state.get("task") or {}).get("planRevision")
                if isinstance(current_execution_state, Mapping)
                else None
            )
            followup_requests, _repeated_rejected_requests = _eligible_followup_requests(
                proposed_followup_requests,
                executed_request_signatures,
                rejected_request_retry_signatures,
                retryable_failure_identities=retryable_failure_identities,
                successful_signatures=successful_request_signatures,
                rejected_plan_revisions=rejected_request_plan_revisions,
                current_plan_revision=current_plan_revision,
            )
            if not followup_requests:
                content, terminal_ms = request_terminal_tool_synthesis(
                    content,
                    reason="duplicate_tool_request",
                )
                second_llm_ms += terminal_ms
                answer_finish_reason = services.model.finish_reason()
                tool_loop_stop_reason = "duplicate_tool_request"
                break
            resource_reason = tool_budget_exhausted(body, followup_requests)
            if resource_reason:
                content, terminal_ms = request_terminal_tool_synthesis(
                    content,
                    reason=resource_reason,
                )
                second_llm_ms += terminal_ms
                answer_finish_reason = services.model.finish_reason()
                tool_loop_stop_reason = resource_reason
                break
            tool_rounds += 1
            write_server_log(
                "tool_query_loop_round_started",
                requestId=request_id,
                round=tool_rounds,
                requestedTools=[
                    str(item.get("name") or "")
                    for item in followup_requests
                ],
            )
            update_chat_request_progress(
                request_id,
                "tool-executing",
                f"正在执行第 {tool_rounds} 轮必要工具并核对新增证据",
            )
            followup_results = execute_agent_tools(
                followup_requests,
                settings,
                user_message,
                preflight=tool_preflight,
                request_id=request_id,
                cognitive_state=cognitive_execution_state(body),
                schedule_decision=body.get("_scheduleDecision"),
                time_context=turn_time_context(body, settings),
                evidence_context=body,
            )
            if not followup_results:
                content, terminal_ms = request_terminal_tool_synthesis(
                    content,
                    reason="no_tool_result",
                )
                second_llm_ms += terminal_ms
                answer_finish_reason = services.model.finish_reason()
                tool_loop_stop_reason = "no_tool_result"
                break
            executed_tool_results = effective_tool_results([
                *executed_tool_results,
                *followup_results,
            ])
            record_tool_attempts(body, followup_results)
            if has_typed_visual_evidence(followup_results):
                turn_contract = bind_current_visual_observation_contract(
                    turn_contract,
                    semantic_text=semantic_task_request,
                )
                visual_plan = EpistemicPlan.from_dict(prepared_action_turn.epistemic_plan)
                if visual_plan is not None:
                    visual_plan = visual_plan.with_current_visual_observation(
                        subject=semantic_task_request,
                    )
                    prepared_action_turn = replace(
                        prepared_action_turn,
                        epistemic_plan=visual_plan.to_dict(),
                        epistemic_state=visual_plan.epistemic_state().to_dict(),
                    )
                    body["_preparedActionTurn"] = prepared_action_turn
            executed_request_signatures.update(
                signature
                for item in followup_results
                if isinstance(item, Mapping)
                for signature in (tool_result_request_signature(item),)
                if signature
            )
            executed_request_signatures.update(
                _attempted_request_signatures(followup_requests, followup_results)
            )
            retryable_failure_identities.update(
                _retryable_failure_identities(followup_requests, followup_results)
            )
            successful_request_signatures.update(
                _successful_request_signatures(followup_requests, followup_results)
            )
            rejected_request_retry_signatures.update(
                _rejected_request_retry_signatures(followup_requests, followup_results)
            )
            for signature, revisions in _rejected_request_plan_revisions(
                followup_requests,
                followup_results,
            ).items():
                rejected_request_plan_revisions.setdefault(signature, set()).update(revisions)
            is_last_round = bool(tool_budget_exhausted(body))
            loop_payload: dict[str, Any] = {
                "model": settings["apiModel"],
                "temperature": 0.55,
                "max_tokens": 6000 if file_output_expected else min(
                    6_000,
                    fast_stream_token_budget(turn_policy) + 240,
                ),
                "messages": [
                    *base_messages,
                    {"role": "assistant", "content": content},
                    {"role": "system", "content": tool_context_prompt(
                        executed_tool_results,
                        context_resolution,
                        max_chars=turn_policy.tool_evidence_max_chars,
                     model_owned=bool(cognitive_turn_state(body)))},
                    *([{"role": "system", "content": visual_evidence_prompt(
                        executed_tool_results,
                        user_question=semantic_task_request,
                    )}] if visual_evidence_prompt(
                        executed_tool_results,
                        user_question=semantic_task_request,
                    ) else []),
                    *(
                        [{"role": "system", "content": tool_grounding_instruction(
                            executed_tool_results
                        )}]
                        if tool_grounding_instruction(executed_tool_results)
                        else []
                    ),
                    {"role": "system", "content": (
                        "根据用户目标检查所有已返回证据，决定继续取证或按 semantic-v3 输出最终 kind='turn'。"
                        "不要复述工具调度过程。"
                        + (
                            "本轮已到资源上限，不得再输出 tool_use；"
                            "目标未完成时 outcome.status=partial，保留未完成任务，说明已知结论与待核实事项，不要声称已完成研究。"
                            if is_last_round else
                            "还有影响答案的证据缺口时，输出 kind='tool_use' 和 calls。"
                            "同一工具可继续读取不同来源、章节或页面范围；不要重复没有新增信息的调用。"
                        )
                    )},
                    {"role": "system", "content": core_dialogue_turn_contract_prompt(
                        utterance_interpretation,
                        turn_contract,
                        model_owned=bool(cognitive_turn_state(body)),
                    )},
                ],
            }
            if body.get("_researchBudget") or body.get("_toolBudget"):
                loop_payload["messages"].append({"role": "system", "content": tool_budget_prompt(body)})
            if settings.get("useResponseFormat"):
                loop_payload["response_format"] = {"type": "json_object"}
            if "deepseek.com" in settings["apiEndpoint"]:
                loop_payload["thinking"] = {"type": "disabled"}
            loop_payload["messages"], context_pressure_metrics = pressure_managed_prompt(
                loop_payload["messages"],
                settings,
                reserved_output_tokens=int(loop_payload["max_tokens"]),
                request_id=request_id,
                stage=f"chat_tool_round_{tool_rounds}",
                source_metadata=messages,
            )
            loop_started = time.perf_counter()
            body["_cognitiveEvidence"] = {**(body.get("_cognitiveEvidence") or {}), **collect_evidence(executed_tool_results)}
            content = send_conversation_brain_payload(
                body, settings,
                loop_payload,
                timeout=90,
                request_id=request_id,
                purpose=f"chat_tool_query_loop_round_{tool_rounds}",
            )
            semantic_task_request = current_task_semantic_request(body, user_message)
            answer_finish_reason = services.model.finish_reason()
            second_llm_ms += round((time.perf_counter() - loop_started) * 1000)
            write_server_log(
                "tool_query_loop_round_completed",
                requestId=request_id,
                round=tool_rounds,
                resultCount=len(followup_results),
            )
        write_server_log(
            "tool_query_loop_stopped",
            requestId=request_id,
            toolRounds=tool_rounds,
            researchBudget={k: v for k, v in (body.get("_researchBudget") or {}).items() if k != "startedAt"},
            toolBudget={k: v for k, v in (body.get("_toolBudget") or {}).items() if k != "startedAt"},
            stopReason=(
                tool_loop_stop_reason
            ),
        )
    body["_cognitiveToolRoundsUsed"] = (
        tool_rounds if requested_tools else prior_tool_rounds
    )

    rounds_used = tool_rounds if requested_tools else prior_tool_rounds
    return ToolLoopResult(
        content=content,
        finish_reason=answer_finish_reason,
        executed_results=executed_tool_results,
        requested_tools=requested_tools,
        parsed=parsed,
        rounds=rounds_used,
        stop_reason=tool_loop_stop_reason,
        model_ms=second_llm_ms,
        research_review_ms=research_review_ms,
        research_bridge_emitted=research_bridge_emitted,
        todo_continuation_transition=todo_continuation_transition,
        semantic_task_request=semantic_task_request,
        capability=capability,
        research_decision=research_decision,
        prepared_action_turn=prepared_action_turn,
        tool_preflight=tool_preflight,
        turn_contract=turn_contract,
        context_pressure_metrics=context_pressure_metrics,
        uncertainty_triggered_web=uncertainty_triggered_web,
    )
