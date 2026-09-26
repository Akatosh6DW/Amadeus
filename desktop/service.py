"""后端服务的 idle 监视，server 创建和辅助服务装配仍由入口负责。"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .backend import BACKEND_HEARTBEAT_SECONDS, BACKEND_IDLE_SECONDS, BACKEND_STARTUP_GRACE_SECONDS


class BackendIdleMonitor:
    """只有监视线程改写时间状态；睡醒续租、活跃请求和关闭顺序保持原规则。"""

    def __init__(
        self,
        server,
        port: int,
        *,
        started: float,
        refresh_leases: Callable[[], None],
        active_client_count: Callable[[], int],
        write_log: Callable[..., None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.server = server
        self.port = port
        self.started = started
        self.last_client_seen = started
        self.last_monitor_tick = started
        self.refresh_leases = refresh_leases
        self.active_client_count = active_client_count
        self.write_log = write_log
        self.monotonic = monotonic
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def tick(self, now: float) -> bool:
        """单次真实 idle 决策；可用临时时钟和假 server 独立验证。"""
        if now - self.last_monitor_tick > BACKEND_HEARTBEAT_SECONDS * 3:
            # Windows 睡醒可能跳过多个计时器周期，先延长租约让 UI 心跳恢复。
            self.refresh_leases()
            self.last_client_seen = now
        self.last_monitor_tick = now
        if self.active_client_count() > 0:
            self.last_client_seen = now
            return False
        activity = self.server.activity_snapshot()
        if int(activity.get("activeRequests") or 0) > 0:
            self.last_client_seen = now
            return False
        self.last_client_seen = max(self.last_client_seen, float(activity.get("lastActivity") or self.started))
        idle = now - self.last_client_seen
        grace = BACKEND_STARTUP_GRACE_SECONDS if self.last_client_seen == self.started else BACKEND_IDLE_SECONDS
        if idle >= grace:
            self.write_log(
                "desktop_backend_idle_shutdown",
                port=self.port,
                idleSeconds=round(idle, 1),
                activeRequests=0,
            )
            self.server.shutdown()
            return True
        return False

    def run(self) -> None:
        while not self.stop_event.wait(2.0):
            if self.tick(self.monotonic()):
                return

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="amadeus-backend-idle-monitor", daemon=True)
        self.thread.start()
