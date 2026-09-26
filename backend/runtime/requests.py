"""管理聊天请求、客户端租约、取消信号和流式工具闸门。

本模块只依赖构造时传入的时钟、回调和限值。入口层可保留旧函数名，
但请求状态及其同步锁由 RequestRuntime 实例持有。
"""

from __future__ import annotations

from contextlib import contextmanager
import re
import threading
from typing import Any, Callable, Iterator, Protocol

from agent.turn_runtime import TerminalReason, TurnPhase, TurnState, phase_for_progress_stage


class RuntimeClock(Protocol):
    """提供请求租约和进度所需的两种时间。"""

    def monotonic(self) -> float: ...

    def time(self) -> float: ...


class RequestValidationError(ValueError):
    """客户端请求无法被本地服务处理。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RequestRuntime:
    """持有一个后端进程内的请求状态及同步边界。

    回调由入口层提供，因此取消流程无需知道 TTS 或日志的具体实现。
    clock 是提供器，便于入口层替换时间对象，也便于单测使用虚拟时钟。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], RuntimeClock],
        uuid_hex: Callable[[], str],
        log_event: Callable[..., None],
        cancel_tts: Callable[[str], bool],
        cache_ttl_seconds: float,
        cache_limit: int,
        client_lease_seconds: float,
    ) -> None:
        self._clock = clock
        self._uuid_hex = uuid_hex
        self._log_event = log_event
        self._cancel_tts = cancel_tts
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cache_limit = cache_limit
        self.client_lease_seconds = client_lease_seconds

        self.chat_request_lock = threading.Lock()
        self.chat_requests: dict[str, dict[str, Any]] = {}
        self.active_upstream_response_lock = threading.Lock()
        self.active_upstream_responses: dict[str, list[Any]] = {}
        self.chat_stream_tool_gate_lock = threading.Lock()
        self.chat_stream_tool_gates: dict[str, threading.Event] = {}
        self.runtime_client_lock = threading.Lock()
        self.runtime_clients: dict[str, dict[str, Any]] = {}

    def _monotonic(self) -> float:
        return self._clock().monotonic()

    def _wall_time(self) -> float:
        return self._clock().time()

    @staticmethod
    def normalize_runtime_client_id(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9._:-]", "", str(value or ""))[:120]

    def prune_runtime_clients(self, now: float | None = None) -> None:
        """删除租约已过期的客户端；调用方负责在并发场景下持有锁。"""

        current = self._monotonic() if now is None else now
        expired = [
            client_id
            for client_id, entry in self.runtime_clients.items()
            if current - float(entry.get("lastSeen") or 0) > self.client_lease_seconds
        ]
        for client_id in expired:
            self.runtime_clients.pop(client_id, None)

    def touch_runtime_client(self, client_id: Any, mode: Any = "desktop") -> dict[str, Any]:
        """登记心跳并返回当前连接数及租约长度。"""

        normalized = self.normalize_runtime_client_id(client_id)
        if not normalized:
            raise ValueError("clientId is required")
        now = self._monotonic()
        with self.runtime_client_lock:
            self.prune_runtime_clients(now)
            current = self.runtime_clients.get(normalized, {})
            self.runtime_clients[normalized] = {
                "clientId": normalized,
                "mode": re.sub(r"[^A-Za-z0-9._:-]", "", str(mode or "desktop"))[:40] or "desktop",
                "connectedAt": float(current.get("connectedAt") or now),
                "lastSeen": now,
            }
            count = len(self.runtime_clients)
        return {"clientId": normalized, "activeClients": count, "leaseSeconds": self.client_lease_seconds}

    def remove_runtime_client(self, client_id: Any) -> int:
        normalized = self.normalize_runtime_client_id(client_id)
        with self.runtime_client_lock:
            if normalized:
                self.runtime_clients.pop(normalized, None)
            self.prune_runtime_clients()
            return len(self.runtime_clients)

    def runtime_client_snapshot(self) -> list[dict[str, Any]]:
        now = self._monotonic()
        with self.runtime_client_lock:
            self.prune_runtime_clients(now)
            return [
                {
                    "clientId": str(entry.get("clientId") or ""),
                    "mode": str(entry.get("mode") or "desktop"),
                    "ageSeconds": round(max(0.0, now - float(entry.get("lastSeen") or now)), 1),
                }
                for entry in self.runtime_clients.values()
            ]

    def refresh_runtime_client_leases(self) -> int:
        """机器从睡眠恢复后，延长已存在窗口的租约。"""

        now = self._monotonic()
        with self.runtime_client_lock:
            for entry in self.runtime_clients.values():
                entry["lastSeen"] = now
            return len(self.runtime_clients)

    def active_runtime_client_count(self) -> int:
        return len(self.runtime_client_snapshot())

    @staticmethod
    def normalize_chat_request_id(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9._:-]", "", str(value or ""))[:120]

    def prune_chat_request_cache(self, now: float | None = None) -> None:
        """只回收终态请求；运行中的请求不可因缓存限额而丢失。"""

        current = self._monotonic() if now is None else now
        expired = [
            request_id
            for request_id, entry in self.chat_requests.items()
            if entry.get("state") != "running"
            and current - float(entry.get("updatedAt") or entry.get("createdAt") or current)
            > self.cache_ttl_seconds
        ]
        for request_id in expired:
            self.chat_requests.pop(request_id, None)
        if len(self.chat_requests) <= self.cache_limit:
            return
        removable = sorted(
            (
                (request_id, float(entry.get("updatedAt") or entry.get("createdAt") or 0))
                for request_id, entry in self.chat_requests.items()
                if entry.get("state") != "running"
            ),
            key=lambda item: item[1],
        )
        for request_id, _ in removable[:max(0, len(self.chat_requests) - self.cache_limit)]:
            self.chat_requests.pop(request_id, None)

    def prepare_external_chat_body(self, body: dict[str, Any]) -> str:
        """剥离客户端伪造的内部字段，建立用于重试去重的请求标识。"""

        for key in [item for item in body if str(item).startswith("_")]:
            body.pop(key, None)
        request_id = self.normalize_chat_request_id(body.get("requestId"))
        if not request_id:
            request_id = f"turn-{int(self._wall_time() * 1000)}-{self._uuid_hex()[:12]}"
        body["requestId"] = request_id
        return request_id

    def claim_chat_request(self, request_id: str, supersedes_request_id: str = "") -> tuple[str, Any]:
        """认领新请求、复用完成结果，或等待相同请求的处理者。"""

        if not request_id:
            return "owner", None
        with self.chat_request_lock:
            self.prune_chat_request_cache()
            if supersedes_request_id and supersedes_request_id != request_id:
                superseded = self.chat_requests.get(supersedes_request_id)
                if superseded and superseded.get("state") == "running":
                    superseded["cancelled"] = True
                    superseded["updatedAt"] = self._monotonic()
                    superseded_turn_state = superseded.get("turnState")
                    if isinstance(superseded_turn_state, TurnState) and not superseded_turn_state.terminal:
                        try:
                            superseded_turn_state.finish(
                                TurnPhase.CANCELLED,
                                TerminalReason.USER_CANCELLED,
                                reason="superseded_by_new_request",
                            )
                        except ValueError:
                            pass
            # 重用完成结果或等待正在处理的同一请求，避免网络重试重复写入用户回合。
            existing = self.chat_requests.get(request_id)
            if existing:
                if existing.get("state") == "completed" and isinstance(existing.get("result"), dict):
                    return "cached", dict(existing["result"])
                if existing.get("state") == "running":
                    return "wait", existing["event"]
                self.chat_requests.pop(request_id, None)
            event = threading.Event()
            turn_id = f"{request_id or self._uuid_hex()}:turn"
            self.chat_requests[request_id] = {
                "state": "running",
                "event": event,
                "createdAt": self._monotonic(),
                "updatedAt": self._monotonic(),
                "cancelled": False,
                "turnState": TurnState(request_id=request_id, turn_id=turn_id),
                "progress": {
                    "stage": "received",
                    "detail": "请求已进入处理队列",
                    "sequence": 1,
                    "startedAt": self._monotonic(),
                    "updatedAt": self._monotonic(),
                },
            }
        return "owner", None

    def update_chat_request_progress(self, request_id: str, stage: str, detail: str = "") -> None:
        """向开发界面发布有界、且不含请求正文的进度。"""

        normalized = self.normalize_chat_request_id(request_id)
        if not normalized:
            return
        now = self._monotonic()
        with self.chat_request_lock:
            entry = self.chat_requests.get(normalized)
            if not entry or entry.get("state") != "running":
                return
            turn_state = entry.get("turnState")
            target_phase = phase_for_progress_stage(stage)
            if (
                isinstance(turn_state, TurnState)
                and target_phase is not None
                and target_phase != TurnPhase.RECEIVED
                and not turn_state.terminal
            ):
                previous_phase = turn_state.phase
                try:
                    turn_state.advance_to(target_phase, reason=f"progress:{stage}")
                    if target_phase == TurnPhase.WAITING_MODEL and previous_phase != target_phase:
                        turn_state.note_model_round()
                    if target_phase == TurnPhase.WAITING_TOOL and previous_phase != target_phase:
                        turn_state.note_tool_round()
                except ValueError as error:
                    self._log_event(
                        "turn_state_transition_rejected",
                        requestId=normalized,
                        currentPhase=turn_state.phase.value,
                        targetPhase=target_phase.value,
                        progressStage=str(stage)[:48],
                        error=str(error)[:240],
                    )
            previous = entry.get("progress") if isinstance(entry.get("progress"), dict) else {}
            entry["progress"] = {
                "stage": re.sub(r"[^a-z0-9_-]", "", str(stage or "working").lower())[:48] or "working",
                "detail": str(detail or "")[:160],
                "sequence": int(previous.get("sequence") or 0) + 1,
                "startedAt": float(previous.get("startedAt") or now),
                "updatedAt": now,
            }
            entry["updatedAt"] = now

    def chat_request_progress(self, request_id: Any) -> dict[str, Any]:
        normalized = self.normalize_chat_request_id(request_id)
        now = self._monotonic()
        with self.chat_request_lock:
            self.prune_chat_request_cache(now)
            entry = self.chat_requests.get(normalized)
            if not entry:
                return {"ok": False, "requestId": normalized, "state": "missing"}
            progress = entry.get("progress") if isinstance(entry.get("progress"), dict) else {}
            started_at = float(progress.get("startedAt") or entry.get("createdAt") or now)
            return {
                "ok": True,
                "requestId": normalized,
                "state": str(entry.get("state") or "running"),
                "cancelled": bool(entry.get("cancelled")),
                "stage": str(progress.get("stage") or entry.get("state") or "working"),
                "detail": str(progress.get("detail") or ""),
                "sequence": int(progress.get("sequence") or 0),
                "elapsedMs": round(max(0.0, now - started_at) * 1000),
                **(
                    {"turnState": entry["turnState"].to_dict()}
                    if isinstance(entry.get("turnState"), TurnState)
                    else {}
                ),
            }

    def chat_request_cancelled(self, request_id: str) -> bool:
        if not request_id:
            return False
        with self.chat_request_lock:
            entry = self.chat_requests.get(request_id)
            return bool(entry and entry.get("cancelled"))

    def raise_if_chat_request_cancelled(self, request_id: str) -> None:
        if self.chat_request_cancelled(request_id):
            raise RequestValidationError("request_cancelled", "这轮回答已被用户终止。")

    @contextmanager
    def tracked_upstream_response(self, request_id: str, response: Any) -> Iterator[Any]:
        """登记上游连接，让用户取消时能够主动关闭正在读取的流。"""

        normalized = self.normalize_chat_request_id(request_id)
        if not normalized:
            yield response
            return
        with self.active_upstream_response_lock:
            self.active_upstream_responses.setdefault(normalized, []).append(response)
        try:
            yield response
        finally:
            with self.active_upstream_response_lock:
                items = self.active_upstream_responses.get(normalized, [])
                remaining = [item for item in items if item is not response]
                if remaining:
                    self.active_upstream_responses[normalized] = remaining
                else:
                    self.active_upstream_responses.pop(normalized, None)

    def cancel_chat_request(self, request_id: Any, *, reason: str = "user_stop_button") -> dict[str, Any]:
        """跨模型、工具闸门和 TTS 阶段取消一轮回答。"""

        normalized = self.normalize_chat_request_id(request_id)
        if not normalized:
            return {"ok": False, "cancelled": False, "error": "request_id_required"}
        found = False
        already_terminal = False
        with self.chat_request_lock:
            entry = self.chat_requests.get(normalized)
            if entry:
                found = True
                already_terminal = entry.get("state") != "running"
                entry["cancelled"] = True
                entry["updatedAt"] = self._monotonic()
                previous = entry.get("progress") if isinstance(entry.get("progress"), dict) else {}
                entry["progress"] = {
                    "stage": "cancelled",
                    "detail": "用户已终止当前回答",
                    "sequence": int(previous.get("sequence") or 0) + 1,
                    "startedAt": float(previous.get("startedAt") or entry.get("createdAt") or self._monotonic()),
                    "updatedAt": self._monotonic(),
                }
                turn_state = entry.get("turnState")
                if isinstance(turn_state, TurnState) and not turn_state.terminal:
                    try:
                        turn_state.finish(
                            TurnPhase.CANCELLED,
                            TerminalReason.USER_CANCELLED,
                            reason=str(reason or "user_stop_button")[:120],
                        )
                    except ValueError:
                        pass
        # 先释放请求锁再调用外部动作：关闭流或 TTS 回调可能阻塞，也可能间接读取取消状态。
        self.release_chat_stream_tool_gate(normalized)
        with self.active_upstream_response_lock:
            responses = list(self.active_upstream_responses.get(normalized, []))
        closed_responses = 0
        for response in responses:
            try:
                response.close()
                closed_responses += 1
            except Exception:
                pass
        tts_cancelled = self._cancel_tts(normalized)
        self._log_event(
            "chat_request_cancelled_by_user",
            requestId=normalized,
            found=found,
            alreadyTerminal=already_terminal,
            closedUpstreamResponses=closed_responses,
            ttsCancelForwarded=tts_cancelled,
        )
        return {
            "ok": True,
            "requestId": normalized,
            "cancelled": True,
            "found": found,
            "alreadyTerminal": already_terminal,
            "closedUpstreamResponses": closed_responses,
            "ttsCancelForwarded": tts_cancelled,
        }

    def create_chat_stream_tool_gate(self, request_id: str) -> threading.Event:
        gate = threading.Event()
        if not request_id:
            gate.set()
            return gate
        with self.chat_stream_tool_gate_lock:
            self.chat_stream_tool_gates[request_id] = gate
        return gate

    def release_chat_stream_tool_gate(self, request_id: str) -> bool:
        if not request_id:
            return False
        with self.chat_stream_tool_gate_lock:
            gate = self.chat_stream_tool_gates.get(request_id)
        if not isinstance(gate, threading.Event):
            return False
        gate.set()
        return True

    def wait_chat_stream_tool_gate(
        self,
        request_id: str,
        gate: threading.Event,
        *,
        timeout_seconds: float = 45.0,
    ) -> str:
        deadline = self._monotonic() + max(0.1, float(timeout_seconds))
        while self._monotonic() < deadline:
            if gate.wait(min(0.12, max(0.0, deadline - self._monotonic()))):
                return "released"
            if self.chat_request_cancelled(request_id):
                return "cancelled"
        return "timeout"

    def remove_chat_stream_tool_gate(self, request_id: str, gate: threading.Event) -> None:
        if not request_id:
            return
        with self.chat_stream_tool_gate_lock:
            if self.chat_stream_tool_gates.get(request_id) is gate:
                self.chat_stream_tool_gates.pop(request_id, None)

    def finish_chat_request(self, request_id: str, result: dict[str, Any] | None = None, error: str = "") -> None:
        """锁内发布终态并唤醒相同请求的等待者。"""

        if not request_id:
            return
        with self.chat_request_lock:
            entry = self.chat_requests.get(request_id)
            if not entry:
                return
            entry["updatedAt"] = self._monotonic()
            if entry.get("cancelled"):
                result = {"cancelled": True, "requestId": request_id}
                error = ""
            turn_state = entry.get("turnState")
            if isinstance(result, dict):
                entry["state"] = "completed"
                entry["result"] = dict(result)
                entry.pop("error", None)
                completion_stage = "cancelled" if result.get("cancelled") else "complete"
                completion_detail = "请求已被后续输入替代" if result.get("cancelled") else "回答已交付前端"
                if isinstance(turn_state, TurnState) and not turn_state.terminal:
                    try:
                        turn_state.finish(
                            TurnPhase.CANCELLED if result.get("cancelled") else TurnPhase.COMPLETED,
                            TerminalReason.USER_CANCELLED if result.get("cancelled") else TerminalReason.END_TURN,
                            reason=completion_detail,
                        )
                    except ValueError as transition_error:
                        self._log_event(
                            "turn_state_finish_rejected",
                            requestId=request_id,
                            error=str(transition_error)[:240],
                        )
                if isinstance(turn_state, TurnState):
                    result["turnState"] = turn_state.to_dict()
                    entry["result"] = dict(result)
            else:
                entry["state"] = "failed"
                entry["error"] = str(error or "request_failed")[:500]
                completion_stage = "failed"
                completion_detail = "处理链路发生错误"
                if isinstance(turn_state, TurnState) and not turn_state.terminal:
                    try:
                        turn_state.finish(
                            TurnPhase.FAILED,
                            TerminalReason.UPSTREAM_FAILURE,
                            reason=str(error or "request_failed")[:240],
                        )
                    except ValueError as transition_error:
                        self._log_event(
                            "turn_state_finish_rejected",
                            requestId=request_id,
                            error=str(transition_error)[:240],
                        )
            previous = entry.get("progress") if isinstance(entry.get("progress"), dict) else {}
            entry["progress"] = {
                "stage": completion_stage,
                "detail": completion_detail,
                "sequence": int(previous.get("sequence") or 0) + 1,
                "startedAt": float(previous.get("startedAt") or entry.get("createdAt") or self._monotonic()),
                "updatedAt": self._monotonic(),
            }
            event = entry.get("event")
            if isinstance(event, threading.Event):
                event.set()

    def completed_chat_request(self, request_id: str) -> dict[str, Any] | None:
        if not request_id:
            return None
        with self.chat_request_lock:
            entry = self.chat_requests.get(request_id)
            if entry and entry.get("state") == "completed" and isinstance(entry.get("result"), dict):
                return dict(entry["result"])
        return None
