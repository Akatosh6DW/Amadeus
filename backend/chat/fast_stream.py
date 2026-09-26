"""快速语义流的解析、证据门缓冲、升级和协议尾部恢复。

本模块只拥有一次快速模型流的临时状态。请求缓存、认知状态、工具执行和
历史持久化仍由各自服务负责；候选语句在最终证据门通过前不会写入 HTTP。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable, Mapping, Protocol, Sequence

from agent.action_grounding import PreparedTurn
from agent.dialogue_architecture import TurnContract, TurnPolicy, UtteranceInterpretation
from agent.epistemic_architecture import grounded_claim_gate_decision
from agent.model_semantic_stream import ModelSemanticStreamParser
from agent.persona_context import PersonaContextSnapshot
from agent.research_architecture import (
    CapabilityEnvelope,
    EpistemicPlan,
    ResearchDecision,
    create_epistemic_plan,
)
from agent.semantic_recovery import recover_semantic_output
from agent.turn_execution_plan import TurnExecutionPlan, execution_plan_from_dict
from backend.chat.fast_candidate import FastCandidateContext, FastCandidateFinalizer
from backend.common.text import unique_strings
from backend.common.values import clamp_float
from backend.providers.errors import UpstreamProtocolError
from backend.runtime.requests import RequestValidationError
from backend.tools.validation import normalize_tool_authorization
from backend.tts.delivery import enforce_turn_emotion_envelope


class ExecutionState(Protocol):
    body: dict[str, Any]
    request_id: str
    settings: dict[str, Any]
    started: float
    stream_started_perf: float
    user_message: str
    messages: list[dict[str, Any]]
    preflight: dict[str, Any] | None
    route_name: str
    fast_persona_snapshot: PersonaContextSnapshot | None


class ProtocolSession(Protocol):
    header_payload: dict[str, Any]
    candidate_utterances: list[dict[str, Any]]
    delivered_answer_utterances: int

    def emit(self, event_type: str, sequence: int, payload: dict[str, Any]) -> bool: ...
    def emit_header(self, sequence: int, payload: dict[str, Any]) -> bool: ...
    def emit_progress(self, sequence: int, payload: dict[str, Any]) -> bool: ...
    def emit_answer(self, sequence: int, payload: dict[str, Any]) -> bool: ...
    def record_candidate(self, payload: dict[str, Any]) -> None: ...


class FastRequestPort(Protocol):
    def cancelled(self, request_id: str) -> bool: ...
    def progress(self, request_id: str, stage: str, detail: str = "") -> None: ...
    def finish(
        self,
        request_id: str,
        result: dict[str, Any] | None = None,
        *,
        error: str = "",
    ) -> None: ...


class FullPipelinePort(Protocol):
    def process_chat_request_body(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...


class FastRuntimePort(Protocol):
    def write_log(self, event: str, **details: Any) -> None: ...
    def perf_counter(self) -> float: ...
    def release_mode(self) -> bool: ...
    def semantic_recovery_shadow_enabled(self) -> bool: ...
    def plain_text_salvage_enabled(self) -> bool: ...


class FastPreparationPort(Protocol):
    """准备冻结上下文、人格和快速流提示。"""

    def body_prepared_action_turn(self, *args: Any, **kwargs: Any) -> PreparedTurn: ...
    def turn_contract_context(
        self, *args: Any, **kwargs: Any
    ) -> tuple[UtteranceInterpretation, TurnPolicy, TurnContract]: ...
    def frozen_turn_relationship_state(self, body: Mapping[str, Any]) -> Mapping[str, Any] | None: ...
    def read_relationship_state(self) -> Mapping[str, Any]: ...
    def current_turn_context_artifacts(self, body: Mapping[str, Any]) -> Sequence[Any]: ...
    def frozen_turn_context_state(self, body: Mapping[str, Any]) -> Mapping[str, Any] | None: ...
    def cognitive_turn_state(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def contextual_reference_resolution(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def build_persona_context_snapshot(self, *args: Any, **kwargs: Any) -> PersonaContextSnapshot: ...
    def build_fast_stream_messages(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...
    def pressure_managed_prompt(self, *args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]: ...
    def fast_stream_token_budget(self, turn_policy: TurnPolicy) -> int: ...


class FastModelPort(Protocol):
    """快速生成以及正文冻结后的有限模型恢复。"""

    def iter_conversation_brain_stream(self, *args: Any, **kwargs: Any) -> Iterable[str]: ...
    def repair_frozen_utterance_emotions_once(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...
    def recover_atomic_fast_stream_utterance(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...
    def recover_fast_stream_continuation(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...


class FastProjectionPort(Protocol):
    """把语义事件投影成既有回复、日程和工具协议。"""

    def normalize_streaming_utterance(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def resolve_model_schedule_decision(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def schedule_tool_requests(self, decision: Mapping[str, Any]) -> list[dict[str, Any]]: ...
    def normalize_tool_requests(self, value: Any) -> list[dict[str, Any]]: ...
    def fast_stream_combined_result(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...


class FastPersistencePort(Protocol):
    """完成已冻结候选的延迟持久化。"""

    def complete_deferred_chat_persistence(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class FastStreamServices:
    preparation: FastPreparationPort
    model: FastModelPort
    projection: FastProjectionPort
    persistence: FastPersistencePort


@dataclass(frozen=True)
class FastStreamContext:
    execution: ExecutionState
    route: Mapping[str, Any]
    session: ProtocolSession


@dataclass
class FastRunState:
    prepared_turn: PreparedTurn
    interpretation: UtteranceInterpretation
    policy: TurnPolicy
    contract: TurnContract
    relationship_state: Mapping[str, Any]
    persona_snapshot: PersonaContextSnapshot
    route_research_decision: ResearchDecision | None
    route_execution_plan: TurnExecutionPlan | None
    parser: ModelSemanticStreamParser = field(default_factory=ModelSemanticStreamParser)
    raw_stream_parts: list[str] = field(default_factory=list)
    trailer_seen: bool = False
    atomic_protocol_recovered: bool = False
    first_utterance_at: float = 0.0
    research_escalation: dict[str, Any] | None = None
    research_escalation_sequence: int = 0
    tool_escalation: dict[str, Any] | None = None
    tool_escalation_sequence: int = 0
    buffer_until_evidence_gate: bool = True
    buffered_utterance_events: list[tuple[int, dict[str, Any]]] = field(default_factory=list)


class FastStreamRunner:
    """运行一次快速模型流；异常上抛给外层有限恢复协调器。"""

    def __init__(
        self,
        services: FastStreamServices,
        *,
        requests: FastRequestPort,
        pipeline: FullPipelinePort,
        runtime: FastRuntimePort,
        candidate_finalizer: FastCandidateFinalizer,
    ) -> None:
        self.preparation = services.preparation
        self.model = services.model
        self.projection = services.projection
        self.persistence = services.persistence
        self.requests = requests
        self.pipeline = pipeline
        self.runtime = runtime
        self.candidate_finalizer = candidate_finalizer

    def run(self, ctx: FastStreamContext) -> None:
        execution = ctx.execution
        body = execution.body
        request_id = execution.request_id
        settings = execution.settings
        user_message = execution.user_message
        messages = execution.messages
        self.requests.progress(
            request_id,
            "model-generating",
            "快路径正在生成第一条完整语句",
        )
        prepared = self.preparation.body_prepared_action_turn(
            body,
            user_message,
            request_id=request_id,
        )
        interpretation, policy, contract = self.preparation.turn_contract_context(
            user_message,
            prepared,
            task_body=body,
        )
        body["_cognitiveTurnContract"] = contract
        relationship_state = (
            self.preparation.frozen_turn_relationship_state(body)
            or self.preparation.read_relationship_state()
        )
        persona_snapshot = self.preparation.build_persona_context_snapshot(
            user_message,
            messages,
            self.preparation.contextual_reference_resolution(
                user_message,
                messages,
                request_id=request_id,
                current_artifacts=self.preparation.current_turn_context_artifacts(body),
                context_state=self.preparation.frozen_turn_context_state(body),
                model_owned=bool(self.preparation.cognitive_turn_state(body)),
            ),
            relationship_state,
            cognitive_body=body,
        )
        execution.fast_persona_snapshot = persona_snapshot
        state = FastRunState(
            prepared_turn=prepared,
            interpretation=interpretation,
            policy=policy,
            contract=contract,
            relationship_state=relationship_state,
            persona_snapshot=persona_snapshot,
            route_research_decision=ResearchDecision.from_dict(
                ctx.route.get("researchDecision")
                if isinstance(ctx.route.get("researchDecision"), dict)
                else prepared.research_decision
            ),
            route_execution_plan=execution_plan_from_dict(
                ctx.route.get("executionPlan")
                if isinstance(ctx.route.get("executionPlan"), dict)
                else body.get("_turnExecutionPlan")
                if isinstance(body.get("_turnExecutionPlan"), dict)
                else None
            ),
        )
        payload = self._build_payload(ctx, state)
        for delta in self.model.iter_conversation_brain_stream(
            body,
            settings,
            payload,
            timeout=90,
            request_id=request_id,
            purpose="chat_fast_utterance_stream",
        ):
            if self.requests.cancelled(request_id):
                raise RequestValidationError(
                    "request_superseded",
                    "这轮请求已被后续输入替代。",
                )
            state.raw_stream_parts.append(str(delta or ""))
            for event in state.parser.feed(delta):
                if not self._consume_event(ctx, state, event):
                    return
                if state.research_escalation or state.tool_escalation:
                    break
            if state.research_escalation or state.tool_escalation:
                break
        if state.research_escalation or state.tool_escalation:
            self._deliver_escalation(ctx, state)
            return
        self._finish_stream(ctx, state)

    def _build_payload(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
    ) -> dict[str, Any]:
        execution = ctx.execution
        max_tokens = self.preparation.fast_stream_token_budget(state.policy)
        messages, pressure = self.preparation.pressure_managed_prompt(
            self.preparation.build_fast_stream_messages(
                execution.body,
                execution.settings,
                execution.user_message,
                execution.messages,
                state.policy,
                execution.request_id,
                state.interpretation,
                state.contract,
                state.persona_snapshot,
            ),
            execution.settings,
            reserved_output_tokens=max_tokens,
            request_id=execution.request_id,
            stage="fast_stream_primary",
        )
        execution.body["_contextPressure"] = pressure
        payload: dict[str, Any] = {
            "model": execution.settings["apiModel"],
            "temperature": 0.35,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if execution.settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in execution.settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        return payload

    def _resolve_missing_emotions(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        utterances: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not any(item.get("emotionMissingFromModel") for item in utterances):
            return utterances
        execution = ctx.execution
        try:
            self.requests.progress(
                execution.request_id,
                "repairing-emotion",
                "正文已冻结，正在补齐表情与语音情绪",
            )
            return self.model.repair_frozen_utterance_emotions_once(
                execution.settings,
                utterances,
                user_message=execution.user_message,
                recent_messages=execution.messages,
                relationship_state=state.relationship_state,
                request_id=execution.request_id,
            )
        except Exception as error:
            self.runtime.write_log(
                "stream_emotion_label_recovery_failed",
                requestId=execution.request_id,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
            return [
                {
                    **item,
                    "emotionResolvedLocally": True,
                    "emotionUnavailableReason": "emotion_label_recovery_failed",
                }
                for item in utterances
            ]

    def _apply_emotion_envelope(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
    ) -> None:
        session = ctx.session
        updated = self._resolve_missing_emotions(
            ctx,
            state,
            session.candidate_utterances,
        )
        updated = enforce_turn_emotion_envelope(
            updated,
            request_id=ctx.execution.request_id,
            write_log=self.runtime.write_log,
        )
        session.candidate_utterances[:] = updated
        if state.buffered_utterance_events:
            state.buffered_utterance_events = [
                (sequence, updated[index])
                for index, (sequence, _) in enumerate(state.buffered_utterance_events)
            ]

    def _consume_event(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        event: Mapping[str, Any],
    ) -> bool:
        execution = ctx.execution
        session = ctx.session
        event_type = str(event["type"])
        sequence = int(event["sequence"])
        payload = dict(event["payload"])
        if event_type == "turn_header":
            if sequence != 0:
                raise UpstreamProtocolError("turn_header must be the first event")
            return session.emit_header(sequence, {
                **payload,
                "route": execution.route_name,
                "routeReason": ctx.route.get("reason"),
            })
        if event_type == "utterance":
            if state.research_escalation or state.tool_escalation:
                raise UpstreamProtocolError(
                    "fast stream emitted an utterance after tool escalation"
                )
            normalized = self.projection.normalize_streaming_utterance(
                payload,
                execution.settings,
                sequence=sequence,
                user_message=execution.user_message,
                request_id=execution.request_id,
                turn_contract=state.contract,
                first_visible=not session.candidate_utterances,
                persona_snapshot=state.persona_snapshot,
            )
            if not session.candidate_utterances:
                state.first_utterance_at = self.runtime.perf_counter()
                self.runtime.write_log(
                    "chat_stream_first_utterance_ready",
                    requestId=execution.request_id,
                    elapsedMs=round(
                        max(0.0, state.first_utterance_at - execution.stream_started_perf)
                        * 1000
                    ),
                    textCharacters=len(str(normalized.get("textZh") or "")),
                    spokenCharacters=len(str(normalized.get("spokenTextJa") or "")),
                )
                self.requests.progress(
                    execution.request_id,
                    "grounding-gate",
                    "第一条完整语句已生成，正在核对事实与证据边界",
                )
            session.record_candidate(normalized)
            state.buffered_utterance_events.append((sequence, normalized))
            return True
        if event_type == "research_escalation":
            self._accept_research_escalation(ctx, state, sequence, payload)
            return session.emit("tool_status", sequence, {
                "stage": "research-escalation",
                "displayText": "已发现需要核实的外部事实",
                "speakable": False,
                "voiceGate": "not_required",
            })
        if event_type == "tool_escalation":
            self._accept_tool_escalation(ctx, state, sequence, payload)
            return session.emit("tool_status", sequence, {
                "stage": "tool-escalation",
                "displayText": "正在确认回答所需的工具与目标",
                "speakable": False,
                "voiceGate": "not_required",
            })
        if event_type == "turn_trailer":
            return self._consume_trailer(ctx, state, sequence, payload)
        if event_type == "turn_abort":
            session.emit("turn_abort", sequence, payload)
            raise UpstreamProtocolError(
                str(payload.get("reason") or "upstream turn aborted")
            )
        return True

    def _active_capability(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
    ) -> CapabilityEnvelope:
        return CapabilityEnvelope.from_dict(
            ctx.route.get("capabilityEnvelope")
            if isinstance(ctx.route.get("capabilityEnvelope"), dict)
            else state.prepared_turn.capability_envelope
        )

    def _accept_research_escalation(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        sequence: int,
        payload: Mapping[str, Any],
    ) -> None:
        if (
            ctx.session.candidate_utterances
            and not state.buffer_until_evidence_gate
        ):
            raise UpstreamProtocolError(
                "research escalation must precede every user-visible utterance"
            )
        capability = self._active_capability(ctx, state)
        if not capability.may_escalate:
            raise UpstreamProtocolError(
                "research escalation is forbidden by the capability envelope"
            )
        queries = unique_strings([
            str(item).strip()
            for item in (
                payload.get("queries")
                if isinstance(payload.get("queries"), list)
                else []
            )
            if str(item).strip()
        ])[:3]
        unknown_entities = unique_strings([
            str(item).strip()
            for item in (
                payload.get("unknownEntities")
                if isinstance(payload.get("unknownEntities"), list)
                else []
            )
            if str(item).strip()
        ])[:8]
        verification_targets = unique_strings([
            str(item).strip()
            for item in (
                payload.get("verificationTargets")
                if isinstance(payload.get("verificationTargets"), list)
                else []
            )
            if str(item).strip()
        ])[:8]
        reason = str(
            payload.get("reason")
            or "model_confidence_requires_external_evidence"
        ).strip()[:160]
        if not (queries or unknown_entities or verification_targets):
            raise UpstreamProtocolError(
                "research escalation omitted every verification target"
            )
        state.research_escalation = {
            "reason": reason,
            "queries": queries,
            "unknownEntities": unknown_entities,
            "verificationTargets": verification_targets,
            "confidence": clamp_float(payload.get("confidence"), 0.0, 1.0, 0.45),
            "origin": "fast_stream_model",
            "progress": (
                dict(payload.get("progress"))
                if isinstance(payload.get("progress"), dict)
                else {}
            ),
        }
        state.research_escalation_sequence = sequence
        ctx.execution.body["_researchEscalation"] = dict(state.research_escalation)
        self.requests.progress(
            ctx.execution.request_id,
            "research-escalation",
            "模型发现当前事实需要外部证据，正在切换到研究循环",
        )
        self.runtime.write_log(
            "fast_path_research_escalated",
            requestId=ctx.execution.request_id,
            reason=reason,
            queries=queries,
            unknownEntities=unknown_entities,
            verificationTargets=verification_targets,
        )

    def _accept_tool_escalation(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        sequence: int,
        payload: Mapping[str, Any],
    ) -> None:
        if (
            ctx.session.candidate_utterances
            and not state.buffer_until_evidence_gate
        ):
            raise UpstreamProtocolError(
                "tool escalation must precede every user-visible utterance"
            )
        capability = self._active_capability(ctx, state)
        if not capability.may_search_tools:
            raise UpstreamProtocolError(
                "tool search is forbidden by the capability envelope"
            )
        target_domain = str(payload.get("targetDomain") or "none").strip()
        if target_domain not in {
            "none",
            "screen",
            "physical_user",
            "uploaded_image",
            "document",
            "unknown_visual",
        }:
            target_domain = "unknown_visual"
        raw_tool_requests = self.projection.normalize_tool_requests({
            "toolRequests": payload.get("toolRequests")
        })
        capability_query = str(payload.get("capabilityQuery") or "").strip()[:300]
        if not raw_tool_requests and not capability_query:
            raise UpstreamProtocolError(
                "tool escalation omitted both tool requests and capability query"
            )
        todo_evidence_escalation = any(
            item.get("name") in {"todo_search", "todo_manage"}
            for item in raw_tool_requests
        )
        state.tool_escalation = {
            "reason": str(
                payload.get("reason") or "model_discovered_missing_capability"
            ).strip()[:160],
            "targetDomain": target_domain,
            "toolAuthorization": normalize_tool_authorization(
                payload.get("toolAuthorization")
            ),
            "capabilityQuery": capability_query,
            "toolRequests": raw_tool_requests,
            "confidence": clamp_float(payload.get("confidence"), 0.0, 1.0, 0.5),
            "origin": "fast_stream_model",
            "progress": (
                dict(payload.get("progress"))
                if isinstance(payload.get("progress"), dict)
                and not todo_evidence_escalation
                else {}
            ),
        }
        state.tool_escalation_sequence = sequence
        ctx.execution.body["_toolEscalation"] = dict(state.tool_escalation)
        self.requests.progress(
            ctx.execution.request_id,
            "tool-escalation",
            "模型发现回答还需要工具证据，正在校验并加载对应能力",
        )
        self.runtime.write_log(
            "fast_path_tool_escalated",
            requestId=ctx.execution.request_id,
            reason=state.tool_escalation["reason"],
            targetDomain=target_domain,
            capabilityQuery=capability_query,
            requestedTools=[
                str(item.get("name") or "") for item in raw_tool_requests
            ],
        )

    def _consume_trailer(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        sequence: int,
        payload: dict[str, Any],
    ) -> bool:
        execution = ctx.execution
        session = ctx.session
        if not session.candidate_utterances:
            raise UpstreamProtocolError("stream ended without a valid utterance")
        if payload.get("researchNeeded") or payload.get("toolRequests"):
            raise UpstreamProtocolError(
                "fast path attempted to emit an unverified tool conclusion"
            )
        schedule_decision = self.projection.resolve_model_schedule_decision(
            payload,
            execution.settings,
            user_message=execution.user_message,
            messages=execution.messages,
            request_id=execution.request_id,
            body=execution.body,
        )
        execution.body["_scheduleDecision"] = schedule_decision
        schedule_requests = self.projection.schedule_tool_requests(schedule_decision)
        if schedule_requests:
            schedule_intent = (
                schedule_decision.get("intent")
                if isinstance(schedule_decision.get("intent"), Mapping)
                else {}
            )
            state.tool_escalation = {
                "reason": "schedule_intent_requires_local_capability",
                "targetDomain": "local_schedule",
                "capabilityQuery": "",
                "toolRequests": schedule_requests,
                "scheduleIntent": dict(schedule_intent),
                "confidence": schedule_intent.get("confidence", 0.5),
                "origin": "fast_stream_schedule_intent",
                "progress": {},
            }
            state.tool_escalation_sequence = sequence
            execution.body["_toolEscalation"] = dict(state.tool_escalation)
            self.requests.progress(
                execution.request_id,
                "schedule-escalation",
                "已确认日程意图，正在读取或更新本地日程",
            )
            accepted = session.emit("tool_status", sequence, {
                "stage": "schedule-escalation",
                "displayText": "正在处理本地日程",
                "speakable": False,
                "voiceGate": "not_required",
            })
            self.runtime.write_log(
                "fast_path_schedule_escalated",
                requestId=execution.request_id,
                workflowId=str(
                    (schedule_decision.get("workflow") or {}).get("workflowId")
                    if isinstance(schedule_decision.get("workflow"), Mapping)
                    else ""
                ),
                requestedTools=[
                    str(item.get("name") or "") for item in schedule_requests
                ],
            )
            return accepted
        schedule_policy = (
            schedule_decision.get("policy")
            if isinstance(schedule_decision.get("policy"), Mapping)
            else {}
        )
        if schedule_policy.get("requiresUserResponse"):
            payload["requiresUserResponse"] = True
            if (
                schedule_decision.get("recoveredFromMissingProtocol")
                or str((schedule_decision.get("intent") or {}).get("source") or "")
                == "temporal_grounding_guard"
            ):
                state.tool_escalation = {
                    "reason": "schedule_protocol_recovery_requires_clarification",
                    "targetDomain": "local_schedule",
                    "capabilityQuery": "",
                    "toolRequests": [],
                    "scheduleIntent": dict(schedule_decision.get("intent") or {}),
                    "confidence": float(
                        (schedule_decision.get("intent") or {}).get(
                            "confidence",
                            0.5,
                        )
                    ),
                    "origin": "fast_stream_schedule_recovery",
                    "progress": {},
                }
                state.tool_escalation_sequence = sequence
                execution.body["_toolEscalation"] = dict(state.tool_escalation)
                self.requests.progress(
                    execution.request_id,
                    "schedule-clarification",
                    "已识别日程请求，正在确认缺少的必要信息",
                )
                self.runtime.write_log(
                    "fast_path_schedule_clarification_escalated",
                    requestId=execution.request_id,
                    missingFields=list(
                        (schedule_decision.get("intent") or {}).get(
                            "missingFields"
                        )
                        or []
                    ),
                )
                return True

        self._apply_emotion_envelope(ctx, state)
        payload["finalEmotion"] = session.candidate_utterances[-1].get("emotion")
        payload["finalEmotionIntensity"] = session.candidate_utterances[-1].get(
            "emotionIntensity"
        )
        visible_answer = "\n\n".join(
            str(item.get("textZh") or "")
            for item in session.candidate_utterances
        )
        active_plan = EpistemicPlan.from_dict(
            state.prepared_turn.epistemic_plan
        ) or create_epistemic_plan(
            execution.request_id,
            state.interpretation,
            decision=state.route_research_decision,
        )
        gate = grounded_claim_gate_decision(
            visible_answer,
            active_plan.epistemic_state(),
            None,
            research_policy=(
                active_plan.research_policy
                if execution.settings.get("permissionWebSearch")
                else "forbidden"
            ),
        )
        self.runtime.write_log(
            "fast_candidate_grounding_gate_decision",
            requestId=execution.request_id,
            planId=active_plan.plan_id,
            planRevision=active_plan.revision,
            decision=gate.to_dict(),
        )
        if gate.status == "RESEARCH_ESCALATION":
            state.research_escalation = {
                "reason": "evidence_gate_rejected_unverified_external_claim",
                "queries": [],
                "unknownEntities": list(active_plan.unknown_entities),
                "verificationTargets": list(
                    gate.missing_verification_targets
                    or active_plan.verification_targets
                    or ("unsupported external claims",)
                ),
                "confidence": min(float(active_plan.confidence), 0.5),
                "origin": "fast_stream_evidence_gate",
            }
            state.research_escalation_sequence = (
                state.buffered_utterance_events[0][0]
                if state.buffered_utterance_events
                else sequence
            )
            execution.body["_researchEscalation"] = dict(
                state.research_escalation
            )
            accepted = session.emit(
                "tool_status",
                state.research_escalation_sequence,
                {
                    "stage": "research-escalation",
                    "displayText": "回答涉及尚未核实的外部事实，正在补充证据",
                    "speakable": False,
                    "voiceGate": "not_required",
                },
            )
            self.runtime.write_log(
                "evidence_gate_rejected_fast_final",
                requestId=execution.request_id,
                unknownEntities=list(active_plan.unknown_entities),
                gateDecision=gate.to_dict(),
            )
            return accepted

        combined = self._combined(ctx, state, payload)
        result, recovery_route = self._finalize_candidate(ctx, state, combined)
        delivery_route = (
            recovery_route
            if recovery_route != execution.route_name
            else "reference_rewrite"
            if result.get("referenceAttributionRepaired")
            else execution.route_name
            if gate.status == "PASS"
            and not result.get("groundingGateRewrittenByModel")
            else "grounding_rewrite"
        )
        connection_open = True
        if delivery_route == execution.route_name:
            for buffered_sequence, buffered_payload in state.buffered_utterance_events:
                if not session.emit_answer(buffered_sequence, buffered_payload):
                    connection_open = False
                    break
        result = self.persistence.complete_deferred_chat_persistence(
            execution.settings,
            execution.user_message,
            result,
            request_id=execution.request_id,
        )
        self.requests.finish(execution.request_id, result)
        state.buffered_utterance_events.clear()
        if not connection_open:
            return False
        payload.update({
            "route": delivery_route,
            "routeReason": ctx.route.get("reason"),
            "finalResult": result,
        })
        if not session.emit("turn_trailer", sequence, payload):
            return False
        state.trailer_seen = True
        return True

    def _combined(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        trailer: dict[str, Any],
        *,
        protocol_recovered: bool = False,
    ) -> dict[str, Any]:
        combined = self.projection.fast_stream_combined_result(
            ctx.session.candidate_utterances,
            trailer,
            state.policy,
            first_utterance_at=state.first_utterance_at,
            stream_started_at=ctx.execution.stream_started_perf,
            parser_errors=state.parser.errors,
            protocol_recovered=protocol_recovered,
            turn_contract=state.contract,
        )
        combined.setdefault("_chatMetrics", {})["contextPressure"] = dict(
            ctx.execution.body.get("_contextPressure") or {}
        )
        return combined

    def _finalize_candidate(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        combined: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        execution = ctx.execution
        return self.candidate_finalizer.finalize(
            FastCandidateContext(
                body=execution.body,
                settings=execution.settings,
                user_message=execution.user_message,
                messages=execution.messages,
                request_id=execution.request_id,
                started=execution.started,
                preflight=execution.preflight,
                fast_persona_snapshot=state.persona_snapshot,
                prepared_fast_turn=state.prepared_turn,
                fast_policy=state.policy,
                fast_interpretation=state.interpretation,
                fast_contract=state.contract,
                route_execution_plan=state.route_execution_plan,
                route_research_decision=state.route_research_decision,
                route_name=execution.route_name,
            ),
            combined,
        )

    def _deliver_escalation(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
    ) -> None:
        execution = ctx.execution
        session = ctx.session
        research = state.research_escalation
        tool = state.tool_escalation
        active = research or tool or {}
        active_sequence = (
            state.research_escalation_sequence
            if research
            else state.tool_escalation_sequence
        )
        stream_preamble: dict[str, Any] = {}
        next_sequence = active_sequence + 1

        def emit_escalated_progress(payload: dict[str, Any]) -> bool:
            nonlocal next_sequence
            if not isinstance(payload, dict):
                return False
            accepted = session.emit_progress(next_sequence, payload)
            if not accepted:
                return False
            if payload.get("progressOnly"):
                stream_preamble.clear()
                stream_preamble.update(payload)
            next_sequence += 1
            return True

        semantic_progress = (
            active.get("progress")
            if isinstance(active.get("progress"), dict)
            else {}
        )
        if semantic_progress:
            try:
                progress_payload = self.projection.normalize_streaming_utterance(
                    {
                        "textZh": semantic_progress.get("zh")
                        or semantic_progress.get("textZh"),
                        "spokenTextJa": semantic_progress.get("ja")
                        or semantic_progress.get("spokenTextJa"),
                        "emotion": semantic_progress.get("emotion") or "focused",
                    },
                    execution.settings,
                    sequence=next_sequence,
                    user_message=execution.user_message,
                    request_id=execution.request_id,
                    turn_contract=state.contract,
                    first_visible=False,
                    persona_snapshot=state.persona_snapshot,
                )
                progress_payload.update({
                    "progressKind": (
                        "research_bridge" if research else "tool_progress"
                    ),
                    "progressOnly": True,
                    "dependsOnResearchEvidence": False,
                })
                if not emit_escalated_progress(progress_payload):
                    return
                execution.body["_semanticToolProgressProvided"] = True
            except Exception as error:
                self.runtime.write_log(
                    "semantic_tool_progress_rejected",
                    requestId=execution.request_id,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                )

        result = self.pipeline.process_chat_request_body(
            execution.body,
            execution.settings,
            request_id=execution.request_id,
            started_at=execution.started,
            preflight=execution.preflight,
            stream_preamble=stream_preamble,
            progress_callback=emit_escalated_progress,
            persona_snapshot=state.persona_snapshot,
        )
        result[
            "researchEscalatedFromFastPath"
            if research
            else "toolEscalatedFromFastPath"
        ] = True
        self.requests.finish(execution.request_id, result)
        session.emit("turn_trailer", next_sequence, {
            "route": "research_loop" if research else "tool_query_loop",
            "routeReason": (
                "fast_path_model_research_escalation"
                if research
                else "fast_path_model_tool_escalation"
            ),
            "finalResult": result,
            "preambleUtteranceIds": [
                str(item.get("utteranceId") or "")
                for item in session.candidate_utterances
            ],
            (
                "researchEscalation" if research else "toolEscalation"
            ): dict(active),
        })

    def _finish_stream(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
    ) -> None:
        execution = ctx.execution
        session = ctx.session
        self.requests.progress(
            execution.request_id,
            "model-output-parsing",
            "模型响应已结束，正在解析语义事件",
        )
        tail_events = state.parser.finish()
        raw_stream_text = "".join(state.raw_stream_parts)
        raw_stream_bytes = raw_stream_text.encode("utf-8")
        developer_raw_preview = ""
        if execution.body.get("developerDebug") and not self.runtime.release_mode():
            developer_raw_preview = raw_stream_text[:8_000]
            for secret_key in ("apiKey", "webSearchApiKey"):
                secret = str(execution.settings.get(secret_key) or "")
                if secret:
                    developer_raw_preview = developer_raw_preview.replace(
                        secret,
                        "[REDACTED]",
                    )
            developer_raw_preview = re.sub(
                r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{12,}",
                "Bearer [REDACTED]",
                developer_raw_preview,
            )
        self.runtime.write_log(
            "upstream_stream_protocol_diagnostic",
            purpose="chat_fast_utterance_stream",
            requestId=execution.request_id,
            responseMode="stream",
            rawBodyLength=len(raw_stream_bytes),
            nonWhitespaceCount=len(re.sub(r"\s+", "", raw_stream_text)),
            streamEventCount=len(state.raw_stream_parts),
            parserAcceptedEventCount=state.parser.accepted_event_count,
            parserRejectedEventCount=state.parser.rejected_event_count,
            parserErrors=list(state.parser.errors),
            parserProtocolVersion=state.parser.protocol_version,
            **(
                {"developerRawBodyPreview": developer_raw_preview}
                if developer_raw_preview
                else {}
            ),
        )
        if self.runtime.semantic_recovery_shadow_enabled():
            shadow = recover_semantic_output(
                raw_stream_text,
                allow_plain_text=self.runtime.plain_text_salvage_enabled(),
            )
            self.runtime.write_log(
                "semantic_recovery_v2_shadow",
                requestId=execution.request_id,
                parserAcceptedEventCount=state.parser.accepted_event_count,
                **shadow.to_dict(include_payload=False),
            )
        tail_trailer = next(
            (
                event
                for event in tail_events
                if event["type"] == "turn_trailer" and not state.trailer_seen
            ),
            None,
        )
        if tail_trailer:
            if not self._deliver_tail_trailer(ctx, state, tail_trailer):
                return
        if not state.trailer_seen and not session.candidate_utterances:
            if not self._recover_atomic(ctx, state, raw_stream_text):
                return
        if not state.trailer_seen:
            self._deliver_missing_trailer(ctx, state)

    def _delivery_route(
        self,
        ctx: FastStreamContext,
        result: Mapping[str, Any],
        recovery_route: str,
    ) -> str:
        route_name = ctx.execution.route_name
        return (
            recovery_route
            if recovery_route != route_name
            else "reference_rewrite"
            if result.get("referenceAttributionRepaired")
            else "grounding_rewrite"
            if result.get("groundingGateRewrittenByModel")
            else route_name
        )

    def _deliver_tail_trailer(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        tail_trailer: Mapping[str, Any],
    ) -> bool:
        execution = ctx.execution
        session = ctx.session
        payload = dict(tail_trailer["payload"])
        if payload.get("researchNeeded") or payload.get("toolRequests"):
            raise UpstreamProtocolError(
                "fast path attempted to emit an unverified tool conclusion"
            )
        self._apply_emotion_envelope(ctx, state)
        payload["finalEmotion"] = session.candidate_utterances[-1].get("emotion")
        payload["finalEmotionIntensity"] = session.candidate_utterances[-1].get(
            "emotionIntensity"
        )
        result, recovery_route = self._finalize_candidate(
            ctx,
            state,
            self._combined(ctx, state, payload),
        )
        result = self.persistence.complete_deferred_chat_persistence(
            execution.settings,
            execution.user_message,
            result,
            request_id=execution.request_id,
        )
        self.requests.finish(execution.request_id, result)
        delivery_route = self._delivery_route(ctx, result, recovery_route)
        if delivery_route == execution.route_name:
            for sequence, utterance in state.buffered_utterance_events:
                if not session.emit_answer(sequence, utterance):
                    return False
        state.buffered_utterance_events.clear()
        payload.update({
            "route": delivery_route,
            "routeReason": ctx.route.get("reason"),
            "finalResult": result,
        })
        if not session.emit("turn_trailer", int(tail_trailer["sequence"]), payload):
            return False
        state.trailer_seen = True
        return True

    def _recover_atomic(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
        raw_stream_text: str,
    ) -> bool:
        execution = ctx.execution
        session = ctx.session
        recovered = self.model.recover_atomic_fast_stream_utterance(
            raw_stream_text,
            execution.settings,
            user_message=execution.user_message,
            request_id=execution.request_id,
            turn_contract=state.contract,
            persona_snapshot=state.persona_snapshot,
            recent_messages=execution.messages,
            relationship_state=state.relationship_state,
        )
        if not recovered:
            return True
        first = recovered[0]
        header = {
            "route": execution.route_name,
            "routeReason": ctx.route.get("reason"),
            "turnEmotion": first.get("emotion"),
            "emotionIntensity": first.get("emotionIntensity"),
            "gesture": first.get("gesture"),
            "voiceStyle": first.get("voiceStyle"),
            "voiceTone": first.get("voiceTone"),
            "voiceIntensity": first.get("voiceIntensity"),
            "speechPace": first.get("speechPace"),
            "speechSpeed": first.get("speechSpeed"),
            "deliveryMode": first.get("deliveryMode"),
            "blushIntensity": first.get("blushIntensity"),
            "eyeContact": first.get("eyeContact"),
            "gazeX": first.get("gazeX"),
            "gazeY": first.get("gazeY"),
            "tension": first.get("tension"),
            "confidence": 0.72,
            "streamProtocolRecovered": True,
        }
        if not session.emit_header(0, header):
            return False
        for sequence, utterance in enumerate(recovered, 1):
            session.record_candidate(utterance)
            state.buffered_utterance_events.append((sequence, utterance))
        state.atomic_protocol_recovered = True
        state.first_utterance_at = self.runtime.perf_counter()
        self.runtime.write_log(
            "chat_stream_semantic_response_recovered",
            requestId=execution.request_id,
            recoveryMode=str(first.get("semanticRecoveryMode") or "atomic_json"),
            plainTextSalvaged=bool(first.get("plainTextSalvaged")),
            visibleSegmentCount=len(recovered),
            segmentationSource=str(first.get("visibleSegmentationSource") or ""),
            parserErrors=list(state.parser.errors),
        )
        return True

    def _deliver_missing_trailer(
        self,
        ctx: FastStreamContext,
        state: FastRunState,
    ) -> None:
        execution = ctx.execution
        session = ctx.session
        if not session.candidate_utterances:
            raise UpstreamProtocolError(
                "stream ended without a valid utterance or turn_trailer"
            )
        synthetic_sequence = len(session.candidate_utterances) + (
            1 if session.header_payload else 0
        )
        if (
            not state.atomic_protocol_recovered
            and not self.requests.cancelled(execution.request_id)
        ):
            self.requests.progress(
                execution.request_id,
                "model-generating",
                "首段回答已保留，正在确认是否还有未完成内容",
            )
            recovered = self.model.recover_fast_stream_continuation(
                execution.settings,
                execution.user_message,
                execution.messages,
                session.candidate_utterances,
                request_id=execution.request_id,
                starting_sequence=synthetic_sequence,
                persona_snapshot=state.persona_snapshot,
                cognitive_body=execution.body,
            )
            for utterance in recovered:
                session.record_candidate(utterance)
                state.buffered_utterance_events.append(
                    (synthetic_sequence, utterance)
                )
                synthetic_sequence += 1
        self._apply_emotion_envelope(ctx, state)
        synthetic_trailer = {
            "finalEmotion": session.candidate_utterances[-1].get("emotion"),
            "finalEmotionIntensity": session.candidate_utterances[-1].get(
                "emotionIntensity"
            ),
            "emotionDelta": 0.0,
            "coldShoulder": False,
            "memoryUpdates": [],
            "knowledgeConfidence": "medium",
            "knowledgeConfidenceProvided": False,
            "researchNeeded": False,
            "requiresUserResponse": False,
            "expectedResponseType": "",
            "proactiveDirective": (
                session.candidate_utterances[0].get("proactiveDirective")
                if session.candidate_utterances[0].get(
                    "proactiveDirectiveProvided"
                )
                else ""
            ),
            "toolRequests": [],
            "sources": [],
            "completedActs": list(state.contract.required_acts),
            "standalone": True,
            "dependsOnUndeliveredPrefix": False,
            "turnComplete": True,
            "streamProtocolRecovered": True,
            "streamProtocolIssue": (
                str(
                    session.candidate_utterances[-1].get(
                        "semanticRecoveryMode"
                    )
                    or "atomic_json_instead_of_ndjson"
                )
                if state.atomic_protocol_recovered
                else "missing_turn_trailer"
            ),
        }
        result, recovery_route = self._finalize_candidate(
            ctx,
            state,
            self._combined(
                ctx,
                state,
                synthetic_trailer,
                protocol_recovered=True,
            ),
        )
        result = self.persistence.complete_deferred_chat_persistence(
            execution.settings,
            execution.user_message,
            result,
            request_id=execution.request_id,
        )
        self.requests.finish(execution.request_id, result)
        delivery_route = self._delivery_route(ctx, result, recovery_route)
        if delivery_route == execution.route_name:
            for sequence, utterance in state.buffered_utterance_events:
                if not session.emit_answer(sequence, utterance):
                    return
        state.buffered_utterance_events.clear()
        synthetic_trailer.update({
            "route": delivery_route,
            "routeReason": ctx.route.get("reason"),
            "finalResult": result,
        })
        if not session.emit("turn_trailer", synthetic_sequence, synthetic_trailer):
            return
        state.trailer_seen = True
        self.runtime.write_log(
            "chat_stream_protocol_recovered",
            requestId=execution.request_id,
            emittedUtterances=len(session.candidate_utterances),
            issue=(
                "atomic_json_instead_of_ndjson"
                if state.atomic_protocol_recovered
                else "missing_turn_trailer"
            ),
            parserErrors=list(state.parser.errors),
        )
