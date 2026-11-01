"""本轮语义合同、事实范围与模型上下文预算。

只根据明确传入的合同、计划和消息生成提示；预算压缩复用已有算法，
不可压缩内容超限时在请求模型前抛出原校验异常。日志由入口提供。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

from agent.action_grounding import PreparedTurn
from agent.context_pressure import (
    apply_prompt_pressure,
    budget_snapshot,
    context_pressure_diagnostic,
    estimate_tokens,
)
from agent.dialogue_architecture import TurnContract, UtteranceInterpretation
from agent.proactive_control import PROACTIVE_DIRECTIVE_INSTRUCTION
from agent.research_architecture import EpistemicPlan
from backend.common.values import safe_nonnegative_int
from backend.runtime.requests import RequestValidationError


def _aligned_pressure_source_metadata(
    messages: Sequence[Mapping[str, Any]],
    source_metadata: Sequence[Any] | Mapping[int, Any] | None,
) -> Sequence[Any] | Mapping[int, Any] | None:
    """Align out-of-band history rows without assigning ids or reading prose."""

    if not isinstance(source_metadata, Sequence) or isinstance(source_metadata, (str, bytes)):
        return source_metadata
    if len(source_metadata) == len(messages):
        return source_metadata
    by_content: dict[str, list[Any]] = {}
    for item in source_metadata:
        if not isinstance(item, Mapping):
            continue
        content = str(item.get("content") or item.get("text") or "")
        if content:
            by_content.setdefault(content, []).append(item)
    aligned: list[Any] = []
    for message in messages:
        content = str(message.get("content") or "")
        rows = by_content.get(content) or []
        if rows:
            aligned.append(rows.pop(0))
            continue
        role = str(message.get("role") or "").strip().lower()
        if role in {"user", "assistant"}:
            aligned.append({"sourceType": "recent_dialogue"})
        elif content.startswith("[对话历史原文：数据]"):
            aligned.append({"sourceType": "recent_dialogue"})
        else:
            aligned.append(None)
    return aligned


def core_dialogue_turn_contract_prompt(
    interpretation: UtteranceInterpretation,
    contract: TurnContract,
    *, model_owned: bool = False,
) -> str:
    """把本轮必答内容和主动交互约束放在用户输入附近，不改变语义合同。"""

    if model_owned:
        return (
            "[本轮语义判断由 ConversationBrain 负责] 根据用户原话和实际上下文决定 interactionAct、目标、"
            "是否纠正、是否继续任务及 proactiveDirective；不得把词法候选当成已经确认的用户意图。"
            "按认知协议报告 outcome，并以实际状态和回执判断完成程度；中文回答和日语发声稿应表达同一结论。"
        )
    proactive_contract = {"proactiveDirectivePolicy": "model_decides_from_current_user_intent"}
    lines = [
        "[Turn Contract：普通对话首轮只负责语义结果]",
        json.dumps({
            "originalUserMessage": interpretation.original_text,
            "semanticText": interpretation.semantic_text,
            "primarySpeechAct": interpretation.primary_speech_act,
            "answerTarget": interpretation.answer_target,
            **proactive_contract,
            "requiredActs": list(contract.required_acts),
            "optionalActs": list(contract.optional_acts),
            "forbiddenSubstitutions": list(contract.forbidden_substitutions),
            "firstVisibleMustCover": list(contract.first_visible_must_cover),
            "replyMode": contract.reply_mode,
        }, ensure_ascii=False, separators=(",", ":")),
        "先完整完成 requiredActs；optionalActs 只能在答案已经成立后自然加入。",
        "semanticText 只是指代补全，不得替代用户原话。原话及最近对话中用户指定的范围、纠正优先；不得因摘要而擅自缩小范围或重复要求用户作已给出的选择。",
        PROACTIVE_DIRECTIVE_INSTRUCTION,
    ]
    if "answer_from_current_visual_observation" in contract.required_acts:
        lines.extend([
            "本轮已有当前视觉观测。先直接理解 semanticText 要求产出的结果类型：画面描述、文字读取、"
            "应用识别、当前活动判断或进一步分析。必须完成语义上实际要求的那一类，不能用另一类视觉输出代替。",
            "当 semanticText 要求活动判断或基于画面分析时，仅复述可见文字、按钮、框或应用名称不算完成；"
            "要先给出证据能支持的最具体结论，再简短说明画面依据。若只要求描述、OCR 或识别应用，就不强行推断活动。",
        ])
    lines.extend([
        "中文回答和日语发声稿都必须覆盖必答内容，且脱离未显示前文也能成立。",
        "不要声明完成状态；后端会依据输出、结束原因和本轮合同计算。",
    ])
    return "\n".join(lines)


def claim_level_fact_policy_prompt(prepared: PreparedTurn) -> str:
    """逐项呈现规划器要求核查的事实范围，不扩大本轮查证任务。"""

    plan = EpistemicPlan.from_dict(prepared.epistemic_plan)
    if plan is None:
        return ""
    claims = [
        {
            "claim": item.claim_scope or item.subject,
            "verificationLevel": item.verification_level,
            "answerDependency": item.answer_dependency,
            "epistemicRisk": item.epistemic_risk,
            "safeReasoningAvailable": item.safe_reasoning_available,
        }
        for item in plan.fact_requirements
        if item.domain == "external_world" and (item.claim_scope or item.subject)
    ]
    if not claims:
        return ""
    return "\n".join([
        "[Claim-Level Fact Policy：事实门只管理列出的具体主张]",
        json.dumps({
            "researchPolicy": plan.research_policy,
            "claims": claims,
        }, ensure_ascii=False, separators=(",", ":")),
        "NONE 不需要外部核查。SOFT 是附属事实：没有充分证据时只降低该主张的确定性，"
        "仍须完整回答用户的独立情感、观点、建议、推理或哲学问题，禁止用全局无法确认替代。"
        "HARD 才能阻塞依赖该事实的内容；没有证据时不得凭记忆补全。"
        "用户提供的经历和描述只能按用户归属讨论，不要自动升级成公共事实。",
    ])


def pressure_managed_prompt(
    messages: Sequence[dict[str, Any]],
    settings: dict[str, Any],
    *,
    reserved_output_tokens: int,
    request_id: str,
    stage: str,
    default_context_limit: Any,
    write_log: Callable[..., Any],
    source_metadata: Sequence[Any] | dict[int, Any] | None = None,
    summary_callback: Callable[..., Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """保留输出与系统余量；记录压缩结果，仍超限则阻止模型请求。"""
    context_limit = safe_nonnegative_int(
        settings.get("apiContextLimit") or default_context_limit
    ) or int(default_context_limit)
    compact_threshold_raw = safe_nonnegative_int(
        settings.get("apiContextCompactThreshold")
    )
    # The threshold is an early compaction target; the provider input budget
    # still comes from ``apiContextLimit`` after output and system reserves.
    compact_threshold = compact_threshold_raw if compact_threshold_raw > 0 else None
    aligned_source_metadata = _aligned_pressure_source_metadata(messages, source_metadata)
    managed, snapshot = apply_prompt_pressure(
        messages,
        model_context_limit=context_limit,
        reserved_output_tokens=max(256, int(reserved_output_tokens)),
        reserved_system_margin=2048,
        compact_threshold_tokens=compact_threshold,
        source_metadata=aligned_source_metadata,
        summary_callback=summary_callback,
    )
    metrics = snapshot.to_dict()
    diagnostic = context_pressure_diagnostic(
        messages,
        managed,
        snapshot,
        source_metadata=aligned_source_metadata,
    )
    write_log(
        "context_pressure_decision",
        requestId=request_id,
        stage=stage,
        sourceType="context_pressure",
        decision=str(diagnostic.get("decision") or "")[:80],
        **metrics,
        diagnostic=diagnostic,
    )
    if not snapshot.fits_budget:
        raise RequestValidationError(
            "context_budget_exceeded",
            "当前不可压缩的系统与会话上下文仍超过模型输入预算，已在调用模型前停止。"
            f"还需释放约 {snapshot.over_budget_tokens} 个估算 token。",
        )
    return managed, metrics


def payload_overhead_tokens(payload: dict[str, Any]) -> int:
    """Tool schemas and response schemas also share the model context window."""
    return sum(estimate_tokens(json.dumps(payload[key], ensure_ascii=False))
               for key in ("tools", "functions", "response_format") if payload.get(key))


def manage_final_payload(settings: dict[str, Any], payload: dict[str, Any], *,
                         request_id: str, purpose: str, write_log: Callable[..., Any],
                         summary_callback: Callable[..., Any] | None = None) -> dict[str, Any]:
    # This runs after cognition/images/repair feedback have been attached, but
    # before provider-specific system-message merging and HTTP slot admission.
    if purpose == "context_history_compaction":
        return dict(payload)  # Do not recursively summarize the summarizer.
    from backend.context.prompt_assembly import assemble_modules, prompt_manifest, wire_messages
    assembled, removed = assemble_modules(payload.get("messages") or [])
    write_log("prompt_assembly", requestId=request_id, purpose=purpose,
              removedModules=removed, **prompt_manifest(assembled))
    messages, metrics = pressure_managed_prompt(
        assembled, settings,
        reserved_output_tokens=int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 1500)
                               + payload_overhead_tokens(payload),
        request_id=request_id, stage=purpose + "_final_payload", default_context_limit=65536,
        write_log=write_log, summary_callback=summary_callback)
    return {**payload, "messages": wire_messages(messages)}


def validate_final_payload(settings: dict[str, Any], payload: dict[str, Any], *,
                           request_id: str, purpose: str, write_log: Callable[..., Any]) -> None:
    """Check the exact provider-projected message list before any network send."""
    snapshot = budget_snapshot(
        payload.get("messages") or [],
        model_context_limit=int(settings.get("apiContextLimit") or 65536),
        reserved_output_tokens=int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 1500),
        reserved_system_margin=2048 + payload_overhead_tokens(payload))
    write_log("context_final_payload", requestId=request_id, purpose=purpose, **snapshot.to_dict())
    if not snapshot.fits_budget:
        raise RequestValidationError("context_budget_exceeded",
            "最终模型请求（含工具和输出预留）仍超过上下文预算，尚未发送。"
            f"还需释放约 {snapshot.over_budget_tokens} 个估算 token。")
