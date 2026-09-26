"""桌面后端端口、子进程和客户端租约；不依赖窗口或 server 模块。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from desktop_port_selection import choose_available_backend_port

from .loopback import open_loopback_request


BACKEND_START_TIMEOUT_SECONDS = 15.0
BACKEND_HEARTBEAT_SECONDS = 2.5
BACKEND_IDLE_SECONDS = 24.0
BACKEND_STARTUP_GRACE_SECONDS = 35.0


@dataclass(frozen=True)
class BackendConfig:
    """入口一次性给出的安装/构建身份；路径不在模块内按 __file__ 重算。"""

    root: Path
    entrypoint: Path
    log_path: Path
    executable: str
    frozen: bool
    interactive_test_mode: bool
    host: str
    default_port: int
    fallback_ports: tuple[int, ...]
    build: str
    release_mode: bool
    runtime_mode: str
    start_timeout_seconds: float = BACKEND_START_TIMEOUT_SECONDS
    heartbeat_seconds: float = BACKEND_HEARTBEAT_SECONDS


class BackendRuntime:
    """同一进程内唯一启动锁所有者；双窗口竞态和端口 drain 在锁内完成。"""

    def __init__(
        self,
        config: BackendConfig,
        *,
        write_log: Callable[..., None],
        open_request: Callable[..., object] = open_loopback_request,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.write_log = write_log
        self.open_request = open_request
        self.monotonic = monotonic
        self.sleep = sleep
        self.start_lock = threading.Lock()

    def is_amadeus_server(self, port: int) -> bool:
        try:
            with self.open_request(f"http://{self.config.host}:{port}/api/health", timeout=0.8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return (
                payload.get("service") == "amadeus-local"
                and payload.get("build") == self.config.build
                and bool(payload.get("releaseMode")) == self.config.release_mode
                and str(payload.get("runtimeMode") or "") == self.config.runtime_mode
            )
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def port_is_listening(self, port: int) -> bool:
        try:
            with socket.create_connection((self.config.host, int(port)), timeout=0.35):
                return True
        except OSError:
            return False

    def select_backend_port(self, preferred: int | None = None) -> int:
        if preferred is None:
            preferred = self.config.default_port
        fallback_ports = (
            tuple(range(preferred + 1, preferred + 22))
            if preferred != self.config.default_port else self.config.fallback_ports
        )
        port = choose_available_backend_port(
            preferred,
            fallback_ports,
            is_listening=self.port_is_listening,
            is_compatible=self.is_amadeus_server,
        )
        if port != preferred:
            self.write_log(
                "desktop_backend_port_fallback",
                preferredPort=preferred,
                selectedPort=port,
                reason="preferred_port_incompatible_or_occupied",
            )
        return port

    def backend_command(self, port: int) -> list[str]:
        if self.config.frozen:
            command = [self.config.executable, "--backend-service", "--backend-port", str(port)]
        else:
            command = [self.config.executable, str(self.config.entrypoint), "--backend-service", "--backend-port", str(port)]
        if self.config.interactive_test_mode:
            command.append("--test-mode")
        return command

    def spawn_backend_service(self, port: int) -> subprocess.Popen[bytes]:
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if self.config.release_mode:
            env["AMADEUS_RELEASE_MODE"] = "1"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        log_file = self.config.log_path.open("ab")
        try:
            log_file.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] start backend {self.config.build} port={port}\n".encode("utf-8"))
            log_file.flush()
            process = subprocess.Popen(
                self.backend_command(port),
                cwd=str(self.config.root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        finally:
            log_file.close()
        return process

    def backend_log_tail(self, max_chars: int = 1200) -> str:
        try:
            text = self.config.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-max(200, int(max_chars)):].strip()

    def ensure_backend_service(self, port: int | None = None) -> None:
        """锁内复核健康和监听；旧 socket 排空后才启动，失败进程再验胜出的同伴。"""
        if port is None:
            port = self.config.default_port
        with self.start_lock:
            if self.is_amadeus_server(port):
                return
            if self.port_is_listening(port):
                drain_deadline = self.monotonic() + 2.0
                while self.monotonic() < drain_deadline:
                    if self.is_amadeus_server(port):
                        return
                    if not self.port_is_listening(port):
                        break
                    self.sleep(0.12)
                if self.port_is_listening(port):
                    restart_target = "重新打开 AMADEUS.exe" if self.config.release_mode else "重新运行 run_amadeus.ps1"
                    raise RuntimeError(
                        f"桌面后端端口 {port} 已被其他程序或旧版 AMADEUS 占用。"
                        f"请关闭占用进程后{restart_target}。"
                    )
            process = self.spawn_backend_service(port)
            deadline = self.monotonic() + self.config.start_timeout_seconds
            while self.monotonic() < deadline:
                if self.is_amadeus_server(port):
                    return
                if process.poll() is not None:
                    if self.is_amadeus_server(port):
                        return
                    detail = self.backend_log_tail()
                    raise RuntimeError(
                        f"AMADEUS 桌面后端启动失败（退出码 {process.returncode}）。"
                        + (f"\n{detail}" if detail else f"详情见 {self.config.log_path}")
                    )
                self.sleep(0.18)
            raise RuntimeError(
                f"AMADEUS 桌面后端在 {self.config.start_timeout_seconds:.0f} 秒内未就绪。详情见 {self.config.log_path}"
            )

    def post_runtime_lease(self, port: int, path: str, client_id: str, mode: str, timeout: float = 1.5) -> bool:
        request = urllib.request.Request(
            f"http://{self.config.host}:{port}{path}",
            data=json.dumps({"clientId": client_id, "mode": mode}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with self.open_request(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return bool(payload.get("ok"))
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
            return False


class BackendLease:
    """窗口租约：注册重试、心跳失联复核/重注册、关闭时先注销再等线程。"""

    def __init__(self, runtime: BackendRuntime, port: int, mode: str) -> None:
        self.runtime = runtime
        self.port = port
        self.mode = mode
        self.client_id = f"desktop-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        for attempt in range(1, 4):
            self.runtime.ensure_backend_service(self.port)
            if self.runtime.post_runtime_lease(self.port, "/api/runtime/client/register", self.client_id, self.mode):
                self.thread = threading.Thread(target=self._heartbeat_loop, name="amadeus-backend-heartbeat", daemon=True)
                self.thread.start()
                return
            self.runtime.sleep(0.3 * attempt)
        raise RuntimeError("桌面后端已启动，但客户端租约连续三次注册失败。")

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.runtime.config.heartbeat_seconds):
            if self.runtime.post_runtime_lease(self.port, "/api/runtime/client/heartbeat", self.client_id, self.mode):
                continue
            self.runtime.write_log("desktop_backend_heartbeat_lost", port=self.port, clientId=self.client_id)
            try:
                self.runtime.ensure_backend_service(self.port)
                self.runtime.post_runtime_lease(self.port, "/api/runtime/client/register", self.client_id, self.mode)
            except Exception as error:
                self.runtime.write_log(
                    "desktop_backend_restart_failed",
                    port=self.port,
                    clientId=self.client_id,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                )

    def close(self) -> None:
        self.stop_event.set()
        self.runtime.post_runtime_lease(self.port, "/api/runtime/client/unregister", self.client_id, self.mode, timeout=0.8)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
