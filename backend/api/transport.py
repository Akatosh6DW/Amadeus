"""本地 HTTP 的认证、响应编码和流式写入。

不读取应用配置、不执行业务、不持有请求状态。入口只提供日志接线；
路由继续在认证成功后才能访问存储。断开连接的处理保留 JSON 与 NDJSON 的区别。
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
import mimetypes
from pathlib import Path
import sys
from typing import Any, Callable
import urllib.parse

from agent.local_api_security import request_rejection
from backend.attachments.storage import safe_attachment_name

def json_bytes(payload: dict[str, Any], status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(payload, ensure_ascii=False).encode("utf-8")


def content_type_for(path: Path) -> str:
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    textual = content_type.startswith("text/") or content_type in {
        "application/javascript",
        "application/json",
        "application/xml",
        "image/svg+xml",
    }
    if textual and "charset=" not in content_type:
        return f"{content_type}; charset=utf-8"
    return content_type


def authorize_local_request(self, method: str, *, write_log: Callable[..., None]) -> bool:
    rejection = request_rejection(self.headers, server_address=self.server.server_address,
        client_address=self.client_address, method=method, path=self.path,
        session_token=self.server.local_session_token)
    if rejection is None:
        return True
    status, code = rejection
    self.close_connection = True
    # Drain only small, already-sent bodies without parsing or dispatching.
    # Closing a Windows socket with unread POST data can reset the TCP
    # connection before the browser receives the rejection response.
    if method == "POST":
        try:
            remaining = int(self.headers.get("Content-Length", "0"))
            if 0 < remaining <= 65536 and not self.headers.get("Transfer-Encoding"):
                self.connection.settimeout(0.25)
                self.rfile.read(remaining)
        except (ValueError, OSError):
            pass
    # Do not log credentials, request bodies, or attacker-controlled URLs.
    write_log("local_api_request_rejected", method=method, status=status, code=code)
    self.send_json({"ok": False, "error": code, "code": code}, status)
    return False


def send_json(self, payload: dict[str, Any], status: int = 200, *, write_log: Callable[..., None]) -> bool:
    status_code, data = json_bytes(payload, status)
    try:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return True
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as error:
        write_log(
            "client_disconnected_before_response",
            path=self.path,
            status=status_code,
            errorType=type(error).__name__,
            error=str(error)[:300],
        )
        return False


class LocalHttpHandler(BaseHTTPRequestHandler):
    """共享响应协议；业务入口实现认证和 JSON 日志的两条接线。"""

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        if sys.stdout is not None:
            sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def begin_ndjson(self, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def send_ndjson_event(self, event: dict[str, Any]) -> bool:
        try:
            data = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return False

    def send_binary(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_download(self, path: Path, metadata: dict[str, Any]) -> None:
        data = path.read_bytes()
        name = safe_attachment_name(metadata.get("name"), "output.txt")
        encoded_name = urllib.parse.quote(name, safe="")
        self.send_response(200)
        self.send_header("Content-Type", str(metadata.get("mimeType") or content_type_for(path)))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)
