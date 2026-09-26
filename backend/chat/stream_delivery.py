"""流式聊天的应用编排与有界失败交付。

HTTP 入口只提供 NDJSON 通道。请求去重/取消缓存仍由 ``RequestRuntime``
独占，认知与工具状态仍写回原 ``body``；本服务只拥有当前调用的编排状态。
快速语义流的解析和证据门缓冲由 :mod:`backend.chat.fast_stream` 负责。
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
import urllib.error
import uuid
from typing import Any, Mapping, Protocol

from agent.cognitive_brain import CognitiveProtocolError
from agent.context_compatibility import ContextCompatibilityError
from agent.persona_context import PersonaContextSnapshot
from backend.chat.errors import GroundingGateError
from backend.chat.fast_candidate import FastCandidateFinalizer
from backend.chat.fast_stream import FastStreamContext, FastStreamRunner, FastStreamServices
from backend.providers.errors import UpstreamProtocolError
from backend.runtime.requests import RequestValidationError


class NdjsonResponse(Protocol):
    def begin_ndjson(self, status: int = 200) -> None: ...
    def send_ndjson_event(self, event: dict[str, Any]) -> bool: ...


class RequestPort(Protocol):
    """共享请求运行时；实现方不得另建缓存、锁或取消标记。"""

    def prepare(self, body: dict[str, Any]) -> str: ...
    def normalize_id(self, value: Any) -> str: ...
    def claim(self, request_id: str, supersedes_request_id: str = "") -> tuple[str, Any]: ...
    def completed(self, request_id: str) -> dict[str, Any] | None: ...
    def finish(
        self,
        request_id: str,
        result: dict[str, Any] | None = None,
        *,
        error: str = "",
    ) -> None: ...
    def cancelled(self, request_id: str) -> bool: ...
    def progress(self, request_id: str, stage: str, detail: str = "") -> None: ...


class TurnPipelinePort(Protocol):
    """上下文准备、路由与完整生成链路。"""

    def begin_persona_skill_record(self, body: dict[str, Any]) -> Any: ...
    def load_settings(self) -> dict[str, Any]: ...
    def streaming_user_message(
        self, body: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]: ...
    def ensure_turn_context_snapshot(self, *args: Any, **kwargs: Any) -> Any: ...
    def ensure_context_control(self, *args: Any, **kwargs: Any) -> Any: ...
    def resolve_recent_artifact_task_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def dignity_preflight(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def classify_streaming_chat_route(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def process_chat_request_body(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def completed_tools_for_request(self, body: Mapping[str, Any]) -> list[str]: ...
    def preserve_failed_cognitive_request(self, body: Mapping[str, Any]) -> None: ...


class RecoveryPort(Protocol):
    """按既有顺序执行有限恢复，并投影面向用户的错误。"""

    def deterministic_external_action_recovery(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...
    def pipeline_failure_allows_model_rescue(self, failure: Exception) -> bool: ...
    def run_isolated_context_recovery(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...
    def run_model_pipeline_rescue(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...
    def upstream_http_error_user_message(self, error: urllib.error.HTTPError) -> str: ...
    def upstream_connection_user_message(self, error: Exception) -> str: ...


class RuntimePolicyPort(Protocol):
    """入口运行策略和无正文诊断；不拥有对话状态。"""

    def backend_single_turn_recovery_enabled(self) -> bool: ...
    def write_log(self, event: str, **details: Any) -> None: ...
    def wall_time(self) -> float: ...
    def perf_counter(self) -> float: ...
    def uuid_hex(self) -> str: ...


@dataclass
class StreamExecution:
    """当前 owner 请求的唯一应用编排状态。"""

    body: dict[str, Any]
    request_id: str
    settings: dict[str, Any]
    started: float
    stream_started_perf: float
    user_message: str
    messages: list[dict[str, Any]]
    preflight: dict[str, Any] | None = None
    route_name: str = "fallback_legacy"
    fast_persona_snapshot: PersonaContextSnapshot | None = None


class StreamProtocolSession:
    """唯一拥有本轮 NDJSON 包络和已交付计数的会话对象。"""

    def __init__(
        self,
        response: NdjsonResponse,
        request_id: str,
        *,
        turn_id: str,
        schema_version: int = 1,
    ) -> None:
        self.response = response
        self.request_id = request_id
        self.turn_id = turn_id
        self.schema_version = schema_version
        self.header_payload: dict[str, Any] = {}
        self.candidate_utterances: list[dict[str, Any]] = []
        self.delivered_answer_utterances = 0

    def emit(self, event_type: str, sequence: int, payload: dict[str, Any]) -> bool:
        return self.response.send_ndjson_event({
            "type": event_type,
            "requestId": self.request_id,
            "turnId": self.turn_id,
            "sequence": sequence,
            "schemaVersion": self.schema_version,
            "payload": payload,
        })

    def emit_header(self, sequence: int, payload: dict[str, Any]) -> bool:
        self.header_payload = dict(payload)
        return self.emit("turn_header", sequence, payload)

    def record_candidate(self, payload: dict[str, Any]) -> None:
        self.candidate_utterances.append(payload)

    def emit_progress(self, sequence: int, payload: dict[str, Any]) -> bool:
        accepted = self.emit("progress_utterance", sequence, payload)
        if accepted:
            self.record_candidate(dict(payload))
        return accepted

    def emit_answer(self, sequence: int, payload: dict[str, Any]) -> bool:
        accepted = self.emit("utterance", sequence, payload)
        if accepted:
            self.delivered_answer_utterances += 1
        return accepted

    def recovery_sequence(self) -> int:
        return len(self.candidate_utterances) + (1 if self.header_payload else 0)


class RecoveryCoordinator:
    """保留原异常恢复优先级，并确保同一轮只交付一个终态。"""

    def __init__(
        self,
        requests: RequestPort,
        pipeline: TurnPipelinePort,
        recovery: RecoveryPort,
        runtime: RuntimePolicyPort,
    ) -> None:
        self.requests = requests
        self.pipeline = pipeline
        self.recovery = recovery
        self.runtime = runtime

    def deliver_or_abort(
        self,
        error: Exception,
        execution: StreamExecution,
        session: StreamProtocolSession,
    ) -> None:
        request_id = execution.request_id
        body = execution.body
        controlled_action_result = (
            self.recovery.deterministic_external_action_recovery(
                body,
                execution.settings,
                execution.user_message,
                execution.messages,
                request_id=request_id,
                failure=error,
            )
            if session.delivered_answer_utterances == 0
            and not isinstance(
                error,
                (RequestValidationError, CognitiveProtocolError, ContextCompatibilityError),
            )
            and not self.requests.cancelled(request_id)
            else None
        )
        if controlled_action_result is not None:
            self.requests.finish(request_id, controlled_action_result)
            sequence = session.recovery_sequence()
            if not session.header_payload:
                header = {
                    "route": "action_state_recovery",
                    "routeReason": "deterministic_external_action_terminal",
                    "turnEmotion": controlled_action_result.get("emotion", "concerned"),
                }
                if not session.emit_header(sequence, header):
                    return
                sequence += 1
            session.emit("turn_trailer", sequence, {
                "route": "action_state_recovery",
                "routeReason": "deterministic_external_action_terminal",
                "finalResult": controlled_action_result,
            })
            self.runtime.write_log(
                "chat_stream_action_state_recovered",
                requestId=request_id,
                initialErrorType=type(error).__name__,
                toolStatus=str(controlled_action_result.get("toolStatus") or ""),
            )
            return

        context_recovery_result = (
            self.recovery.run_isolated_context_recovery(
                body,
                execution.settings,
                execution.user_message,
                execution.messages,
                request_id=request_id,
                route_name=execution.route_name,
                failure=error,
                started_at=execution.started,
            )
            if session.delivered_answer_utterances == 0
            and self.recovery.pipeline_failure_allows_model_rescue(error)
            and not self.requests.cancelled(request_id)
            else None
        )
        if context_recovery_result is not None:
            self.requests.finish(request_id, context_recovery_result)
            sequence = session.recovery_sequence()
            if not session.header_payload:
                header = {
                    "route": "isolated_context_recovery",
                    "routeReason": "task_reconstructed_then_normal_pipeline",
                    "turnEmotion": context_recovery_result.get("emotion", "concerned"),
                }
                if not session.emit_header(sequence, header):
                    return
                sequence += 1
            session.emit("turn_trailer", sequence, {
                "route": "isolated_context_recovery",
                "routeReason": "task_reconstructed_then_normal_pipeline",
                "finalResult": context_recovery_result,
            })
            self.runtime.write_log(
                "chat_stream_isolated_context_recovered",
                requestId=request_id,
                initialErrorType=type(error).__name__,
                decision=str(context_recovery_result.get("contextRecoveryDecision") or ""),
                taskGoal=str(context_recovery_result.get("contextRecoveryTaskGoal") or "")[:500],
            )
            return

        model_rescue_result = (
            self.recovery.run_model_pipeline_rescue(
                body,
                execution.settings,
                execution.user_message,
                execution.messages,
                request_id=request_id,
                route_name=execution.route_name,
                failure=error,
            )
            if session.delivered_answer_utterances == 0
            and self.recovery.pipeline_failure_allows_model_rescue(error)
            and not self.requests.cancelled(request_id)
            else None
        )
        if model_rescue_result is not None:
            self.requests.finish(request_id, model_rescue_result)
            sequence = session.recovery_sequence()
            if not session.header_payload:
                header = {
                    "route": "model_pipeline_rescue",
                    "routeReason": "architecture_failure_replanned_without_persona",
                    "turnEmotion": model_rescue_result.get("emotion", "concerned"),
                }
                if not session.emit_header(sequence, header):
                    return
                sequence += 1
            session.emit("turn_trailer", sequence, {
                "route": "model_pipeline_rescue",
                "routeReason": "architecture_failure_replanned_without_persona",
                "finalResult": model_rescue_result,
            })
            self.runtime.write_log(
                "chat_stream_model_pipeline_rescued",
                requestId=request_id,
                initialErrorType=type(error).__name__,
                decision=str(model_rescue_result.get("rescueDecision") or ""),
                toolUsed=str(model_rescue_result.get("toolUsed") or ""),
            )
            return

        recoverable_fast_http_error = (
            isinstance(error, urllib.error.HTTPError) and int(error.code) == 400
        )
        if (
            self.runtime.backend_single_turn_recovery_enabled()
            and (isinstance(error, UpstreamProtocolError) or recoverable_fast_http_error)
            and session.delivered_answer_utterances == 0
            and not body.get("_backendSingleTurnRecoveryAttempted")
            and not self.requests.cancelled(request_id)
        ):
            body["_backendSingleTurnRecoveryAttempted"] = True
            detail = (
                "快速主请求被上游拒绝，正在通过兼容的完整生成链路恢复"
                if recoverable_fast_http_error
                else "同一轮回答格式无法恢复，正在进行一次受控完整生成"
            )
            self.requests.progress(request_id, "model-generating", detail)
            try:
                recovery_started = self.runtime.perf_counter()
                recovered_result = self.pipeline.process_chat_request_body(
                    body,
                    execution.settings,
                    request_id=request_id,
                    started_at=execution.started,
                    preflight=execution.preflight,
                    persona_snapshot=execution.fast_persona_snapshot,
                )
                recovered_result.update({
                    "backendSingleTurnRecovery": True,
                    "backendSingleTurnRecoveryReason": type(error).__name__,
                    "backendSingleTurnRecoveryKind": (
                        "upstream_http_400"
                        if recoverable_fast_http_error
                        else "model_protocol"
                    ),
                    "backendSingleTurnRecoveryMs": round(
                        (self.runtime.perf_counter() - recovery_started) * 1000
                    ),
                })
                self.requests.finish(request_id, recovered_result)
                sequence = session.recovery_sequence()
                if not session.header_payload:
                    header = {
                        "route": "backend_single_turn_recovery",
                        "routeReason": "same_turn_protocol_recovery",
                        "turnEmotion": recovered_result.get("emotion", "thinking"),
                        "streamProtocolRecovered": True,
                    }
                    if not session.emit_header(sequence, header):
                        return
                    sequence += 1
                session.emit("turn_trailer", sequence, {
                    "route": "backend_single_turn_recovery",
                    "routeReason": "same_turn_protocol_recovery",
                    "finalResult": recovered_result,
                    "streamProtocolRecovered": True,
                })
                self.runtime.write_log(
                    "chat_stream_backend_single_turn_recovered",
                    requestId=request_id,
                    initialErrorType=type(error).__name__,
                    initialError=str(error)[:500],
                    recoveryMs=recovered_result["backendSingleTurnRecoveryMs"],
                )
                return
            except Exception as recovery_error:
                self.runtime.write_log(
                    "chat_stream_backend_single_turn_recovery_failed",
                    requestId=request_id,
                    initialErrorType=type(error).__name__,
                    recoveryErrorType=type(recovery_error).__name__,
                    recoveryError=str(recovery_error)[:500],
                )
                error = recovery_error

        self.requests.finish(request_id, error=str(error))
        sequence = session.recovery_sequence()
        if isinstance(error, ContextCompatibilityError):
            error_type, error_stage, connection_failure = error.code, "context_compatibility", False
        elif isinstance(error, RequestValidationError):
            error_type, error_stage, connection_failure = error.code, "request", False
        elif isinstance(error, GroundingGateError):
            error_type, error_stage, connection_failure = "grounding_validation_failed", "grounding_gate", False
        elif isinstance(error, CognitiveProtocolError):
            error_type, error_stage, connection_failure = "cognitive_revision_failed", "cognitive_gate", False
            self.pipeline.preserve_failed_cognitive_request(body)
        elif isinstance(error, urllib.error.HTTPError):
            error_type, error_stage, connection_failure = "upstream_http_failed", "upstream_request", False
        elif isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
            error_type, error_stage, connection_failure = "upstream_connection_failed", "upstream", True
        elif isinstance(error, UpstreamProtocolError):
            error_type, error_stage, connection_failure = "upstream_protocol_failed", "model_protocol", False
        else:
            error_type, error_stage, connection_failure = "internal_pipeline_failed", "pipeline", False
        error_message = (
            self.recovery.upstream_http_error_user_message(error)
            if isinstance(error, urllib.error.HTTPError)
            else self.recovery.upstream_connection_user_message(error)
            if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))
            else str(error)[:500]
        )
        session.emit("turn_abort", sequence, {
            "reason": error_type,
            "errorType": error_type,
            "code": (
                f"upstream_http_{int(error.code)}"
                if isinstance(error, urllib.error.HTTPError)
                else ""
            ),
            "stage": error_stage,
            "connectionFailure": connection_failure,
            "completedTools": self.pipeline.completed_tools_for_request(body),
            "message": error_message,
            "discardPendingUtterances": session.delivered_answer_utterances == 0,
            "stopAudio": False,
            "fallbackAllowed": bool(
                session.delivered_answer_utterances == 0
                and error_type in {
                    "upstream_connection_failed",
                    "upstream_protocol_failed",
                    "internal_pipeline_failed",
                }
            ),
        })
        self.runtime.write_log(
            "chat_stream_failed",
            requestId=request_id,
            candidateUtterances=len(session.candidate_utterances),
            deliveredUtterances=session.delivered_answer_utterances,
            errorCategory=error_type,
            errorStage=error_stage,
            connectionFailure=connection_failure,
            errorType=type(error).__name__,
            error=str(error)[:500],
        )


class StreamChatDelivery:
    """从请求认领到唯一终态的流式聊天应用服务。"""

    def __init__(
        self,
        requests: RequestPort,
        pipeline: TurnPipelinePort,
        fast: FastStreamServices,
        recovery: RecoveryPort,
        runtime: RuntimePolicyPort,
        candidate_finalizer: FastCandidateFinalizer,
    ) -> None:
        self.requests = requests
        self.pipeline = pipeline
        self.fast_runner = FastStreamRunner(
            fast,
            requests=requests,
            pipeline=pipeline,
            runtime=runtime,
            candidate_finalizer=candidate_finalizer,
        )
        self.recovery = RecoveryCoordinator(requests, pipeline, recovery, runtime)
        self.runtime = runtime

    def deliver(self, response: NdjsonResponse, body: dict[str, Any]) -> None:
        request_id = self.requests.prepare(body)
        supersedes_request_id = self.requests.normalize_id(body.get("supersedesRequestId"))
        claim, claim_value = self.requests.claim(request_id, supersedes_request_id)
        if claim == "owner":
            self.pipeline.begin_persona_skill_record(body)
        if claim == "wait":
            event = claim_value
            if isinstance(event, threading.Event):
                event.wait(190)
            claim_value = self.requests.completed(request_id)
            claim = "cached" if claim_value else "timeout"

        response.begin_ndjson()
        session = StreamProtocolSession(
            response,
            request_id,
            turn_id=f"{request_id or self.runtime.uuid_hex()}:turn",
        )
        if claim == "cached" and isinstance(claim_value, dict):
            session.emit_header(0, {
                "route": "fallback_legacy",
                "routeReason": "request_deduplicated",
                "turnEmotion": "thinking",
            })
            session.emit("turn_trailer", 1, {
                "route": "fallback_legacy",
                "finalResult": {**claim_value, "requestDeduplicated": True},
            })
            return
        if claim == "timeout":
            session.emit("turn_abort", 0, {
                "reason": "duplicate_request_wait_timeout",
                "discardPendingUtterances": True,
                "stopAudio": False,
            })
            return

        settings = self.pipeline.load_settings()
        execution = StreamExecution(
            body=body,
            request_id=request_id,
            settings=settings,
            started=self.runtime.wall_time(),
            stream_started_perf=self.runtime.perf_counter(),
            user_message="",
            messages=[],
        )
        execution.user_message, execution.messages = (
            self.pipeline.streaming_user_message(body)
        )
        try:
            self._deliver_owned(execution, session)
        except Exception as error:
            self.recovery.deliver_or_abort(error, execution, session)

    def _deliver_owned(
        self,
        execution: StreamExecution,
        session: StreamProtocolSession,
    ) -> None:
        body = execution.body
        request_id = execution.request_id
        settings = execution.settings
        user_message = execution.user_message
        messages = execution.messages
        self.pipeline.ensure_turn_context_snapshot(
            body,
            request_id=request_id,
            user_message=user_message,
        )
        self.pipeline.ensure_context_control(body, user_message=user_message, settings=settings)
        self.pipeline.resolve_recent_artifact_task_context(
            body,
            settings,
            user_message,
            request_id=request_id,
        )
        execution.preflight = self.pipeline.dignity_preflight(
            settings,
            user_message,
            messages,
            request_id=request_id,
        )
        if isinstance(execution.preflight.get("assessment"), dict):
            body["_dignityAssessment"] = dict(execution.preflight["assessment"])
        route = self.pipeline.classify_streaming_chat_route(
            body,
            settings,
            preflight=execution.preflight,
        )
        execution.route_name = str(route.get("route") or "fallback_legacy")
        self.runtime.write_log(
            "chat_stream_route_selected",
            requestId=request_id,
            route=execution.route_name,
            reason=str(route.get("reason") or ""),
        )
        if execution.route_name != "fast_chat":
            self._deliver_non_fast(execution, session, route)
            return
        self.fast_runner.run(
            FastStreamContext(
                execution=execution,
                route=route,
                session=session,
            )
        )

    def _deliver_non_fast(
        self,
        execution: StreamExecution,
        session: StreamProtocolSession,
        route: Mapping[str, Any],
    ) -> None:
        route_name = execution.route_name
        header = {
            "route": route_name,
            "routeReason": route.get("reason"),
            "turnEmotion": "focused" if route_name == "tool_gated" else "thinking",
            "emotionIntensity": 0.72,
            "gesture": "nod",
            "voiceStyle": "calm",
            "voiceTone": "serious",
            "voiceIntensity": "normal",
            "speechPace": "normal",
            "speechSpeed": 1.0,
            "deliveryMode": "normal",
            "blushIntensity": 0.0,
            "eyeContact": 0.7,
            "gazeX": 0.0,
            "gazeY": -0.04,
            "tension": 0.22,
            "confidence": 0.95,
        }
        if not session.emit_header(0, header):
            return
        sequence = 1
        stream_preamble: dict[str, Any] = {}

        def emit_progress_utterance(payload: dict[str, Any]) -> bool:
            nonlocal sequence
            if not isinstance(payload, dict):
                return False
            accepted = session.emit_progress(sequence, payload)
            if not accepted:
                return False
            if payload.get("progressKind") == "research_bridge":
                stream_preamble.clear()
                stream_preamble.update(payload)
            sequence += 1
            return True

        if route_name == "tool_gated" and execution.preflight.get("action") == "continue":
            tool_requests = (
                route.get("toolRequests")
                if isinstance(route.get("toolRequests"), list)
                else []
            )
            tool_names = {
                str(item.get("name") or "")
                for item in tool_requests
                if isinstance(item, dict)
            }
            tool_status = {
                "stage": (
                    "research-planning"
                    if tool_names & {"web_search", "read_web_page"}
                    else "tool-executing"
                ),
                "displayText": (
                    "正在核对回答中需要确认的公开信息"
                    if tool_names & {"web_search", "read_web_page"}
                    else "正在获取回答所需的工具证据"
                ),
                "speakable": False,
                "voiceGate": "not_required",
            }
            if not session.emit("tool_status", sequence, tool_status):
                return
            sequence += 1
        result = self.pipeline.process_chat_request_body(
            execution.body,
            execution.settings,
            request_id=execution.request_id,
            started_at=execution.started,
            preflight=execution.preflight,
            stream_preamble=stream_preamble,
            progress_callback=emit_progress_utterance,
        )
        self.requests.finish(execution.request_id, result)
        session.emit("turn_trailer", sequence, {
            "route": route_name,
            "routeReason": route.get("reason"),
            "finalResult": result,
            "preambleUtteranceIds": [
                str(item.get("utteranceId") or "")
                for item in session.candidate_utterances
            ],
        })
