"""恢复结果的最终合同封装与对话持久化边界。

所有恢复生产者都必须经过这里，先生成与普通回答相同的最终轮次包络，再在共享锁
内持久化实际交付文本。失败草稿的认知覆盖、长期记忆和关系补丁不会在此提交。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol

from agent.dialogue_architecture import (
    bind_missing_input_clarification_contract,
    final_turn_envelope,
)
from backend.runtime.requests import RequestValidationError


class RecoveryCommitRuntimeOperations(Protocol):
    def raise_if_cancelled(self, *args: Any, **kwargs: Any) -> Any: ...
    def write_log(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryCommitPlanningOperations(Protocol):
    def body_prepared_action_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def turn_contract_context(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryCommitContextOperations(Protocol):
    def bind_turn_context(self, *args: Any, **kwargs: Any) -> Any: ...


class RecoveryCommitPersistenceOperations(Protocol):
    @property
    def conversation_lock(self) -> Any: ...
    def state_database(self, *args: Any, **kwargs: Any) -> Any: ...
    def read_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def append_turn(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RecoveryCommitServices:
    runtime: RecoveryCommitRuntimeOperations
    planning: RecoveryCommitPlanningOperations
    context: RecoveryCommitContextOperations
    persistence: RecoveryCommitPersistenceOperations


def seal_recovery_turn_result(
    services: RecoveryCommitServices,
    body: dict[str, Any],
    user_message: str,
    result: Mapping[str, Any],
    *,
    settings: dict[str, Any],
    request_id: str,
    delivery_path: str,
) -> dict[str, Any]:
    """用 UI 校验的同一种合同封装每一份恢复回复。

    旧恢复路径会返回看似合理的回复字典，却跳过最终轮次包络；浏览器因此会拒绝
    半成品协议并重试整个用户轮次。这里让恢复成为统一最终轮次协议的另一个生产者，
    并持久化实际交付对话，因为只有包络、没有历史记录时，下一轮冻结上下文仍看不见
    该回复。恢复文本可交付，也不能据此接受失败草稿中的状态修订。
    """

    services.runtime.raise_if_cancelled(request_id)
    cached = body.get("_persistedRecoveryTurnResult")
    if isinstance(cached, dict) and cached.get("requestId") == request_id:
        return deepcopy(cached)
    prepared = services.planning.body_prepared_action_turn(
        body,
        user_message,
        request_id=request_id,
    )
    interpretation, _policy, contract = services.planning.turn_contract_context(
        user_message,
        prepared,
        has_tools=bool(
            result.get("toolUsed") or result.get("toolResults") or result.get("actionReceipt")
        ),
        task_body=body,
    )
    sealed = {key: value for key, value in result.items() if not key.startswith("_")}
    if bool(sealed.get("requiresUserResponse")):
        contract = bind_missing_input_clarification_contract(
            contract,
            semantic_text=interpretation.semantic_text,
        )
        sealed["semanticKind"] = "clarification"
        sealed["turnContractVerification"] = "recovery_missing_input_clarification"
    else:
        sealed["turnContractVerification"] = "trusted_pipeline_recovery"
    sealed.update(
        {
            "ok": True,
            "requestId": request_id,
            "completedActs": list(contract.required_acts),
            "completedActsProvided": True,
            "standalone": True,
            "standaloneProvided": True,
            "dependsOnUndeliveredPrefix": False,
            "dependsOnUndeliveredPrefixProvided": True,
            "turnComplete": True,
            "turnCompleteProvided": True,
            "turnContract": contract.to_dict(),
            "utteranceInterpretation": interpretation.to_dict(),
            "proactiveDirective": "unchanged",
            "proactiveDirectiveProvided": True,
        }
    )
    envelope = final_turn_envelope(
        sealed,
        contract,
        turn_id=f"{request_id}:turn",
        delivery_path=delivery_path,
    )
    sealed["finalTurnEnvelope"] = envelope.to_dict()
    # 只绑定冻结身份，保留原回复锚点；失败路由的认知覆盖和指代解析改动不能提交。
    snapshot = body.get("_turnContextSnapshot")
    identity_snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    identity_snapshot.pop("_contextControl", None)
    services.context.bind_turn_context(
        {"_turnContextSnapshot": identity_snapshot},
        sealed,
        bind_cognition=False,
    )
    metrics = sealed.setdefault("_chatMetrics", {})
    metrics.update(requestId=request_id, conversationOrigin="recovery")
    metrics.setdefault("currentTurnId", request_id)
    if isinstance(sealed.get("toolResults"), list):
        sealed["_conversationToolResults"] = sealed["toolResults"]
    # 这里只持久化对话。恢复不授权新的长期记忆、关系变化或模型提出的状态补丁。
    deduplicated = False
    try:
        with services.persistence.conversation_lock:
            services.runtime.raise_if_cancelled(request_id)
            with services.persistence.state_database() as connection:
                previous = connection.execute(
                    "SELECT user_text, assistant_text, metadata_json FROM conversation_turns "
                    "WHERE session_id = 'default' AND turn_id = ?",
                    (metrics["currentTurnId"],),
                ).fetchone()
            if previous is not None:
                metadata = json.loads(previous["metadata_json"])
                if (
                    previous["user_text"] != str(user_message or "").strip()
                    or previous["assistant_text"] != str(sealed.get("reply") or "").strip()
                    or (
                        metrics.get("conversationId")
                        and metadata.get("conversationId") != metrics["conversationId"]
                    )
                    or (
                        metrics.get("conversationEpoch") is not None
                        and metadata.get("conversationEpoch") != metrics["conversationEpoch"]
                    )
                ):
                    raise RequestValidationError(
                        "recovery_turn_conflict",
                        "这轮已有不同的已保存回答，不能用恢复草稿覆盖。",
                    )
                sealed["turnIdentity"] = {
                    key: metadata.get(key, "")
                    for key in (
                        "conversationId",
                        "turnId",
                        "userMessageId",
                        "assistantMessageId",
                        "userContentHash",
                        "assistantContentHash",
                        "rootSnapshotId",
                    )
                }
                context = services.persistence.read_context()
                deduplicated = True
            else:
                context = services.persistence.append_turn(user_message, sealed, settings, metrics)
    except Exception as error:
        # 不能交付一份下一请求看不见的“完整”恢复；在实际持久化边界保留原失败。
        services.runtime.write_log(
            "recovery_turn_persistence_failed",
            requestId=request_id,
            deliveryPath=delivery_path,
            errorType=type(error).__name__,
            error=str(error)[:500],
        )
        raise
    # 持久化可能添加原生视觉输入 ID 等账务字段，不能让它们进入公开结果或重放缓存。
    for key in list(sealed):
        if key.startswith("_"):
            sealed.pop(key)
    body["_persistedRecoveryTurnResult"] = deepcopy(sealed)
    services.runtime.write_log(
        "recovery_turn_persisted",
        requestId=request_id,
        turnId=sealed.get("turnIdentity", {}).get("turnId"),
        conversationId=sealed.get("turnIdentity", {}).get("conversationId"),
        deliveryPath=delivery_path,
        turnEpoch=metrics.get("conversationEpoch"),
        activeEpoch=context.get("conversationEpoch"),
        deduplicated=deduplicated,
    )
    services.runtime.write_log(
        "recovery_turn_contract_completed",
        requestId=request_id,
        deliveryPath=delivery_path,
        envelope=envelope.to_dict(),
    )
    return sealed
