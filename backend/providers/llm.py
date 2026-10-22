"""上游模型的非流式和 SSE 请求传输。

只处理单个已准入请求；入口负责请求槽、网络 opener、取消状态和结束原因。
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
import http.client
import json
import re
import time
from typing import Any, ContextManager
import urllib.error
import urllib.request

from agent.fingerprints import runtime_fingerprint
from agent.upstream_response_reader import (
    UpstreamResponseBodyReadError, UpstreamResponseBodyTimeout,
    iter_response_lines, read_complete_response_body,
)
from backend.providers import errors as provider_errors
from backend.providers import protocol as llm_protocol
from backend.providers.errors import UpstreamProtocolError


@dataclass(frozen=True)
class LLMPolicy:
    """由入口逐次提供的重试和限时读取规则。"""

    retryable_status: Collection[int]
    retry_delays: tuple[float, ...]
    background_purposes: Collection[str]
    bounded_body_purposes: Collection[str]


@dataclass(frozen=True)
class LLMIO:
    """请求所需的最小入口端口；回调在 server 中动态解析。"""

    open_request: Callable[..., Any]
    track_response: Callable[[str, Any], ContextManager[Any]]
    check_cancelled: Callable[[str], None]
    log_event: Callable[..., None]
    set_finish_reason: Callable[[str], Any]
    chat_endpoint: Callable[[str], str]


def send_uncoordinated(
    settings: dict[str, Any],
    payload: dict[str, Any],
    timeout: int = 90,
    *,
    request_id: str = "",
    purpose: str = "chat",
    io: LLMIO,
    policy: LLMPolicy,
) -> str:
    """发送一次已获请求槽准入的完整模型请求，保留原有重试预算。"""
    headers = {"Content-Type": "application/json"}
    if settings.get("apiKey"):
        headers["Authorization"] = f"Bearer {settings['apiKey']}"
    safe_request_id = re.sub(r"[^A-Za-z0-9._:-]", "", str(request_id or ""))[:120]
    if safe_request_id:
        headers["X-Amadeus-Request-Id"] = safe_request_id
    active_payload, json_mode_state, json_mode_key = llm_protocol.prepare_upstream_json_mode(settings, payload)
    active_payload = llm_protocol.prepare_provider_payload(settings, active_payload)
    request_body = json.dumps(active_payload, ensure_ascii=False).encode("utf-8")
    json_mode_active = bool(active_payload.get("response_format"))
    # DeepSeek documents that JSON Output can occasionally return HTTP 200
    # with an empty content field. Reserve one immediate non-JSON-mode attempt
    # while keeping the prompt's strict JSON instruction and local validator.
    background = purpose in policy.background_purposes
    attempts = 1 if background or purpose == "context_compatibility_recovery" else len(policy.retry_delays) + 1 + int(json_mode_active)
    request_timeout = max(0.001, float(timeout))
    deadline = time.monotonic() + request_timeout
    # The socket's connection timeout is not an inference/body timeout.
    # The cognitive phase owns this budget regardless of model tier or proxy.
    response_body_read_enabled = purpose in policy.bounded_body_purposes or bool(active_payload.get("tools"))
    last_error: Exception | None = None
    protocol_failure_count = 0
    for attempt in range(1, attempts + 1):
        io.check_cancelled(safe_request_id)
        data: Any = None
        raw = b""
        http_status = 0
        attempt_started = time.perf_counter()
        request = urllib.request.Request(
            io.chat_endpoint(settings["apiEndpoint"]),
            data=request_body,
            headers=headers,
            method="POST",
        )
        try:
            remaining = max(0.001, deadline - time.monotonic())
            attempt_timeout = max(0.001, min(8 if background else 45, remaining))
            with io.open_request(
                request,
                settings["apiEndpoint"],
                timeout=attempt_timeout,
                request_id=safe_request_id,
                purpose=purpose,
                settings=settings,
            ) as response, io.track_response(safe_request_id, response):
                http_status = int(getattr(response, "status", 200) or 200)
                if response_body_read_enabled:
                    body_started = time.perf_counter()
                    # Non-streaming reasoning may delay body bytes after HTTP
                    # 200. Keep this admitted request within its total deadline
                    # instead of inheriting the connection timeout and replaying.
                    body_budget = max(0.0, deadline - time.monotonic())
                    io.log_event(
                        "upstream_response_headers_received",
                        purpose=purpose,
                        requestId=safe_request_id,
                        attempt=attempt,
                        attempts=attempts,
                        httpStatus=http_status,
                        readPhase="headerReceived",
                        headerReceived=True,
                        readStarted=False,
                        readFinished=False,
                        bodyReadMode="bounded_complete",
                        bodyReadTimeoutSeconds=round(body_budget, 3),
                        totalTimeoutSeconds=round(request_timeout, 3),
                        rawBodyLength=0,
                        bodyReadBytes=0,
                        bodyReadElapsedMs=0,
                        elapsedMs=round((time.perf_counter() - attempt_started) * 1000),
                    )
                    raw = read_complete_response_body(
                        response,
                        deadline=deadline,
                        idle_timeout=None,
                        check_cancelled=lambda: io.check_cancelled(safe_request_id),
                    )
                    io.log_event(
                        "upstream_response_body_read_completed",
                        purpose=purpose,
                        requestId=safe_request_id,
                        attempt=attempt,
                        attempts=attempts,
                        httpStatus=http_status,
                        readPhase="readFinished",
                        headerReceived=True,
                        readStarted=True,
                        readFinished=True,
                        bodyReadMode="bounded_complete",
                        bodyReadTimeoutSeconds=round(body_budget, 3),
                        totalTimeoutSeconds=round(request_timeout, 3),
                        rawBodyLength=len(raw),
                        bodyReadBytes=len(raw),
                        bodyReadElapsedMs=round((time.perf_counter() - body_started) * 1000),
                        elapsedMs=round((time.perf_counter() - attempt_started) * 1000),
                    )
                else:
                    raw = response.read()
            io.check_cancelled(safe_request_id)
            data = json.loads(raw.decode("utf-8"))
            content = llm_protocol.extract_upstream_chat_text(data)
            usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}
            io.set_finish_reason(llm_protocol.upstream_finish_reason(data))
            io.log_event(
                "upstream_response_received",
                purpose=purpose,
                requestId=safe_request_id,
                attempt=attempt,
                attempts=attempts,
                characters=len(content),
                nonWhitespaceCount=len(re.sub(r"\s+", "", content)),
                responseMode="json" if json_mode_active else "text",
                httpStatus=http_status,
                outputTokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
                finishReason=llm_protocol.upstream_finish_reason(data),
                rawBodyLength=len(raw),
                requestPayloadFingerprint=runtime_fingerprint(request_body, length=24),
                responseContentFingerprint=runtime_fingerprint(content, length=24),
                providerResponseId=str(data.get("id") or "")[:120] if isinstance(data, dict) else "",
                streamEventCount=0,
                parserAcceptedEventCount=0,
                parserRejectedEventCount=0,
                elapsedMs=round((time.perf_counter() - attempt_started) * 1000),
            )
            return content
        except urllib.error.HTTPError as error:
            last_error = error
            retryable = int(error.code) in policy.retryable_status
            diagnostics = provider_errors.upstream_http_error_diagnostics(error)
            io.log_event(
                "upstream_http_error",
                purpose=purpose,
                requestId=safe_request_id,
                attempt=attempt,
                attempts=attempts,
                status=int(error.code),
                retryable=retryable,
                providerErrorCode=diagnostics["providerErrorCode"],
                providerErrorType=diagnostics["providerErrorType"],
                providerMessage=diagnostics["providerMessage"],
                responseBodyLength=diagnostics["responseBodyLength"],
            )
            if not retryable or attempt >= attempts or time.monotonic() >= deadline:
                raise
        except AttributeError as error:
            # An explicit stop closes the active urllib response. That can
            # race with HTTPResponse.read() after its internal stream is set
            # to None. Surface it as cancellation, not as a pipeline crash.
            # A non-cancellation programming error remains visible.
            io.check_cancelled(safe_request_id)
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError, UpstreamProtocolError) as error:
            io.check_cancelled(safe_request_id)
            last_error = error
            body_bytes_received = int(getattr(error, "bytes_received", 0) or 0)
            if isinstance(error, (UpstreamResponseBodyTimeout, UpstreamResponseBodyReadError)):
                io.log_event(
                    "upstream_response_body_timeout"
                    if isinstance(error, UpstreamResponseBodyTimeout)
                    else "upstream_response_body_read_failed",
                    purpose=purpose,
                    requestId=safe_request_id,
                    attempt=attempt,
                    attempts=attempts,
                    httpStatus=http_status,
                    readPhase=(
                        "bodyReadTimeout"
                        if isinstance(error, UpstreamResponseBodyTimeout)
                        else str(getattr(error, "stage", "body_read"))
                    ),
                    headerReceived=True,
                    readStarted=True,
                    readFinished=False,
                    timeoutKind=str(getattr(error, "timeout_kind", "")),
                    bodyReadTimeoutSeconds=round(float(getattr(error, "timeout_seconds", 0.0) or 0.0), 3),
                    totalTimeoutSeconds=round(request_timeout, 3),
                    rawBodyLength=body_bytes_received,
                    bodyReadBytes=body_bytes_received,
                    bodyReadElapsedMs=round(float(getattr(error, "elapsed_seconds", 0.0) or 0.0) * 1000),
                    elapsedMs=round((time.perf_counter() - attempt_started) * 1000),
                )
            if isinstance(error, UpstreamProtocolError):
                protocol_failure_count += 1
            connection_category = (
                "body_read_timeout"
                if isinstance(error, UpstreamResponseBodyTimeout)
                else "body_read_failed"
                if isinstance(error, UpstreamResponseBodyReadError)
                else provider_errors.upstream_connection_error_category(error)
                if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))
                else ""
            )
            retryable = connection_category != "tls_certificate_untrusted" and not (
                isinstance(error, UpstreamResponseBodyTimeout)
                and error.timeout_kind == "total_deadline"
            )
            io.log_event(
                "upstream_transient_error",
                purpose=purpose,
                requestId=safe_request_id,
                attempt=attempt,
                attempts=attempts,
                errorType=type(error).__name__,
                error=str(error)[:500],
                responseShape=llm_protocol.upstream_response_shape(data) if data is not None else {},
                responseMode="json" if json_mode_active else "text",
                httpStatus=http_status,
                outputTokens=int(
                    (
                        data.get("usage", {}).get("completion_tokens")
                        or data.get("usage", {}).get("output_tokens")
                        or 0
                    )
                    if isinstance(data, dict) and isinstance(data.get("usage"), dict)
                    else 0
                ),
                rawBodyLength=body_bytes_received or len(raw),
                streamEventCount=0,
                parserAcceptedEventCount=0,
                parserRejectedEventCount=0,
                retryable=retryable,
                connectionCategory=connection_category,
            )
            if not retryable:
                raise
            if isinstance(error, UpstreamProtocolError) and json_mode_active:
                if json_mode_state is not None:
                    json_mode_state[json_mode_key] = True
                active_payload = llm_protocol.json_mode_fallback_payload(active_payload)
                active_payload = llm_protocol.prepare_provider_payload(settings, active_payload)
                request_body = json.dumps(active_payload, ensure_ascii=False).encode("utf-8")
                json_mode_active = False
                io.log_event(
                    "upstream_json_mode_fallback",
                    purpose=purpose,
                    requestId=safe_request_id,
                    attempt=attempt,
                    reason="empty_or_incomplete_json_mode_response",
                )
                if time.monotonic() < deadline:
                    continue
            # A structurally unusable HTTP-200 answer is a generation failure,
            # not an unstable network. Permit exactly one complete regeneration;
            # further blind retries only add latency and can start a repair loop.
            if isinstance(error, UpstreamProtocolError) and protocol_failure_count >= 2:
                raise
            if attempt >= attempts or time.monotonic() >= deadline:
                raise
        retry_delay = policy.retry_delays[min(attempt - 1, len(policy.retry_delays) - 1)]
        io.check_cancelled(safe_request_id)
        if time.monotonic() + retry_delay >= deadline:
            raise last_error or UpstreamProtocolError("upstream retry deadline exceeded")
        time.sleep(retry_delay)
    raise last_error or UpstreamProtocolError("upstream request failed without an error")


def iter_uncoordinated(
    settings: dict[str, Any],
    payload: dict[str, Any],
    timeout: int = 90,
    *,
    request_id: str = "",
    purpose: str = "chat_stream",
    io: LLMIO,
    policy: LLMPolicy,
    send_complete: Callable[..., str],
):
    """惰性读取模型 SSE 增量，必要时执行原有完整请求回退。"""

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if settings.get("apiKey"):
        headers["Authorization"] = f"Bearer {settings['apiKey']}"
    safe_request_id = re.sub(r"[^A-Za-z0-9._:-]", "", str(request_id or ""))[:120]
    if safe_request_id:
        headers["X-Amadeus-Request-Id"] = safe_request_id
    active_payload, json_mode_state, json_mode_key = llm_protocol.prepare_upstream_json_mode(settings, payload)
    active_payload = llm_protocol.prepare_provider_payload(settings, active_payload)
    active_payload["stream"] = True
    request = urllib.request.Request(
        io.chat_endpoint(settings["apiEndpoint"]),
        data=json.dumps(active_payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    stream_deadline = time.monotonic() + max(0.001, float(timeout))
    finish_reasons: list[str] = []
    yielded_non_whitespace = 0
    sse_data_event_count = 0
    sse_json_event_count = 0
    sse_json_decode_error_count = 0
    content_delta_count = 0
    content_delta_chars = 0
    reasoning_delta_count = 0
    reasoning_delta_chars = 0
    reasoning_delta_non_whitespace = 0
    try:
        io.check_cancelled(safe_request_id)
        with io.open_request(
            request,
            settings["apiEndpoint"],
            timeout=timeout,
            connection_attempts=2,
            request_id=safe_request_id,
            purpose=purpose,
            settings=settings,
        ) as response, io.track_response(safe_request_id, response):
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                raw = read_complete_response_body(
                    response,
                    deadline=stream_deadline,
                    idle_timeout=None,
                    check_cancelled=lambda: io.check_cancelled(safe_request_id),
                )
                data = json.loads(raw.decode("utf-8"))
                text = llm_protocol.extract_upstream_chat_text(data)
                io.set_finish_reason(llm_protocol.upstream_finish_reason(data))
                io.log_event(
                    "upstream_stream_fell_back_to_complete_response",
                    purpose=purpose,
                    requestId=safe_request_id,
                    characters=len(text),
                    elapsedMs=round((time.perf_counter() - started) * 1000),
                )
                yield text
                return
            for raw_line in iter_response_lines(
                response,
                deadline=stream_deadline,
                check_cancelled=lambda: io.check_cancelled(safe_request_id),
            ):
                line_kind, data = llm_protocol.parse_sse_data_line(raw_line)
                if line_kind == "skip":
                    continue
                if line_kind == "done":
                    break
                sse_data_event_count += 1
                if line_kind == "malformed":
                    sse_json_decode_error_count += 1
                    continue
                sse_json_event_count += 1
                choices = data.get("choices") if isinstance(data, dict) else []
                for choice in choices if isinstance(choices, list) else []:
                    if not isinstance(choice, dict):
                        continue
                    finish_reason = str(choice.get("finish_reason") or "").strip()
                    if finish_reason and finish_reason not in finish_reasons:
                        finish_reasons.append(finish_reason[:80])
                    delta_payload = choice.get("delta")
                    if isinstance(delta_payload, dict):
                        content_value = delta_payload.get("content")
                        if isinstance(content_value, str):
                            content_delta_count += 1
                            content_delta_chars += len(content_value)
                        reasoning_value = delta_payload.get("reasoning_content")
                        if isinstance(reasoning_value, str):
                            reasoning_delta_count += 1
                            reasoning_delta_chars += len(reasoning_value)
                            reasoning_delta_non_whitespace += len(
                                re.sub(r"\s+", "", reasoning_value)
                            )
                delta = llm_protocol.extract_upstream_stream_delta(data)
                if delta:
                    yielded_non_whitespace += len(re.sub(r"\s+", "", delta))
                    yield delta
            if yielded_non_whitespace == 0:
                # No usable content exists. Avoid repeating the JSON-mode
                # failure atomically; preserve the application's JSON contract
                # in the prompt while changing only the provider mode.
                fallback_payload = dict(active_payload)
                fallback_payload["stream"] = False
                if fallback_payload.get("response_format"):
                    if json_mode_state is not None:
                        json_mode_state[json_mode_key] = True
                    fallback_payload = llm_protocol.json_mode_fallback_payload(fallback_payload)
                if json_mode_state is not None:
                    fallback_payload["_cognitiveJsonModeState"] = json_mode_state
                empty_stream_reason = (
                    "reasoning_only_without_final_content"
                    if reasoning_delta_non_whitespace > 0
                    else "whitespace_only_content_deltas"
                    if content_delta_chars > 0
                    else "malformed_sse_data"
                    if sse_data_event_count > 0 and sse_json_event_count == 0
                    else "json_events_without_content_delta"
                    if sse_json_event_count > 0
                    else "no_sse_data_events"
                )
                io.log_event(
                    "upstream_empty_stream_atomic_fallback_started",
                    purpose=purpose,
                    requestId=safe_request_id,
                    elapsedMs=round((time.perf_counter() - started) * 1000),
                    emptyStreamReason=empty_stream_reason,
                    sseDataEventCount=sse_data_event_count,
                    sseJsonEventCount=sse_json_event_count,
                    sseJsonDecodeErrorCount=sse_json_decode_error_count,
                    contentDeltaCount=content_delta_count,
                    contentDeltaChars=content_delta_chars,
                    reasoningDeltaCount=reasoning_delta_count,
                    reasoningDeltaChars=reasoning_delta_chars,
                    reasoningDeltaNonWhitespace=reasoning_delta_non_whitespace,
                    finishReasons=finish_reasons,
                )
                io.check_cancelled(safe_request_id)
                fallback_text = send_complete(
                    settings,
                    fallback_payload,
                    timeout=max(1, min(45, timeout)),
                    request_id=safe_request_id,
                    purpose=f"{purpose}_empty_stream_atomic_fallback",
                )
                yielded_non_whitespace = len(re.sub(r"\s+", "", fallback_text))
                io.log_event(
                    "upstream_empty_stream_atomic_fallback_completed",
                    purpose=purpose,
                    requestId=safe_request_id,
                    characters=len(fallback_text),
                    nonWhitespaceCount=yielded_non_whitespace,
                    elapsedMs=round((time.perf_counter() - started) * 1000),
                )
                yield fallback_text
    except urllib.error.HTTPError as error:
        diagnostics = provider_errors.upstream_http_error_diagnostics(error)
        io.log_event(
            "upstream_stream_http_error",
            purpose=purpose,
            requestId=safe_request_id,
            status=int(error.code),
            retryable=int(error.code) in policy.retryable_status,
            providerErrorCode=diagnostics["providerErrorCode"],
            providerErrorType=diagnostics["providerErrorType"],
            providerMessage=diagnostics["providerMessage"],
            responseBodyLength=diagnostics["responseBodyLength"],
            elapsedMs=round((time.perf_counter() - started) * 1000),
        )
        raise
    except (
        urllib.error.URLError,
        TimeoutError,
        ConnectionError,
        OSError,
        AttributeError,
        ValueError,
        http.client.IncompleteRead,
        UnicodeDecodeError,
    ) as error:
        io.check_cancelled(safe_request_id)
        transport_error = error if isinstance(
            error,
            (urllib.error.URLError, TimeoutError, ConnectionError, OSError),
        ) else ConnectionError(f"upstream stream body interrupted: {type(error).__name__}")
        category = provider_errors.upstream_connection_error_category(transport_error)
        io.log_event(
            "upstream_stream_transport_interrupted",
            purpose=purpose,
            requestId=safe_request_id,
            category=category,
            timeoutKind=(
                str(getattr(error, "timeout_kind", ""))
                if isinstance(error, UpstreamResponseBodyTimeout) else ""
            ),
            afterContent=yielded_non_whitespace > 0,
            errorType=type(error).__name__,
            error=provider_errors.sanitize_provider_error_message(error)[:500],
            elapsedMs=round((time.perf_counter() - started) * 1000),
        )
        # 首段有效文本交付后不能原子重放，否则用户会看到重复语音；此前可走完整请求回退。
        # TLS 信任和超时等确定性连接错误仍须原样暴露。
        if yielded_non_whitespace > 0 or category in {
            "tls_certificate_untrusted",
            "connect_timeout",
            "dns_failed",
            "connection_refused",
            "response_body_timeout",
        }:
            raise transport_error from error
        fallback_payload = dict(active_payload)
        fallback_payload["stream"] = False
        if json_mode_state is not None:
            fallback_payload["_cognitiveJsonModeState"] = json_mode_state
        io.log_event(
            "upstream_stream_transport_atomic_fallback_started",
            purpose=purpose,
            requestId=safe_request_id,
            category=category,
            elapsedMs=round((time.perf_counter() - started) * 1000),
        )
        fallback_text = send_complete(
            settings,
            fallback_payload,
            timeout=max(1, min(45, timeout)),
            request_id=safe_request_id,
            purpose=f"{purpose}_transport_atomic_fallback",
        )
        io.log_event(
            "upstream_stream_transport_atomic_fallback_completed",
            purpose=purpose,
            requestId=safe_request_id,
            characters=len(fallback_text),
            nonWhitespaceCount=len(re.sub(r"\s+", "", fallback_text)),
            elapsedMs=round((time.perf_counter() - started) * 1000),
        )
        yield fallback_text
        return
    io.set_finish_reason(
        finish_reasons[-1] if finish_reasons else ""
    )
    io.log_event(
        "upstream_stream_completed",
        purpose=purpose,
        requestId=safe_request_id,
        elapsedMs=round((time.perf_counter() - started) * 1000),
        finishReasons=finish_reasons,
    )
