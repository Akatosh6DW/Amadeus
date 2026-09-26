"""快速交付完成后的延迟持久化阶段。

服务只编排已经接受并交付的回复如何进入现有最终持久化 owner；它不拥有
会话状态、模型调用或流式发送，也不吞掉取消和存储异常。
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Protocol

from agent.dialogue_architecture import UtteranceInterpretation, interpret_utterance
from backend.common.text import unique_strings


class DeferredPersistenceCancellation(Protocol):
    def raise_if_cancelled(self, request_id: str) -> None: ...


class DeferredPersistenceFinalizer(Protocol):
    def persist(
        self,
        settings: dict[str, Any],
        user_message: str,
        result: dict[str, Any],
        *,
        request_id: str,
        interpretation: UtteranceInterpretation,
        chat_metrics: dict[str, Any],
        cold_shoulder_short_circuit: bool,
    ) -> tuple[dict[str, Any], list[str]]: ...


@dataclass(frozen=True)
class DeferredPersistenceServices:
    cancellation: DeferredPersistenceCancellation
    finalizer: DeferredPersistenceFinalizer


def complete_deferred_chat_persistence(
    services: DeferredPersistenceServices,
    settings: dict[str, Any],
    user_message: str,
    result: dict[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    """完成快速轮次在语句交付后的持久化。"""

    services.cancellation.raise_if_cancelled(request_id)
    value = dict(result)
    deferred = value.pop("_deferredPersistence", {})
    if not isinstance(deferred, dict) or not deferred:
        return value
    persistence_started = time.perf_counter()
    interpretation = interpret_utterance(user_message)
    persisted, warnings = services.finalizer.persist(
        settings,
        user_message,
        value,
        request_id=request_id,
        interpretation=interpretation,
        chat_metrics=(
            dict(deferred.get("chatMetrics"))
            if isinstance(deferred.get("chatMetrics"), dict) else {}
        ),
        cold_shoulder_short_circuit=bool(deferred.get("coldShoulderShortCircuit")),
    )
    existing_warnings = [
        str(item) for item in persisted.get("pipelineWarnings", [])
        if str(item).strip()
    ]
    combined_warnings = unique_strings([*existing_warnings, *warnings])
    if combined_warnings:
        persisted["pipelineWarnings"] = combined_warnings
    metrics = (
        persisted.get("pipelineMetrics")
        if isinstance(persisted.get("pipelineMetrics"), dict) else {}
    )
    metrics["persistenceDeferredUntilAfterUtterance"] = True
    metrics["postDeliveryPersistenceWarnings"] = list(warnings)
    persistence_ms = round((time.perf_counter() - persistence_started) * 1000)
    metrics["postDeliveryPersistenceMs"] = persistence_ms
    metrics["serverTotalMs"] = int(value.get("latencyMs") or 0) + persistence_ms
    persisted["pipelineMetrics"] = metrics
    persisted["latencyMs"] = metrics["serverTotalMs"]
    persisted.pop("_conversationToolResults", None)
    return persisted
