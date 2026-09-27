"""Live2D 桌面窗口的 pywebview 桥接方法。

桥接层只负责页面可调用的行为；服务状态和原生窗口实现由入口装配，
通过有限的显式能力传入，使本模块可以在没有真实窗口或网络时独立测试。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import json
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from tools.safe_web import SafeWebError, validate_public_url


@dataclass(frozen=True)
class BridgeWindowOps:
    """桥接层需要的有限原生窗口能力，不反向依赖入口或全局状态。"""

    native_window_handle: Callable[[Any], int]
    window_logical_position: Callable[[Any], tuple[int, int]]
    move_native_window: Callable[[Any, int, int], int]
    resize_native_window: Callable[[Any, int, int], None]
    set_window_topmost_with_retry: Callable[[Any, bool], int]
    window_resize_factory: Callable[[], Any]


class UserLive2DApi:
    """向 Live2D 页面提供用户侧桌面操作。"""

    def __init__(
        self,
        port: int,
        *,
        host: str,
        data_dir: Path,
        webview_module: Any,
        release_mode: bool,
        normalize_window_size: Callable[[int, int, Any], tuple[int, int]],
        window_ops: BridgeWindowOps,
        ensure_backend: Callable[[int], None],
        write_log: Callable[..., Any],
        resolve_generated_file: Callable[[str], Any],
        open_loopback_request: Callable[[Any, float], Any],
    ) -> None:
        self.port = port
        self._host = host
        self._data_dir = data_dir
        self._webview = webview_module
        self._release_mode = release_mode
        self._normalize_window_size = normalize_window_size
        self._window_ops = window_ops
        self._ensure_backend = ensure_backend
        self._write_log = write_log
        self._resolve_generated_file = resolve_generated_file
        self._open_loopback_request = open_loopback_request
        self.window_resize = window_ops.window_resize_factory()

    def closeApp(self) -> None:
        if self._webview.windows:
            self._webview.windows[0].destroy()

    def ensureBackend(self) -> dict:
        try:
            self._ensure_backend(self.port)
            return {
                "ok": True,
                "port": self.port,
                "endpoint": f"http://{self._host}:{self.port}",
            }
        except Exception as error:
            self._write_log(
                "desktop_backend_recovery_failed",
                port=self.port,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
            return {"ok": False, "port": self.port, "error": str(error)}

    def applyWindowSettings(self, settings: dict) -> dict:
        if not self._webview.windows:
            return {"ok": False}
        window = self._webview.windows[0]
        width, height = self._normalize_window_size(
            int(settings.get("windowWidth") or window.width),
            int(settings.get("windowHeight") or window.height),
            settings.get("layoutMode"),
        )
        self._window_ops.resize_native_window(window, width, height)
        try:
            enabled = bool(settings.get("alwaysOnTop", True))
            handle = self._window_ops.set_window_topmost_with_retry(window, enabled)
        except Exception as error:
            self._write_log("desktop_topmost_apply_failed", error=str(error)[:500])
            return {"ok": False, "width": width, "height": height, "error": str(error)}
        return {
            "ok": True,
            "width": width,
            "height": height,
            "onTop": enabled,
            "nativeHandle": f"0x{handle:x}" if handle else "",
        }

    def setAlwaysOnTop(self, enabled: bool) -> dict:
        if not self._webview.windows:
            return {"ok": False, "error": "window_unavailable"}
        window = self._webview.windows[0]
        try:
            handle = self._window_ops.set_window_topmost_with_retry(window, bool(enabled))
            return {
                "ok": True,
                "onTop": bool(enabled),
                "nativeHandle": f"0x{handle:x}" if handle else "",
            }
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def beginWindowResize(self, edge: str, mode: str, screenX: float, screenY: float) -> dict:
        if sys.platform != "win32" or not self._webview.windows:
            return {"ok": False, "error": "window_resize_unavailable"}
        try:
            handle = self._window_ops.native_window_handle(self._webview.windows[0])
            if not handle:
                raise RuntimeError("native_window_unavailable")
            return self.window_resize.begin(handle, edge, mode, screenX, screenY)
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def updateWindowResize(self, token: str, screenX: float, screenY: float, finish: bool = False) -> dict:
        try:
            return self.window_resize.update(token, screenX, screenY, finish)
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def beginWindowDrag(self) -> dict:
        """为显式 HTML 拖动桥提供稳定的窗口起点。"""

        if not self._webview.windows:
            return {"ok": False, "error": "window_unavailable"}
        window = self._webview.windows[0]
        try:
            x, y = self._window_ops.window_logical_position(window)
            handle = (
                self._window_ops.native_window_handle(window)
                if sys.platform == "win32"
                else 0
            )
            return {
                "ok": True,
                "x": x,
                "y": y,
                "nativeHandle": f"0x{handle:x}" if handle else "",
            }
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def moveWindow(self, x: int, y: int) -> dict:
        """非 Windows 后端使用的 HTML 拖动回退路径。"""

        if not self._webview.windows:
            return {"ok": False, "error": "window_unavailable"}
        try:
            handle = self._window_ops.move_native_window(
                self._webview.windows[0], int(x), int(y)
            )
            return {
                "ok": True,
                "x": int(x),
                "y": int(y),
                "nativeHandle": f"0x{handle:x}" if handle else "",
            }
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def openScreenCaptureDirectory(self) -> dict:
        target = (self._data_dir / "runtime" / "screen-captures").resolve()
        try:
            target.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(target))
            else:
                subprocess.Popen(["xdg-open", str(target)])
            return {"ok": True, "path": str(target)}
        except Exception as error:
            return {"ok": False, "path": str(target), "error": str(error)}

    def chooseBackgroundFile(self) -> dict:
        if not self._webview.windows:
            return {"ok": False}
        result = self._webview.windows[0].create_file_dialog(
            self._webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=(
                "Image Files (*.png;*.jpg;*.jpeg;*.webp)",
                "All files (*.*)",
            ),
        )
        if not result:
            return {"ok": False}
        try:
            from PIL import Image, ImageOps

            path = Path(result[0])
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                return {"ok": False, "error": "请选择 PNG、JPG 或 WebP 图片"}
            with Image.open(path) as original:
                oriented = ImageOps.exif_transpose(original)
                transparent = "A" in oriented.getbands() or "transparency" in oriented.info
                preview = oriented.convert("RGBA" if transparent else "RGB")
                preview.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                buffer = BytesIO()
                preview.save(buffer, format="PNG" if transparent else "JPEG", quality=88)
            mime = "image/png" if transparent else "image/jpeg"
            return {
                "ok": True,
                "path": str(path),
                "previewUrl": (
                    f"data:{mime};base64,"
                    + base64.b64encode(buffer.getvalue()).decode("ascii")
                ),
            }
        except (OSError, ValueError) as error:
            return {"ok": False, "error": f"图片无法读取：{error}"}

    def openExternalUrl(self, url: str) -> dict:
        try:
            parsed, _ = validate_public_url(str(url or "").strip())
        except SafeWebError:
            return {"ok": False, "error": "unsupported_url"}
        payload = json.dumps(
            {
                "url": urllib.parse.urlunsplit(parsed),
                "actionId": f"desktop_{uuid.uuid4().hex}",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://{self._host}:{self.port}/api/actions/open-browser",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with self._open_loopback_request(request, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
            return (
                result
                if isinstance(result, dict)
                else {"ok": False, "error": "invalid_gateway_response"}
            )
        except (OSError, ValueError, urllib.error.URLError) as error:
            return {"ok": False, "error": str(error)[:300]}

    def saveGeneratedFile(
        self, file_id: str, suggested_name: str = "output.txt"
    ) -> dict:
        if not self._webview.windows:
            return {"ok": False, "error": "window_unavailable"}
        resolved = self._resolve_generated_file(file_id)
        if not resolved:
            return {"ok": False, "error": "file_not_found"}
        source, metadata = resolved
        safe_name = Path(
            str(suggested_name or metadata.get("name") or "output.txt")
        ).name
        result = self._webview.windows[0].create_file_dialog(
            self._webview.SAVE_DIALOG,
            save_filename=safe_name,
            file_types=("All files (*.*)",),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        target = Path(result[0]).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return {"ok": True, "path": str(target), "size": target.stat().st_size}


class Live2DApi(UserLive2DApi):
    """用户侧桥接的开发模式附加操作。"""

    def openLive2DExpressionCaptureDirectory(self) -> dict:
        if self._release_mode:
            return {"ok": False, "error": "not_available_in_release"}
        target = (
            self._data_dir / "runtime" / "live2d-expression-captures"
        ).resolve()
        try:
            target.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(target))
            else:
                subprocess.Popen(["xdg-open", str(target)])
            return {"ok": True, "path": str(target)}
        except Exception as error:
            return {"ok": False, "path": str(target), "error": str(error)}

    def openDeveloperWindow(self) -> dict:
        if self._release_mode:
            return {"ok": False, "error": "not_available_in_release"}
        url = f"http://{self._host}:{self.port}/live2d/?dev=1"
        self._webview.create_window(
            "Amadeus Developer",
            url=url,
            js_api=self,
            width=1040,
            height=900,
            min_size=(760, 640),
            frameless=False,
            easy_drag=False,
            on_top=False,
            transparent=False,
            background_color="#050808",
            text_select=True,
            zoomable=True,
        )
        return {"ok": True, "url": url}

    def chooseMemoryFile(self) -> dict:
        if self._release_mode:
            return {"ok": False, "error": "not_available_in_release"}
        if not self._webview.windows:
            return {"ok": False}
        result = self._webview.windows[0].create_file_dialog(
            self._webview.SAVE_DIALOG,
            save_filename="memory.json",
            file_types=("JSON Files (*.json)", "All files (*.*)"),
        )
        if not result:
            return {"ok": False}
        return {"ok": True, "path": result[0]}


__all__ = ["BridgeWindowOps", "Live2DApi", "UserLive2DApi"]
