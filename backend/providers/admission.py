"""模型请求排队与后台连接熔断的状态所有者。

前台对话优先于可选后台请求。同一线程的嵌套调用复用已经取得的请求槽，避免
恢复请求等待自己；连接失败的熔断只影响后台请求，前台仍可尝试恢复连接。
"""

from contextlib import contextmanager
import threading
from typing import Any, Callable

from backend.providers.errors import UpstreamBackgroundDeferred


class UpstreamAdmission:
    """每个实例持有一套排队锁、线程嵌套深度与连接状态。"""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        timestamp: Callable[[], str],
        check_cancelled: Callable[[str], None],
    ) -> None:
        self._monotonic = monotonic
        self._timestamp = timestamp
        self._check_cancelled = check_cancelled
        self._condition = threading.Condition()
        self._active = ""
        self._foreground_waiters = 0
        self._local = threading.local()
        self._connectivity_lock = threading.Lock()
        self._circuit_open_until = 0.0
        self._connectivity: dict[str, Any] = {
            "state": "unknown",
            "consecutiveFailures": 0,
            "lastSuccessAt": "",
            "lastFailureAt": "",
            "lastFailureCategory": "",
            "lastTransportRoute": "",
        }

    def connectivity_snapshot(self) -> dict[str, Any]:
        """返回状态副本，调用者不能通过状态查询修改运行中的请求协调器。"""
        with self._connectivity_lock:
            state = dict(self._connectivity)
            retry_after = max(0.0, self._circuit_open_until - self._monotonic())
        state["backgroundCircuitOpen"] = retry_after > 0
        state["backgroundRetryAfterSeconds"] = round(retry_after, 1)
        return state

    def record_connectivity(self, success: bool, *, category: str = "", route: str = "") -> None:
        """登记连接结果；连续两次失败暂停后台 45 秒，成功立即恢复。"""
        with self._connectivity_lock:
            if success:
                self._circuit_open_until = 0.0
                self._connectivity.update({
                    "state": "available",
                    "consecutiveFailures": 0,
                    "lastSuccessAt": self._timestamp(),
                    "lastFailureCategory": "",
                    "lastTransportRoute": route,
                })
            else:
                failure_count = int(self._connectivity.get("consecutiveFailures") or 0) + 1
                if failure_count >= 2:
                    self._circuit_open_until = max(self._circuit_open_until, self._monotonic() + 45.0)
                self._connectivity.update({
                    "state": "unavailable",
                    "consecutiveFailures": failure_count,
                    "lastFailureAt": self._timestamp(),
                    "lastFailureCategory": category,
                    "lastTransportRoute": route,
                })

    @contextmanager
    def request_slot(self, *, background: bool, request_id: str = ""):
        """前台可取消地排队；后台遇到前台占用、排队或熔断时立即让位。"""
        depth = int(getattr(self._local, "depth", 0) or 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        if background:
            with self._connectivity_lock:
                circuit_open = self._monotonic() < self._circuit_open_until
            if circuit_open:
                raise UpstreamBackgroundDeferred("upstream connectivity circuit is open")
        with self._condition:
            if background:
                if self._active or self._foreground_waiters:
                    raise UpstreamBackgroundDeferred("foreground upstream request has priority")
                self._active = "background"
            else:
                self._foreground_waiters += 1
                try:
                    while self._active:
                        self._check_cancelled(request_id)
                        self._condition.wait(timeout=0.25)
                    self._check_cancelled(request_id)
                    self._active = "foreground"
                finally:
                    self._foreground_waiters -= 1
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._condition:
                self._active = ""
                self._condition.notify_all()
