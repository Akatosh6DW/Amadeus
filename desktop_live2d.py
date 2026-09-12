#!/usr/bin/env python3
"""Desktop Live2D AMADEUS window backed by the local API server."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import webview

from desktop_port_selection import choose_available_backend_port

INTERACTIVE_TEST_MODE = "--test-mode" in sys.argv
if INTERACTIVE_TEST_MODE and (
    str(os.environ.get("AMADEUS_RUNTIME_MODE") or "").strip().lower() != "test"
    or not str(os.environ.get("AMADEUS_DATA_DIR") or "").strip()
):
    raise SystemExit("交互测试模式必须通过 run_amadeus.ps1 -TestMode 启动。")

if "--release" in sys.argv:
    os.environ.setdefault("AMADEUS_RELEASE_MODE", "1")

from server import (
    APP_BUILD,
    PORT_REGISTRY,
    RuntimeProcessLock,
    AmadeusHandler,
    AmadeusHTTPServer,
    DATA_DIR,
    RELEASE_MODE,
    RUNTIME_MODE,
    active_runtime_client_count,
    ensure_data_dir,
    ensure_memory_file,
    ensure_vision_service,
    load_settings,
    refresh_runtime_client_leases,
    resolve_generated_file,
    should_start_vision_service,
    stop_gpt_sovits_service,
    stop_vision_service,
    write_server_log,
)
from tools.safe_web import SafeWebError, validate_public_url


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
HOST = "127.0.0.1"
DEFAULT_PORT = PORT_REGISTRY["desktopBackend"]
BACKEND_FALLBACK_PORTS = tuple(range(DEFAULT_PORT + 1, DEFAULT_PORT + 22))
TEST_BACKEND_PORT = DEFAULT_PORT + 10
BACKEND_START_TIMEOUT_SECONDS = 15.0
BACKEND_HEARTBEAT_SECONDS = 2.5
BACKEND_IDLE_SECONDS = 24.0
BACKEND_STARTUP_GRACE_SECONDS = 35.0
BACKEND_LOG_PATH = DATA_DIR / "logs" / "desktop_backend.log"
_backend_start_lock = threading.Lock()


def _native_window_handle(window) -> int:
    """Return the Win32 handle without asking WinForms to cross UI threads."""

    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    def top_level(handle: int) -> int:
        if not handle or not user32.IsWindow(handle):
            return 0
        root = int(user32.GetAncestor(handle, 2) or 0)  # GA_ROOT
        return root if root and user32.IsWindow(root) else int(handle)

    try:
        gui = getattr(window, "gui", None)
        native = gui.BrowserView.instances.get(window.uid) if gui and hasattr(gui, "BrowserView") else None
        handle = int(native.Handle.ToInt64()) if native is not None else 0
        handle = top_level(handle)
        if handle:
            return handle
    except Exception:
        pass

    # Some pywebview/WinForms versions do not expose BrowserView on the module
    # attached to Window. Enumerate only this process, then match the exact title.
    candidates: list[tuple[int, str]] = []
    process_id = os.getpid()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd, _lparam):
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value != process_id or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        candidates.append((top_level(int(hwnd)), buffer.value))
        return True

    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(collect, 0)
    title = str(getattr(window, "title", "") or "")
    exact = next((handle for handle, value in candidates if value == title), 0)
    if exact:
        return exact
    return next((handle for handle, value in candidates if "AMADEUS Live2D" in value), 0)


def _window_logical_position(window) -> tuple[int, int]:
    """Read a Win32 window origin in the logical pixels expected by pywebview."""

    if sys.platform != "win32":
        return int(getattr(window, "x", 0) or 0), int(getattr(window, "y", 0) or 0)
    import ctypes
    from ctypes import wintypes

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long), ("top", ctypes.c_long),
            ("right", ctypes.c_long), ("bottom", ctypes.c_long),
        ]

    handle = _native_window_handle(window)
    if not handle:
        raise RuntimeError("native_window_unavailable")
    user32 = ctypes.windll.user32
    # Use a generic output pointer because ctypes stores function signatures on
    # the shared DLL object; a private Structure type would reject other valid
    # RECT-compatible callers in the same process.
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowRect.restype = wintypes.BOOL
    rect = Rect()
    if not user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect)):
        raise ctypes.WinError()
    try:
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        dpi = int(user32.GetDpiForWindow(wintypes.HWND(handle)) or 96)
    except (AttributeError, OSError):
        dpi = 96
    scale = max(0.5, dpi / 96.0)
    return round(rect.left / scale), round(rect.top / scale)


def _move_native_window(window, x: int, y: int) -> int:
    """Move the real top-level HWND without crossing WinForms UI-thread properties."""

    if sys.platform != "win32":
        window.move(int(x), int(y))
        return 0
    import ctypes
    from ctypes import wintypes

    handle = _native_window_handle(window)
    if not handle:
        raise RuntimeError("native_window_unavailable")
    user32 = ctypes.windll.user32
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    dpi = int(user32.GetDpiForWindow(wintypes.HWND(handle)) or 96)
    scale = max(0.5, dpi / 96.0)
    flags = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE
    if not user32.SetWindowPos(
        wintypes.HWND(handle), None, round(int(x) * scale), round(int(y) * scale), 0, 0, flags
    ):
        raise ctypes.WinError()
    return handle


def _set_window_topmost(window, enabled: bool) -> int:
    """Toggle topmost through Win32; WinForms.TopMost can deadlock from a JS API thread."""

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        handle = _native_window_handle(window)
        if not handle:
            raise RuntimeError("native_window_unavailable")
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        hwnd_insert_after = wintypes.HWND(-1 if enabled else -2)  # HWND_TOPMOST / HWND_NOTOPMOST
        flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
        if not user32.SetWindowPos(handle, hwnd_insert_after, 0, 0, 0, 0, flags):
            raise ctypes.WinError()
        actual = bool(user32.GetWindowLongW(handle, -20) & 0x00000008)  # GWL_EXSTYLE / WS_EX_TOPMOST
        if actual != bool(enabled):
            raise RuntimeError(f"topmost_state_mismatch: requested={bool(enabled)} actual={actual}")
        # Keep pywebview's cached value consistent without calling its unsafe setter.
        setattr(window, "_Window__on_top", bool(enabled))
        return handle
    window.on_top = bool(enabled)
    return 0


def _set_window_topmost_with_retry(window, enabled: bool, attempts: int = 12) -> int:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return _set_window_topmost(window, enabled)
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.06 + attempt * 0.015)
    raise last_error or RuntimeError("topmost_apply_failed")


def open_loopback_request(request: str | urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if isinstance(request, urllib.request.Request) and request.get_method() == "POST":
        from agent.local_api_client import local_api_headers
        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.path.startswith("/api/"):
            for name, value in local_api_headers(request.full_url, timeout=timeout, opener=opener.open).items():
                request.add_header(name, value)
    return opener.open(request, timeout=timeout)


class UserLive2DApi:
    def __init__(self, port: int) -> None:
        self.port = port

    def closeApp(self) -> None:
        if webview.windows:
            webview.windows[0].destroy()

    def ensureBackend(self) -> dict:
        try:
            ensure_backend_service(self.port)
            return {"ok": True, "port": self.port, "endpoint": f"http://{HOST}:{self.port}"}
        except Exception as error:
            write_server_log(
                "desktop_backend_recovery_failed",
                port=self.port,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
            return {"ok": False, "port": self.port, "error": str(error)}

    def applyWindowSettings(self, settings: dict) -> dict:
        if not webview.windows:
            return {"ok": False}
        window = webview.windows[0]
        width = int(settings.get("windowWidth") or window.width)
        height = int(settings.get("windowHeight") or window.height)
        window.resize(width, height)
        try:
            enabled = bool(settings.get("alwaysOnTop", True))
            handle = _set_window_topmost_with_retry(window, enabled)
        except Exception as error:
            write_server_log("desktop_topmost_apply_failed", error=str(error)[:500])
            return {"ok": False, "width": width, "height": height, "error": str(error)}
        return {
            "ok": True, "width": width, "height": height, "onTop": enabled,
            "nativeHandle": f"0x{handle:x}" if handle else "",
        }

    def setAlwaysOnTop(self, enabled: bool) -> dict:
        if not webview.windows:
            return {"ok": False, "error": "window_unavailable"}
        window = webview.windows[0]
        try:
            handle = _set_window_topmost_with_retry(window, bool(enabled))
            return {"ok": True, "onTop": bool(enabled), "nativeHandle": f"0x{handle:x}" if handle else ""}
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def beginWindowDrag(self) -> dict:
        """Return a stable origin for the explicit HTML drag bridge."""

        if not webview.windows:
            return {"ok": False, "error": "window_unavailable"}
        window = webview.windows[0]
        try:
            x, y = _window_logical_position(window)
            handle = _native_window_handle(window) if sys.platform == "win32" else 0
            return {
                "ok": True, "x": x, "y": y,
                "nativeHandle": f"0x{handle:x}" if handle else "",
            }
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def moveWindow(self, x: int, y: int) -> dict:
        """Fallback used by the HTML drag bridge on non-Windows backends."""

        if not webview.windows:
            return {"ok": False, "error": "window_unavailable"}
        try:
            handle = _move_native_window(webview.windows[0], int(x), int(y))
            return {
                "ok": True, "x": int(x), "y": int(y),
                "nativeHandle": f"0x{handle:x}" if handle else "",
            }
        except Exception as error:
            return {"ok": False, "error": str(error)}

    def openScreenCaptureDirectory(self) -> dict:
        target = (DATA_DIR / "runtime" / "screen-captures").resolve()
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
        if not webview.windows:
            return {"ok": False}
        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("Image Files (*.png;*.jpg;*.jpeg;*.webp)", "All files (*.*)"),
        )
        if not result:
            return {"ok": False}
        return {"ok": True, "path": result[0]}

    def openExternalUrl(self, url: str) -> dict:
        try:
            parsed, _ = validate_public_url(str(url or "").strip())
        except SafeWebError:
            return {"ok": False, "error": "unsupported_url"}
        payload = json.dumps({
            "url": urllib.parse.urlunsplit(parsed),
            "actionId": f"desktop_{uuid.uuid4().hex}",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"http://{HOST}:{self.port}/api/actions/open-browser",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with open_loopback_request(request, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
            return result if isinstance(result, dict) else {"ok": False, "error": "invalid_gateway_response"}
        except (OSError, ValueError, urllib.error.URLError) as error:
            return {"ok": False, "error": str(error)[:300]}

    def saveGeneratedFile(self, file_id: str, suggested_name: str = "output.txt") -> dict:
        if not webview.windows:
            return {"ok": False, "error": "window_unavailable"}
        resolved = resolve_generated_file(file_id)
        if not resolved:
            return {"ok": False, "error": "file_not_found"}
        source, metadata = resolved
        safe_name = Path(str(suggested_name or metadata.get("name") or "output.txt")).name
        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
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

    def openLive2DExpressionCaptureDirectory(self) -> dict:
        target = (DATA_DIR / "runtime" / "live2d-expression-captures").resolve()
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
        url = f"http://{HOST}:{self.port}/live2d/?dev=1"
        webview.create_window(
            "AMADEUS Developer",
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
        if not webview.windows:
            return {"ok": False}
        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename="memory.json",
            file_types=("JSON Files (*.json)", "All files (*.*)"),
        )
        if not result:
            return {"ok": False}
        return {"ok": True, "path": result[0]}


def start_server(port: int) -> AmadeusHTTPServer:
    """Start an in-process server for tests and explicit standalone tools."""
    server = AmadeusHTTPServer((HOST, port), AmadeusHandler)
    thread = threading.Thread(target=server.serve_forever, name="amadeus-live2d-server", daemon=True)
    thread.start()
    return server


def is_amadeus_server(port: int) -> bool:
    try:
        with open_loopback_request(f"http://{HOST}:{port}/api/health", timeout=0.8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            payload.get("service") == "amadeus-local"
            and payload.get("build") == APP_BUILD
            and bool(payload.get("releaseMode")) == RELEASE_MODE
            and str(payload.get("runtimeMode") or "") == RUNTIME_MODE
        )
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return False


def port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection((HOST, int(port)), timeout=0.35):
            return True
    except OSError:
        return False


def select_backend_port(preferred: int = DEFAULT_PORT) -> int:
    fallback_ports = (
        tuple(range(preferred + 1, preferred + 22))
        if preferred != DEFAULT_PORT else BACKEND_FALLBACK_PORTS
    )
    port = choose_available_backend_port(
        preferred,
        fallback_ports,
        is_listening=port_is_listening,
        is_compatible=is_amadeus_server,
    )
    if port != preferred:
        write_server_log(
            "desktop_backend_port_fallback",
            preferredPort=preferred,
            selectedPort=port,
            reason="preferred_port_incompatible_or_occupied",
        )
    return port


def backend_command(port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--backend-service", "--backend-port", str(port)]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), "--backend-service", "--backend-port", str(port)]
    if INTERACTIVE_TEST_MODE:
        command.append("--test-mode")
    return command


def spawn_backend_service(port: int) -> subprocess.Popen[bytes]:
    BACKEND_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if RELEASE_MODE:
        env["AMADEUS_RELEASE_MODE"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    log_file = BACKEND_LOG_PATH.open("ab")
    try:
        log_file.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] start backend {APP_BUILD} port={port}\n".encode("utf-8"))
        log_file.flush()
        process = subprocess.Popen(
            backend_command(port),
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        log_file.close()
    return process


def backend_log_tail(max_chars: int = 1200) -> str:
    try:
        text = BACKEND_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max(200, int(max_chars)):].strip()


def ensure_backend_service(port: int = DEFAULT_PORT) -> None:
    """Ensure the fixed-port backend exists independently of every UI window."""
    with _backend_start_lock:
        if is_amadeus_server(port):
            return
        if port_is_listening(port):
            # A terminated backend can keep the listen socket visible briefly.
            # Give that socket a bounded drain window before declaring a real conflict.
            drain_deadline = time.monotonic() + 2.0
            while time.monotonic() < drain_deadline:
                if is_amadeus_server(port):
                    return
                if not port_is_listening(port):
                    break
                time.sleep(0.12)
            if port_is_listening(port):
                restart_target = "重新打开 AMADEUS.exe" if RELEASE_MODE else "重新运行 run_amadeus.ps1"
                raise RuntimeError(
                    f"桌面后端端口 {port} 已被其他程序或旧版 AMADEUS 占用。"
                    f"请关闭占用进程后{restart_target}。"
                )
        process = spawn_backend_service(port)
        deadline = time.monotonic() + BACKEND_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if is_amadeus_server(port):
                return
            if process.poll() is not None:
                # Another simultaneously launched window may have won the bind race.
                if is_amadeus_server(port):
                    return
                detail = backend_log_tail()
                raise RuntimeError(
                    f"AMADEUS 桌面后端启动失败（退出码 {process.returncode}）。"
                    + (f"\n{detail}" if detail else f"详情见 {BACKEND_LOG_PATH}")
                )
            time.sleep(0.18)
        raise RuntimeError(f"AMADEUS 桌面后端在 {BACKEND_START_TIMEOUT_SECONDS:.0f} 秒内未就绪。详情见 {BACKEND_LOG_PATH}")


def post_runtime_lease(port: int, path: str, client_id: str, mode: str, timeout: float = 1.5) -> bool:
    request = urllib.request.Request(
        f"http://{HOST}:{port}{path}",
        data=json.dumps({"clientId": client_id, "mode": mode}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with open_loopback_request(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return False


class BackendLease:
    def __init__(self, port: int, mode: str) -> None:
        self.port = port
        self.mode = mode
        self.client_id = f"desktop-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        for attempt in range(1, 4):
            ensure_backend_service(self.port)
            if post_runtime_lease(self.port, "/api/runtime/client/register", self.client_id, self.mode):
                self.thread = threading.Thread(target=self._heartbeat_loop, name="amadeus-backend-heartbeat", daemon=True)
                self.thread.start()
                return
            time.sleep(0.3 * attempt)
        raise RuntimeError("桌面后端已启动，但客户端租约连续三次注册失败。")

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(BACKEND_HEARTBEAT_SECONDS):
            if post_runtime_lease(self.port, "/api/runtime/client/heartbeat", self.client_id, self.mode):
                continue
            write_server_log("desktop_backend_heartbeat_lost", port=self.port, clientId=self.client_id)
            try:
                ensure_backend_service(self.port)
                post_runtime_lease(self.port, "/api/runtime/client/register", self.client_id, self.mode)
            except Exception as error:
                write_server_log(
                    "desktop_backend_restart_failed",
                    port=self.port,
                    clientId=self.client_id,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                )

    def close(self) -> None:
        self.stop_event.set()
        post_runtime_lease(self.port, "/api/runtime/client/unregister", self.client_id, self.mode, timeout=0.8)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)


def backend_service_main(port: int) -> None:
    ensure_data_dir()
    ensure_memory_file()
    runtime_lock = RuntimeProcessLock()
    try:
        runtime_lock.acquire()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    server: AmadeusHTTPServer | None = None
    try:
        server = AmadeusHTTPServer((HOST, int(port)), AmadeusHandler)
    except Exception:
        runtime_lock.release()
        raise
    started = time.monotonic()
    last_client_seen = started
    last_monitor_tick = started
    stop_monitor = threading.Event()

    def idle_monitor() -> None:
        nonlocal last_client_seen, last_monitor_tick
        while not stop_monitor.wait(2.0):
            now = time.monotonic()
            if now - last_monitor_tick > BACKEND_HEARTBEAT_SECONDS * 3:
                # Windows timers jump across sleep. Keep leases long enough for
                # the UI heartbeat to resume instead of shutting down on wake.
                refresh_runtime_client_leases()
                last_client_seen = now
            last_monitor_tick = now
            if active_runtime_client_count() > 0:
                last_client_seen = now
                continue
            activity = server.activity_snapshot()
            if int(activity.get("activeRequests") or 0) > 0:
                last_client_seen = now
                continue
            last_client_seen = max(last_client_seen, float(activity.get("lastActivity") or started))
            idle = now - last_client_seen
            grace = BACKEND_STARTUP_GRACE_SECONDS if last_client_seen == started else BACKEND_IDLE_SECONDS
            if idle >= grace:
                write_server_log(
                    "desktop_backend_idle_shutdown",
                    port=port,
                    idleSeconds=round(idle, 1),
                    activeRequests=0,
                )
                server.shutdown()
                return

    monitor = threading.Thread(target=idle_monitor, name="amadeus-backend-idle-monitor", daemon=True)
    monitor.start()
    settings = load_settings()
    if os.environ.get("AMADEUS_SKIP_AUX_SERVICES") != "1" and should_start_vision_service(settings):
        threading.Thread(
            target=ensure_vision_service,
            args=(settings, 180),
            daemon=True,
            name="amadeus-vision-starter",
        ).start()
    write_server_log("desktop_backend_started", port=port, pid=os.getpid(), build=APP_BUILD)
    print(f"AMADEUS desktop backend: http://{HOST}:{port} ({APP_BUILD})", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop_monitor.set()
        stop_gpt_sovits_service()
        stop_vision_service()
        if server is not None:
            server.server_close()
        runtime_lock.release()
        write_server_log("desktop_backend_stopped", port=port, pid=os.getpid())


def backend_port_from_args() -> int:
    try:
        index = sys.argv.index("--backend-port")
        return int(sys.argv[index + 1])
    except (ValueError, IndexError, TypeError):
        return DEFAULT_PORT


def show_startup_error(message: str) -> None:
    text = str(message or "AMADEUS 启动失败")
    write_server_log("desktop_startup_failed", error=text[:800])
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "AMADEUS 启动失败", 0x10)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def self_test() -> None:
    required = [
        ROOT / "live2d" / "index.html",
        ROOT / "live2d" / "live2d_app.js",
        ROOT / "assets" / "live2d" / "models.json",
        ROOT / "assets" / "live2d" / "AiPoetKurisu" / "kurisu.model3.json",
        ROOT / "assets" / "live2d" / "AiPoetKurisuDaily" / "kurisu-daily.model3.json",
        ROOT / "assets" / "vendor" / "live2d" / "pixi.min.js",
        ROOT / "assets" / "vendor" / "live2d" / "pixi-live2d-display-cubism4.min.js",
        ROOT / "assets" / "vendor" / "live2d" / "live2dcubismcore.min.js",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("missing required files:\n" + "\n".join(missing))
    print("desktop live2d self-test ok")


def main() -> None:
    if "--backend-service" in sys.argv:
        backend_service_main(backend_port_from_args())
        return
    if "--self-test" in sys.argv:
        exit_code = 0
        diagnostic = "desktop live2d self-test ok"
        try:
            self_test()
        except BaseException as error:
            exit_code = 1
            diagnostic = f"{type(error).__name__}: {error}"
        log_path = str(os.environ.get("AMADEUS_SELF_TEST_LOG") or "").strip()
        if log_path:
            try:
                Path(log_path).write_text(diagnostic + "\n", encoding="utf-8")
            except OSError:
                pass
        # A windowed PyInstaller executable can display an invisible traceback
        # dialog for an unhandled self-test error. It can also retain
        # pythonnet/pywebview threads after a successful import. Convert both
        # paths into an explicit diagnostic plus process exit code.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, OSError):
                pass
        os._exit(exit_code)

    release_mode = RELEASE_MODE
    test_mode = INTERACTIVE_TEST_MODE and RUNTIME_MODE == "test" and not release_mode
    dev_mode = "--dev" in sys.argv and not release_mode and not test_mode
    try:
        port = select_backend_port(TEST_BACKEND_PORT if test_mode else DEFAULT_PORT)
    except Exception as error:
        show_startup_error(str(error))
        return
    lease = BackendLease(port, "developer" if dev_mode else "test" if test_mode else "desktop")
    try:
        lease.start()
    except Exception as error:
        show_startup_error(str(error))
        return
    url = f"http://{HOST}:{port}/live2d/{'?dev=1' if dev_mode else '?test=1' if test_mode else ''}"
    settings = load_settings()

    window = webview.create_window(
        "AMADEUS Live2D DEV" if dev_mode else "AMADEUS Live2D TEST" if test_mode else "AMADEUS Live2D",
        url=url,
        js_api=UserLive2DApi(port) if release_mode else Live2DApi(port),
        width=max(760, int(settings.get("windowWidth") or 520)) if dev_mode else int(settings.get("windowWidth") or 520),
        height=max(860, int(settings.get("windowHeight") or 760)) if dev_mode else int(settings.get("windowHeight") or 760),
        min_size=(360, 520),
        frameless=True,
        easy_drag=False,
        on_top=bool(settings.get("alwaysOnTop", True)),
        transparent=False,
        background_color="#091011",
        text_select=True,
        zoomable=False,
    )

    def apply_initial_topmost() -> None:
        try:
            handle = _set_window_topmost_with_retry(window, bool(settings.get("alwaysOnTop", True)))
            write_server_log(
                "desktop_topmost_initialized",
                enabled=bool(settings.get("alwaysOnTop", True)),
                nativeHandle=f"0x{handle:x}" if handle else "",
            )
        except Exception as error:
            write_server_log(
                "desktop_topmost_initialization_failed",
                errorType=type(error).__name__,
                error=str(error)[:500],
            )

    window.events.loaded += apply_initial_topmost

    try:
        webview.start(debug=False, private_mode=False, storage_path=str(DATA_DIR / "webview"))
    finally:
        lease.close()
        _ = window


if __name__ == "__main__":
    main()
