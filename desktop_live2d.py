#!/usr/bin/env python3
"""AMADEUS 桌面入口：解析运行模式，装配窗口、桥接和本地后端生命周期。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview

from desktop.window import (
    native_window_handle as _native_window_handle,
    window_logical_position as _window_logical_position,
    move_native_window as _move_native_window,
    resize_native_window as _resize_native_window,
    set_window_topmost as _set_window_topmost,
    set_window_topmost_with_retry as _set_window_topmost_with_retry,
)

from desktop.backend import (
    BACKEND_START_TIMEOUT_SECONDS, BACKEND_HEARTBEAT_SECONDS,
    BACKEND_IDLE_SECONDS, BACKEND_STARTUP_GRACE_SECONDS,
    BackendConfig, BackendRuntime, BackendLease as _RuntimeBackendLease,
)
from desktop.loopback import open_loopback_request
from desktop.service import BackendIdleMonitor
from desktop_window_geometry import normalize_window_size
from desktop_window_resize import WindowResizeController
from desktop.bridge import (
    BridgeWindowOps,
    Live2DApi as _BridgeLive2DApi,
    UserLive2DApi as _BridgeUserLive2DApi,
)

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


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
HOST = "127.0.0.1"
DEFAULT_PORT = PORT_REGISTRY["desktopBackend"]
BACKEND_FALLBACK_PORTS = tuple(range(DEFAULT_PORT + 1, DEFAULT_PORT + 22))
TEST_BACKEND_PORT = DEFAULT_PORT + 10
BACKEND_LOG_PATH = DATA_DIR / "logs" / "desktop_backend.log"
_backend_runtime = BackendRuntime(
    BackendConfig(
        root=ROOT,
        entrypoint=Path(__file__).resolve(),
        log_path=BACKEND_LOG_PATH,
        executable=sys.executable,
        frozen=bool(getattr(sys, "frozen", False)),
        interactive_test_mode=INTERACTIVE_TEST_MODE,
        host=HOST,
        default_port=DEFAULT_PORT,
        fallback_ports=BACKEND_FALLBACK_PORTS,
        build=APP_BUILD,
        release_mode=RELEASE_MODE,
        runtime_mode=RUNTIME_MODE,
    ),
    write_log=write_server_log,
)


def _bridge_window_ops() -> BridgeWindowOps:
    """在入口边界把桥接绑定到原生窗口操作。"""

    return BridgeWindowOps(
        native_window_handle=_native_window_handle,
        window_logical_position=_window_logical_position,
        move_native_window=_move_native_window,
        resize_native_window=_resize_native_window,
        set_window_topmost_with_retry=_set_window_topmost_with_retry,
        window_resize_factory=WindowResizeController,
    )


def _bridge_kwargs() -> dict:
    """只组装 JS 桥接所需的服务和文件能力。"""

    return {
        "host": HOST,
        "data_dir": DATA_DIR,
        "webview_module": webview,
        "release_mode": RELEASE_MODE,
        "normalize_window_size": normalize_window_size,
        "window_ops": _bridge_window_ops(),
        "ensure_backend": lambda port: ensure_backend_service(port),
        "write_log": lambda event, **details: write_server_log(event, **details),
        "resolve_generated_file": lambda file_id: resolve_generated_file(file_id),
        "open_loopback_request": lambda request, timeout: open_loopback_request(request, timeout),
    }


class UserLive2DApi(_BridgeUserLive2DApi):
    """保留旧的 pywebview 构造签名，供入口和现有测试继续使用。"""

    def __init__(self, port: int) -> None:
        super().__init__(port, **_bridge_kwargs())


class Live2DApi(_BridgeLive2DApi):
    """保留开发模式桥接的旧构造签名。"""

    def __init__(self, port: int) -> None:
        super().__init__(port, **_bridge_kwargs())


def start_server(port: int) -> AmadeusHTTPServer:
    """为测试或独立工具显式启动进程内服务，普通桌面使用独立后端进程。"""
    server = AmadeusHTTPServer((HOST, port), AmadeusHandler)
    thread = threading.Thread(target=server.serve_forever, name="amadeus-live2d-server", daemon=True)
    thread.start()
    return server


# 稳定性工具仍可调用原入口函数，具体状态和规则只由后端运行时持有。
def is_amadeus_server(port: int) -> bool:
    return _backend_runtime.is_amadeus_server(port)


def port_is_listening(port: int) -> bool:
    return _backend_runtime.port_is_listening(port)


def select_backend_port(preferred: int = DEFAULT_PORT) -> int:
    return _backend_runtime.select_backend_port(preferred)


def backend_command(port: int) -> list[str]:
    return _backend_runtime.backend_command(port)


def spawn_backend_service(port: int) -> subprocess.Popen[bytes]:
    return _backend_runtime.spawn_backend_service(port)


def backend_log_tail(max_chars: int = 1200) -> str:
    return _backend_runtime.backend_log_tail(max_chars)


def ensure_backend_service(port: int = DEFAULT_PORT) -> None:
    return _backend_runtime.ensure_backend_service(port)


def post_runtime_lease(port: int, path: str, client_id: str, mode: str, timeout: float = 1.5) -> bool:
    return _backend_runtime.post_runtime_lease(port, path, client_id, mode, timeout)


class BackendLease(_RuntimeBackendLease):
    def __init__(self, port: int, mode: str) -> None:
        super().__init__(_backend_runtime, port, mode)


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
    # 入口保留 server/辅助服务装配；监视器独占 idle 时间状态。
    idle_monitor = BackendIdleMonitor(
        server,
        port,
        started=time.monotonic(),
        refresh_leases=refresh_runtime_client_leases,
        active_client_count=active_runtime_client_count,
        write_log=write_server_log,
    )
    idle_monitor.start()
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
        idle_monitor.stop_event.set()
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
    if RELEASE_MODE:
        from server import ARCHITECTURE_FEATURES, build_persona_context_snapshot

        if ARCHITECTURE_FEATURES.persona_context_mode != "on":
            raise RuntimeError("release Persona Skill is disabled")
        snapshot = build_persona_context_snapshot(
            "时间机器真的能造出来吗？", relationship_state={"generation": 0}
        )
        if snapshot.fallback_reason or "scientific-reasoning" not in snapshot.provider_module_ids:
            raise RuntimeError(
                "release Persona Skill retrieval unavailable: "
                + str(snapshot.fallback_reason or snapshot.provider_module_ids)
            )
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

    initial_width, initial_height = normalize_window_size(
        max(760, int(settings.get("windowWidth") or 520)) if dev_mode else int(settings.get("windowWidth") or 520),
        max(860, int(settings.get("windowHeight") or 760)) if dev_mode else int(settings.get("windowHeight") or 760),
        settings.get("layoutMode"),
    )
    window = webview.create_window(
        "AMADEUS Live2D DEV" if dev_mode else "AMADEUS Live2D TEST" if test_mode else "AMADEUS Live2D",
        url=url,
        js_api=UserLive2DApi(port) if release_mode else Live2DApi(port),
        width=initial_width,
        height=initial_height,
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
            _resize_native_window(window, initial_width, initial_height)
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
