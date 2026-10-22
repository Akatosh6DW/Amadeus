"""上游服务错误的分类、诊断和用户提示规则。

本模块只处理错误值本身，不负责日志落盘、网络重试或 HTTP 响应发送。这样
诊断规则可以在不启动服务入口的情况下测试；入口层通过参数保留旧的
monkeypatch 接口。
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
from collections.abc import Callable
from typing import Any

from agent.upstream_response_reader import (
    UpstreamResponseBodyReadError,
    UpstreamResponseBodyTimeout,
)


class UpstreamProtocolError(RuntimeError):
    """服务商返回 HTTP 成功，但正文没有可用的完成结果。"""


class UpstreamBackgroundDeferred(ConnectionError):
    """非必要模型请求让位给前台聊天请求。"""


def sanitize_provider_error_message(value: Any) -> str:
    """保留服务商诊断信息，同时避免把凭证写入日志或提示。"""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"(?i)\b(?:bearer\s+)?(?:sk|ds)-[A-Za-z0-9._-]{8,}\b",
        "[REDACTED_API_KEY]",
        text,
    )
    text = re.sub(
        r"(?i)\bAIza[A-Za-z0-9_-]{10,}\b",
        "[REDACTED_API_KEY]",
        text,
    )
    text = re.sub(
        r"(?i)(api[_ -]?key\s*[:=]\s*)\S+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)((?:x-goog-api-key|google-api-key|access[_ -]?token)\s*[:=]\s*)\S+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:key|api[_-]?key)=)[^&\s]+",
        r"\1[REDACTED]",
        text,
    )
    return text[:500]


def _provider_error_fields(payload: Any) -> tuple[str, str, str]:
    """Extract code, status/type and message from common provider envelopes."""

    candidates: list[Any]
    if isinstance(payload, dict):
        detail = payload.get("error")
        if isinstance(detail, list):
            candidates = detail
        elif detail is None:
            candidates = [payload]
        else:
            candidates = [detail]
    elif isinstance(payload, list):
        candidates = payload
    else:
        return "", "", ""

    scalar_message = ""
    for candidate in candidates:
        if isinstance(candidate, dict):
            nested = candidate.get("error")
            detail = nested if isinstance(nested, dict) else candidate
            provider_code = str(detail.get("code") or "")
            provider_type = str(detail.get("status") or detail.get("type") or "")
            provider_message = str(detail.get("message") or "")
            if provider_code or provider_type or provider_message:
                if isinstance(payload, dict):
                    provider_code = provider_code or str(payload.get("code") or "")
                    provider_type = provider_type or str(payload.get("status") or payload.get("type") or "")
                    provider_message = provider_message or str(payload.get("message") or "")
                return provider_code, provider_type, provider_message
        elif candidate is not None:
            scalar_message = str(candidate)
    if isinstance(payload, dict):
        return (
            str(payload.get("code") or ""),
            str(payload.get("status") or payload.get("type") or ""),
            str(payload.get("message") or scalar_message or ""),
        )
    return "", "", scalar_message


def upstream_http_error_diagnostics(
    error: urllib.error.HTTPError,
    *,
    sanitizer: Callable[[Any], str] = sanitize_provider_error_message,
) -> dict[str, Any]:
    """读取 HTTP 错误正文一次，并保留经过清理的服务商元数据。

    HTTP 错误正文通常是一次性流；先缓存诊断结果，后续日志和响应处理
    复用缓存，避免第二次读取只得到空正文。正文只提取错误字段且经过
    ``sanitizer``，不会把 API Key 等凭证带入持久化诊断。
    """

    cached = getattr(error, "_amadeus_upstream_diagnostics", None)
    if isinstance(cached, dict):
        return dict(cached)

    raw = b""
    try:
        raw = error.read(16_384)
    except Exception:
        pass

    provider_code = ""
    provider_type = ""
    provider_message = ""
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            payload = None
        provider_code, provider_type, provider_message = _provider_error_fields(payload)

    diagnostics = {
        "status": int(error.code),
        "providerErrorCode": sanitizer(provider_code),
        "providerErrorType": sanitizer(provider_type),
        "providerMessage": sanitizer(provider_message),
        "responseBodyLength": len(raw),
    }
    setattr(error, "_amadeus_upstream_diagnostics", dict(diagnostics))
    return diagnostics


def upstream_http_error_user_message(
    error: urllib.error.HTTPError,
    *,
    diagnostics_reader: Callable[[urllib.error.HTTPError], dict[str, Any]] = upstream_http_error_diagnostics,
) -> str:
    """把上游 HTTP 状态转换为不泄露正文的用户提示。"""

    diagnostics = diagnostics_reader(error)
    status = int(diagnostics.get("status") or error.code)
    provider_code = str(diagnostics.get("providerErrorCode") or "").strip()
    provider_type = str(diagnostics.get("providerErrorType") or "").strip()
    provider_message = str(diagnostics.get("providerMessage") or "").strip()
    provider_label = provider_type or provider_code
    suffix = f"，错误类型 {provider_label}" if provider_label else ""
    if provider_type and provider_code and provider_type != provider_code:
        suffix += f"（{provider_code}）"
    if provider_message:
        suffix += f"，服务商信息：{provider_message}"
    if status == 400:
        return f"服务商拒绝了主对话请求（HTTP 400{suffix}），请求格式或模型上下文不兼容。"
    if status == 401:
        return f"API Key 验证失败（HTTP 401{suffix}）。服务商未接受当前密钥，请在设置中检查 API Key 和 Endpoint，保存后测试连接。"
    if status == 402:
        return f"API 账户余额不足（HTTP 402{suffix}），请检查服务商账户余额。"
    if status == 403:
        return f"API 访问被拒绝（HTTP 403{suffix}），请检查密钥的模型访问权限或服务商访问限制。"
    if status == 404:
        return f"接口或模型不存在（HTTP 404{suffix}），请核对 Endpoint 和 Model。"
    if status == 429:
        return f"请求过于频繁或触及额度限制（HTTP 429{suffix}），请稍后重试。"
    return f"服务商返回 HTTP {status}{suffix}。"


def upstream_connection_error_category(error: BaseException) -> str:
    """将传输错误归入稳定类别，供重试和用户提示共同使用。"""

    if isinstance(error, UpstreamResponseBodyTimeout):
        return "response_body_timeout"
    if isinstance(error, UpstreamResponseBodyReadError):
        return "response_body_incomplete"
    reason = getattr(error, "reason", error)
    text = f"{type(reason).__name__}: {reason}".lower()
    if (
        isinstance(reason, ssl.SSLCertVerificationError)
        or "certificate_verify_failed" in text
        or "self-signed" in text
    ):
        return "tls_certificate_untrusted"
    if "timed out" in text or "10060" in text:
        return "connect_timeout"
    if "name or service not known" in text or "getaddrinfo" in text or "11001" in text:
        return "dns_failed"
    if "connection refused" in text or "10061" in text:
        return "connection_refused"
    if isinstance(error, UpstreamBackgroundDeferred):
        return "background_deferred"
    return "connection_failed"


def upstream_connection_user_message(
    error: BaseException,
    *,
    category_reader: Callable[[BaseException], str] = upstream_connection_error_category,
) -> str:
    """把传输错误类别转换为面向用户的简洁提示。"""

    category = category_reader(error)
    if category == "tls_certificate_untrusted":
        return "上游 HTTPS 证书不受信任；请检查 VPN、HTTPS 扫描或自定义 CA 配置。"
    if category == "response_body_timeout":
        return "上游模型已开始响应，但未在预算内接收完整正文。"
    if category == "response_body_incomplete":
        return "上游模型响应正文未完整接收，请重试。"
    if category == "background_deferred":
        return "后台模型请求已让位给主聊天。"
    if category == "dns_failed":
        return "无法解析上游模型服务地址，请检查 DNS 或网络连接。"
    if category == "connection_refused":
        return "上游模型服务拒绝了连接。"
    return "连接上游模型服务超时，请检查当前网络后重试。"


__all__ = [
    "UpstreamBackgroundDeferred",
    "UpstreamProtocolError",
    "UpstreamResponseBodyReadError",
    "UpstreamResponseBodyTimeout",
    "sanitize_provider_error_message",
    "upstream_connection_error_category",
    "upstream_connection_user_message",
    "upstream_http_error_diagnostics",
    "upstream_http_error_user_message",
]
