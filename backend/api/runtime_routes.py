"""运行时、会话和本地模型生命周期的 HTTP 路由。

会话令牌和服务地址由每个请求显式传入；租约、状态和 GPT-SoVITS 服务
仍由现有运行时所有者管理。POST 分为 release gate 前后的两段，避免把
原本受发布限制位置约束的业务路由提前派发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class RuntimeResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class RuntimeOperations:
    session_path: str
    model_status: Callable[[Any], dict[str, Any]]
    runtime_status: Callable[[Any], dict[str, Any]]
    gpt_sovits_status: Callable[[], dict[str, Any]]
    touch_runtime_client: Callable[[Any, Any], dict[str, Any]]
    remove_runtime_client: Callable[[Any], int]
    start_gpt_sovits: Callable[[dict[str, Any]], dict[str, Any]]
    stop_gpt_sovits: Callable[[], dict[str, Any]]
    public_settings: Callable[[], dict[str, Any]]


class RuntimeRoutes:
    def __init__(self, operations: RuntimeOperations) -> None:
        self.operations = operations

    def get_session(
        self,
        response: RuntimeResponse,
        path: str,
        session_token: Any,
    ) -> bool:
        if path != self.operations.session_path:
            return False
        response.send_json({"ok": True, "token": session_token})
        return True

    def get_status(self, response: RuntimeResponse, path: str, server_address: Any) -> bool:
        if path == "/api/health":
            response.send_json(self.operations.model_status(server_address))
            return True
        if path == "/api/runtime/status":
            response.send_json(self.operations.runtime_status(server_address))
            return True
        if path == "/api/gpt-sovits/status":
            response.send_json({"ok": True, "status": self.operations.gpt_sovits_status()})
            return True
        return False

    def post_before_release(self, response: RuntimeResponse, path: str, body: dict[str, Any]) -> bool:
        if path in {"/api/runtime/client/register", "/api/runtime/client/heartbeat"}:
            lease = self.operations.touch_runtime_client(body.get("clientId"), body.get("mode"))
            response.send_json({"ok": True, **lease})
            return True
        if path == "/api/runtime/client/unregister":
            count = self.operations.remove_runtime_client(body.get("clientId"))
            response.send_json({"ok": True, "activeClients": count})
            return True
        return False

    def post_after_release(self, response: RuntimeResponse, path: str, body: dict[str, Any]) -> bool:
        if path == "/api/gpt-sovits/start":
            incoming = body.get("settings") if isinstance(body.get("settings"), dict) else {}
            status = self.operations.start_gpt_sovits(incoming)
            response.send_json({
                "ok": bool(status.get("running") or status.get("starting")),
                "status": status,
                "settings": self.operations.public_settings(),
            })
            return True
        if path == "/api/gpt-sovits/stop":
            status = self.operations.stop_gpt_sovits()
            response.send_json({
                "ok": True,
                "status": status,
                "settings": self.operations.public_settings(),
            })
            return True
        return False
