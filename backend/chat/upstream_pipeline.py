"""完整聊天生成的应用编排。

本模块按准备、工具与研究取证、模型工具循环、验收恢复和结果投影推进一轮
请求。``body`` 仍是认知与工具状态的唯一 owner，模块只持有当前调用的临时
变量。纯规则直接引用 ``agent``/``backend`` 的实际 owner；端口仅连接仍由运行
时拥有的上下文、权限工具、模型 I/O、持久化和事件边界，不反向导入 server。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import re
import time
import uuid
from typing import Any, Mapping, Protocol

from agent.cognitive_brain import CognitiveProtocolError
from agent.cognitive_runtime import collect_evidence
from agent.dialogue_architecture import (
    bind_current_visual_failure_contract,
    bind_current_visual_observation_contract,
    build_turn_contract,
    choose_turn_policy,
    policy_prompt,
)
from agent.discourse_reference import (
    DiscourseLedger,
    extend_with_visual_observations,
    resolve_discourse_references,
)
from agent.epistemic_architecture import grounded_claim_gate_decision, research_receipt_from_tool_results
from agent.fingerprints import runtime_fingerprint
from agent.multimodal_chat import native_visual_chat_enabled
from agent.persona_context import PersonaContextSnapshot
from agent.research_architecture import EpistemicPlan, capability_envelope, create_epistemic_plan
from agent.schedule_architecture import SCHEDULE_INTENT_PROMPT
from agent.tool_architecture import (
    DEFAULT_TOOL_REGISTRY,
    PerceptionNeed,
    bind_required_capabilities,
    merge_searched_tool_candidates,
)
from agent.turn_context import pin_reply_context
from agent.visual_grounding import has_current_visual_evidence as has_typed_visual_evidence, visual_evidence_prompt
from backend.chat import tool_query_loop as chat_tool_query_loop
from backend.chat.completion import reply_integrity_issues
from backend.chat.parsing import extract_complete_json_string_field, looks_like_incomplete_structured_reply, parse_json_object
from backend.common.text import unique_strings
from backend.common.values import clamp_float, safe_nonnegative_int as _safe_nonnegative_int
from backend.context.reference import _reference_evidence, contextual_reference_prompt, visual_relevant_context_prompt
from backend.research.citations import readable_public_source_uids
from backend.research.query import web_search_freshness, web_search_query_from_message
from backend.tools.outcomes import effective_tool_results


class RuntimeOperations(Protocol):
    def load_settings(self, *args: Any, **kwargs: Any) -> Any: ...
    def configured(self, *args: Any, **kwargs: Any) -> Any: ...
    def update_progress(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_log(self, *args: Any, **kwargs: Any) -> Any: ...
    def now_iso(self, *args: Any, **kwargs: Any) -> Any: ...
    @property
    def last_finish_reason(self) -> Any: ...

class ContextOperations(Protocol):
    def contextual_reference_resolution(self, *args: Any, **kwargs: Any) -> Any: ...
    def frozen_turn_context_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def frozen_turn_relationship_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_turn_context_artifacts(self, *args: Any, **kwargs: Any) -> Any: ...
    def read_relationship_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def build_persona_context_snapshot(self, *args: Any, **kwargs: Any) -> Any: ...
    def ensure_turn_persona_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def retrieve_turn_memories(self, *args: Any, **kwargs: Any) -> Any: ...
    def bind_cognitive_memories(self, *args: Any, **kwargs: Any) -> Any: ...
    def conversation_history_messages(self, *args: Any, **kwargs: Any) -> Any: ...
    def conversation_image_inputs(self, *args: Any, **kwargs: Any) -> Any: ...
    def recalled_screen_capture_results(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_runtime_has_user_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def message_needs_recent_context(self, *args: Any, **kwargs: Any) -> Any: ...

class ContextPromptsOperations(Protocol):
    def identity_grounding_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def speaker_attribution_evidence_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def pending_assistant_question_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def pending_todo_action_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_schedule_workflow_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def relationship_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def dignity_boundary_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def transition_style_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def permission_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def conversation_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def session_temporal_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def immediate_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def body_context_control_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def body_reply_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def explicit_user_position_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def retrieved_memory_prompt(self, *args: Any, **kwargs: Any) -> Any: ...

class DeliveryPromptsOperations(Protocol):
    def attachment_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def tool_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def tool_grounding_instruction(self, *args: Any, **kwargs: Any) -> Any: ...
    def visual_relationship_reaction_instruction(self, *args: Any, **kwargs: Any) -> Any: ...
    def tool_preflight_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def canon_style_motif_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def nickname_reaction_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def claim_level_fact_policy_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def proactive_control_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def ordinary_core_dialogue_protocol(self, *args: Any, **kwargs: Any) -> Any: ...
    def core_dialogue_turn_contract_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def todo_reconciliation_prompt(self, *args: Any, **kwargs: Any) -> Any: ...

class TurnPlanningOperations(Protocol):
    def current_task_semantic_request(self, *args: Any, **kwargs: Any) -> Any: ...
    def body_prepared_action_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def apply_contextual_research_subject(self, *args: Any, **kwargs: Any) -> Any: ...
    def bind_resolved_task_interpretation(self, *args: Any, **kwargs: Any) -> Any: ...
    def utterance_interpretation_from_prepared(self, *args: Any, **kwargs: Any) -> Any: ...
    def body_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...
    def capability_with_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...
    def enrich_prepared_turn_with_research(self, *args: Any, **kwargs: Any) -> Any: ...
    def reconcile_research_with_tool_preflight(self, *args: Any, **kwargs: Any) -> Any: ...
    def reconcile_preanswer_research_route(self, *args: Any, **kwargs: Any) -> Any: ...
    def reconcile_research_decision_with_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def finalize_turn_execution_plan(self, *args: Any, **kwargs: Any) -> Any: ...
    def persist_prepared_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_tool_preflight_log(self, *args: Any, **kwargs: Any) -> Any: ...
    def synchronize_prepared_epistemic_plan(self, *args: Any, **kwargs: Any) -> Any: ...
    def pressure_managed_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def fast_stream_token_budget(self, *args: Any, **kwargs: Any) -> Any: ...

class TaskPlanningOperations(Protocol):
    def body_todo_reconciliation(self, *args: Any, **kwargs: Any) -> Any: ...
    def cognitive_turn_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def cognitive_execution_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def task_local_dialogue_messages(self, *args: Any, **kwargs: Any) -> Any: ...
    def task_review_dialogue_messages(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_pending_action(self, *args: Any, **kwargs: Any) -> Any: ...
    def todo_context_from_tool_results(self, *args: Any, **kwargs: Any) -> Any: ...
    def finalize_schedule_workflow_from_results(self, *args: Any, **kwargs: Any) -> Any: ...
    def apply_todo_continuation_transition(self, *args: Any, **kwargs: Any) -> Any: ...
    def apply_model_schedule_payload(self, *args: Any, **kwargs: Any) -> Any: ...
    def turn_time_context(self, *args: Any, **kwargs: Any) -> Any: ...

class ToolPlanningOperations(Protocol):
    def is_attachment_followup_query(self, *args: Any, **kwargs: Any) -> Any: ...
    def explicitly_requested_sensor_tools(self, *args: Any, **kwargs: Any) -> Any: ...
    def infer_agent_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def task_bound_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def constrain_attachment_sensor_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def normalize_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def constrain_model_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def merge_preexecution_tool_requests(self, *args: Any, **kwargs: Any) -> Any: ...
    def preexecuted_attachment_request_satisfied(self, *args: Any, **kwargs: Any) -> Any: ...
    def bind_structured_perception_request(self, *args: Any, **kwargs: Any) -> Any: ...
    def visual_result_matches_perception_target(self, *args: Any, **kwargs: Any) -> Any: ...

class ToolExecutionOperations(Protocol):
    @property
    def tool_loop_services(self) -> Any: ...
    def attachment_read_tool_results(self, *args: Any, **kwargs: Any) -> Any: ...
    def execute_agent_tools(self, *args: Any, **kwargs: Any) -> Any: ...
    def effective_tool_results_for_status(self, *args: Any, **kwargs: Any) -> Any: ...
    def enforce_external_action_receipt(self, *args: Any, **kwargs: Any) -> Any: ...
    def displayed_tool_name(self, *args: Any, **kwargs: Any) -> Any: ...
    def public_attachment_tool_result(self, *args: Any, **kwargs: Any) -> Any: ...
    def browser_action_args_from_tool_results(self, *args: Any, **kwargs: Any) -> Any: ...
    def browser_args_with_usage_tracking(self, *args: Any, **kwargs: Any) -> Any: ...
    def save_pending_browser_action(self, *args: Any, **kwargs: Any) -> Any: ...
    def clear_pending_action(self, *args: Any, **kwargs: Any) -> Any: ...
    def record_action_receipt(self, *args: Any, **kwargs: Any) -> Any: ...
    def tool_open_browser(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_pending_action_record(self, *args: Any, **kwargs: Any) -> Any: ...

class ResearchOperations(Protocol):
    def plan_web_research(self, *args: Any, **kwargs: Any) -> Any: ...
    def fallback_web_research_plan(self, *args: Any, **kwargs: Any) -> Any: ...
    def fallback_web_research_query(self, *args: Any, **kwargs: Any) -> Any: ...
    def outbound_web_query(self, *args: Any, **kwargs: Any) -> Any: ...
    def run_research_query_loop(self, *args: Any, **kwargs: Any) -> Any: ...
    def recent_message_web_sources(self, *args: Any, **kwargs: Any) -> Any: ...
    def public_web_sources(self, *args: Any, **kwargs: Any) -> Any: ...
    def audit_model_research_need(self, *args: Any, **kwargs: Any) -> Any: ...
    def needs_post_answer_research_audit(self, *args: Any, **kwargs: Any) -> Any: ...
    def audit_evidence_limited_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def ensure_verified_web_citation(self, *args: Any, **kwargs: Any) -> Any: ...
    def attach_verified_web_sources(self, *args: Any, **kwargs: Any) -> Any: ...
    def freeze_source_batch(self, *args: Any, **kwargs: Any) -> Any: ...
    def persist_source_batch(self, *args: Any, **kwargs: Any) -> Any: ...
    def web_answer_diagnostics(self, *args: Any, **kwargs: Any) -> Any: ...
    def context_repair_needs_web_research(self, *args: Any, **kwargs: Any) -> Any: ...
    def sanitize_secondary_only_precision(self, *args: Any, **kwargs: Any) -> Any: ...
    def select_best_research_candidate(self, *args: Any, **kwargs: Any) -> Any: ...
    def synthesize_evidence_limited_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def evidence_synthesis_appends_prior_answer(self, *args: Any, **kwargs: Any) -> Any: ...

class ModelOperations(Protocol):
    def read_system_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def send_conversation_brain_payload(self, *args: Any, **kwargs: Any) -> Any: ...
    def normalize_model_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def model_reply_text(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_tool_grounded_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_context_grounded_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_incomplete_model_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_task_completion_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_dialogue_style_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_dialogue_fields_once(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_empty_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_frozen_reply_emotion_once(self, *args: Any, **kwargs: Any) -> Any: ...
    def select_best_model_candidate(self, *args: Any, **kwargs: Any) -> Any: ...
    def terminal_grounding_boundary_reply(self, *args: Any, **kwargs: Any) -> Any: ...

class ValidationOperations(Protocol):
    def tool_grounding_violation(self, *args: Any, **kwargs: Any) -> Any: ...
    def context_grounding_violation(self, *args: Any, **kwargs: Any) -> Any: ...
    def task_completion_violation(self, *args: Any, **kwargs: Any) -> Any: ...
    def dialogue_validation_for_result(self, *args: Any, **kwargs: Any) -> Any: ...
    def dialogue_output_validation(self, *args: Any, **kwargs: Any) -> Any: ...
    def enforce_visual_semantic_grounding(self, *args: Any, **kwargs: Any) -> Any: ...

@dataclass(frozen=True)
class PipelinePolicy:
    amadeus_tool_rule: str
    cognitive_dialogue_reply_rule: str
    emotion_selection_rule: str
    max_attachment_context_chars: int
    natural_dialogue_style: str
    speaker_attribution_rule: str
    technical_accuracy_rule: str
    utterance_stream_reply_rule: str
    protocol_error: type[Exception]

@dataclass(frozen=True)
class UpstreamPipelineServices:
    runtime: RuntimeOperations
    context: ContextOperations
    context_prompts: ContextPromptsOperations
    delivery_prompts: DeliveryPromptsOperations
    turn_planning: TurnPlanningOperations
    task_planning: TaskPlanningOperations
    tool_planning: ToolPlanningOperations
    tool_execution: ToolExecutionOperations
    research: ResearchOperations
    model: ModelOperations
    validation: ValidationOperations
    policy: PipelinePolicy

def action_receipts_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从有效工具结果投影动作回执，不改变事务结果的 owner。"""

    return [
        dict(item["actionReceipt"])
        for item in effective_tool_results(results)
        if isinstance(item, dict)
        and isinstance(item.get("actionReceipt"), dict)
        and item["actionReceipt"].get("actionId")
    ]


def tool_result_was_attempted(item: Mapping[str, Any]) -> bool:
    """被准入层拒绝的调用只算反馈，不算已经执行。"""

    return item.get("attempted") is not False and item.get("error") != "cognitive_task_constraint"


@dataclass
class UpstreamRunState:
    """单轮完整生成的唯一临时状态 owner；不保存会话或提交历史。"""

    body: dict[str, Any]
    progress_callback: Any | None = None
    persona_snapshot: PersonaContextSnapshot | None = None
    continuation: Mapping[str, Any] | None = None
    active_persona_snapshot: Any = None
    answer_finish_reason: Any = None
    attachment_prompt: Any = None
    attachment_prompt_for_model: Any = None
    attachment_tool_results: Any = None
    attribution_evidence: Any = None
    base_messages: Any = None
    capability: Any = None
    cognitive_accepted: Any = None
    compact_core_protocol: Any = None
    content: Any = None
    context_grounding_repair_ms: Any = None
    context_pressure_metrics: Any = None
    context_recovery_triggered_web: Any = None
    context_recovery_web_ms: Any = None
    context_resolution: Any = None
    continuation_content: Any = None
    continuation_tool_results: Any = None
    continuation_tool_rounds: Any = None
    current_attachments: Any = None
    deep_persona_prompt: Any = None
    deferred_browser_requests: Any = None
    dialogue_style_repair_ms: Any = None
    dialogue_validation: Any = None
    discourse_ledger: Any = None
    discourse_resolution: Any = None
    escalation: Any = None
    evidence_limited_precision_audit_ms: Any = None
    evidence_limited_synthesis_ms: Any = None
    executed_tool_results: Any = None
    existing_tool_rounds: Any = None
    explicitly_requested_sensors: Any = None
    file_output_expected: Any = None
    first_llm_ms: Any = None
    grounding_repair_ms: Any = None
    grounding_tool_results: Any = None
    has_bound_attachment: Any = None
    has_web_grounding: Any = None
    identity_context: Any = None
    inferred_requests: Any = None
    interrupted_previous_reply: Any = None
    memories: Any = None
    memory_need: Any = None
    memory_records: Any = None
    messages: Any = None
    non_json_reply_recovered: Any = None
    non_visual_grounding_tool_results: Any = None
    parsed: Any = None
    pending_browser_action: Any = None
    pending_question_context: Any = None
    pending_relation: Any = None
    pending_todo_before: Any = None
    pipeline_started: Any = None
    pre_context_result: Any = None
    pre_tool_ms: Any = None
    pre_tool_results: Any = None
    preexecution_tool_rounds: Any = None
    prepared_action_turn: Any = None
    proactive_context: Any = None
    prompt: Any = None
    prompt_tool_results: Any = None
    recalled_visual_results: Any = None
    relationship_state: Any = None
    repair_started: Any = None
    reply_integrity_repair_ms: Any = None
    request_id: Any = None
    requested_tools: Any = None
    research_bridge_emitted: Any = None
    research_bridge_ms: Any = None
    research_candidates: Any = None
    research_decision: Any = None
    research_plan: Any = None
    research_planning_ms: Any = None
    research_review: Any = None
    research_review_ms: Any = None
    result: Any = None
    schedule_state_context: Any = None
    second_llm_ms: Any = None
    semantic_research_audit_failed: Any = None
    semantic_task_request: Any = None
    settings: Any = None
    structured_transport_invalid: Any = None
    successive_inputs: Any = None
    supplied_memories: Any = None
    task_completion_repair_ms: Any = None
    todo_continuation_transition: Any = None
    todo_pending_context: Any = None
    todo_reconciliation: Any = None
    tool_grounding_candidates: Any = None
    tool_preflight: Any = None
    tool_started: Any = None
    turn_contract: Any = None
    turn_persona_context: Any = None
    turn_policy: Any = None
    uncertainty_triggered_web: Any = None
    user_message: Any = None
    utterance_interpretation: Any = None
    visual_focus_mode: Any = None
    visual_grounding_active: Any = None
    visual_grounding_audit_ms: Any = None
    visual_grounding_rewrite_ms: Any = None
    web_evidence_status: Any = None
    emit_research_bridge_once: Any = None


def prepare_upstream_turn(services: UpstreamPipelineServices, state: UpstreamRunState) -> None:
    """冻结输入并确定能力、研究和工具执行计划。"""

    state.pipeline_started = time.perf_counter()
    state.settings = services.runtime.load_settings()
    state.request_id = str(state.body.get("requestId") or "").strip()
    state.settings = {**state.settings, "_directVisualChat": native_visual_chat_enabled(state.settings)}
    state.continuation = (
        deepcopy(dict(state.continuation))
        if isinstance(state.continuation, Mapping)
        else None
    )
    state.continuation_content = (
        str(state.continuation.get("content") or "")
        if state.continuation is not None else ""
    )
    state.continuation_tool_results = (
        [dict(item) for item in state.continuation.get("toolResults", [])
         if isinstance(item, Mapping)]
        if state.continuation is not None and isinstance(state.continuation.get("toolResults"), list)
        else []
    )
    state.existing_tool_rounds = _safe_nonnegative_int(
        state.body.get("_cognitiveToolRoundsUsed", 0)
    )
    state.continuation_tool_rounds = (
        max(
            _safe_nonnegative_int(
                state.continuation.get("toolRounds", state.existing_tool_rounds)
            ),
            1 if state.continuation_tool_results else 0,
        )
        if state.continuation is not None else state.existing_tool_rounds
    )
    if state.continuation is not None:
        if not services.task_planning.cognitive_turn_state(state.body):
            raise CognitiveProtocolError(
                "final_projection_continuation_requires_cognitive_state"
            )
        if not state.continuation_content:
            raise CognitiveProtocolError(
                "final_projection_continuation_missing_candidate"
            )
        continuation_preview = parse_json_object(state.continuation_content) or {}
        continuation_requests = (
            continuation_preview.get("calls")
            or continuation_preview.get("toolRequests")
            or []
        ) if isinstance(continuation_preview, Mapping) else []
        if (
            state.continuation_tool_rounds >= 3
            and str(continuation_preview.get("kind") or "").lower() == "tool_use"
            and isinstance(continuation_requests, list)
            and continuation_requests
        ):
            raise CognitiveProtocolError(
                "final_projection_continuation_tool_round_budget_exhausted"
            )
    services.runtime.update_progress(state.request_id, "context", "正在整理当前对话、记忆与人格上下文")
    state.messages = state.body.get("messages") if isinstance(state.body.get("messages"), list) else []
    state.supplied_memories = state.body.get("memories") if isinstance(state.body.get("memories"), list) else []
    state.successive_inputs = [
        str(item).strip()
        for item in (state.body.get("userMessages") if isinstance(state.body.get("userMessages"), list) else [])
        if str(item).strip()
    ]
    state.user_message = "\n".join(state.successive_inputs) if state.successive_inputs else str(state.body.get("userMessage") or "")
    state.semantic_task_request = services.turn_planning.current_task_semantic_request(state.body, state.user_message)
    incoming_tool_escalation = (
        {}
        if state.continuation is not None
        else state.body.get("_toolEscalation")
        if isinstance(state.body.get("_toolEscalation"), Mapping)
        else {}
    )
    if (
        isinstance(incoming_tool_escalation.get("scheduleIntent"), Mapping)
        and not isinstance(state.body.get("_scheduleDecision"), Mapping)
    ):
        state.body["_scheduleDecision"] = services.task_planning.apply_model_schedule_payload(
            incoming_tool_escalation,
            request_id=state.request_id,
            user_message=state.user_message,
        )
    state.prepared_action_turn = services.turn_planning.body_prepared_action_turn(
        state.body,
        state.user_message,
        request_id=state.request_id,
    )
    state.pending_relation = state.prepared_action_turn.pending_relation.relation
    if state.pending_relation == "cancel" and not services.task_planning.cognitive_turn_state(state.body):
        services.tool_execution.clear_pending_action()
    elif state.pending_relation == "pause" and not services.task_planning.cognitive_turn_state(state.body):
        pending = services.task_planning.current_pending_action()
        if pending:
            pending["status"] = "paused"
            pending["updatedAt"] = services.runtime.now_iso()
            services.tool_execution.write_pending_action_record(pending)
    state.pending_todo_before = ({} if state.body.get("_scheduledTaskExecution")
                                 else services.task_planning.current_pending_action())
    state.pending_todo_before = (
        state.pending_todo_before
        if state.pending_todo_before.get("type") == "todo_mutation" else {}
    )
    state.todo_continuation_transition: dict[str, Any] = {}
    attachment_requested = bool(
        state.body.get("attachments") if isinstance(state.body.get("attachments"), list) else []
    ) or services.tool_planning.is_attachment_followup_query(state.user_message)
    state.file_output_expected = attachment_requested and bool(re.search(
        r"修改|改写|改一下|改成|改为|编辑|修复|重写|更新|生成|创建|新建|输出|导出|保存|补全|翻译|替换|换成|删掉|追加",
        state.user_message,
        re.I,
    ))
    state.body["_attachmentOutputRequested"] = state.file_output_expected
    state.utterance_interpretation = services.turn_planning.utterance_interpretation_from_prepared(
        state.prepared_action_turn
    )
    preliminary_policy = choose_turn_policy(
        state.user_message,
        has_attachments=attachment_requested,
        file_output_expected=state.file_output_expected,
        interpretation=state.utterance_interpretation,
    )
    state.attachment_prompt = services.delivery_prompts.attachment_context_prompt(
        state.body,
        state.messages,
        state.user_message,
        max_chars=(
            services.policy.max_attachment_context_chars
            if state.file_output_expected else preliminary_policy.tool_evidence_max_chars
        ),
    )
    state.interrupted_previous_reply = bool(state.body.get("interruptedPreviousReply"))
    state.proactive_context = str(state.body.get("proactiveContext") or "").strip()
    state.relationship_state = services.context.frozen_turn_relationship_state(state.body) or services.context.read_relationship_state()
    state.identity_context = services.context_prompts.identity_grounding_prompt(state.user_message)
    # 人设组合由后端统一负责。旧客户端可能仍会发送已废弃的提示词；若继续接收，
    # 会绕过分层检索，并使同一服务因打开它的界面不同而产生不同行为。
    state.prompt = services.model.read_system_prompt()
    state.attribution_evidence = services.context_prompts.speaker_attribution_evidence_prompt(state.messages, state.user_message)
    state.context_resolution = services.context.contextual_reference_resolution(
        state.user_message,
        state.messages,
        request_id=state.request_id,
        current_artifacts=services.context.current_turn_context_artifacts(state.body),
        resolved_task=state.semantic_task_request,
        context_state=services.context.frozen_turn_context_state(state.body),
        model_owned=bool(services.task_planning.cognitive_turn_state(state.body)),
    )
    state.body["_contextResolution"] = state.context_resolution
    state.body["_discourseLedger"] = state.context_resolution.get("referenceLedger", {})
    state.prepared_action_turn = services.turn_planning.apply_contextual_research_subject(
        state.prepared_action_turn,
        state.context_resolution,
    )
    state.body["_preparedActionTurn"] = state.prepared_action_turn
    state.utterance_interpretation = services.turn_planning.utterance_interpretation_from_prepared(
        state.prepared_action_turn
    )
    state.utterance_interpretation = services.turn_planning.bind_resolved_task_interpretation(state.utterance_interpretation, state.body)
    state.pending_question_context = services.context_prompts.pending_assistant_question_prompt(state.messages, state.user_message)
    state.todo_pending_context = ("" if state.body.get("_scheduledTaskExecution")
                                  else services.context_prompts.pending_todo_action_prompt())
    state.schedule_state_context = ("" if state.body.get("_scheduledTaskExecution")
                                    else services.context_prompts.current_schedule_workflow_prompt())
    state.active_persona_snapshot = state.persona_snapshot or services.context.build_persona_context_snapshot(
        state.user_message,
        state.messages,
        state.context_resolution,
        state.relationship_state,
        cognitive_body=state.body,
    )
    state.prompt = state.active_persona_snapshot.system_prompt
    state.deep_persona_prompt = state.active_persona_snapshot.prompt_text
    state.turn_persona_context = services.context.ensure_turn_persona_context(
        state.body, state.user_message, snapshot=state.active_persona_snapshot, relationship_state=state.relationship_state,
    )
    loaded_persona_modules = list(state.active_persona_snapshot.provider_module_ids)
    state.context_resolution["personaModules"] = loaded_persona_modules
    state.tool_started = time.perf_counter()
    state.attachment_tool_results = services.tool_execution.attachment_read_tool_results(state.body)
    state.current_attachments = state.body.get("_processedAttachments") if isinstance(state.body.get("_processedAttachments"), list) else []
    attachment_followup = services.tool_planning.is_attachment_followup_query(state.user_message)
    state.has_bound_attachment = bool(state.current_attachments or attachment_followup)
    state.explicitly_requested_sensors = services.tool_planning.explicitly_requested_sensor_tools(state.user_message)
    state.todo_reconciliation = services.task_planning.body_todo_reconciliation(
        state.body,
        state.user_message,
        state.messages,
    )
    state.inferred_requests = services.tool_planning.infer_agent_tool_requests(
        state.user_message,
        state.messages,
        state.context_resolution,
        state.prepared_action_turn,
        state.todo_reconciliation,
    )
    state.inferred_requests = services.tool_planning.task_bound_tool_requests(state.body, state.inferred_requests)
    if services.task_planning.cognitive_turn_state(state.body):
        # 主模型看到用户反馈并修订待办任务前，不执行旧版词法候选。
        # 通过校验的模型升级请求会在下方合并，并继续经过常规工具守卫。
        state.inferred_requests = []
    if state.continuation is not None:
        # 最终审计恢复发生在原轮次预执行之后。修复后的候选是新请求的唯一来源；
        # 此处不得再从原始用户文本推断或重放动作。
        state.inferred_requests = []
    state.inferred_requests, rejected_attachment_sensor_requests = (
        services.tool_planning.constrain_attachment_sensor_requests(
            state.inferred_requests,
            state.user_message,
            has_bound_attachment=state.has_bound_attachment,
        )
    )
    for item in rejected_attachment_sensor_requests:
        services.runtime.write_log(
            "attachment_sensor_request_rejected",
            requestId=state.request_id,
            toolName=str(item.get("name") or ""),
            source="inferred_tool_request",
            reason="current_attachment_is_authoritative_target",
        )
    state.turn_policy = choose_turn_policy(
        state.user_message,
        has_tools=bool(state.inferred_requests or state.attachment_tool_results or state.continuation_tool_results),
        has_attachments=bool(state.attachment_prompt or state.current_attachments),
        file_output_expected=state.file_output_expected,
        interpretation=state.utterance_interpretation,
    )
    state.turn_contract = build_turn_contract(
        state.utterance_interpretation,
        state.turn_policy,
    )
    state.body["_cognitiveTurnContract"] = state.turn_contract
    state.tool_preflight = services.turn_planning.body_tool_preflight(
        state.body,
        state.user_message,
        state.messages,
        reply_mode=state.turn_policy.reply_mode,
        speech_act=state.utterance_interpretation.primary_speech_act,
        settings=state.settings,
    )
    tool_escalation = (
        None
        if state.continuation is not None
        else state.body.get("_toolEscalation")
        if isinstance(state.body.get("_toolEscalation"), dict)
        else None
    )
    if tool_escalation:
        escalation_target = str(tool_escalation.get("targetDomain") or "").strip()
        if (
            escalation_target == "screen"
            and (
                not state.has_bound_attachment
                or "capture_screen" in state.explicitly_requested_sensors
            )
        ):
            screen_need = PerceptionNeed(
                required=True,
                target_domain="screen",
                freshness="current",
                confidence=clamp_float(
                    tool_escalation.get("confidence"),
                    0.55,
                    1.0,
                    0.72,
                ),
                ambiguities=("target_refined_by_fast_model_before_visible_output",),
            )
            state.tool_preflight = replace(
                state.tool_preflight,
                candidate_capabilities=("capture_screen",),
                mandatory_capabilities=("capture_screen",),
                likely_no_tool=False,
                confidence=screen_need.confidence,
                perception=screen_need,
                reasons=tuple(dict.fromkeys([
                    *state.tool_preflight.reasons,
                    "fast_model_refined_perception_target:screen",
                ])),
            )
            state.body["_toolPreflight"] = state.tool_preflight
        escalation_requests = services.tool_planning.normalize_tool_requests({
            "toolRequests": tool_escalation.get("toolRequests"),
            "targetDomain": escalation_target,
            "toolAuthorization": tool_escalation.get("toolAuthorization"),
        })
        capability_query = str(tool_escalation.get("capabilityQuery") or "").strip()
        if capability_query and not escalation_requests:
            searched = DEFAULT_TOOL_REGISTRY.search(capability_query, limit=4)
            services.runtime.write_log(
                "tool_search_completed",
                requestId=state.request_id,
                query=capability_query[:300],
                resultToolIds=[item.tool_id for item in searched],
            )
            state.tool_preflight = merge_searched_tool_candidates(
                state.tool_preflight,
                searched,
                query=capability_query,
            )
            state.body["_toolPreflight"] = state.tool_preflight
            if escalation_target == "screen" and any(
                item.tool_id == "capture_screen" for item in searched
            ):
                escalation_requests = [{"name": "capture_screen", "args": {}}]
        escalation_requests = services.tool_planning.constrain_model_tool_requests(
            escalation_requests,
            state.user_message,
            state.context_resolution,
            state.messages,
            state.body.get("_scheduleDecision"),
            cognitive_state=services.task_planning.cognitive_execution_state(state.body), evidence=state.body.get("_cognitiveEvidence") or {},
        )
        escalation_requests, rejected_escalation_sensor_requests = (
            services.tool_planning.constrain_attachment_sensor_requests(
                escalation_requests,
                state.user_message,
                has_bound_attachment=state.has_bound_attachment,
            )
        )
        state.tool_preflight = services.tool_planning.bind_structured_perception_request(
            state.tool_preflight,
            escalation_requests,
            source="fast_stream_tool_escalation",
        )
        state.body["_toolPreflight"] = state.tool_preflight
        for item in rejected_escalation_sensor_requests:
            services.runtime.write_log(
                "attachment_sensor_request_rejected",
                requestId=state.request_id,
                toolName=str(item.get("name") or ""),
                source="fast_stream_tool_escalation",
                reason="current_attachment_is_authoritative_target",
                modelTargetDomain=escalation_target,
            )
        annotated_escalation_requests = [
            {
                **item,
                "reason": "fast model escalated a missing capability before visible output",
                "selection": {
                    **(
                        dict(item.get("selection"))
                        if isinstance(item.get("selection"), Mapping) else {}
                    ),
                    "source": "fast_stream_tool_escalation",
                    "targetDomain": (item.get("selection") or {}).get("targetDomain") or escalation_target,
                },
            }
            for item in escalation_requests
        ]
        combined_request_count = len(state.inferred_requests) + len(
            annotated_escalation_requests
        )
        state.inferred_requests = services.tool_planning.merge_preexecution_tool_requests(
            state.inferred_requests,
            annotated_escalation_requests,
        )
        if len(state.inferred_requests) < combined_request_count:
            services.runtime.write_log(
                "preexecution_tool_requests_merged",
                requestId=state.request_id,
                beforeCount=combined_request_count,
                afterCount=len(state.inferred_requests),
                refinedTools=[
                    str(item.get("name") or "") for item in escalation_requests
                ],
            )
        if escalation_requests:
            state.turn_policy = choose_turn_policy(
                state.user_message,
                has_tools=True,
                has_attachments=bool(state.attachment_prompt or state.current_attachments),
                file_output_expected=state.file_output_expected,
                interpretation=state.utterance_interpretation,
            )
            state.turn_contract = build_turn_contract(
                state.utterance_interpretation,
                state.turn_policy,
            )
            state.body["_cognitiveTurnContract"] = state.turn_contract
            state.tool_preflight = replace(
                state.tool_preflight,
                reply_mode=state.turn_policy.reply_mode,
            )
    state.escalation = (
        None
        if state.continuation is not None
        else state.body.get("_researchEscalation")
        if isinstance(state.body.get("_researchEscalation"), dict)
        else None
    )
    state.prepared_action_turn, state.research_decision, state.capability = (
        services.turn_planning.enrich_prepared_turn_with_research(
            state.prepared_action_turn,
            state.turn_policy,
            state.turn_contract,
            state.settings,
            interpretation=state.utterance_interpretation,
            escalation=state.escalation,
        )
    )
    document_results = [
        item for item in state.attachment_tool_results
        if item.get("name") == "read_file"
    ]
    if document_results:
        document_plan = EpistemicPlan.from_dict(state.prepared_action_turn.epistemic_plan)
        if document_plan is not None:
            document_subject = "；".join(
                str(item.get("fileName") or "current uploaded document")
                for item in document_results
            )
            document_plan = document_plan.with_current_document_evidence(
                subject=document_subject,
                content_available=any(item.get("ok") for item in document_results),
            )
            state.prepared_action_turn = replace(
                state.prepared_action_turn,
                epistemic_plan=document_plan.to_dict(),
                epistemic_state=document_plan.epistemic_state().to_dict(),
            )
            state.body["_preparedActionTurn"] = state.prepared_action_turn
    local_research_decision = services.turn_planning.reconcile_research_with_tool_preflight(
        state.research_decision,
        state.tool_preflight,
        state.user_message,
    )
    if local_research_decision != state.research_decision:
        state.research_decision = local_research_decision
        state.prepared_action_turn, state.research_decision = services.turn_planning.synchronize_prepared_epistemic_plan(
            state.prepared_action_turn,
            state.research_decision,
        )
        state.capability = capability_envelope(
            state.turn_policy,
            state.research_decision,
            web_permission=bool(state.settings.get("permissionWebSearch")),
        )
    state.prepared_action_turn, state.research_decision, state.capability = (
        services.turn_planning.reconcile_preanswer_research_route(
            state.settings,
            state.user_message,
            state.prepared_action_turn,
            state.research_decision,
            state.turn_policy,
            state.body,
            request_id=str(state.body.get("requestId") or ""),
        )
    )
    state.capability = services.turn_planning.capability_with_tool_preflight(
        state.capability,
        state.tool_preflight,
        state.settings,
        extra_tool_ids=[
            str(item.get("name") or "")
            for item in state.inferred_requests
            if isinstance(item, dict)
        ],
    )
    state.prepared_action_turn = replace(
        state.prepared_action_turn,
        capability_envelope=state.capability.to_dict(),
    )
    reconciled_decision = services.turn_planning.reconcile_research_decision_with_tool_requests(
        state.research_decision,
        state.inferred_requests,
    )
    if reconciled_decision != state.research_decision:
        state.research_decision = reconciled_decision
        state.prepared_action_turn, state.research_decision = services.turn_planning.synchronize_prepared_epistemic_plan(
            state.prepared_action_turn,
            state.research_decision,
        )
        state.capability = capability_envelope(
            state.turn_policy,
            state.research_decision,
            web_permission=bool(state.settings.get("permissionWebSearch")),
        )
        state.capability = services.turn_planning.capability_with_tool_preflight(
            state.capability,
            state.tool_preflight,
            state.settings,
            extra_tool_ids=[
                str(item.get("name") or "")
                for item in state.inferred_requests
                if isinstance(item, dict)
            ],
        )
        state.prepared_action_turn = replace(
            state.prepared_action_turn,
            research_decision=state.research_decision.to_dict(),
            capability_envelope=state.capability.to_dict(),
        )
    execution_plan = services.turn_planning.finalize_turn_execution_plan(
        state.body,
        user_message=state.user_message,
        decision=state.research_decision,
        preflight=state.tool_preflight,
        tool_requests=state.inferred_requests,
        settings=state.settings,
        has_attachments=bool(state.current_attachments or state.attachment_prompt),
        model_research_escalation=bool(state.escalation),
        model_tool_escalation=bool(tool_escalation),
    )
    state.tool_preflight = bind_required_capabilities(
        state.tool_preflight,
        execution_plan.required_capabilities,
    )
    state.body["_toolPreflight"] = state.tool_preflight
    if execution_plan.research_policy != state.research_decision.policy:
        state.research_decision = replace(
            state.research_decision,
            policy=execution_plan.research_policy,
            reasons=tuple(dict.fromkeys((
                *state.research_decision.reasons,
                "turn_execution_plan_finalized",
            ))),
            plan_revision=max(
                state.research_decision.plan_revision + 1,
                execution_plan.revision,
            ),
        )
        state.prepared_action_turn, state.research_decision = services.turn_planning.synchronize_prepared_epistemic_plan(
            state.prepared_action_turn,
            state.research_decision,
        )
    state.capability = capability_envelope(
        state.turn_policy,
        state.research_decision,
        web_permission=bool(state.settings.get("permissionWebSearch")),
    )
    state.capability = services.turn_planning.capability_with_tool_preflight(
        state.capability,
        state.tool_preflight,
        state.settings,
        extra_tool_ids=[
            str(item.get("name") or "")
            for item in state.inferred_requests
            if isinstance(item, dict)
        ],
    )
    state.capability = replace(
        state.capability,
        research_policy=execution_plan.research_policy,
        required_tools=execution_plan.required_capabilities,
        max_tool_rounds=execution_plan.max_tool_rounds,
        tool_policy=(
            "required" if execution_plan.required_capabilities else "model_decides"
        ),
    )
    state.prepared_action_turn = replace(
        state.prepared_action_turn,
        research_decision=state.research_decision.to_dict(),
        capability_envelope=state.capability.to_dict(),
    )
    state.body["_preparedActionTurn"] = state.prepared_action_turn
    services.turn_planning.persist_prepared_turn(state.prepared_action_turn)
    services.turn_planning.write_tool_preflight_log(state.request_id, state.tool_preflight, state.capability)
    services.runtime.write_log(
        "turn_execution_plan_consumed",
        requestId=state.request_id,
        executionPlan=execution_plan.to_dict(),
    )
    if execution_plan.research_policy == "forbidden":
        state.inferred_requests = [
            item
            for item in state.inferred_requests
            if str(item.get("name") or "") not in {"web_search", "read_web_page"}
        ]
    elif (
        state.continuation is None
        and not services.task_planning.cognitive_turn_state(state.body)
        and execution_plan.research_policy == "required"
        and not any(
        str(item.get("name") or "") in {"web_search", "read_web_page"}
        for item in state.inferred_requests
        )
    ):
        escalation_queries = (
            state.escalation.get("queries")
            if isinstance(state.escalation, dict) and isinstance(state.escalation.get("queries"), list)
            else []
        )
        query = next(
            (str(item).strip() for item in escalation_queries if str(item).strip()),
            services.research.fallback_web_research_query(state.user_message, state.context_resolution, state.messages),
        )
        state.inferred_requests.append({
            "name": "web_search",
            "args": {
                "query": query[:500],
                "freshness": web_search_freshness(state.user_message),
                "researchDecision": state.research_decision.to_dict(),
            },
            "reason": "unified research decision or fast-path escalation",
        })
    services.runtime.write_log(
        "turn_contract_created",
        requestId=state.request_id,
        interpretation=state.utterance_interpretation.to_dict(),
        contract=state.turn_contract.to_dict(),
        researchDecision=state.research_decision.to_dict(),
        capabilityEnvelope=state.capability.to_dict(),
    )


def collect_upstream_evidence(services: UpstreamPipelineServices, state: UpstreamRunState) -> None:
    """按既有顺序执行预模型工具、研究和视觉证据绑定。"""

    state.memories, state.memory_records, state.memory_need = services.context.retrieve_turn_memories(
        state.settings,
        state.supplied_memories,
        state.user_message,
        state.messages,
        limit=state.turn_policy.memory_top_k,
        cognitive_state=services.task_planning.cognitive_turn_state(state.body),
        context_state=services.context.frozen_turn_context_state(state.body),
    )
    state.memories, state.memory_records = services.context.bind_cognitive_memories(state.body, state.memories, state.memory_records)
    inferred_web_searches = [
        item for item in state.inferred_requests if item.get("name") == "web_search"
    ] if state.continuation is None else []
    browser_target_resolution_request = next(
        (
            item
            for item in inferred_web_searches
            if isinstance(item.get("args"), dict)
            and item["args"].get("browserTargetResolution")
            and str(item["args"].get("query") or "").strip()
        ),
        None,
    )
    browser_target_resolution_query = (
        str(browser_target_resolution_request["args"].get("query") or "").strip()[:240]
        if browser_target_resolution_request else ""
    )
    inferred_direct_requests = [
        item for item in state.inferred_requests if item.get("name") != "web_search"
    ] if state.continuation is None else []
    state.deferred_browser_requests = [
        item for item in inferred_direct_requests
        if item.get("name") == "open_browser"
        and isinstance(item.get("args"), dict)
        and item["args"].get("deferToResearch")
    ] if state.continuation is None else []
    inferred_direct_requests = [
        item for item in inferred_direct_requests
        if item not in state.deferred_browser_requests
    ]
    # 附件摄取已经生成 read_file/read_image 回执。这一层的能力只用于声明，
    # 没有第二个执行器；重复执行会在成功回执旁制造伪失败，令界面误报
    # toolStatus=failed。
    preexecuted_inferred_attachment_requests = [
        item for item in inferred_direct_requests
        if services.tool_planning.preexecuted_attachment_request_satisfied(item, state.attachment_tool_results)
    ]
    if preexecuted_inferred_attachment_requests:
        inferred_direct_requests = [
            item for item in inferred_direct_requests
            if item not in preexecuted_inferred_attachment_requests
        ]
        services.runtime.write_log(
            "preexecuted_inferred_attachment_request_satisfied",
            requestId=state.request_id,
            requestedTools=[
                str(item.get("name") or "")
                for item in preexecuted_inferred_attachment_requests
            ],
        )
    browser_requests = [
        item
        for item in [*inferred_direct_requests, *state.deferred_browser_requests]
        if item.get("name") == "open_browser"
    ]
    state.pending_browser_action: dict[str, Any] = {}
    if browser_requests:
        continue_pending_browser_action = state.pending_relation == "continue"
        target_hint = (
            browser_target_resolution_query
            or next(
                (
                    str(item.get("args", {}).get("resolveQuery") or item.get("args", {}).get("query") or "")
                    for item in browser_requests
                    if isinstance(item.get("args"), dict)
                ),
                "",
            )
            or services.research.outbound_web_query(web_search_query_from_message(state.user_message))
        )
        candidate_sources, _ = services.research.recent_message_web_sources(state.messages)
        state.pending_browser_action = services.tool_execution.save_pending_browser_action(
            request_id=state.request_id,
            target_hint=target_hint,
            batch_id=str(
                next(
                    (
                        item.get("batchId")
                        for item in candidate_sources
                        if item.get("batchId")
                    ),
                    "",
                )
            ),
            status="resolving" if state.deferred_browser_requests else "ready",
            candidates=candidate_sources,
            reuse_existing=continue_pending_browser_action,
        )
        for browser_request in browser_requests:
            args = (
                dict(browser_request.get("args"))
                if isinstance(browser_request.get("args"), dict)
                else {}
            )
            args["actionId"] = str(state.pending_browser_action.get("actionId") or "")
            args["_actionGroupId"] = str(state.pending_browser_action.get("actionGroupId") or state.pending_browser_action.get("actionId") or "")
            browser_request["args"] = args
    inferred_has_visual_tool = any(
        item.get("name") in {"capture_screen", "capture_camera"}
        for item in inferred_direct_requests
    )
    state.research_plan: dict[str, Any] | None = None
    state.research_planning_ms = 0
    state.research_review_ms = 0
    state.research_review: dict[str, Any] | None = None
    planned_web_results: list[dict[str, Any]] = []
    state.research_bridge_ms = 0
    state.research_bridge_emitted = bool(state.body.get("_semanticToolProgressProvided"))

    def emit_research_bridge_once(active_plan: dict[str, Any]) -> None:
        # Semantic Stream V2 从同一次主模型 tool_use 响应中取得可选的自然进度语。
        # 不为一句“我去看看”额外调用模型；模型未给进度时，tool_status 已能维持
        # 界面的进行中状态。
        del active_plan
        return

    if inferred_web_searches and services.task_planning.cognitive_turn_state(state.body):
        # The conversation model owns search/read/answer, just like other tools.
        # Return observations to its existing tool loop without a second planner
        # or reviewer deciding whether it may continue.
        services.runtime.update_progress(state.request_id, "tool-executing", "正在搜索相关网页")
        planned_web_results = services.tool_execution.execute_agent_tools(
            inferred_web_searches,
            state.settings,
            state.user_message,
            preflight=state.tool_preflight,
            request_id=state.request_id,
            cognitive_state=services.task_planning.cognitive_execution_state(state.body),
            schedule_decision=state.body.get("_scheduleDecision"),
            time_context=services.task_planning.turn_time_context(state.body, state.settings),
        )
    elif inferred_web_searches and state.settings.get("permissionWebSearch"):
        services.runtime.update_progress(
            state.request_id,
            "research-planning",
            "正在理解问题、保留专有名词并规划检索方向",
        )
        planning_started = time.perf_counter()
        model_escalation_queries = unique_strings(
            [
                *(
                    state.escalation.get("queries")
                    if isinstance(state.escalation, dict)
                    and isinstance(state.escalation.get("queries"), list)
                    else []
                ),
                str(state.body.get("_preAnswerResearchQuery") or "").strip(),
            ]
        )[:3]
        state.research_plan = (
            {
                "needsWeb": True,
                "resolvedQuestion": f"解析要直接打开的公网目标：{browser_target_resolution_query}",
                "plannerResolvedQuestion": "",
                "userAnchor": browser_target_resolution_query[:80],
                "strictAnchorMatch": False,
                "queries": [{
                    "query": browser_target_resolution_query,
                    "purpose": "找到用户所指目标的权威直接页面 URL",
                }],
                "answerRequirements": ["只选择与目标匹配的直接页面，不打开搜索结果页"],
                "ambiguities": [],
                "questionAnalysis": {
                    "intent": "解析并打开用户指定的网页目标",
                    "subject": browser_target_resolution_query[:120],
                    "protectedTerms": [browser_target_resolution_query[:100]],
                    "factsToVerify": ["目标页面的真实公网 URL"],
                },
                "fallback": False,
                "browserTargetResolution": True,
            }
            if browser_target_resolution_query else
            {
                "needsWeb": True,
                "resolvedQuestion": str(
                    state.context_resolution.get("resolvedQuestion") or state.user_message
                ).strip()[:500],
                "plannerResolvedQuestion": "",
                "userAnchor": str(state.user_message or "").strip()[:80],
                "strictAnchorMatch": False,
                "queries": [
                    {
                        "query": query[:500],
                        "purpose": "核实主模型在同一 QueryLoop 中识别出的证据缺口",
                    }
                    for query in model_escalation_queries
                ],
                "answerRequirements": list(
                    state.escalation.get("verificationTargets")
                    if isinstance(state.escalation, dict)
                    and isinstance(state.escalation.get("verificationTargets"), list)
                    else []
                ),
                "ambiguities": list(
                    state.escalation.get("unknownEntities")
                    if isinstance(state.escalation, dict)
                    and isinstance(state.escalation.get("unknownEntities"), list)
                    else []
                ),
                "questionAnalysis": {
                    "intent": "核实回答所需的公开事实",
                    "subject": str(
                        state.context_resolution.get("resolvedQuestion") or state.user_message
                    ).strip()[:160],
                    "protectedTerms": list(
                        state.escalation.get("unknownEntities")
                        if isinstance(state.escalation, dict)
                        and isinstance(state.escalation.get("unknownEntities"), list)
                        else []
                    ),
                    "factsToVerify": list(
                        state.escalation.get("verificationTargets")
                        if isinstance(state.escalation, dict)
                        and isinstance(state.escalation.get("verificationTargets"), list)
                        else []
                    ),
                },
                "fallback": False,
                "plannedBy": "query_loop_main_model",
            }
            if model_escalation_queries else
            services.research.plan_web_research(
                state.settings,
                state.user_message,
                state.messages,
                state.context_resolution,
                request_id=state.request_id,
            )
        )
        state.research_planning_ms = round((time.perf_counter() - planning_started) * 1000)
        # 确定性策略决定是否必须取得公开证据。规划器可以细化路径，但不得静默
        # 取消明确、当前或高风险的研究请求。
        if not state.research_plan.get("queries"):
            state.research_plan = services.research.fallback_web_research_plan(state.user_message, state.context_resolution, state.messages)
        if state.research_decision.source_policy_id:
            state.research_plan["sourcePolicyId"] = state.research_decision.source_policy_id
        state.research_plan["researchPolicy"] = state.research_decision.policy
        state.research_plan["verificationTargets"] = unique_strings([
            *(
                state.research_plan.get("verificationTargets")
                if isinstance(state.research_plan.get("verificationTargets"), list)
                else []
            ),
            *state.research_decision.verification_targets,
        ])
        state.research_plan["entityTerms"] = unique_strings([
            *state.research_decision.unknown_entities,
            *(
                state.research_plan.get("entityTerms")
                if isinstance(state.research_plan.get("entityTerms"), list)
                else []
            ),
        ])
        emit_research_bridge_once(state.research_plan)
        research_analysis = (
            state.research_plan.get("questionAnalysis")
            if isinstance(state.research_plan.get("questionAnalysis"), dict)
            else {}
        )
        research_subject = re.sub(
            r"\s+",
            " ",
            str(research_analysis.get("subject") or ""),
        ).strip()[:80]
        services.runtime.update_progress(
            state.request_id,
            "researching",
            (
                f"已提炼核心主题“{research_subject}”，正在查阅 "
                f"{len(state.research_plan.get('queries', []))} 个证据方向"
                if research_subject
                else f"已提炼核心问题，正在查阅 {len(state.research_plan.get('queries', []))} 个证据方向"
            ),
        )
        services.runtime.update_progress(
            state.request_id,
            "research-reviewing",
            "正在循环核对来源正文、实体映射与尚缺的证据",
        )
        planned_result, state.research_plan, state.research_review, state.research_review_ms = (
            services.research.run_research_query_loop(
                state.settings,
                state.user_message,
                state.messages,
                state.research_plan,
                state.context_resolution,
                state.research_decision,
                state.capability,
                request_id=state.request_id,
                cognitive_body=state.body,
            )
        )
        planned_web_results = [planned_result]
    elif inferred_web_searches:
        # 保留明确的权限错误，不为无法执行的搜索额外调用 API 来规划。
        inferred_direct_requests.extend(inferred_web_searches)
    if inferred_direct_requests:
        services.runtime.update_progress(
            state.request_id,
            "tool-executing",
            "正在执行回答所需的本地工具或指定网页读取",
        )
    inferred_direct_results = [] if state.continuation is not None else (
        services.tool_execution.execute_agent_tools(
            inferred_direct_requests,
            state.settings,
            state.user_message,
            preflight=state.tool_preflight,
            request_id=state.request_id,
            cognitive_state=services.task_planning.cognitive_execution_state(state.body),
            schedule_decision=state.body.get("_scheduleDecision"),
            time_context=services.task_planning.turn_time_context(state.body, state.settings),
        )
        if inferred_has_visual_tool else services.tool_execution.execute_agent_tools(
            inferred_direct_requests,
            state.settings,
            state.user_message,
            preflight=state.tool_preflight,
            request_id=state.request_id,
            cognitive_state=services.task_planning.cognitive_execution_state(state.body),
            schedule_decision=state.body.get("_scheduleDecision"),
            time_context=services.task_planning.turn_time_context(state.body, state.settings),
        )
    )
    deferred_browser_results: list[dict[str, Any]] = []
    if state.deferred_browser_requests and planned_web_results:
        browser_candidates = services.research.public_web_sources(planned_web_results)
        candidate_batch = (
            services.research.persist_source_batch(services.research.freeze_source_batch(
                browser_candidates,
                request_id=state.request_id,
                topic_key=browser_target_resolution_query or state.user_message,
            ))
            if browser_candidates else {}
        )
        if candidate_batch:
            browser_candidates = [
                {
                    **source,
                    "id": str(source.get("displayId") or ""),
                }
                for source in candidate_batch.get("sources", [])
                if isinstance(source, dict)
            ]
            state.pending_browser_action = services.tool_execution.save_pending_browser_action(
                request_id=state.request_id,
                target_hint=browser_target_resolution_query,
                batch_id=str(candidate_batch.get("batchId") or ""),
                status="resolving",
                candidates=browser_candidates,
            )
        browser_args = services.tool_execution.browser_action_args_from_tool_results(
            f"{state.user_message} {browser_target_resolution_query}".strip(),
            planned_web_results,
        )
        if browser_args:
            browser_args = services.tool_execution.browser_args_with_usage_tracking(
                browser_args,
                user_message=state.user_message,
                request_id=state.request_id,
                fallback_alias=str(
                    state.pending_browser_action.get("targetHint")
                    or browser_target_resolution_query
                    or ""
                ),
            )
            browser_args["actionId"] = str(state.pending_browser_action.get("actionId") or "")
            browser_args["_actionGroupId"] = str(state.pending_browser_action.get("actionGroupId") or state.pending_browser_action.get("actionId") or "")
            browser_args["batchId"] = str(
                candidate_batch.get("batchId")
                or browser_args.get("batchId")
                or ""
            )
            deferred_browser_results = [
                services.tool_execution.tool_open_browser(state.settings, browser_args)
            ]
        else:
            ambiguous = len(browser_candidates) > 1
            error_code = "ambiguous_browser_target" if ambiguous else "no_verified_source_to_open"
            deferred_browser_results = [{
                "name": "open_browser",
                "ok": False,
                "error": error_code,
                "candidates": browser_candidates[:8],
                "message": (
                    "找到了多个可能的目标，需要用户指定来源编号。"
                    if ambiguous else
                    "没有取得可验证的目标 URL，因此没有打开搜索页冒充目标页面。"
                ),
                "actionReceipt": {
                    "actionId": str(state.pending_browser_action.get("actionId") or f"act_{uuid.uuid4().hex}"),
                    "actionGroupId": str(state.pending_browser_action.get("actionGroupId") or state.pending_browser_action.get("actionId") or ""),
                    "requestId": state.request_id,
                    "tool": "open_browser",
                    "status": "rejected",
                    "requestedTarget": browser_target_resolution_query,
                    "resolvedTarget": "",
                    "adapter": "target_resolver",
                    "error": error_code,
                    "timestamp": services.runtime.now_iso(),
                },
            }]
    elif state.deferred_browser_requests:
        deferred_browser_results = [{
            "name": "open_browser",
            "ok": False,
            "error": "no_verified_source_to_open",
            "message": "没有取得可验证的目标 URL，因此没有打开搜索页冒充目标页面。",
            "actionReceipt": {
                "actionId": str(state.pending_browser_action.get("actionId") or f"act_{uuid.uuid4().hex}"),
                "actionGroupId": str(state.pending_browser_action.get("actionGroupId") or state.pending_browser_action.get("actionId") or ""),
                "requestId": state.request_id,
                "tool": "open_browser",
                "status": "rejected",
                "requestedTarget": browser_target_resolution_query,
                "resolvedTarget": "",
                "adapter": "target_resolver",
                "error": "no_verified_source_to_open",
                "timestamp": services.runtime.now_iso(),
            },
        }]
    inferred_tool_results = [
        *inferred_direct_results,
        *planned_web_results,
        *deferred_browser_results,
    ]
    browser_receipts = action_receipts_from_results(inferred_tool_results)
    for action_receipt in browser_receipts:
        services.tool_execution.record_action_receipt(action_receipt)
        if action_receipt.get("status") == "accepted":
            services.tool_execution.clear_pending_action(str(action_receipt.get("actionId") or ""))
        elif state.pending_browser_action:
            services.tool_execution.save_pending_browser_action(
                request_id=state.request_id,
                target_hint=browser_target_resolution_query,
                batch_id=str(state.pending_browser_action.get("batchId") or ""),
                status=(
                    "needs_confirmation"
                    if action_receipt.get("error") == "target_confirmation_required"
                    else
                    "needs_target"
                    if action_receipt.get("error") in {
                        "unresolved_browser_target",
                        "missing_url",
                        "no_verified_source_to_open",
                        "ambiguous_browser_target",
                    }
                    else "failed"
                ),
                candidates=(
                    deferred_browser_results[0].get("candidates", [])
                    if deferred_browser_results
                    else state.pending_browser_action.get("candidateSources", [])
                ),
            )
    raw_pre_tool_results = (
        state.continuation_tool_results
        if state.continuation is not None
        else [*state.attachment_tool_results, *inferred_tool_results]
    )
    state.preexecution_tool_rounds = 1 if any(tool_result_was_attempted(item) for item in raw_pre_tool_results) else 0
    # 保存有界且仅属于本请求的恢复快照。它不会持久化为记忆，也不会写入人设提示；
    # 后续架构阶段失败时，恢复控制器仍可检查真实工具结果。
    state.body["_rescueToolResults"] = [
        dict(item) for item in raw_pre_tool_results[-12:]
        if isinstance(item, Mapping)
    ]
    state.pre_tool_results = effective_tool_results(raw_pre_tool_results)
    if len(state.pre_tool_results) != len(raw_pre_tool_results):
        services.runtime.write_log(
            "effective_tool_results_projected",
            requestId=state.request_id,
            stage="pre_model",
            rawCount=len(raw_pre_tool_results),
            effectiveCount=len(state.pre_tool_results),
        )
    expected_perception_target = state.tool_preflight.perception.target_domain
    grounded_pre_tool_results: list[dict[str, Any]] = []
    for item in state.pre_tool_results:
        tool_name = str(item.get("name") or "")
        mismatched_visual = (
            item.get("ok")
            and tool_name in {"capture_screen", "capture_camera", "read_image"}
            and not services.tool_planning.visual_result_matches_perception_target(
                item,
                expected_perception_target,
            )
            and tool_name not in state.explicitly_requested_sensors
        )
        if mismatched_visual:
            services.runtime.write_log(
                "visual_evidence_quarantined",
                requestId=state.request_id,
                toolName=tool_name,
                expectedPerceptionTarget=expected_perception_target,
                reason="tool_result_does_not_match_authoritative_turn_target",
            )
            continue
        grounded_pre_tool_results.append(item)
    state.pre_tool_results = grounded_pre_tool_results
    has_current_visual_evidence = any(
        item.get("ok") and item.get("name") in {"capture_screen", "capture_camera", "read_image"}
        for item in state.pre_tool_results
    )
    visual_focus_results = [
        item for item in state.pre_tool_results
        if services.tool_planning.visual_result_matches_perception_target(
            item,
            expected_perception_target,
        )
    ]
    if has_typed_visual_evidence(visual_focus_results):
        # 根据实际观察回执绑定任务契约。“看看”或“我在干什么”等措辞仅是路由提示，
        # 不再决定证据义务或放行结果。
        state.turn_contract = bind_current_visual_observation_contract(
            state.turn_contract,
            semantic_text=state.semantic_task_request,
        )
        state.body["_cognitiveTurnContract"] = state.turn_contract
        visual_plan = EpistemicPlan.from_dict(state.prepared_action_turn.epistemic_plan)
        if visual_plan is not None:
            visual_plan = visual_plan.with_current_visual_observation(
                subject=state.semantic_task_request,
            )
            state.prepared_action_turn = replace(
                state.prepared_action_turn,
                epistemic_plan=visual_plan.to_dict(),
                epistemic_state=visual_plan.epistemic_state().to_dict(),
            )
            state.body["_preparedActionTurn"] = state.prepared_action_turn
    elif visual_focus_results:
        # 当前捕获失败仍是这次尝试的权威本地证据。替换通用公开事实占位符，避免有效的
        # 权限或设备失败说明被误判为“证据不足”，甚至让整轮返回 503。
        visual_plan = EpistemicPlan.from_dict(state.prepared_action_turn.epistemic_plan)
        if visual_plan is not None:
            visual_plan = visual_plan.with_current_visual_observation(
                subject=state.user_message,
                observation_available=False,
            )
            state.prepared_action_turn = replace(
                state.prepared_action_turn,
                epistemic_plan=visual_plan.to_dict(),
                epistemic_state=visual_plan.epistemic_state().to_dict(),
            )
            state.body["_preparedActionTurn"] = state.prepared_action_turn
        state.turn_contract = bind_current_visual_failure_contract(
            state.turn_contract,
            semantic_text=state.semantic_task_request,
        )
        state.body["_cognitiveTurnContract"] = state.turn_contract
    state.recalled_visual_results = (
        []
        if state.continuation is not None or state.current_attachments or has_current_visual_evidence
        else services.context.recalled_screen_capture_results(state.user_message)
    )
    native_visual_inputs = (
        services.context.conversation_image_inputs(state.body, state.settings)
        if native_visual_chat_enabled(state.settings) else []
    )
    # 提供方具备视觉能力，不代表本次请求实际交付了像素。测试替身、不可用的捕获及
    # 仅元数据上传仍须经过类型化证据路径，避免旧人设、记忆或附件上下文与当前观察竞争。
    state.visual_focus_mode = has_typed_visual_evidence(visual_focus_results) and not native_visual_inputs
    current_attachment_kinds = {
        str(item.get("kind") or "file")
        for item in state.current_attachments
        if isinstance(item, dict)
    }
    # 视觉轮次消费下方的类型化视觉证据包，不让旧的扁平附件转储与其竞争。
    # 图片与文档混合时，文档仍使用附件提示，视觉工具结果则保留并投影为类型化证据。
    state.attachment_prompt_for_model = (
        ""
        if state.visual_focus_mode and current_attachment_kinds == {"image"}
        else state.attachment_prompt
    )
    # 普通文档已由 attachment_prompt 表示。图片结果特意保留，使
    # visual_evidence_prompt 能分别表达场景、OCR 和窗口上下文证据，而非将其压平。
    state.prompt_tool_results = [
        *[
            item for item in state.pre_tool_results
            if not (
                state.attachment_prompt_for_model
                and item.get("name") == "read_file"
            )
        ],
        *state.recalled_visual_results,
    ]
    state.discourse_ledger = DiscourseLedger.from_dict(
        state.context_resolution.get("referenceLedger")
        if isinstance(state.context_resolution.get("referenceLedger"), Mapping) else None
    )
    if state.prompt_tool_results:
        state.discourse_ledger = extend_with_visual_observations(
            state.discourse_ledger,
            state.prompt_tool_results,
        )
    state.discourse_resolution = resolve_discourse_references(
        state.discourse_ledger,
        message_id=state.discourse_ledger.current_message_id,
    )
    state.context_resolution["referenceLedger"] = state.discourse_ledger.to_dict()
    state.context_resolution["referenceResolution"] = state.discourse_resolution.to_dict()
    state.context_resolution["referenceEvidence"] = _reference_evidence(
        state.discourse_ledger,
        state.discourse_resolution,
    )
    state.body["_discourseLedger"] = state.discourse_ledger.to_dict()
    state.pre_tool_ms = round((time.perf_counter() - state.tool_started) * 1000)
    state.emit_research_bridge_once = emit_research_bridge_once


def generate_upstream_candidate(services: UpstreamPipelineServices, state: UpstreamRunState) -> None:
    """装配提示并运行主模型及有界工具循环。"""

    state.compact_core_protocol = services.delivery_prompts.ordinary_core_dialogue_protocol(
        state.turn_policy,
        state.prepared_action_turn,
        state.tool_preflight,
        state.research_decision,
        pre_tool_results=state.pre_tool_results,
        attachment_prompt=state.attachment_prompt,
        file_output_expected=state.file_output_expected,
    )

    interruption_context = ""
    if len(state.successive_inputs) > 1 or state.interrupted_previous_reply:
        interruption_context = (
            "用户在上一轮处理期间又连续补充了内容。把这些输入视为同一轮，综合理解后只回答一次；"
            "不要逐条机械复述。"
            + (
                "上一段语音确实被用户打断了。开头可用一个很短、自然且不固定的接话，例如“等、慢点说”，"
                "随后直接回应合并后的意思；不要解释系统中断。"
                if state.interrupted_previous_reply else
                "上一段语音尚未开始，不要假装被打断，直接综合回答。"
            )
        )

    # 这是每轮交付契约，不是重复的人设文案；Persona Context Skills 使用混合提供方时，
    # 它仍必须对模型可见。
    legacy_nickname_prompt = services.delivery_prompts.nickname_reaction_prompt(state.user_message, state.request_id)
    legacy_motif_prompt = (
        ""
        if state.active_persona_snapshot.uses_hybrid_provider_context
        else services.delivery_prompts.canon_style_motif_prompt(state.user_message)
    )
    current_visual_contract = visual_evidence_prompt(
        state.prompt_tool_results,
        user_question=state.semantic_task_request,
    )
    explicit_reply_context = services.context_prompts.body_reply_context_prompt(state.body)
    explicit_context_control = services.context_prompts.body_context_control_prompt(state.body)
    generic_prompt_tool_results = (
        [
            item for item in state.prompt_tool_results
            if str(item.get("name") or "")
            not in {"capture_screen", "capture_camera", "read_image"}
        ]
        if state.visual_focus_mode else state.prompt_tool_results
    )
    generic_tool_context = (
        services.delivery_prompts.tool_context_prompt(
            generic_prompt_tool_results,
            state.context_resolution,
            max_chars=state.turn_policy.tool_evidence_max_chars,
            model_owned=bool(services.task_planning.cognitive_turn_state(state.body)),
        )
        if generic_prompt_tool_results else ""
    )
    generic_grounding_instruction = services.delivery_prompts.tool_grounding_instruction(
        generic_prompt_tool_results
    )
    visual_style_context = (
        "[当前视觉任务] 你是 Amadeus。先准确完成用户针对当前画面的具体问题；"
        "人格只影响简短自然的措辞、情绪与动作，不能提供任何画面事实。"
        "与本轮直接承接的近期对话只可帮助理解用户所指和可见文字；"
        "无关旧对话、长期记忆、日程、关系经历和常识猜测都不是当前画面证据。"
        f"关系阶段={str(state.relationship_state.get('stage') or 'close_companion')[:40]}。"
        if state.visual_focus_mode else ""
    )
    visual_recent_context = (
        visual_relevant_context_prompt(state.context_resolution)
        if state.visual_focus_mode else ""
    )
    visual_candidate_capabilities = {
        str(item)
        for item in (
            getattr(state.tool_preflight, "candidate_capabilities", ()) or ()
        )
        if str(item)
    }
    visual_only_focus_mode = bool(
        state.visual_focus_mode
        and visual_candidate_capabilities
        and visual_candidate_capabilities.issubset({
            "read_image", "capture_screen", "capture_camera",
        })
        and not generic_prompt_tool_results
        and not state.file_output_expected
    )
    state.base_messages = [
        *([{"role": "system", "content": state.prompt}]
          if not state.visual_focus_mode else []),
        {"role": "system", "content": services.policy.natural_dialogue_style},
        {"role": "system", "content": (
            services.policy.cognitive_dialogue_reply_rule if services.task_planning.cognitive_turn_state(state.body) else services.policy.utterance_stream_reply_rule
        )},
        *([{"role": "system", "content": state.deep_persona_prompt}]
          if not state.visual_focus_mode else []),
        *([{"role": "system", "content": state.turn_persona_context["factsPrompt"]}]
          if not state.visual_focus_mode else []),
        *([{"role": "system", "content": visual_style_context}]
          if visual_style_context else []),
        *([{"role": "system", "content": visual_recent_context}]
          if visual_recent_context else []),
        *(services.task_planning.task_local_dialogue_messages(state.body) if state.visual_focus_mode else []),
        {"role": "system", "content": policy_prompt(state.turn_policy)},
        {"role": "system", "content": services.policy.speaker_attribution_rule},
        {"role": "system", "content": services.policy.technical_accuracy_rule},
        {"role": "system", "content": services.policy.emotion_selection_rule},
        *([{"role": "system", "content": services.policy.amadeus_tool_rule}]
          if not state.visual_focus_mode and not services.task_planning.cognitive_turn_state(state.body) else []),
        *([{"role": "system", "content": SCHEDULE_INTENT_PROMPT}]
          if not state.visual_focus_mode else []),
        {"role": "system", "content": services.delivery_prompts.tool_preflight_prompt(state.tool_preflight, state.capability)},
        *([{"role": "system", "content": services.delivery_prompts.todo_reconciliation_prompt(state.todo_reconciliation)}]
          if services.delivery_prompts.todo_reconciliation_prompt(state.todo_reconciliation) and not state.visual_focus_mode and not services.task_planning.cognitive_turn_state(state.body) else []),
        *([{"role": "system", "content": services.context_prompts.relationship_context_prompt(state.relationship_state)}]
          if not state.visual_focus_mode else []),
        *([{"role": "system", "content": services.context_prompts.dignity_boundary_context_prompt(
            state.relationship_state,
            state.body.get("_dignityAssessment") if isinstance(state.body.get("_dignityAssessment"), dict) else None,
            require_output_field=False,
        )}] if not state.visual_focus_mode else []),
        *([{"role": "system", "content": services.context_prompts.transition_style_prompt(state.messages)}]
          if not state.visual_focus_mode else []),
        *([{"role": "system", "content": services.context_prompts.permission_context_prompt(state.settings, body=state.body)}]
          if not state.visual_focus_mode else []),
        *([{"role": "system", "content": state.attachment_prompt_for_model}]
          if state.attachment_prompt_for_model else []),
        *([{"role": "system", "content": generic_tool_context}]
          if generic_tool_context else []),
        *([{"role": "system", "content": generic_grounding_instruction}]
          if generic_grounding_instruction else []),
        *([{"role": "system", "content": services.delivery_prompts.visual_relationship_reaction_instruction(state.prompt_tool_results, state.relationship_state)}]
          if services.delivery_prompts.visual_relationship_reaction_instruction(state.prompt_tool_results, state.relationship_state)
          and not state.visual_focus_mode else []),
        *([{"role": "system", "content": interruption_context}] if interruption_context else []),
        *([{"role": "system", "content": explicit_context_control}]
          if explicit_context_control else []),
        *([{"role": "system", "content": explicit_reply_context}]
          if explicit_reply_context else []),
        *([{"role": "system", "content": services.context_prompts.explicit_user_position_prompt(state.user_message)}]
          if services.context_prompts.explicit_user_position_prompt(state.user_message) and not state.visual_focus_mode else []),
        *([{"role": "system", "content": "主动互动连续性：" + state.proactive_context}]
          if state.proactive_context and not state.visual_focus_mode else []),
        *([{"role": "system", "content": services.context_prompts.retrieved_memory_prompt(
            state.memories,
            state.memory_records,
            state.memory_need,
            cognitive_state=services.task_planning.cognitive_turn_state(state.body),
        )}] if not state.visual_focus_mode else []),
        *([{"role": "system", "content": services.context_prompts.conversation_context_prompt(
            state.user_message, state.turn_policy, state.memory_need, state.context_resolution,
            context_state=services.context.frozen_turn_context_state(state.body),
            cognitive_state=services.task_planning.cognitive_turn_state(state.body),
        )}]
          if not state.visual_focus_mode else []),
        *([{"role": "system", "content": services.delivery_prompts.proactive_control_prompt(services.context.frozen_turn_context_state(state.body))}]
          if state.visual_focus_mode else []),
        *([{"role": "system", "content": services.context_prompts.session_temporal_context_prompt(state.user_message, settings=state.settings)}]
          if not state.visual_focus_mode else []),
        *([{"role": "system", "content": state.attribution_evidence}]
          if state.attribution_evidence and not state.visual_focus_mode else []),
        *services.context.conversation_history_messages(state.body, [
            item for item in (
                state.messages[-state.turn_policy.recent_message_limit:]
                if services.task_planning.cognitive_turn_state(state.body) or services.context.current_runtime_has_user_turn() or services.context.message_needs_recent_context(state.user_message)
                else []
            )
            if isinstance(item, dict) and not state.visual_focus_mode
        ]),
        *([{"role": "system", "content": state.identity_context}]
          if state.identity_context and not state.visual_focus_mode else []),
        *([{"role": "system", "content": legacy_nickname_prompt}]
          if legacy_nickname_prompt and not state.visual_focus_mode else []),
        *([{"role": "system", "content": legacy_motif_prompt}]
          if legacy_motif_prompt and not state.visual_focus_mode else []),
        *([{"role": "system", "content": services.context_prompts.immediate_context_prompt(
            state.messages,
            state.user_message,
            state.turn_policy,
            context_state=services.context.frozen_turn_context_state(state.body),
            cognitive_state=services.task_planning.cognitive_turn_state(state.body),
        )}]
          if not state.visual_focus_mode else []),
        *([{"role": "system", "content": state.pending_question_context}]
          if state.pending_question_context and not state.visual_focus_mode else []),
        *([{"role": "system", "content": state.todo_pending_context}]
          if state.todo_pending_context and not state.visual_focus_mode else []),
        *([{"role": "system", "content": state.schedule_state_context}]
          if state.schedule_state_context and not state.visual_focus_mode else []),
        *([{"role": "system", "content": contextual_reference_prompt(state.context_resolution)}]
          if contextual_reference_prompt(state.context_resolution) and not state.visual_focus_mode else []),
        *([{"role": "system", "content": current_visual_contract}]
          if current_visual_contract else []),
        *([{"role": "system", "content": services.delivery_prompts.claim_level_fact_policy_prompt(
            state.prepared_action_turn
        )}] if services.delivery_prompts.claim_level_fact_policy_prompt(state.prepared_action_turn) and not state.visual_focus_mode else []),
        {"role": "system", "content": services.delivery_prompts.core_dialogue_turn_contract_prompt(
            state.utterance_interpretation,
            state.turn_contract,
            model_owned=bool(services.task_planning.cognitive_turn_state(state.body)),
        )},
        {"role": "user", "content": state.user_message},
    ]
    if visual_only_focus_mode:
        state.base_messages = [
            {"role": "system", "content": visual_style_context},
            {"role": "system", "content": services.delivery_prompts.proactive_control_prompt(services.context.frozen_turn_context_state(state.body))},
            {"role": "system", "content": (
                "[视觉专用 semantic-v3 协议] 只输出一个 JSON 对象，不要 Markdown 或额外说明。"
                "输出 kind='turn' 和一到三项 speechUnits；每项 zh 用中文直接回答当前视觉问题，"
                "ja 用自然日语表达完全相同的事实；不要重复输出 reply 或 spokenTextJa。"
                "emotion 从 focused/curious/surprised/amused/embarrassed 中选最合适的一项，"
                "gesture 从 explain/tilt_head/look_away 中选一项。"
                "同时按相邻 Turn Contract 输出 proactiveDirective；不得因当前是视觉回答就覆盖用户的暂停或恢复意图，"
                "以及 scheduleIntent={kind:'none',relation:'unrelated',authorization:'none',commands:[],"
                "missingFields:[],sourceSpans:[],summary:'',confidence:0.0}。"
                "不要输出 completedActs、standalone 或 turnComplete；它们由后端依据视觉证据审计结果确认。"
                "不得增加其他事实字段，也不得用人格反应替代视觉答案。"
            )},
            *([{"role": "system", "content": visual_recent_context}]
              if visual_recent_context else []),
            *services.task_planning.task_local_dialogue_messages(state.body),
            *([{"role": "system", "content": explicit_reply_context}]
              if explicit_reply_context else []),
            {"role": "system", "content": current_visual_contract},
            {"role": "system", "content": services.delivery_prompts.core_dialogue_turn_contract_prompt(
                state.utterance_interpretation,
                state.turn_contract,
                model_owned=bool(services.task_planning.cognitive_turn_state(state.body)),
            )},
            {"role": "user", "content": state.user_message},
        ]

    state.base_messages = pin_reply_context(state.base_messages, state.body.get("_turnContextSnapshot"))
    primary_max_tokens = (
        6000 if state.file_output_expected else services.turn_planning.fast_stream_token_budget(state.turn_policy)
    )
    state.base_messages, state.context_pressure_metrics = services.turn_planning.pressure_managed_prompt(
        state.base_messages,
        state.settings,
        reserved_output_tokens=primary_max_tokens,
        request_id=state.request_id,
        stage="chat_primary",
    )
    payload: dict[str, Any] = {
        "model": state.settings["apiModel"],
        "temperature": 0.66,
        "max_tokens": primary_max_tokens,
        "messages": state.base_messages,
    }
    if state.settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in state.settings["apiEndpoint"]:
        payload["thinking"] = {"type": "disabled"}

    state.body["_cognitiveEvidence"] = {**(state.body.get("_cognitiveEvidence") or {}), **collect_evidence(state.pre_tool_results)}
    if state.continuation is None:
        services.runtime.update_progress(state.request_id, "model-generating", "正在请求模型生成第一版回答")
        llm_started = time.perf_counter()
        state.content = services.model.send_conversation_brain_payload(
            state.body, state.settings, payload, timeout=90, request_id=state.request_id, purpose="chat_primary"
        )
    else:
        services.runtime.update_progress(state.request_id, "model-generating", "沿用修复后的模型候选，进入本轮工具循环")
        llm_started = time.perf_counter()
        state.content = state.continuation_content
    state.semantic_task_request = services.turn_planning.current_task_semantic_request(state.body, state.user_message)
    state.answer_finish_reason = services.runtime.last_finish_reason.get()
    state.first_llm_ms = round((time.perf_counter() - llm_started) * 1000)
    tool_loop_result = chat_tool_query_loop.run_model_tool_loop(
        chat_tool_query_loop.ToolLoopContext(
            settings=state.settings,
            body=state.body,
            base_messages=state.base_messages,
            content=state.content,
            context_resolution=state.context_resolution,
            user_message=state.user_message,
            messages=state.messages,
            turn_policy=state.turn_policy,
            file_output_expected=state.file_output_expected,
            interpretation=state.utterance_interpretation,
            contract=state.turn_contract,
            request_id=state.request_id,
            tool_preflight=state.tool_preflight,
            research_decision=state.research_decision,
            prepared_action_turn=state.prepared_action_turn,
            capability=state.capability,
            semantic_task_request=state.semantic_task_request,
            answer_finish_reason=state.answer_finish_reason,
            context_pressure_metrics=state.context_pressure_metrics,
            research_review_ms=state.research_review_ms,
            research_bridge_emitted=state.research_bridge_emitted,
            todo_continuation_transition=state.todo_continuation_transition,
            pre_tool_results=state.pre_tool_results,
            inferred_requests=state.inferred_requests,
            current_attachments=state.current_attachments,
            attachment_prompt=state.attachment_prompt,
            has_bound_attachment=state.has_bound_attachment,
            preexecution_tool_rounds=state.preexecution_tool_rounds,
            existing_tool_rounds=state.existing_tool_rounds,
            continuation_tool_rounds=state.continuation_tool_rounds,
            continuation=state.continuation,
            pending_todo_before=state.pending_todo_before,
            deferred_browser_requests=state.deferred_browser_requests,
            pending_browser_action=state.pending_browser_action,
            relationship_state=state.relationship_state,
            progress_callback=state.progress_callback,
        ),
        services.tool_execution.tool_loop_services,
    )
    state.content = tool_loop_result.content
    state.answer_finish_reason = tool_loop_result.finish_reason
    state.executed_tool_results = tool_loop_result.executed_results
    state.requested_tools = tool_loop_result.requested_tools
    state.parsed = tool_loop_result.parsed
    state.second_llm_ms = tool_loop_result.model_ms
    state.research_review_ms = tool_loop_result.research_review_ms
    state.research_bridge_emitted = tool_loop_result.research_bridge_emitted
    state.todo_continuation_transition = tool_loop_result.todo_continuation_transition
    state.semantic_task_request = tool_loop_result.semantic_task_request
    state.capability = tool_loop_result.capability
    state.research_decision = tool_loop_result.research_decision
    state.prepared_action_turn = tool_loop_result.prepared_action_turn
    state.tool_preflight = tool_loop_result.tool_preflight
    state.turn_contract = tool_loop_result.turn_contract
    state.context_pressure_metrics = tool_loop_result.context_pressure_metrics
    state.uncertainty_triggered_web = tool_loop_result.uncertainty_triggered_web


def apply_upstream_grounding_gates(services: UpstreamPipelineServices, state: UpstreamRunState) -> None:
    """核验工具、研究、上下文与视觉证据边界。"""

    state.grounding_tool_results = [
        *state.executed_tool_results,
        *([] if any(
            item.get("ok") and item.get("name") in {"capture_screen", "capture_camera", "read_image"}
            for item in state.executed_tool_results
        ) else state.recalled_visual_results),
    ]
    # 当前视觉事实由下方类型化语义门控负责。旧正则校验器只保留给非视觉工具和低成本
    # 诊断，不能判断视觉句子是否属实。
    state.non_visual_grounding_tool_results = [
        item for item in state.grounding_tool_results
        if str(item.get("name") or "") not in {"capture_screen", "capture_camera", "read_image"}
    ]
    state.visual_grounding_active = has_typed_visual_evidence(state.grounding_tool_results)
    state.web_evidence_status = next(
        (
            item.get("evidenceStatus") for item in state.executed_tool_results
            if item.get("ok") and isinstance(item.get("evidenceStatus"), dict)
        ),
        None,
    )
    if state.web_evidence_status:
        state.context_resolution["webEvidenceStatus"] = state.web_evidence_status
    state.grounding_repair_ms = 0
    state.context_recovery_web_ms = 0
    state.context_recovery_triggered_web = False
    state.non_json_reply_recovered = bool(
        isinstance(state.content, str)
        and state.content.strip()
        and parse_json_object(state.content) is None
    )
    state.structured_transport_invalid = bool(
        state.non_json_reply_recovered and looks_like_incomplete_structured_reply(state.content)
    )
    if state.non_json_reply_recovered:
        services.runtime.write_log(
            "upstream_non_json_reply_recovered",
            requestId=state.request_id,
            purpose="chat_tool_followup" if state.requested_tools else "chat_primary",
            characters=len(state.content),
            toolResultCount=len(state.executed_tool_results),
        )
    if state.structured_transport_invalid:
        services.runtime.write_log(
            "upstream_structured_reply_truncated",
            requestId=state.request_id,
            purpose="chat_tool_followup" if state.requested_tools else "chat_primary",
            replyFieldRecovered=bool(extract_complete_json_string_field(state.content, "reply")),
            characters=len(state.content),
        )
    state.cognitive_accepted = (state.body.get("_cognitiveOutcomeVerification") or {}).get("valid") is True
    if services.task_planning.cognitive_turn_state(state.body) and not state.cognitive_accepted:
        raise CognitiveProtocolError("tool loop did not produce a validated final outcome")
    services.runtime.update_progress(state.request_id, "validating", "正在检查回答完整性、上下文与工具证据")
    state.content = services.research.ensure_verified_web_citation(state.content, state.executed_tool_results)
    state.tool_grounding_candidates: list[dict[str, Any]] = [
        services.model.normalize_model_reply(state.content, settings=state.settings)
    ]
    state.research_candidates: list[dict[str, Any]] = []
    state.has_web_grounding = any(
        item.get("ok") and item.get("name") in {"web_search", "read_web_page"}
        for item in state.grounding_tool_results
    )
    if state.has_web_grounding:
        state.research_candidates.append(services.model.normalize_model_reply(state.content, settings=state.settings))
    if not state.cognitive_accepted and (state.non_visual_grounding_tool_results and services.validation.tool_grounding_violation(
        state.content, state.non_visual_grounding_tool_results, state.user_message, state.relationship_state
    )):
        services.runtime.update_progress(state.request_id, "repairing", "回答未充分使用工具证据，正在定向修正")
        grounding_started = time.perf_counter()
        try:
            state.content = services.model.repair_tool_grounded_reply(
                state.settings, state.user_message, state.content, state.grounding_tool_results, state.context_resolution,
                state.relationship_state, state.active_persona_snapshot
            )
            state.content = services.research.ensure_verified_web_citation(state.content, state.executed_tool_results)
            state.tool_grounding_candidates.append(services.model.normalize_model_reply(state.content, settings=state.settings))
            if state.has_web_grounding:
                state.research_candidates.append(services.model.normalize_model_reply(state.content, settings=state.settings))
        except Exception:
            pass
        state.grounding_repair_ms = round((time.perf_counter() - grounding_started) * 1000)
    active_epistemic_plan = EpistemicPlan.from_dict(
        state.prepared_action_turn.epistemic_plan
    ) or create_epistemic_plan(
        state.request_id,
        state.utterance_interpretation,
        decision=state.research_decision,
    )
    pre_gate_receipt = research_receipt_from_tool_results(
        state.request_id,
        state.executed_tool_results,
    )
    web_research_attempted = any(
        item.get("name") in {"web_search", "read_web_page"}
        for item in state.executed_tool_results
        if isinstance(item, Mapping)
    )
    pre_gate_decision = grounded_claim_gate_decision(
        str(state.tool_grounding_candidates[0].get("reply") or ""),
        active_epistemic_plan.epistemic_state(),
        pre_gate_receipt,
        research_policy=(
            active_epistemic_plan.research_policy
            if state.settings.get("permissionWebSearch")
            else "forbidden"
        ),
        # 在此早期阶段，本地捕获成功只授权进行视觉合成；下方独立的逐主张审计再决定
        # 实际文案能否交付。
        visual_evidence_verified=has_typed_visual_evidence(state.grounding_tool_results),
        citation_source_uids=readable_public_source_uids(
            state.executed_tool_results
        ),
    )
    evidence_gate_triggered = bool(
        pre_gate_decision.status == "RESEARCH_ESCALATION"
    )
    semantic_research_audit: dict[str, Any] = {}
    state.semantic_research_audit_failed = False
    if (
        not state.cognitive_accepted and (not web_research_attempted
        and state.settings.get("permissionWebSearch")
        and state.research_decision.policy != "forbidden"
        and services.research.needs_post_answer_research_audit(
            active_epistemic_plan,
            pre_gate_decision,
            pre_gate_receipt,
        ))
    ):
        try:
            semantic_research_audit = services.research.audit_model_research_need(
                state.settings,
                state.user_message,
                str(state.tool_grounding_candidates[0].get("reply") or ""),
                active_epistemic_plan,
                request_id=state.request_id,
            )
            evidence_gate_triggered = bool(
                evidence_gate_triggered
                or semantic_research_audit.get("researchNeeded")
            )
            services.runtime.write_log(
                "post_answer_research_need_audit",
                requestId=state.request_id,
                **semantic_research_audit,
            )
        except Exception as audit_error:
            state.semantic_research_audit_failed = True
            services.runtime.write_log(
                "post_answer_research_need_audit_failed",
                requestId=state.request_id,
                errorType=type(audit_error).__name__,
                error=str(audit_error)[:500],
            )
    services.runtime.write_log(
        "query_loop_grounding_gate_decision",
        requestId=state.request_id,
        planId=active_epistemic_plan.plan_id,
        planRevision=active_epistemic_plan.revision,
        decision=pre_gate_decision.to_dict(),
    )
    if (
        not state.cognitive_accepted and ((
            evidence_gate_triggered
            or (
                services.validation.context_grounding_violation(state.content, state.context_resolution)
                and services.research.context_repair_needs_web_research(
                    state.user_message,
                    state.context_resolution,
                    state.executed_tool_results,
                )
            )
        )
        and not web_research_attempted
        and state.research_decision.policy != "forbidden"
        and state.settings.get("permissionWebSearch"))
    ):
        services.runtime.update_progress(state.request_id, "researching", "现有证据不足，正在补充外部事实")
        recovery_web_started = time.perf_counter()
        recovery_plan = services.research.fallback_web_research_plan(
            state.user_message,
            state.context_resolution,
            state.messages,
        )
        semantic_query = str(semantic_research_audit.get("query") or "").strip()
        if semantic_query:
            recovery_plan["queries"] = [{
                "query": semantic_query,
                "purpose": "model semantic evidence audit",
            }]
        recovery_execution_plan = services.turn_planning.finalize_turn_execution_plan(
            state.body,
            user_message=state.user_message,
            decision=state.research_decision,
            preflight=state.tool_preflight,
            tool_requests=[{
                "name": "web_search",
                "args": {
                    "query": semantic_query or services.research.fallback_web_research_query(
                        state.user_message,
                        state.context_resolution,
                        state.messages,
                    ),
                },
                "reason": "grounded claim gate identified an evidence gap",
            }],
            settings=state.settings,
            evidence_recovery_escalation=True,
        )
        state.research_decision = replace(
            state.research_decision,
            policy=recovery_execution_plan.research_policy,
            reasons=tuple(dict.fromkeys((
                *state.research_decision.reasons,
                "grounded_claim_gate_evidence_gap",
            ))),
            verification_targets=(
                state.research_decision.verification_targets
                or ("claims rejected by context evidence gate",)
            ),
            plan_revision=max(
                state.research_decision.plan_revision + 1,
                recovery_execution_plan.revision,
            ),
        )
        state.prepared_action_turn, state.research_decision = services.turn_planning.synchronize_prepared_epistemic_plan(
            state.prepared_action_turn,
            state.research_decision,
        )
        state.capability = capability_envelope(
            state.turn_policy,
            state.research_decision,
            web_permission=True,
        )
        state.capability = replace(
            state.capability,
            research_policy=recovery_execution_plan.research_policy,
            required_tools=recovery_execution_plan.required_capabilities,
            max_tool_rounds=recovery_execution_plan.max_tool_rounds,
            tool_policy="required",
        )
        state.prepared_action_turn = replace(
            state.prepared_action_turn,
            research_decision=state.research_decision.to_dict(),
            capability_envelope=state.capability.to_dict(),
        )
        state.body["_preparedActionTurn"] = state.prepared_action_turn
        services.turn_planning.persist_prepared_turn(state.prepared_action_turn)
        state.emit_research_bridge_once(recovery_plan)
        recovered_result, recovery_plan, _, recovery_review_ms = (
            services.research.run_research_query_loop(
                state.settings,
                state.user_message,
                state.messages,
                recovery_plan,
                state.context_resolution,
                state.research_decision,
                state.capability,
                request_id=state.request_id,
                cognitive_body=state.body,
            )
        )
        recovery_results = [recovered_result]
        state.research_review_ms += recovery_review_ms
        state.executed_tool_results.extend(recovery_results)
        state.grounding_tool_results.extend(recovery_results)
        state.context_recovery_triggered_web = True
        state.context_recovery_web_ms = round((time.perf_counter() - recovery_web_started) * 1000)
        recovered_web_evidence_status = next(
            (
                item.get("evidenceStatus") for item in recovery_results
                if item.get("ok") and isinstance(item.get("evidenceStatus"), dict)
            ),
            None,
        )
        if recovered_web_evidence_status:
            state.web_evidence_status = recovered_web_evidence_status
            state.context_resolution["webEvidenceStatus"] = recovered_web_evidence_status
        state.has_web_grounding = any(
            item.get("ok") and item.get("name") in {"web_search", "read_web_page"}
            for item in state.grounding_tool_results
        )
        services.runtime.write_log(
            "context_repair_web_research",
            requestId=state.request_id,
            evidenceGateTriggered=evidence_gate_triggered,
            ok=any(item.get("ok") for item in recovery_results),
            queries=[
                str(item.get("query") or "")[:240]
                for item in recovery_plan.get("queries", [])
                if isinstance(item, dict)
            ],
        )
    state.pre_context_result = services.model.normalize_model_reply(state.content, settings=state.settings)
    state.context_grounding_repair_ms = 0
    if not state.cognitive_accepted and (state.context_recovery_triggered_web):
        context_started = time.perf_counter()
        try:
            state.content = services.model.repair_tool_grounded_reply(
                state.settings, state.user_message, state.content, state.grounding_tool_results, state.context_resolution,
                state.relationship_state, state.active_persona_snapshot
            )
            state.content = services.research.ensure_verified_web_citation(state.content, state.executed_tool_results)
            state.research_candidates.append(services.model.normalize_model_reply(state.content, settings=state.settings))
        except Exception:
            pass
        state.context_grounding_repair_ms = round((time.perf_counter() - context_started) * 1000)
    elif (
        not state.cognitive_accepted and (not state.visual_grounding_active
        and not state.has_web_grounding
        and services.validation.context_grounding_violation(state.content, state.context_resolution))
    ):
        services.runtime.update_progress(state.request_id, "repairing", "正在按完整上下文修正回答")
        context_started = time.perf_counter()
        try:
            state.content = services.model.repair_context_grounded_reply(
                state.settings, state.user_message, state.content, state.context_resolution, state.grounding_tool_results,
                state.active_persona_snapshot
            )
        except Exception:
            pass
        state.context_grounding_repair_ms = round((time.perf_counter() - context_started) * 1000)


def repair_upstream_candidate(services: UpstreamPipelineServices, state: UpstreamRunState) -> None:
    """按原额度修复结构、任务、证据和对话候选。"""

    services.runtime.update_progress(state.request_id, "finalizing", "正在选择完整回答并整理情绪与语音参数")
    state.result = services.model.normalize_model_reply(state.content, settings=state.settings)
    state.result = services.research.ensure_verified_web_citation(state.result, state.executed_tool_results)
    if state.semantic_research_audit_failed:
        state.result["webEvidenceLimited"] = True
        state.result["semanticResearchAuditFailed"] = True
    if state.has_web_grounding:
        state.research_candidates.append(dict(state.result))
    if (
        not state.cognitive_accepted and (not state.visual_grounding_active
        and not state.has_web_grounding
        and services.validation.context_grounding_violation(state.result, state.context_resolution))
    ):
        state.result, context_contract_satisfied = services.model.select_best_model_candidate(
            [state.pre_context_result, state.result],
            lambda candidate: services.validation.context_grounding_violation(candidate, state.context_resolution),
        )
        if not context_contract_satisfied:
            state.result["contextGroundingUnverified"] = True
    tool_grounding_rejected = bool(
        state.non_visual_grounding_tool_results
        and services.validation.tool_grounding_violation(
            state.result,
            state.non_visual_grounding_tool_results,
            state.user_message,
            state.relationship_state,
        )
    )
    if not state.cognitive_accepted and tool_grounding_rejected:
        if state.has_web_grounding:
            state.result, research_contract_satisfied = services.research.select_best_research_candidate(
                [*state.research_candidates, state.result], state.grounding_tool_results
            )
            if not research_contract_satisfied:
                state.result["webSynthesisUnverified"] = True
                state.result["webSynthesisDiagnostics"] = services.research.web_answer_diagnostics(state.result, state.grounding_tool_results)
            if services.validation.tool_grounding_violation(state.result, state.non_visual_grounding_tool_results, state.user_message, state.relationship_state):
                state.result["toolGroundingUnverified"] = True
        else:
            state.result, tool_contract_satisfied = services.model.select_best_model_candidate(
                [*state.tool_grounding_candidates, state.result],
                lambda candidate: services.validation.tool_grounding_violation(
                    candidate, state.non_visual_grounding_tool_results, state.user_message, state.relationship_state
                ),
            )
            if not tool_contract_satisfied:
                diagnostics = ["no_model_candidate_satisfied_tool_contract"]
                state.result["toolGroundingUnverified"] = True
                state.result["toolGroundingDiagnostics"] = diagnostics
    state.reply_integrity_repair_ms = 0
    if not state.cognitive_accepted and (reply_integrity_issues(state.result)):
        services.runtime.write_log(
            "reply_integrity_violation",
            requestId=state.request_id,
            issues=reply_integrity_issues(state.result),
            characters=len(str(state.result.get("reply") or "")),
        )
        integrity_candidates = [state.pre_context_result, *state.research_candidates, state.result]
        if state.has_web_grounding:
            selected_integrity_candidate, _ = services.research.select_best_research_candidate(
                integrity_candidates, state.grounding_tool_results
            )
        else:
            selected_integrity_candidate, _ = services.model.select_best_model_candidate(
                integrity_candidates,
                lambda candidate: bool(reply_integrity_issues(candidate)),
            )
        state.result = selected_integrity_candidate
        if reply_integrity_issues(state.result):
            integrity_started = time.perf_counter()
            try:
                repaired_content = services.model.repair_incomplete_model_reply(
                    state.settings, state.user_message, state.result, state.context_resolution, state.grounding_tool_results,
                    state.active_persona_snapshot
                )
                repaired_content = services.research.ensure_verified_web_citation(repaired_content, state.executed_tool_results)
                repaired_result = services.model.normalize_model_reply(repaired_content, settings=state.settings)
                if not reply_integrity_issues(repaired_result):
                    state.result = repaired_result
                    state.content = repaired_content
                    state.result["replyIntegrityRepaired"] = True
                else:
                    state.result["replyIntegrityUnverified"] = True
                    state.result["replyIntegrityIssues"] = reply_integrity_issues(repaired_result)
            except Exception:
                state.result["replyIntegrityUnverified"] = True
                state.result["replyIntegrityIssues"] = reply_integrity_issues(state.result)
            state.reply_integrity_repair_ms = round((time.perf_counter() - integrity_started) * 1000)
    state.repair_started = time.perf_counter()
    if state.result.get("emptyReply"):
        state.result = services.model.repair_empty_reply(
            state.settings,
            state.memories,
            state.user_message,
            state.content,
            request_id=state.request_id,
            persona_snapshot=state.active_persona_snapshot,
        )
    if (
        not state.visual_grounding_active
        and not state.has_web_grounding
        and services.validation.context_grounding_violation(state.result, state.context_resolution)
    ):
        # 校验器可以请求一次模型修复，但不能自行撰写答案；各领域都保留最佳可见模型候选。
        state.result["contextGroundingUnverified"] = True
    state.task_completion_repair_ms = 0
    if not state.cognitive_accepted and (services.validation.task_completion_violation(state.result, state.context_resolution)):
        original_task_candidate = dict(state.result)
        task_repair_started = time.perf_counter()
        try:
            repaired_content = services.model.repair_task_completion_reply(
                state.settings, state.user_message, state.result, state.context_resolution, state.grounding_tool_results,
                state.active_persona_snapshot
            )
            repaired_result = services.model.normalize_model_reply(repaired_content, settings=state.settings)
            repaired_satisfies_contract = not services.validation.task_completion_violation(repaired_result, state.context_resolution)
            state.result, task_contract_satisfied = services.model.select_best_model_candidate(
                [original_task_candidate, repaired_result],
                lambda candidate: services.validation.task_completion_violation(candidate, state.context_resolution),
            )
            if task_contract_satisfied and repaired_satisfies_contract and state.result.get("reply") == repaired_result.get("reply"):
                state.result["taskCompletionRepaired"] = True
            elif not task_contract_satisfied:
                state.result["taskCompletionUnverified"] = True
        except Exception:
            state.result = original_task_candidate
            state.result["taskCompletionUnverified"] = True
        state.task_completion_repair_ms = round((time.perf_counter() - task_repair_started) * 1000)
    failed_web_results = [
        item for item in state.executed_tool_results
        if item.get("name") in {"web_search", "read_web_page"} and not item.get("ok")
    ]
    successful_web_results = [
        item for item in state.executed_tool_results
        if item.get("name") in {"web_search", "read_web_page"} and item.get("ok")
    ]
    insufficient_web_results = [
        item for item in successful_web_results
        if isinstance(item.get("evidenceStatus"), dict)
        and not item.get("evidenceStatus", {}).get("sufficient")
    ]
    web_retrieval_failed = bool(failed_web_results and not successful_web_results)
    web_evidence_limited = bool(
        web_retrieval_failed
        or (
            successful_web_results
            and len(insufficient_web_results) == len(successful_web_results)
        )
    )
    state.evidence_limited_synthesis_ms = 0
    state.evidence_limited_precision_audit_ms = 0
    if web_evidence_limited:
        # Retrieval coverage is context for the model, not a veto over its answer.
        # Keep the original speech and record the retrieval outcome for diagnosis.
        state.result["webEvidenceLimited"] = True
        state.result["webSearchFailed"] = web_retrieval_failed
        services.runtime.write_log(
            "web_evidence_limitation_recorded",
            requestId=state.request_id,
            retrievalFailed=web_retrieval_failed,
            answerPreserved=True,
        )
    if state.cognitive_accepted:
        # 主模型已经报告核验后的结果；保留回执元数据，不用旧版重试问题替换其答案。
        receipts = action_receipts_from_results(state.executed_tool_results)
        if receipts:
            state.result["actionReceipts"] = receipts
            if len(receipts) == 1:
                state.result["actionReceipt"] = receipts[0]
    else:
        state.result = services.tool_execution.enforce_external_action_receipt(
            state.result, state.executed_tool_results, schedule_decision=state.body.get("_scheduleDecision"),
        )
    state.dialogue_style_repair_ms = 0
    state.dialogue_validation = services.validation.dialogue_validation_for_result(
        state.result,
        state.turn_policy,
        state.messages,
    )
    if not state.cognitive_accepted and (state.dialogue_validation.needs_rewrite):
        services.runtime.update_progress(
            state.request_id,
            "repairing",
            "回答超出本轮篇幅或动作预算，正在保留事实并压缩表达",
        )
        style_started = time.perf_counter()
        original_style_candidate = dict(state.result)
        try:
            repaired_style_candidate = services.model.repair_dialogue_style_reply(
                state.settings,
                state.user_message,
                state.result,
                state.turn_policy,
                state.grounding_tool_results,
                state.context_resolution,
                state.relationship_state,
                state.deep_persona_prompt,
                state.active_persona_snapshot,
            )
            repaired_style_candidate = services.research.ensure_verified_web_citation(
                repaired_style_candidate,
                state.executed_tool_results,
            )
            repaired_validation = services.validation.dialogue_validation_for_result(
                repaired_style_candidate,
                state.turn_policy,
                state.messages,
            )
            violates_required_contract = bool(
                repaired_validation.needs_rewrite
                or reply_integrity_issues(repaired_style_candidate)
                or services.validation.task_completion_violation(repaired_style_candidate, state.context_resolution)
                or (
                    state.non_visual_grounding_tool_results
                    and services.validation.tool_grounding_violation(
                        repaired_style_candidate,
                        state.non_visual_grounding_tool_results,
                        state.user_message,
                        state.relationship_state,
                    )
                )
                or (
                    not state.visual_grounding_active
                    and not state.has_web_grounding
                    and services.validation.context_grounding_violation(
                        repaired_style_candidate,
                        state.context_resolution,
                    )
                )
            )
            if violates_required_contract:
                state.result = original_style_candidate
                state.result["dialogueStyleUnverified"] = True
            else:
                state.result = repaired_style_candidate
                state.result["dialogueStyleRepaired"] = True
                state.dialogue_validation = repaired_validation
                state.content = json.dumps(state.result, ensure_ascii=False)
        except Exception as error:
            state.result = original_style_candidate
            state.result["dialogueStyleUnverified"] = True
            services.runtime.write_log(
                "dialogue_style_repair_failed",
                requestId=state.request_id,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
        state.dialogue_style_repair_ms = round((time.perf_counter() - style_started) * 1000)
    state.visual_grounding_audit_ms = 0
    state.visual_grounding_rewrite_ms = 0
    state.result["_nativeVisualInputIds"] = list(state.body.get("_nativeVisualInputIds") or [])
    if not state.cognitive_accepted and (has_typed_visual_evidence(state.grounding_tool_results)):
        state.result, state.visual_grounding_audit_ms, state.visual_grounding_rewrite_ms = (
            services.validation.enforce_visual_semantic_grounding(
                state.settings,
                state.semantic_task_request,
                state.result,
                state.grounding_tool_results,
                state.relationship_state,
                request_id=state.request_id,
                task_dialogue=services.task_planning.task_review_dialogue_messages(state.body, state.messages),
            )
        )
        # 聚焦视觉重写可能替换文案，因此重新计算风格诊断；最终视觉审计后不再进行
        # 另一轮文案创作。
        state.dialogue_validation = services.validation.dialogue_validation_for_result(
            state.result,
            state.turn_policy,
            state.messages,
        )
    state.result["dialoguePolicy"] = state.turn_policy.to_dict()
    state.result["dialogueValidation"] = state.dialogue_validation.to_dict()
    state.result["_utteranceInterpretation"] = state.utterance_interpretation
    state.result["_turnContract"] = state.turn_contract
    state.result["_semanticTaskRequest"] = services.turn_planning.current_task_semantic_request(state.body, state.user_message)
    state.result["_taskDialogue"] = services.task_planning.task_review_dialogue_messages(state.body, state.messages)
    state.result["coreDialogueProtocol"] = state.compact_core_protocol
    state.result["semanticDialogueProtocol"] = "semantic-v3-unified"
    state.result = services.research.attach_verified_web_sources(state.result, state.executed_tool_results)


def project_upstream_result(services: UpstreamPipelineServices, state: UpstreamRunState) -> None:
    """补齐交付字段并投影工具、日程和性能状态。"""

    field_validation = services.validation.dialogue_output_validation(state.settings, state.result)
    primary_emotion_missing = "emotion" in field_validation["missingFields"]
    field_repair_ms = 0
    field_repair_source = "not_needed"
    emotion_label_ms = 0
    emotion_recovery_calls = 0
    emotion_label_source = "primary_model"
    if state.result.get("speechSegmentCountMissingFromModel"):
        # 缺失模型契约时仍视为一个原子交付单元，绝不根据标点或回复长度推断可见边界。
        state.result["speechSegmentCount"] = 1
        state.result["speechSegmentCountSource"] = "atomic_no_model_boundary"
    if field_validation["requiresFieldRepair"]:
        field_started = time.perf_counter()
        state.result["dialogueFieldRepairAttempted"] = True
        services.runtime.update_progress(
            state.request_id,
            "repairing-fields",
            "正文已保留，正在补齐缺失的语音字段",
        )
        services.runtime.write_log(
            "dialogue_field_repair_started",
            requestId=state.request_id,
            missingFields=field_validation["missingFields"],
            invalidFields=field_validation["invalidFields"],
            frozenReplyFingerprint=runtime_fingerprint(
                state.result.get("reply"), length=32
            ),
        )
        try:
            state.result = services.model.repair_dialogue_fields_once(
                state.settings,
                state.result,
                field_validation,
                user_message=state.user_message,
                recent_messages=state.messages,
                relationship_state=state.relationship_state,
                request_id=state.request_id,
                persona_snapshot=state.active_persona_snapshot,
            )
            state.result["structuredOutputResolved"] = True
            field_repair_source = "minimal_speech_model"
            services.runtime.write_log(
                "dialogue_field_repair_completed",
                requestId=state.request_id,
                repairedFields=state.result.get("dialogueFieldRepairFields", []),
                maxTokens=state.result.get("dialogueFieldRepairMaxTokens"),
                finishReason=state.result.get("dialogueFieldRepairFinishReason"),
                jsonComplete=bool(state.result.get("dialogueFieldRepairJsonComplete")),
                frozenReplyPreserved=True,
            )
        except Exception as error:
            field_repair_source = "failed"
            state.result["dialogueFieldRepairFailed"] = True
            state.result["ttsUnavailableReason"] = "dialogue_field_repair_failed"
            # 可选日语呈现轨道无法修复时，已验收且有依据的中文答案仍是完整交付物。
            # 明确标记纯文本终止路径，避免浏览器静默丢弃整份结果。
            state.result["textOnlyDelivery"] = True
            state.result["deliveryFallbackReason"] = "dialogue_field_repair_failed"
            services.runtime.write_log(
                "dialogue_field_repair_failed",
                requestId=state.request_id,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
        field_repair_ms = round((time.perf_counter() - field_started) * 1000)
    if primary_emotion_missing and not state.result.get("dialogueFieldRepairFailed"):
        emotion_started = time.perf_counter()
        if services.runtime.configured(state.settings):
            emotion_recovery_calls = 1
            services.runtime.update_progress(
                state.request_id,
                "repairing-emotion",
                "正文已冻结，正在补齐表情与语音情绪",
            )
            try:
                state.result = services.model.repair_frozen_reply_emotion_once(
                    state.settings,
                    state.result,
                    user_message=state.user_message,
                    recent_messages=state.messages,
                    relationship_state=state.relationship_state,
                    request_id=state.request_id,
                )
                emotion_label_source = "frozen_reply_semantic_classifier"
            except Exception as error:
                state.result["emotionResolvedLocally"] = True
                state.result["emotionLabelRecoveryFailed"] = True
                state.result["emotionUnavailableReason"] = "emotion_label_recovery_failed"
                emotion_label_source = "local_default_after_classifier_failure"
                services.runtime.write_log(
                    "emotion_label_recovery_failed",
                    requestId=state.request_id,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                )
        else:
            state.result["emotionResolvedLocally"] = True
            state.result["emotionUnavailableReason"] = "model_not_configured"
            emotion_label_source = "local_default_model_unavailable"
        emotion_label_ms = round((time.perf_counter() - emotion_started) * 1000)
    elif primary_emotion_missing:
        state.result["emotionResolvedLocally"] = True
        state.result["emotionUnavailableReason"] = "speech_field_repair_failed"
        emotion_label_source = "local_default_after_speech_repair_failure"
    if state.web_evidence_status:
        state.result["webEvidenceStatus"] = state.web_evidence_status
    if state.executed_tool_results:
        tool_names = list(dict.fromkeys(services.tool_execution.displayed_tool_name(item) for item in state.executed_tool_results))
        status_results = services.tool_execution.effective_tool_results_for_status(state.executed_tool_results)
        failed_tools = [
            str(item.get("name") or "tool")
            for item in status_results
            if not item.get("ok")
        ]
        state.result["toolUsed"] = ",".join(tool_names)
        state.result["toolStatus"] = "failed" if failed_tools else "ok"
        state.result["toolErrors"] = [
            {
                "name": str(item.get("name") or "tool"),
                "error": str(item.get("error") or "unknown_error"),
            }
            for item in status_results
            if not item.get("ok")
        ]
        public_attachment_tools = [
            services.tool_execution.public_attachment_tool_result(item) for item in state.executed_tool_results
            if item.get("name") in {"read_file", "read_image"}
        ]
        if public_attachment_tools:
            state.result["toolResults"] = public_attachment_tools
        todo_context = services.task_planning.todo_context_from_tool_results(state.executed_tool_results)
        if todo_context:
            state.result["todoContext"] = todo_context
        # 仅保留到 finalize_chat_response 持久化本轮为止。这样会话可以记住多次屏幕观察，
        # 同时不会在浏览器响应中暴露完整本地工具载荷。
        state.result["_conversationToolResults"] = effective_tool_results(
            state.executed_tool_results
        )
    state.result["_referenceLedger"] = state.discourse_ledger.to_dict()
    attachment_action = state.result.get("attachmentAction") or {}
    state.result["_fileOutputExpected"] = (
        attachment_action.get("kind") in {"edit", "create"} and attachment_action.get("status") == "completed"
        if attachment_action else state.file_output_expected
    )
    state.result["_chatMetrics"] = {
        "requestId": state.request_id,
        "preToolMs": state.pre_tool_ms,
        "researchPlanningMs": state.research_planning_ms,
        "researchBridgeMs": state.research_bridge_ms,
        "researchBridgeEmitted": state.research_bridge_emitted,
        "researchReviewMs": state.research_review_ms,
        "researchReview": state.research_review or {},
        "researchPlanQueries": (
            [
                str(item.get("query") or "")[:240]
                for item in state.research_plan.get("queries", [])
                if isinstance(item, dict)
            ]
            if isinstance(state.research_plan, dict) else []
        ),
        "firstLlmMs": state.first_llm_ms,
        "secondLlmMs": state.second_llm_ms,
        "toolGroundingRepairMs": state.grounding_repair_ms,
        "contextGroundingRepairMs": state.context_grounding_repair_ms,
        "contextRecoveryWebMs": state.context_recovery_web_ms,
        "replyIntegrityRepairMs": state.reply_integrity_repair_ms,
        "taskCompletionRepairMs": state.task_completion_repair_ms,
        "dialogueStyleRepairMs": state.dialogue_style_repair_ms,
        "visualGroundingAuditMs": state.visual_grounding_audit_ms,
        "visualGroundingRewriteMs": state.visual_grounding_rewrite_ms,
        "evidenceLimitedSynthesisMs": state.evidence_limited_synthesis_ms,
        "evidenceLimitedPrecisionAuditMs": state.evidence_limited_precision_audit_ms,
        "emptyReplyRepairMs": round((time.perf_counter() - state.repair_started) * 1000),
        "promptChars": sum(len(str(item.get("content") or "")) for item in state.base_messages),
        "personaCoreChars": len(state.prompt),
        "deepPersonaChars": len(state.deep_persona_prompt),
        "deepPersonaModules": state.context_resolution.get("personaModules", []),
        "personaContextSnapshot": state.active_persona_snapshot.safe_diagnostic(),
        "historyMessages": min(state.turn_policy.recent_message_limit, len(state.messages)),
        "retrievedMemories": len(state.memories),
        "memoryNeed": state.memory_need.value,
        "contextPressure": state.context_pressure_metrics,
        "replyMode": state.turn_policy.reply_mode,
        "actionBudget": state.turn_policy.action_budget,
        "turnPolicyReason": state.turn_policy.reason,
        "turnPolicy": state.turn_policy.to_dict(),
        "replySentences": state.dialogue_validation.sentence_count,
        "replyCharacters": state.dialogue_validation.character_count,
        "assistantPhraseHits": list(state.dialogue_validation.forbidden_phrase_hits),
        "dialogueValidationIssues": list(state.dialogue_validation.issues),
        "tools": [str(item.get("name") or "tool") for item in state.executed_tool_results],
        "toolStatus": state.result.get("toolStatus") or "not_requested",
        "nonJsonReplyRecovered": state.non_json_reply_recovered,
        "structuredTransportInvalid": state.structured_transport_invalid,
        "primaryOutputProtocol": "semantic-v3-unified",
        "dialogueFieldValidation": field_validation,
        "dialogueFieldRepairMs": field_repair_ms,
        "dialogueFieldRepairSource": field_repair_source,
        "dialogueFieldRepairCalls": int(bool(state.result.get("dialogueFieldRepairAttempted"))),
        "fullReplyRepairCalls": int(bool(state.result.get("fullReplyRepairAttempted"))),
        "independentEmotionRecoveryCalls": emotion_recovery_calls,
        "emotionLabelMs": emotion_label_ms,
        "emotionLabelSource": emotion_label_source,
        "uncertaintyTriggeredWeb": state.uncertainty_triggered_web,
        "preAnswerResearchAudited": bool(state.body.get("_preAnswerResearchAudit")),
        "preAnswerResearchTriggeredWeb": bool(
            isinstance(state.body.get("_preAnswerResearchAudit"), Mapping)
            and state.body["_preAnswerResearchAudit"].get("researchNeeded")
        ),
        "contextRecoveryTriggeredWeb": state.context_recovery_triggered_web,
        "knowledgeConfidence": str(
            state.parsed.get("knowledgeConfidence") or state.parsed.get("knowledge_confidence") or ""
        )[:16],
        "contextResolution": {
            "active": bool(state.context_resolution.get("active")),
            "kind": str(state.context_resolution.get("kind") or "none"),
            "subject": str(state.context_resolution.get("subject") or "")[:240],
            "intent": str(state.context_resolution.get("intent") or "")[:80],
            "resolvedQuestion": str(state.context_resolution.get("resolvedQuestion") or "")[:500],
            "matchup": str(state.context_resolution.get("matchup") or "")[:200],
            "eventYear": str(state.context_resolution.get("eventYear") or "")[:20],
            "requestedAction": str(state.context_resolution.get("requestedAction") or "")[:80],
            "constraints": [str(item)[:160] for item in state.context_resolution.get("constraints", [])[:8]],
            "entities": [str(item)[:100] for item in state.context_resolution.get("entities", [])[:8]],
            "analysisFocus": [str(item)[:100] for item in state.context_resolution.get("analysisFocus", [])[:8]],
            "userPosition": str(state.context_resolution.get("userPosition") or "")[:240],
            "webEvidenceStatus": state.web_evidence_status or {},
            "personaModules": state.context_resolution.get("personaModules", [])[:8],
            "clearActiveTask": bool(state.context_resolution.get("clearActiveTask")),
            "referenceCurrentMessageId": state.discourse_ledger.current_message_id,
            "referenceAntecedentCount": len(
                state.discourse_resolution.antecedent_message_ids
            ),
            "referenceUnresolvedCount": len(
                state.discourse_resolution.unresolved_mention_ids
            ),
            "referenceAmbiguousCount": len(
                state.discourse_resolution.ambiguous_mention_ids
            ),
        },
        "modelPipelineMs": round((time.perf_counter() - state.pipeline_started) * 1000),
    }
    state.result["_upstreamFinishReason"] = state.answer_finish_reason
    state.result["personaContextSnapshotId"] = state.active_persona_snapshot.snapshot_id
    if isinstance(state.body.get("_scheduleDecision"), Mapping):
        finalized_schedule = services.task_planning.finalize_schedule_workflow_from_results(
            state.body.get("_scheduleDecision"),
            state.executed_tool_results,
            request_id=state.request_id,
        )
        state.result["scheduleDecision"] = {
            **dict(state.body["_scheduleDecision"]),
            "workflow": finalized_schedule,
        }
    if state.todo_continuation_transition and not state.body.get("_scheduledTaskExecution"):
        pending_after = services.task_planning.apply_todo_continuation_transition(
            state.todo_continuation_transition,
            request_id=state.request_id,
            user_message=state.user_message,
        )
        state.result["todoContinuation"] = {
            "status": (
                "awaiting_user_input"
                if pending_after else
                "cleared"
                if state.todo_continuation_transition.get("decision") in {"unrelated", "cancel"}
                else "resolved"
            ),
            "decision": str(state.todo_continuation_transition.get("decision") or ""),
            "actionId": str(pending_after.get("actionId") or ""),
        }
    elif state.pending_todo_before:
        todo_attempts = [
            item for item in state.executed_tool_results
            if isinstance(item, Mapping) and item.get("name") == "todo_manage"
        ]
        if todo_attempts:
            all_accepted = all(
                item.get("ok")
                and isinstance(item.get("actionReceipt"), Mapping)
                and item["actionReceipt"].get("status") == "accepted"
                for item in todo_attempts
            )
            event = (
                "todo_continuation_fulfilled"
                if all_accepted else "todo_continuation_partial_failure"
            )
            services.runtime.write_log(
                event,
                requestId=state.request_id,
                actionId=str(state.pending_todo_before.get("actionId") or ""),
                attemptCount=len(todo_attempts),
                acceptedCount=sum(
                    1 for item in todo_attempts
                    if isinstance(item.get("actionReceipt"), Mapping)
                    and item["actionReceipt"].get("status") == "accepted"
                ),
            )
            state.result["todoContinuation"] = {
                "status": "resolved" if all_accepted else "partial_failure",
                "decision": "execute",
                "actionId": str(state.pending_todo_before.get("actionId") or ""),
            }


def run_upstream_pipeline(
    services: UpstreamPipelineServices,
    body: dict[str, Any],
    *,
    progress_callback: Any | None = None,
    persona_snapshot: PersonaContextSnapshot | None = None,
    continuation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """依次执行准备、取证、生成、验收修复和结果投影。"""

    state = UpstreamRunState(
        body=body,
        progress_callback=progress_callback,
        persona_snapshot=persona_snapshot,
        continuation=continuation,
    )
    prepare_upstream_turn(services, state)
    collect_upstream_evidence(services, state)
    generate_upstream_candidate(services, state)
    apply_upstream_grounding_gates(services, state)
    repair_upstream_candidate(services, state)
    project_upstream_result(services, state)
    return state.result
