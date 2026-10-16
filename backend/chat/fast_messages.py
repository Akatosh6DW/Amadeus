"""快速语义流的冻结上下文与模型消息装配。

人格、记忆和上下文仍由各自存储服务拥有；本模块只组合本轮模型输入。模型提示
常量以不可变策略值注入，动态状态与提示构建分别通过窄端口读取。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from typing import Any, Mapping, Protocol

from agent.dialogue_architecture import (
    TurnContract,
    TurnPolicy,
    UtteranceInterpretation,
    build_turn_contract,
    choose_turn_policy,
    policy_prompt,
    turn_contract_prompt,
)
from agent.persona_context import PersonaContextSnapshot
from agent.research_architecture import (
    CapabilityEnvelope,
    ResearchDecision,
    capability_envelope,
    deterministic_research_decision,
)
from agent.schedule_architecture import SCHEDULE_INTENT_PROMPT
from agent.tool_architecture import TurnPreflight, build_turn_preflight
from agent.turn_context import pin_reply_context
from backend.context.recall import retrieved_memory_prompt
from backend.context.reference import contextual_reference_prompt, message_needs_recent_context


class FastMessagePlanningOperations(Protocol):
    def body_prepared_action_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def utterance_interpretation_from_prepared(self, *args: Any, **kwargs: Any) -> Any: ...
    def bind_resolved_task_interpretation(self, *args: Any, **kwargs: Any) -> Any: ...


class FastMessageContextOperations(Protocol):
    def cognitive_turn_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_turn_context_artifacts(self, *args: Any, **kwargs: Any) -> Any: ...
    def retrieve_turn_memories(self, *args: Any, **kwargs: Any) -> Any: ...
    def frozen_turn_context_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def bind_cognitive_memories(self, *args: Any, **kwargs: Any) -> Any: ...
    def frozen_turn_relationship_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def read_relationship_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def contextual_reference_resolution(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_task_semantic_request(self, *args: Any, **kwargs: Any) -> Any: ...
    def build_persona_context_snapshot(self, *args: Any, **kwargs: Any) -> Any: ...
    def ensure_turn_persona_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_runtime_has_user_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def conversation_history_messages(self, *args: Any, **kwargs: Any) -> Any: ...


class FastMessagePromptOperations(Protocol):
    def body_reply_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def body_context_control_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def identity_grounding_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def speaker_attribution_evidence_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def pending_assistant_question_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def pending_todo_action_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_schedule_workflow_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def tool_preflight_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def todo_reconciliation_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def relationship_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def dignity_boundary_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def transition_style_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def permission_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def conversation_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def session_temporal_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def nickname_reaction_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def canon_style_motif_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def immediate_context_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def claim_level_fact_policy_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def current_turn_attachment_binding_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def cognitive_turn_prompt(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class FastMessageServices:
    planning: FastMessagePlanningOperations
    context: FastMessageContextOperations
    prompts: FastMessagePromptOperations


@dataclass(frozen=True)
class FastMessagePolicy:
    amadeus_tool_rule: str
    cognitive_dialogue_reply_rule: str
    emotion_selection_rule: str
    natural_dialogue_style: str
    speaker_attribution_rule: str
    technical_accuracy_rule: str
    utterance_stream_reply_rule: str


def build_fast_stream_messages(
    services: FastMessageServices,
    policy: FastMessagePolicy,
    body: dict[str, Any],
    settings: dict[str, Any],
    user_message: str,
    messages: list[dict[str, Any]],
    turn_policy: TurnPolicy | None = None,
    request_id: str = "",
    interpretation: UtteranceInterpretation | None = None,
    turn_contract: TurnContract | None = None,
    persona_snapshot: PersonaContextSnapshot | None = None,
) -> list[dict[str, str]]:
    """装配快速流模型输入；只读取本轮冻结状态，不执行模型或提交历史。"""
    prepared = services.planning.body_prepared_action_turn(
        body, user_message, request_id=request_id
    )
    active_interpretation = (
        interpretation or services.planning.utterance_interpretation_from_prepared(prepared)
    )
    active_interpretation = services.planning.bind_resolved_task_interpretation(
        active_interpretation, body
    )
    active_policy = turn_policy or choose_turn_policy(
        user_message, interpretation=active_interpretation
    )
    active_contract = turn_contract or build_turn_contract(active_interpretation, active_policy)
    active_decision = (
        ResearchDecision.from_dict(prepared.research_decision)
        if prepared.research_decision
        else deterministic_research_decision(active_interpretation)
    )
    active_capability = (
        CapabilityEnvelope.from_dict(prepared.capability_envelope)
        if prepared.capability_envelope
        else capability_envelope(
            active_policy, active_decision, web_permission=bool(settings.get("permissionWebSearch"))
        )
    )
    active_tool_preflight = (
        body.get("_toolPreflight")
        if isinstance(body.get("_toolPreflight"), TurnPreflight)
        else build_turn_preflight(
            user_message,
            messages,
            reply_mode=active_policy.reply_mode,
            speech_act=active_interpretation.primary_speech_act,
        )
    )
    if services.context.cognitive_turn_state(body) and (
        not services.context.current_turn_context_artifacts(body)
    ):
        current_state_tool = str(
            (
                active_tool_preflight.current_state_evidence_requirement
                if isinstance(active_tool_preflight.current_state_evidence_requirement, Mapping)
                else {}
            ).get("toolName")
            or ""
        )
        retained_requirements = tuple(
            (
                item
                for item in active_tool_preflight.mandatory_capabilities
                if item == current_state_tool
            )
        )
        active_tool_preflight = replace(
            active_tool_preflight,
            mandatory_capabilities=retained_requirements,
            perception=replace(active_tool_preflight.perception, required=False),
            tool_escalation_allowed=True,
        )
        active_capability = replace(
            active_capability,
            required_tools=retained_requirements,
            tool_policy="required" if retained_requirements else "model_decides",
        )
    supplied_memories = body.get("memories") if isinstance(body.get("memories"), list) else []
    memories, memory_records, memory_need = services.context.retrieve_turn_memories(
        settings,
        supplied_memories,
        user_message,
        messages,
        limit=active_policy.memory_top_k,
        cognitive_state=services.context.cognitive_turn_state(body),
        context_state=services.context.frozen_turn_context_state(body),
    )
    memories, memory_records = services.context.bind_cognitive_memories(
        body, memories, memory_records
    )
    relationship_state = (
        services.context.frozen_turn_relationship_state(body)
        or services.context.read_relationship_state()
    )
    context_resolution = services.context.contextual_reference_resolution(
        user_message,
        messages,
        request_id=request_id,
        current_artifacts=services.context.current_turn_context_artifacts(body),
        resolved_task=services.context.current_task_semantic_request(body, user_message),
        context_state=services.context.frozen_turn_context_state(body),
        model_owned=bool(services.context.cognitive_turn_state(body)),
    )
    body["_contextResolution"] = context_resolution
    body["_discourseLedger"] = context_resolution.get("referenceLedger", {})
    identity_context = services.prompts.identity_grounding_prompt(user_message)
    attribution = services.prompts.speaker_attribution_evidence_prompt(messages, user_message)
    pending_question = services.prompts.pending_assistant_question_prompt(messages, user_message)
    todo_pending = services.prompts.pending_todo_action_prompt()
    schedule_state = services.prompts.current_schedule_workflow_prompt()
    active_persona_snapshot = persona_snapshot or services.context.build_persona_context_snapshot(
        user_message, messages, context_resolution, relationship_state, cognitive_body=body
    )
    deep_persona_prompt = active_persona_snapshot.prompt_text
    explicit_reply_context = services.prompts.body_reply_context_prompt(body)
    turn_persona_context = services.context.ensure_turn_persona_context(
        body, user_message, snapshot=active_persona_snapshot, relationship_state=relationship_state
    )
    explicit_context_control = services.prompts.body_context_control_prompt(body)
    result: list[dict[str, str]] = [
        {"role": "system", "content": active_persona_snapshot.system_prompt},
        {"role": "system", "content": policy.natural_dialogue_style},
        {"role": "system", "content": deep_persona_prompt},
        {"role": "system", "content": turn_persona_context["factsPrompt"]},
        {"role": "system", "content": policy_prompt(active_policy)},
        {"role": "system", "content": policy.speaker_attribution_rule},
        {"role": "system", "content": policy.technical_accuracy_rule},
        {"role": "system", "content": policy.emotion_selection_rule},
        {"role": "system", "content": policy.amadeus_tool_rule},
        {"role": "system", "content": SCHEDULE_INTENT_PROMPT},
        {
            "role": "system",
            "content": services.prompts.tool_preflight_prompt(
                active_tool_preflight, active_capability
            ),
        },
        *(
            [
                {
                    "role": "system",
                    "content": services.prompts.todo_reconciliation_prompt(
                        body.get("_todoReconciliation")
                    ),
                }
            ]
            if services.prompts.todo_reconciliation_prompt(body.get("_todoReconciliation"))
            and (not services.context.cognitive_turn_state(body))
            else []
        ),
        {
            "role": "system",
            "content": services.prompts.relationship_context_prompt(relationship_state),
        },
        {
            "role": "system",
            "content": services.prompts.dignity_boundary_context_prompt(
                relationship_state,
                (
                    body.get("_dignityAssessment")
                    if isinstance(body.get("_dignityAssessment"), dict)
                    else None
                ),
            ),
        },
        {"role": "system", "content": services.prompts.transition_style_prompt(messages)},
        {
            "role": "system",
            "content": services.prompts.permission_context_prompt(settings, body=body),
        },
        {
            "role": "system",
            "content": retrieved_memory_prompt(
                memories,
                memory_records,
                memory_need,
                cognitive_state=services.context.cognitive_turn_state(body),
            ),
        },
        {
            "role": "system",
            "content": services.prompts.conversation_context_prompt(
                user_message,
                active_policy,
                memory_need,
                context_resolution,
                context_state=services.context.frozen_turn_context_state(body),
                cognitive_state=services.context.cognitive_turn_state(body),
            ),
        },
        {
            "role": "system",
            "content": services.prompts.session_temporal_context_prompt(
                user_message, settings=settings
            ),
        },
    ]
    if explicit_context_control:
        result.append({"role": "system", "content": explicit_context_control})
    if explicit_reply_context:
        result.append({"role": "system", "content": explicit_reply_context})
    if attribution:
        result.append({"role": "system", "content": attribution})
    adjacent_messages = (
        messages
        if services.context.cognitive_turn_state(body)
        or services.context.current_runtime_has_user_turn()
        or message_needs_recent_context(user_message)
        else []
    )
    result.extend(
        services.context.conversation_history_messages(
            body, adjacent_messages[-active_policy.recent_message_limit :]
        )
    )
    if identity_context:
        result.append({"role": "system", "content": identity_context})
    # 混合人格上下文也必须保留原始昵称反应的精确单元合同。
    nickname_prompt = services.prompts.nickname_reaction_prompt(user_message, request_id)
    if nickname_prompt:
        result.append({"role": "system", "content": nickname_prompt})
    motif_prompt = (
        ""
        if active_persona_snapshot.uses_hybrid_provider_context
        else services.prompts.canon_style_motif_prompt(user_message)
    )
    if motif_prompt:
        result.append({"role": "system", "content": motif_prompt})
    result.append(
        {
            "role": "system",
            "content": services.prompts.immediate_context_prompt(
                messages,
                user_message,
                active_policy,
                context_state=services.context.frozen_turn_context_state(body),
                cognitive_state=services.context.cognitive_turn_state(body),
            ),
        }
    )
    if pending_question:
        result.append({"role": "system", "content": pending_question})
    if todo_pending:
        result.append({"role": "system", "content": todo_pending})
    if schedule_state:
        result.append({"role": "system", "content": schedule_state})
    contextual_prompt = contextual_reference_prompt(context_resolution)
    if contextual_prompt:
        result.append({"role": "system", "content": contextual_prompt})
    result.append(
        {"role": "system", "content": turn_contract_prompt(active_interpretation, active_contract)}
    )
    fact_policy_prompt = services.prompts.claim_level_fact_policy_prompt(prepared)
    if fact_policy_prompt:
        result.append({"role": "system", "content": fact_policy_prompt})
    result.append(
        {
            "role": "system",
            "content": "[Capability Envelope：与回复长度相互独立]\n"
            + json.dumps(
                {
                    "researchDecision": active_decision.to_dict(),
                    "capabilityEnvelope": active_capability.to_dict(),
                },
                ensure_ascii=False,
            )
            + (
                "\n联网工具按用户请求与信息需要选择，用 kind=tool_use 和 calls 调用；取得结果后继续同一工具循环。research_policy=forbidden 时不得联网。"
                if services.context.cognitive_turn_state(body)
                else "\nresearch_policy=allowed 且知识信心不足时必须输出 research_escalation，不能把不确定的外部事实写成 FinalAnswer。research_policy=forbidden 时不得联网升级。"
            ),
        }
    )
    attachment_binding = services.prompts.current_turn_attachment_binding_prompt(body)
    if attachment_binding:
        result.append({"role": "system", "content": attachment_binding})
    # 线协议紧邻用户消息。上下文块较多时，服务商通常更重视靠后的 system
    # 消息；放在这里可以减少模型退化为普通文本、破坏流协议的概率。
    result.append(
        {
            "role": "system",
            "content": (
                policy.cognitive_dialogue_reply_rule
                if services.context.cognitive_turn_state(body)
                else policy.utterance_stream_reply_rule
            ),
        }
    )
    cognition = services.prompts.cognitive_turn_prompt(body)
    if cognition:
        body["_cognitiveOutcomeRequired"] = True
        result.append({"role": "system", "content": cognition})
    result.append({"role": "user", "content": user_message})
    return pin_reply_context(result, body.get("_turnContextSnapshot"))


def fast_stream_token_budget(turn_policy: TurnPolicy) -> int:
    """为双语 JSON 包络预留空间，同时保持既有回复长度策略。"""
    content_budget = max(1, int(turn_policy.max_output_tokens))
    return min(6000, max(content_budget + 480, math.ceil(content_budget * 1.5)))
