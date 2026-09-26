"""长期记忆 observer 的后台任务、作用域和熔断边界。"""
from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
import contextvars
from typing import Any, Callable, Mapping, Sequence

from agent.memory_circuit import BackgroundCircuitRegistry


@dataclass(frozen=True)
class MemoryObserverPorts:
    """observer 的外部边界；任务状态和 executor 由本服务唯一持有。"""

    normalize_scope: Callable[[Any], str]
    current_scope: Callable[[], str]
    scope_context: contextvars.ContextVar[str]
    merge_typed: Callable[..., list[str]]
    clear_generation: Callable[[dict[str, Any] | None], int]
    write_log: Callable[..., Any]
    circuit: Callable[[], BackgroundCircuitRegistry]
    executor: Callable[[], Executor]


class MemoryObserverService:
    """管理 memory observer 的提交、熔断、异常记录和 scope 恢复。"""

    def __init__(self, ports: MemoryObserverPorts) -> None:
        self.ports = ports
        self.executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="amadeus-memory-observer",
        )
        self.circuits = BackgroundCircuitRegistry(
            failure_threshold=3,
            cooldown_seconds=180,
        )

    def run(
        self,
        updates: Sequence[Any],
        *,
        user_message: str,
        settings: dict[str, Any],
        source_turn_id: str,
        storage_scope: str,
        expected_clear_generation: int,
        update_types: Mapping[str, str] | None = None,
    ) -> None:
        key = "memory_observer:" + self.ports.normalize_scope(storage_scope)
        scope_token = self.ports.scope_context.set(self.ports.normalize_scope(storage_scope))
        try:
            circuit = self.ports.circuit()
            if not circuit.allow(key):
                self.ports.write_log("memory_background_circuit_open", jobKey=key)
                return
            self.ports.merge_typed(
                updates,
                user_message=user_message,
                settings=settings,
                source_turn_id=source_turn_id,
                expected_clear_generation=expected_clear_generation,
                update_types=update_types,
            )
            circuit_state = circuit.success(key)
            self.ports.write_log(
                "memory_observer_completed",
                sourceTurnId=str(source_turn_id)[:120],
                jobCircuit=circuit_state.to_dict(),
            )
        except Exception as error:
            circuit = self.ports.circuit()
            circuit_state = circuit.failure(key, error)
            self.ports.write_log(
                "memory_observer_failed",
                sourceTurnId=str(source_turn_id)[:120],
                error=str(error)[:1000],
                jobCircuit=circuit_state.to_dict(),
            )
        finally:
            self.ports.scope_context.reset(scope_token)

    def schedule(
        self,
        updates: Sequence[Any],
        *,
        user_message: str,
        settings: dict[str, Any],
        source_turn_id: str,
        update_types: Mapping[str, str] | None = None,
    ) -> None:
        if not updates:
            return
        self.ports.executor().submit(
            self.run,
            tuple(dict(item) if isinstance(item, Mapping) else str(item) for item in updates),
            user_message=str(user_message or ""),
            settings=dict(settings),
            source_turn_id=str(source_turn_id or ""),
            storage_scope=self.ports.current_scope(),
            expected_clear_generation=self.ports.clear_generation(settings),
            update_types=dict(update_types or {}),
        )
