"""本地语音 HTTP 传输错误；保留本地服务诊断，不混同模型上游错误。"""
from __future__ import annotations
import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable

class LocalTtsSynthesisError(RuntimeError):
    """The loopback speech service rejected or failed a synthesis request."""

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int = 500,
        deterministic: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message or "本地语音服务合成失败")[:1000]
        self.upstream_status = int(upstream_status)
        self.deterministic = bool(deterministic)
        self.diagnostics = dict(diagnostics or {})
        self.code = (
            "tts_incomplete_audio"
            if self.deterministic
            else "tts_service_error"
        )


def open_local_tts_request(request: urllib.request.Request, timeout: int = 90, *, open_direct: Callable[..., Any]):
    """Preserve the loopback TTS error instead of relabeling it as DeepSeek."""

    try:
        return open_direct(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raw = b""
        try:
            raw = error.read(16_384)
        except Exception:
            pass
        message = ""
        diagnostics: dict[str, Any] = {}
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(payload, dict):
                    message = str(payload.get("error") or "")
                    diagnostics = (
                        dict(payload.get("diagnostics"))
                        if isinstance(payload.get("diagnostics"), dict)
                        else {}
                    )
            except json.JSONDecodeError:
                message = raw.decode("utf-8", errors="replace")
        message = re.sub(r"\s+", " ", message).strip()[:1000]
        if not message:
            message = f"本地语音服务返回 HTTP {int(error.code)}"
        deterministic = bool(re.search(
            r"produced no audio after retries|(?:segment|inference chunk) \d+/\d+ produced no audio|"
            r"incomplete audio|incomplete speech|残缺音频|语音不完整",
            message,
            flags=re.I,
        ))
        raise LocalTtsSynthesisError(
            message,
            upstream_status=int(error.code),
            deterministic=deterministic,
            diagnostics=diagnostics,
        ) from error

