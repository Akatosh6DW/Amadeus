"""普通聊天的请求去重、生成、失败恢复和最终交付。

只拥有本次调用的临时变量；请求缓存/取消状态由同一请求运行时持有。
先登记完成结果再发送响应，保留断连后的结果可重取语义。等待重复请求
不会再启动模型；只对既有允许恢复的异常执行一次隔离恢复。
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable, Protocol

from backend.chat.input import streaming_user_message
from backend.runtime.requests import RequestRuntime


class RequestPort(Protocol):
    def prepare_external_chat_body(self, body: dict[str, Any]) -> str: ...
    def claim_chat_request(self, request_id: str, supersedes_request_id: str = "") -> tuple[str, Any]: ...
    def completed_chat_request(self, request_id: str) -> dict[str, Any] | None: ...
    def finish_chat_request(self, request_id: str, result: dict[str, Any] | None = None, *, error: str = "") -> None: ...
    def chat_request_cancelled(self, request_id: str) -> bool: ...


class JsonResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class GenerationPort:
    begin_persona_skill_record: Callable[[dict[str, Any]], Any]
    process_chat_request_body: Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class RecoveryPort:
    pipeline_failure_allows_model_rescue: Callable[[Exception], bool]
    run_isolated_context_recovery: Callable[..., dict[str, Any] | None]


class AtomicChatDelivery:
    def __init__(
        self, requests: RequestPort, generation: GenerationPort, recovery: RecoveryPort,
        *, load_settings: Callable[[], dict[str, Any]], write_log: Callable[..., None],
        clock: Callable[[], float],
    ) -> None:
        self.requests = requests
        self.generation = generation
        self.recovery = recovery
        self.load_settings = load_settings
        self.write_log = write_log
        self.clock = clock

    def deliver(self, response: JsonResponse, body: dict[str, Any]) -> None:
        request_id = self.requests.prepare_external_chat_body(body)
        supersedes_request_id = RequestRuntime.normalize_chat_request_id(body.get("supersedesRequestId"))
        claim, claim_value = self.requests.claim_chat_request(request_id, supersedes_request_id)
        if claim == "owner":
            self.generation.begin_persona_skill_record(body)
        if claim == "cached":
            cached = dict(claim_value)
            cached["requestDeduplicated"] = True
            response.send_json(cached)
            return
        if claim == "wait":
            event = claim_value
            if isinstance(event, threading.Event) and event.wait(190):
                cached = self.requests.completed_chat_request(request_id)
                if cached:
                    cached["requestDeduplicated"] = True
                    response.send_json(cached)
                    return
            response.send_json({
                "ok": False,
                "code": "duplicate_request_wait_timeout",
                "error": "相同请求仍在处理中，请稍后重试。",
                "retryable": True,
                "requestId": request_id,
            }, 503)
            return

        settings = self.load_settings()
        started = self.clock()
        try:
            result = self.generation.process_chat_request_body(
                body,
                settings,
                request_id=request_id,
                started_at=started,
            )
            self.requests.finish_chat_request(request_id, result)
            delivered = response.send_json(result)
            self.write_log(
                "chat_response_delivery",
                requestId=request_id,
                delivered=delivered,
                source=str(result.get("source") or ""),
                characters=len(str(result.get("reply") or "")),
                latencyMs=int(result.get("latencyMs") or 0),
                postprocessDegraded=bool(result.get("pipelineWarnings")),
            )
        except Exception as error:
            user_message, messages = streaming_user_message(body)
            recovered = (
                self.recovery.run_isolated_context_recovery(
                    body,
                    settings,
                    user_message,
                    messages,
                    request_id=request_id,
                    route_name="atomic_chat",
                    failure=error,
                    started_at=started,
                )
                if self.recovery.pipeline_failure_allows_model_rescue(error)
                and not self.requests.chat_request_cancelled(request_id)
                else None
            )
            if recovered is not None:
                self.requests.finish_chat_request(request_id, recovered)
                response.send_json(recovered)
                self.write_log(
                    "chat_atomic_isolated_context_recovered",
                    requestId=request_id,
                    initialErrorType=type(error).__name__,
                    decision=str(recovered.get("contextRecoveryDecision") or ""),
                )
                return
            self.requests.finish_chat_request(request_id, error=str(error))
            raise
