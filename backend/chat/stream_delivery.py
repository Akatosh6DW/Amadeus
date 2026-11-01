"""流式聊天的应用编排与有界失败交付。

HTTP 入口只提供 NDJSON 通道。请求去重/取消缓存仍由 ``RequestRuntime``
独占，认知与工具状态仍写回原 ``body``；本服务只拥有当前调用的编排状态。
快速语义流的解析和证据门缓冲由 :mod:`backend.chat.fast_stream` 负责。
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import json
import threading
import time
import urllib.error
import uuid
from typing import Any, Mapping, Protocol

from agent.cognitive_brain import CognitiveProtocolError
from agent.context_compatibility import ContextCompatibilityError
from agent.persona_context import PersonaContextSnapshot
from backend.chat.delivery import DeliveryError, require_delivery
from backend.chat.errors import GroundingGateError
from backend.chat.fast_candidate import FastCandidateFinalizer
from backend.chat.fast_stream import FastStreamContext, FastStreamRunner, FastStreamServices
from backend.providers.errors import UpstreamProtocolError
from backend.runtime.requests import RequestValidationError


_STREAM_RECEIPT_STATES = frozenset({"draft", "sending", "sent", "unknown", "failed", "rejected"})
_STREAM_QUERY_TOOLS = frozenset({
    "qq_status",
    "qq_activity",
    "qq_history",
    "qq_identity",
})
_STREAM_SEND_ACTIONS = frozenset({"delegate", "send", "approve"})
_STREAM_PREFLIGHT_ERRORS = frozenset({
    "invalid_target_type",
    "qq_request_id_required",
    "invalid_qq_attachment_ids",
    "qq_attachments_only_draft_send",
    "invalid_qq_image_ids",
    "qq_account_required",
    "qq_account_changed",
    "qq_attachment_import_failed",
    "qq_attachment_not_found",
    "invalid_imported_qq_image_ids",
    "qq_image_selection_limit",
    "qq_draft_required",
})
_STREAM_RECEIPT_FIELDS = (
    "toolCallId",
    "targetType",
    "targetId",
    "account",
    "messageId",
    "draftId",
)


def _stream_safe_token(value: Any, *, limit: int = 100) -> str:
    text = str(value or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. :"
    return text if text and len(text) <= limit and all(char in allowed for char in text) else ""


def _stream_safe_id(value: Any, *, limit: int = 180) -> str:
    text = str(value or "").strip()
    return text if text and len(text) <= limit and text.isascii() else ""


def _stream_signature_args(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Recover host-bound action arguments from the signed request envelope."""

    signature = value.get("_cognitiveRequestSignature")
    if isinstance(signature, Mapping):
        args = signature.get("args")
        return args if isinstance(args, Mapping) else {}
    if not isinstance(signature, str):
        return {}
    try:
        parsed = json.loads(signature)
    except (TypeError, ValueError):
        return {}
    args = parsed.get("args") if isinstance(parsed, Mapping) else None
    return args if isinstance(args, Mapping) else {}


def _stream_tool_results(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Read host tool results left on this mutable turn body.

    The failure envelope must not copy model prose or QQ message content.  It
    only reads the same bounded execution result locations used by recovery.
    """

    results: list[Mapping[str, Any]] = []
    evidence = body.get("_cognitiveEvidence")
    if isinstance(evidence, Mapping):
        for item in evidence.values():
            if not isinstance(item, Mapping):
                continue
            if item.get("historical"):
                continue
            receipt = item.get("receipt")
            if isinstance(receipt, Mapping):
                results.append(receipt)
            elif item.get("sourceType") == "tool" and (
                item.get("name") or item.get("deliveryState") or item.get("actionReceipt")
            ):
                results.append(item)
    for key in ("_rescueToolResults", "_toolResults", "toolResults"):
        value = body.get(key)
        if isinstance(value, list):
            results.extend(item for item in value if isinstance(item, Mapping))
    return results


def _stream_receipt_outcome(
    *,
    operation: str,
    ok: bool | None,
    delivery_state: str,
    message_id: str,
    sent: bool | None,
) -> str:
    if operation == "query":
        return "completed" if ok is True else "failed" if ok is False else "unknown"
    if operation == "draft":
        return "draft" if delivery_state == "draft" or ok is True else "failed" if ok is False else "unknown"
    if operation == "send":
        # QQ's authoritative success contract is state=sent plus a platform
        # message id.  A tool name, prose field, or bare sent flag is not
        # sufficient evidence for a sent message.
        if delivery_state == "sent" and message_id:
            return "sent"
        if delivery_state in {"sending", "unknown"} or sent is True:
            return "unknown"
        if delivery_state in {"failed", "rejected"} or ok is False:
            return "failed"
        return "unknown"
    return "completed" if ok is True else "failed" if ok is False else "unknown"


def _project_stream_tool_receipt(result: Mapping[str, Any]) -> dict[str, Any] | None:
    action_receipt = result.get("actionReceipt")
    merged: dict[str, Any] = dict(result)
    if isinstance(action_receipt, Mapping):
        merged.update(action_receipt)
    tool = _stream_safe_token(merged.get("name") or merged.get("tool"))
    if not tool:
        return None
    signature_args = _stream_signature_args(merged)
    ok_value = merged.get("ok")
    ok = ok_value if isinstance(ok_value, bool) else None
    delivery_state = str(merged.get("deliveryState") or "").strip().lower()
    if delivery_state not in _STREAM_RECEIPT_STATES:
        delivery_state = ""
    message_id = _stream_safe_id(merged.get("messageId"))
    draft_id = _stream_safe_id(merged.get("draftId"))
    sent_value = merged.get("sent")
    sent = sent_value if isinstance(sent_value, bool) else None
    action = _stream_safe_token(
        merged.get("action") or signature_args.get("action"), limit=40,
    ).lower()
    if tool in _STREAM_QUERY_TOOLS:
        operation = "query"
    elif delivery_state or message_id or draft_id or sent is not None:
        operation = "draft" if delivery_state == "draft" else "send"
    elif tool == "qq_manage" and action == "draft":
        operation = "draft"
    elif tool == "qq_manage" and action in _STREAM_SEND_ACTIONS:
        operation = "send"
    elif tool == "qq_manage":
        operation = "action"
    else:
        operation = "tool"
    outcome = _stream_receipt_outcome(
        operation=operation,
        ok=ok,
        delivery_state=delivery_state,
        message_id=message_id,
        sent=sent,
    )
    projected: dict[str, Any] = {
        "tool": tool,
        "operation": operation,
        "outcome": outcome,
    }
    if ok is not None:
        projected["ok"] = ok
    if action:
        projected["action"] = action
    if delivery_state:
        projected["deliveryState"] = delivery_state
    if sent is not None:
        projected["sent"] = sent
    if message_id:
        projected["messageId"] = message_id
    if draft_id:
        projected["draftId"] = draft_id
    error_code = _stream_safe_token(
        merged.get("errorCode") or merged.get("error"), limit=80,
    ).lower()
    if not error_code:
        envelope = merged.get("_toolResultEnvelope")
        envelope_error = envelope.get("error") if isinstance(envelope, Mapping) else None
        if isinstance(envelope_error, Mapping):
            error_code = _stream_safe_token(
                envelope_error.get("code"), limit=80,
            ).lower()
    if error_code:
        projected["errorCode"] = error_code
    if (
        tool == "qq_manage"
        and operation == "send"
        and ok is False
        and not delivery_state
        and not message_id
        and not draft_id
        and sent is not True
        and error_code in _STREAM_PREFLIGHT_ERRORS
    ):
        # This records a rejected argument before any gateway delivery state
        # exists.  The browser may fold it only when a later receipt confirms
        # a send to this same target; an actual failed/unknown send remains
        # visible.
        projected["preflightFailure"] = True
    for key in _STREAM_RECEIPT_FIELDS:
        if key in projected:
            continue
        raw_value = merged.get(key)
        if key == "targetType" and not raw_value:
            raw_value = signature_args.get("targetType")
        if key == "targetId" and not raw_value:
            raw_value = signature_args.get("targetId") or signature_args.get("target")
        safe = _stream_safe_id(raw_value) if key != "targetType" else _stream_safe_token(raw_value, limit=30).lower()
        if safe:
            projected[key] = safe
    return projected


def project_stream_tool_receipts(
    body: Mapping[str, Any], completed_tools: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Project bounded host receipts for a terminal stream failure.

    ``completed_tools`` comes from the durable host receipt index.  It may
    recover a read-only QQ query after the mutable evidence was compacted, but
    it is deliberately never enough to claim that ``qq_manage`` sent a
    message; that requires the explicit delivery state and message id above.
    """

    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for result in _stream_tool_results(body):
        receipt = _project_stream_tool_receipt(result)
        if receipt is None:
            continue
        identity = (
            str(receipt.get("tool") or ""),
            str(receipt.get("operation") or ""),
            str(receipt.get("outcome") or ""),
            str(receipt.get("messageId") or receipt.get("draftId") or ""),
            str(receipt.get("targetId") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        projected.append(receipt)
    for raw_tool in completed_tools:
        tool = _stream_safe_token(raw_tool)
        if tool not in _STREAM_QUERY_TOOLS:
            continue
        receipt = {
            "tool": tool,
            "operation": "query",
            "outcome": "completed",
            "ok": True,
            "receiptAuthority": "host_receipt_index",
        }
        identity = (tool, "query", "completed", "", "")
        if identity not in seen:
            seen.add(identity)
            projected.append(receipt)
    return projected[:32]


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

    def conversation_turn_scope(self, body: dict[str, Any], request_id: str) -> Any: ...
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
        receipt_body: Mapping[str, Any] | None = None,
    ) -> None:
        self.response = response
        self.request_id = request_id
        self.turn_id = turn_id
        self.schema_version = schema_version
        self.receipt_body = receipt_body
        self.header_payload: dict[str, Any] = {}
        self.candidate_utterances: list[dict[str, Any]] = []
        self.delivered_answer_utterances = 0

    def emit(self, event_type: str, sequence: int, payload: dict[str, Any]) -> bool:
        if event_type == "turn_trailer" and self.receipt_body is not None:
            final_result = payload.get("finalResult")
            projected = project_stream_tool_receipts(self.receipt_body)
            if isinstance(final_result, Mapping) and projected:
                # Keep this UI projection bounded and receipt-only. The cached
                # request result remains the pipeline's canonical result; the
                # stream envelope carries enough structured evidence for the
                # client to suppress a pure QQ-send acknowledgement safely.
                payload = dict(payload)
                payload["finalResult"] = {
                    **dict(final_result),
                    "deliveryReceipts": projected,
                }
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
            controlled_action_result = require_delivery(controlled_action_result, settings=execution.settings, logger=self.runtime.write_log, request_id=request_id)
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
                persona_snapshot=execution.fast_persona_snapshot,
            )
            if session.delivered_answer_utterances == 0
            and not (isinstance(error, DeliveryError) and error.code == "leak_blocked")
            and self.recovery.pipeline_failure_allows_model_rescue(error)
            and not self.requests.cancelled(request_id)
            else None
        )
        if context_recovery_result is not None:
            context_recovery_result = require_delivery(context_recovery_result, settings=execution.settings, logger=self.runtime.write_log, request_id=request_id)
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
                persona_snapshot=execution.fast_persona_snapshot,
            )
            if session.delivered_answer_utterances == 0
            and not (isinstance(error, DeliveryError) and error.code == "leak_blocked")
            and self.recovery.pipeline_failure_allows_model_rescue(error)
            and not self.requests.cancelled(request_id)
            else None
        )
        if model_rescue_result is not None:
            model_rescue_result = require_delivery(model_rescue_result, settings=execution.settings, logger=self.runtime.write_log, request_id=request_id)
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
            and not (isinstance(error, DeliveryError) and error.code == "leak_blocked")
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
                recovered_result = require_delivery(recovered_result, settings=execution.settings, logger=self.runtime.write_log, request_id=request_id)
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
        completed_tools = self.pipeline.completed_tools_for_request(body)
        if not isinstance(completed_tools, (list, tuple)):
            completed_tools = []
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
            "completedTools": list(completed_tools),
            "completedToolReceipts": project_stream_tool_receipts(body, completed_tools),
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
            receipt_body=body,
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
        scope_factory = getattr(self.pipeline, "conversation_turn_scope", None)
        scope = (
            scope_factory(execution.body, execution.request_id)
            if callable(scope_factory)
            else nullcontext()
        )
        # Keep older adapters and lightweight test ports usable while the
        # production server supplies the shared turn serializer.  A plain
        # mock/legacy return value is not a lease and must not be entered.
        if not callable(getattr(scope, "__enter__", None)) or not callable(
            getattr(scope, "__exit__", None)
        ):
            scope = nullcontext()
        with scope:
            self._deliver_owned_serialized(execution, session)

    def _deliver_owned_serialized(
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
        from backend.chat.preparation_cache import remember_stream_preparation

        remember_stream_preparation(body, settings)
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
        result = require_delivery(result, settings=execution.settings, logger=self.runtime.write_log, request_id=execution.request_id)
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
