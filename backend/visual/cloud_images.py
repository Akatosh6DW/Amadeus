"""云端图片理解的设置就绪检查、模型请求与错误投影。

共享模型网关负责传输；这里不维护独立重试、后台模型进程或另一份凭据。
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable

import time
import urllib.error
from agent.deepseek_vision import (
    VISION_ENDPOINT, vision_api_key, vision_model,
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
        key_ready = bool(vision_api_key(settings))
        return {
            "enabled": enabled,
            "provider": "deepseek", "remote": True,
            "configured": key_ready, "ready": enabled and key_ready,
            "running": False, "starting": False, "pid": None,
            "endpoint": VISION_ENDPOINT, "model": vision_model(settings),
            "missing": [] if key_ready else ["DeepSeek API Key"],
            "logPath": str(self.log_path()),
            "message": (
                "DeepSeek 云端识图已关闭" if not enabled else
                "DeepSeek 云端识图已配置（实际连通性在识图时检查）" if key_ready else
                "请配置 DeepSeek API Key；主聊天使用 DeepSeek 官方接口时可共用 Key。"
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
                "error": service.get("message") or "DeepSeek 云端识图不可用。",
                "errorCode": service.get("error"),
                "status": service,
            }
        started = time.perf_counter()
        try:
            payload, image_metadata = prepare_vision_request(path, user_question, detail_level, model=vision_model(settings))
            vision_settings = {
                **settings, "apiEndpoint": VISION_ENDPOINT,
                "apiModel": vision_model(settings), "apiKey": vision_api_key(settings),
            }
            content = self.send_chat(
                vision_settings, payload, timeout=120, purpose="vision",
            )
            parsed = parse_json_object(content)
            if not isinstance(parsed, dict):
                raise ValueError("invalid_vision_json")
            result = normalize_vision_response(parsed, model=payload["model"])
            result.update(image_metadata)
            result.update({"detailLevel": detail_level, "latencyMs": round((time.perf_counter() - started) * 1000)})
            self.log("vision_completed", provider="deepseek", model=payload["model"],
                             imageCount=image_metadata["imageCount"], elapsedMs=result["latencyMs"])
            return result
        except Exception as error:
            code = int(error.code) if isinstance(error, urllib.error.HTTPError) else 0
            message = {
                400: "DeepSeek 拒绝了本次图片请求，请检查图片或模型支持情况。",
                401: "DeepSeek 视觉 API Key 无效，请检查设置。",
                402: "DeepSeek API 余额不足，本次未能识图。",
                403: "当前 DeepSeek 账号无权调用视觉模型。",
                404: "DeepSeek 视觉模型当前不可用。",
                429: "DeepSeek 视觉请求过于频繁，请稍后再试。",
            }.get(code)
            if message is None:
                message = (
                    "DeepSeek 视觉请求超时，请稍后再试。" if isinstance(error, TimeoutError) else
                    "无法连接 DeepSeek 视觉接口，请检查网络。" if isinstance(error, (urllib.error.URLError, ConnectionError)) else
                    "图片不存在或无法读取。" if isinstance(error, (OSError, FileNotFoundError)) and not code else
                    "DeepSeek 未返回可用的识图结果，本次没有识别成功。"
                )
            self.log("vision_failed", provider="deepseek", model=vision_model(settings),
                             errorType=type(error).__name__, httpStatus=code)
            return {"ok": False, "available": False, "provider": "deepseek", "model": vision_model(settings),
                    "error": message, "errorCode": f"vision_http_{code}" if code else "vision_failed"}
