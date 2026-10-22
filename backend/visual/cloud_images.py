"""云端图片理解的设置就绪检查、模型请求与错误投影。

共享模型网关负责传输；这里不维护独立重试、后台模型进程或另一份凭据。
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable

import time
import urllib.error
from agent.deepseek_vision import (
    vision_model, vision_request_route,
    prepare_vision_request, normalize_vision_response,
)
from backend.chat.parsing import parse_json_object

class CloudImageUnderstanding:
    def __init__(self, *, settings: Callable[[], dict[str, Any]], log_path: Callable[[], Path],
                 send_chat: Callable[..., Any], log: Callable[..., Any]) -> None:
        self.settings = settings
        self.log_path = log_path
        self.send_chat = send_chat
        self.log = log

    def vision_status(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = self.settings() if settings is None else settings
        enabled = bool(settings.get("visionEnabled", True))
        route = vision_request_route(settings)
        provider = route["provider"] or "unknown"
        key_ready = bool(route["apiKey"])
        provider_label = (
            "Google Gemini" if provider == "google" else
            "DeepSeek" if provider == "deepseek" else
            "受支持的服务商"
        )
        endpoint = route["endpoint"] or str(settings.get("apiEndpoint") or "").strip()
        return {
            "enabled": enabled,
            "provider": provider, "remote": True,
            "configured": key_ready, "ready": enabled and key_ready,
            "running": False, "starting": False, "pid": None,
            "endpoint": endpoint, "model": route["model"],
            "networkMode": str(settings.get("apiNetworkMode") or "auto"),
            "missing": [] if key_ready else [f"{provider_label} API Key"],
            "logPath": str(self.log_path()),
            "message": (
                "云端识图已关闭" if not enabled else
                f"{provider_label} 云端识图跟随当前聊天 Endpoint、Model、Key 和连接路线" if key_ready else
                f"请配置当前聊天使用的 {provider_label} API Key；未知服务商不会自动发送密钥。"
            ),
        }


    def ensure_vision_service(self, settings: dict[str, Any], wait_seconds: float = 90) -> dict[str, Any]:
        status = self.vision_status(settings)
        return {
            **status, "ok": status["ready"],
            "error": "" if status["ready"] else "vision_disabled" if not status["enabled"] else "vision_api_key_missing",
        }


    def try_vision_image(
        self,
        path: Path,
        settings: dict[str, Any],
        user_question: str = "",
        detail_level: str = "detailed",
    ) -> dict[str, Any]:
        service = self.ensure_vision_service(settings)
        if not service.get("ok"):
            return {
                "available": False,
                "provider": service.get("provider") or "",
                "model": service.get("model") or vision_model(settings),
                "error": service.get("message") or "云端识图不可用。",
                "errorCode": service.get("error"),
                "status": service,
            }
        started = time.perf_counter()
        route = vision_request_route(settings)
        provider = route["provider"]
        try:
            payload, image_metadata = prepare_vision_request(path, user_question, detail_level, model=route["model"])
            vision_settings = {
                **settings, "apiEndpoint": route["endpoint"],
                "apiModel": route["model"], "apiKey": route["apiKey"],
            }
            if provider == "google":
                # Keep the legacy DeepSeek credential out of the Google route,
                # including from injected/mock transport settings.
                vision_settings.pop("visionApiKey", None)
            content = self.send_chat(
                vision_settings, payload, timeout=120, purpose="vision",
            )
            parsed = parse_json_object(content)
            if not isinstance(parsed, dict):
                raise ValueError("invalid_vision_json")
            result = normalize_vision_response(parsed, model=payload["model"], provider=provider)
            result.update(image_metadata)
            result.update({"detailLevel": detail_level, "latencyMs": round((time.perf_counter() - started) * 1000)})
            self.log("vision_completed", provider=provider, model=payload["model"],
                             imageCount=image_metadata["imageCount"], elapsedMs=result["latencyMs"])
            return result
        except Exception as error:
            code = int(error.code) if isinstance(error, urllib.error.HTTPError) else 0
            provider_label = "Google Gemini" if provider == "google" else "DeepSeek"
            message = {
                400: f"{provider_label} 拒绝了本次图片请求，请检查图片或模型支持情况。",
                401: f"{provider_label} 视觉 API Key 无效，请检查设置。",
                402: f"{provider_label} API 余额不足，本次未能识图。",
                403: f"当前 {provider_label} 账号无权调用视觉模型。",
                404: f"{provider_label} 视觉模型当前不可用。",
                429: f"{provider_label} 视觉请求过于频繁，请稍后再试。",
            }.get(code)
            if message is None:
                message = (
                    f"{provider_label} 视觉请求超时，请稍后再试。" if isinstance(error, TimeoutError) else
                    f"无法连接 {provider_label} 视觉接口，请检查网络。" if isinstance(error, (urllib.error.URLError, ConnectionError)) else
                    "图片不存在或无法读取。" if isinstance(error, (OSError, FileNotFoundError)) and not code else
                    f"{provider_label} 未返回可用的识图结果，本次没有识别成功。"
                )
            self.log("vision_failed", provider=provider, model=route["model"],
                             errorType=type(error).__name__, httpStatus=code)
            return {"ok": False, "available": False, "provider": provider, "model": route["model"],
                    "error": message, "errorCode": f"vision_http_{code}" if code else "vision_failed"}
