"""设置页的真实模型连通性检查与公开诊断；不暴露凭据、不保存设置。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import time
import urllib.error

@dataclass(frozen=True)
class ApiVerificationPorts:
    settings: Callable[[], dict[str, Any]]
    send_chat: Callable[..., Any]
    transport_status: Callable[..., dict[str, Any]]
    error_category: Callable[[BaseException], str]
    error_message: Callable[[BaseException], str]

class ApiVerification:
    def __init__(self, ports: ApiVerificationPorts) -> None:
        self.ports = ports

    def test_api_connection(self) -> dict[str, Any]:
        settings = self.ports.settings()
        if not settings.get("apiEndpoint"):
            return {"ok": False, "code": "missing_endpoint", "message": "尚未配置 API Endpoint。"}
        if not settings.get("apiModel"):
            return {"ok": False, "code": "missing_model", "message": "尚未配置模型名称。"}
        if not settings.get("apiKey"):
            return {
                "ok": False,
                "code": "missing_api_key",
                "message": "API Key 未配置。请在上方粘贴新的 DeepSeek Key，先保存，再测试连接。",
            }
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Reply with OK only."}],
        }
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        started = time.perf_counter()
        try:
            content = self.ports.send_chat(
                settings,
                payload,
                timeout=20,
                purpose="connection_test",
            )
            return {
                "ok": True,
                "code": "ready",
                "message": f"API 连接正常：{settings['apiModel']}（{round((time.perf_counter() - started) * 1000)} ms）",
                "reply": content[:32],
                "transport": self.ports.transport_status(settings.get("apiEndpoint") or "", settings),
            }
        except urllib.error.HTTPError as error:
            status = int(error.code)
            messages = {
                400: "请求格式或模型参数无效。请核对 Endpoint 和 Model。",
                401: "DeepSeek 拒绝了凭证。API Key 无效、已撤销或没有正确保存。",
                402: "DeepSeek 账户余额不足，请充值后重试。",
                403: "当前 API Key 没有访问该模型的权限。",
                404: "接口或模型不存在。请核对 Endpoint 和 Model。",
                429: "请求过于频繁或触及额度限制，请稍后重试。",
            }
            return {"ok": False, "code": f"http_{status}", "message": messages.get(status, f"DeepSeek 返回 HTTP {status}。")}
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            category = self.ports.error_category(error)
            return {
                "ok": False,
                "code": category,
                "message": self.ports.error_message(error),
                "transport": self.ports.transport_status(settings.get("apiEndpoint") or "", settings),
            }
        except Exception as error:
            return {"ok": False, "code": "unknown", "message": f"连接测试失败：{error}"}
