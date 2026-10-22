"""设置页的真实模型连通性检查与公开诊断；不暴露凭据、不保存设置。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import time
import urllib.error
import urllib.parse

from backend.providers import errors as provider_errors


CONNECTION_TEST_MAX_TOKENS = 128


def _supports_gemini_reasoning_control(settings: dict[str, Any]) -> bool:
    """Use Gemini 3's documented low reasoning setting only on its official endpoint."""

    endpoint = str(settings.get("apiEndpoint") or "").strip().lower()
    model = str(settings.get("apiModel") or "").strip().lower()
    try:
        host = urllib.parse.urlsplit(endpoint).hostname or ""
    except ValueError:
        return False
    return host == "generativelanguage.googleapis.com" and model.startswith("gemini-3")


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
                "message": "API Key 未配置。请在上方粘贴新的 API Key，先保存，再测试连接。",
            }
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "max_tokens": CONNECTION_TEST_MAX_TOKENS,
            "messages": [{"role": "user", "content": "Reply with OK only."}],
        }
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        elif _supports_gemini_reasoning_control(settings):
            payload["reasoning_effort"] = "low"
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
            diagnostics = provider_errors.upstream_http_error_diagnostics(error)
            messages = {
                400: "服务商拒绝了请求。请核对 Endpoint 和 Model，以及请求格式。",
                401: "服务商拒绝了凭证。API Key 无效、已撤销或没有正确保存。",
                402: "服务商账户余额不足，请检查账户余额后重试。",
                403: "当前 API Key 没有访问该模型的权限。",
                404: "接口或模型不存在。请核对 Endpoint 和 Model。",
                429: "请求过于频繁或触及额度限制，请稍后重试。",
            }
            detail_parts: list[str] = []
            for key in ("providerErrorType", "providerErrorCode", "providerMessage"):
                value = str(diagnostics.get(key) or "").strip()
                if value and value not in detail_parts:
                    detail_parts.append(value)
            detail = f" 服务商诊断：{'；'.join(detail_parts)}" if detail_parts else ""
            base_message = messages.get(status) or f"服务商返回 HTTP {status}。"
            return {
                "ok": False,
                "code": f"http_{status}",
                "message": f"{base_message}{detail}",
                "providerErrorCode": diagnostics["providerErrorCode"],
                "providerErrorType": diagnostics["providerErrorType"],
                "providerMessage": diagnostics["providerMessage"],
                "transport": self.ports.transport_status(settings.get("apiEndpoint") or "", settings),
            }
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            category = self.ports.error_category(error)
            return {
                "ok": False,
                "code": category,
                "message": self.ports.error_message(error),
                "transport": self.ports.transport_status(settings.get("apiEndpoint") or "", settings),
            }
        except Exception as error:
            detail = provider_errors.sanitize_provider_error_message(error)
            return {"ok": False, "code": "unknown", "message": f"连接测试失败：{detail}"}
