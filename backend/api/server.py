"""本地 HTTP 接收、鉴权和分域路由分派。

业务服务由组装层显式提供；Handler 只读取/校验请求、分派和投影错误，
不持有第二套任务、会话或语音状态。共享服务保存在绑定后的 Handler 类上。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from http.server import ThreadingHTTPServer
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.parse
from agent.context_compatibility import ContextCompatibilityError
from agent.cognitive_brain import CognitiveProtocolError
from backend.runtime.requests import RequestValidationError
from backend.tts.transport import LocalTtsSynthesisError
from backend.providers.errors import UpstreamProtocolError
from backend.api import transport as http_transport

@dataclass(frozen=True)
class HttpRoutes:
    runtime: Any
    assets: Any
    workbench: Any
    settings: Any
    cache: Any
    conversation: Any
    tasks: Any
    memory: Any
    actions: Any
    speech: Any

@dataclass(frozen=True)
class HttpRuntime:
    log: Callable[..., None]
    set_storage_scope: Callable[..., Any]
    release_mode: Callable[[], bool]
    release_blocked: Callable[[str], bool]
    max_body_bytes: Callable[[], int]
    clean_path: Callable[[str], Path | None]
    content_type: Callable[[Path], str]
    parse_json: Callable[[bytes], dict[str, Any]]

@dataclass(frozen=True)
class HttpChat:
    proactive: Callable[..., Any]
    touch: Callable[..., Any]
    normalize_request_id: Callable[..., str]
    release_tool_gate: Callable[..., bool]
    cancel: Callable[..., dict[str, Any]]
    preserve_failed_request: Callable[..., bool]
    completed_tools: Callable[..., list[Any]]
    stream: Callable[..., None]
    atomic: Callable[..., None]

@dataclass(frozen=True)
class HttpErrors:
    retryable_status: Callable[[], set[int]]
    diagnostics: Callable[..., dict[str, Any]]
    user_message: Callable[..., str]

@dataclass(frozen=True)
class HttpServices:
    routes: HttpRoutes
    runtime: HttpRuntime
    chat: HttpChat
    errors: HttpErrors

class AmadeusHTTPServer(ThreadingHTTPServer):
    """Loopback server tuned for concurrent UI, TTS, and diagnostic requests."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.local_session_token = secrets.token_urlsafe(32)
        self._activity_lock = threading.Lock()
        self._active_requests = 0
        self._last_request_activity = time.monotonic()
        super().__init__(*args, **kwargs)

    def request_activity_started(self) -> None:
        with self._activity_lock:
            self._active_requests += 1
            self._last_request_activity = time.monotonic()

    def request_activity_finished(self) -> None:
        with self._activity_lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._last_request_activity = time.monotonic()

    def activity_snapshot(self) -> dict[str, Any]:
        with self._activity_lock:
            return {
                "activeRequests": self._active_requests,
                "lastActivity": self._last_request_activity,
            }

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        self.request_activity_started()
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_activity_finished()


class AmadeusHandler(http_transport.LocalHttpHandler):
    services: HttpServices

    server_version = "AmadeusLocal/0.3"

    def authorize_local_request(self, method: str) -> bool:
        return http_transport.authorize_local_request(self, method, write_log=self.services.runtime.log)

    def do_OPTIONS(self) -> None:
        AmadeusHandler.authorize_local_request(self, "OPTIONS")

    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool:
        return http_transport.send_json(self, payload, status, write_log=self.services.runtime.log)

    def do_GET(self) -> None:
        try:
            self._do_GET()
        except ContextCompatibilityError as error:
            self.send_json({"ok": False, "code": error.code, "error": str(error),
                            "retryable": error.code == "context_recovery_failed"}, 409)

    def _do_GET(self) -> None:
        if not AmadeusHandler.authorize_local_request(self, "GET"):
            return
        self.services.runtime.set_storage_scope(self.headers.get("X-Amadeus-Storage-Scope"))
        # urlsplit preserves semicolons in the pathname; they are not route aliases.
        request_url = urllib.parse.urlsplit(self.path)
        parsed_path = request_url.path
        if self.services.routes.runtime.get_session(self, parsed_path, self.server.local_session_token):
            return
        if self.services.routes.assets.get_file(self, parsed_path):
            return
        if self.services.routes.runtime.get_status(self, parsed_path, self.server.server_address):
            return
        if self.services.runtime.release_mode() and self.services.runtime.release_blocked(parsed_path):
            self.send_json({"ok": False, "error": "not_available_in_release"}, 404)
            return
        if self.services.routes.workbench.get(self, parsed_path, request_url.query):
            return
        if self.services.routes.settings.get(self, parsed_path):
            return
        if self.services.routes.cache.get(self, parsed_path):
            return
        if self.services.routes.conversation.get(self, parsed_path, request_url.query):
            return
        if self.services.routes.assets.get_background(self, parsed_path):
            return
        if self.services.routes.tasks.get(self, parsed_path, request_url.query):
            return
        if self.services.routes.memory.get(self, parsed_path):
            return
        target = self.services.runtime.clean_path(self.path)
        if not target or not target.exists():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self.services.runtime.content_type(target))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if not AmadeusHandler.authorize_local_request(self, "POST"):
            return
        self.services.runtime.set_storage_scope(self.headers.get("X-Amadeus-Storage-Scope"))
        request_url = urllib.parse.urlsplit(self.path)
        parsed_path = request_url.path
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "invalid_content_length"}, 400)
            return
        if length < 0 or length > self.services.runtime.max_body_bytes():
            self.send_json({"ok": False, "error": "request_body_too_large"}, 413)
            return
        body: dict[str, Any] = {}
        try:
            body = self.services.runtime.parse_json(self.rfile.read(length))
            if self.services.routes.runtime.post_before_release(self, parsed_path, body):
                return
            if self.services.routes.actions.post(self, parsed_path, body):
                return
            if self.services.runtime.release_mode() and self.services.runtime.release_blocked(parsed_path):
                self.send_json({"ok": False, "error": "not_available_in_release"}, 404)
                return
            if self.services.routes.workbench.post_release(self, parsed_path, body):
                return
            if self.services.routes.settings.post(self, parsed_path, body):
                return
            if self.services.routes.cache.post(self, parsed_path, body):
                return
            if self.services.routes.workbench.post(self, parsed_path, body):
                return
            if self.services.routes.runtime.post_after_release(self, parsed_path, body):
                return
            if self.services.routes.tasks.post(self, parsed_path, body):
                return
            if self.services.routes.memory.post_generate(self, parsed_path, body):
                return
            if parsed_path == "/api/proactive-generate":
                self.send_json(self.services.chat.proactive(body))
                return
            if self.services.routes.conversation.post(self, parsed_path, body):
                return
            if self.services.routes.memory.post(self, parsed_path, body):
                return
            if self.services.routes.speech.post(self, parsed_path, body):
                return
            if parsed_path == "/api/interaction":
                self.send_json(self.services.chat.touch(body))
                return
            if parsed_path == "/api/chat-stream/continue":
                request_id = self.services.chat.normalize_request_id(body.get("requestId"))
                released = self.services.chat.release_tool_gate(request_id)
                self.services.runtime.log(
                    "chat_stream_tool_gate_ack",
                    requestId=request_id,
                    released=released,
                    voiceStarted=bool(body.get("voiceStarted")),
                )
                self.send_json({
                    "ok": True,
                    "requestId": request_id,
                    "released": released,
                    "voiceStarted": bool(body.get("voiceStarted")),
                })
                return
            if parsed_path == "/api/chat/cancel":
                result = self.services.chat.cancel(body.get("requestId"))
                self.send_json(result, 200 if result.get("ok") else 400)
                return
            if parsed_path == "/api/chat-stream":
                self.handle_chat_stream(body)
                return
            if parsed_path == "/api/chat":
                self.handle_chat(body)
                return
            self.send_error(404)
        except ContextCompatibilityError as error:
            self.send_json({"ok": False, "code": error.code, "error": str(error),
                            "retryable": error.code == "context_recovery_failed"}, 409)
        except RequestValidationError as error:
            self.send_json({
                "ok": False,
                "code": error.code,
                "error": error.message,
                "retryable": False,
            }, 400)
        except LocalTtsSynthesisError as error:
            request_id = self.services.chat.normalize_request_id(body.get("requestId")) if isinstance(body, dict) else ""
            self.services.runtime.log(
                "local_tts_synthesis_failure",
                path=self.path,
                requestId=request_id,
                code=error.code,
                deterministic=error.deterministic,
                upstreamStatus=error.upstream_status,
                error=error.message[:500],
            )
            self.send_json({
                "ok": False,
                "code": error.code,
                "error": error.message,
                "retryable": not error.deterministic,
                "deterministic": error.deterministic,
                "requestId": request_id,
                "diagnostics": error.diagnostics,
            }, 422 if error.deterministic else 502)
        except urllib.error.HTTPError as error:
            request_id = self.services.chat.normalize_request_id(body.get("requestId")) if isinstance(body, dict) else ""
            retryable = int(error.code) in self.services.errors.retryable_status()
            diagnostics = self.services.errors.diagnostics(error)
            self.services.runtime.log(
                "request_upstream_http_failure",
                path=self.path,
                requestId=request_id,
                status=int(error.code),
                retryable=retryable,
                providerErrorCode=diagnostics["providerErrorCode"],
                providerErrorType=diagnostics["providerErrorType"],
                providerMessage=diagnostics["providerMessage"],
                responseBodyLength=diagnostics["responseBodyLength"],
                traceback=traceback.format_exc(limit=8),
            )
            self.send_json({
                "ok": False,
                "code": f"upstream_http_{int(error.code)}",
                "error": self.services.errors.user_message(error),
                "retryable": retryable,
                "requestId": request_id,
            }, 503 if retryable else 502)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            request_id = self.services.chat.normalize_request_id(body.get("requestId")) if isinstance(body, dict) else ""
            self.services.runtime.log(
                "request_network_failure",
                path=self.path,
                requestId=request_id,
                errorType=type(error).__name__,
                error=str(error)[:500],
                traceback=traceback.format_exc(limit=8),
            )
            self.send_json({
                "ok": False,
                "code": "upstream_network_error",
                "error": "连接 DeepSeek 时发生了瞬时网络错误。",
                "retryable": True,
                "requestId": request_id,
            }, 503)
        except CognitiveProtocolError as error:
            request_id = self.services.chat.normalize_request_id(body.get("requestId")) if isinstance(body, dict) else ""
            request_preserved = self.services.chat.preserve_failed_request(body) if isinstance(body, dict) else False
            self.services.runtime.log("request_cognitive_validation_failure", requestId=request_id,
                             error=str(error)[:2000], stage="cognitive_gate")
            self.send_json({"ok": False, "code": "cognitive_revision_failed",
                            "error": "这轮回复没能整理完成，已停止继续执行。已运行工具的结果会保留，这不代表那些操作没有发生。",
                            "completedTools": self.services.chat.completed_tools(body) if isinstance(body, dict) else [],
                            "failedRequestPreserved": request_preserved,
                            "retryable": False, "requestId": request_id, "stage": "cognitive_gate"}, 502)
        except UpstreamProtocolError as error:
            request_id = self.services.chat.normalize_request_id(body.get("requestId")) if isinstance(body, dict) else ""
            self.services.runtime.log(
                "request_upstream_protocol_failure",
                path=self.path,
                requestId=request_id,
                error=str(error)[:500],
                traceback=traceback.format_exc(limit=8),
            )
            self.send_json({
                "ok": False,
                "code": "upstream_incomplete_response",
                "error": "DeepSeek 返回了不完整响应，自动重试后仍未恢复。",
                "retryable": True,
                "requestId": request_id,
            }, 503)
        except Exception as error:
            request_id = self.services.chat.normalize_request_id(body.get("requestId")) if isinstance(body, dict) else ""
            self.services.runtime.log(
                "request_internal_failure",
                path=self.path,
                requestId=request_id,
                errorType=type(error).__name__,
                error=str(error)[:500],
                traceback=traceback.format_exc(limit=12),
            )
            self.send_json({
                "ok": False,
                "code": "internal_error",
                "error": "本地处理链路发生异常，详情已写入 server_errors.jsonl。",
                "retryable": False,
                "requestId": request_id,
            }, 500)

    def handle_chat_stream(self, body: dict[str, Any]) -> None:
        self.services.chat.stream(self, body)


    def handle_chat(self, body: dict[str, Any]) -> None:
        self.services.chat.atomic(self, body)
