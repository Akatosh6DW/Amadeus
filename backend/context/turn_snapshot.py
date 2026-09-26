"""单轮上下文快照的冻结、读取与结果身份投影。

请求 body 是本轮快照和认知覆盖层的唯一持有者。本模块负责在任何附件、工具或
模型处理前冻结一次会话视图，并把同一身份投影到最终结果；会话文件、SQLite、
关系状态和日志仍由入口装配的现有 owner 提供。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from agent.cognitive_adaptations import load_user_adaptations
from agent.cognitive_evidence_journal import (
    read_failed_requests,
    read_receipts as read_cognitive_execution_receipts,
)
from agent.cognitive_runtime import (
    collect_current_user_evidence,
    collect_evidence,
    collect_user_adaptation_evidence,
)
from agent.cognitive_state import (
    load_state as load_cognitive_state,
    load_trace_receipts as load_cognitive_trace_receipts,
)
from agent.turn_context import (
    ReplyAnchorError,
    build_turn_context_snapshot,
    conversation_id_for_epoch,
)
from backend.common.values import safe_nonnegative_int
from backend.context.conversation import turn_conversation_id
from backend.runtime.requests import RequestValidationError


class TurnSnapshotSources(Protocol):
    """读取形成快照所需的配置、会话、动作与关系数据。"""

    def load_settings(self) -> dict[str, Any]: ...
    def read_context(self) -> dict[str, Any]: ...
    def read_pending_action(self) -> Mapping[str, Any]: ...
    def read_archive(self) -> list[dict[str, Any]]: ...
    def read_relationship(self) -> dict[str, Any]: ...


class TurnSnapshotCognitiveStore(Protocol):
    """恢复并读取认知状态；事务和代际仍由现有存储实现拥有。"""

    def recover_projections(self, settings: dict[str, Any]) -> None: ...
    def state_database(self) -> AbstractContextManager[Any]: ...
    def memory_generation(self) -> int: ...


class TurnSnapshotRuntime(Protocol):
    def write_log(self, event: str, **fields: Any) -> None: ...


class TurnSnapshotProjection(Protocol):
    """在结果身份投影时接入认知结果绑定，不复制认知规则。"""

    def bind_cognitive_result(
        self,
        body: Mapping[str, Any],
        result: dict[str, Any],
    ) -> None: ...


@dataclass(frozen=True)
class TurnSnapshotServices:
    sources: TurnSnapshotSources
    cognitive: TurnSnapshotCognitiveStore
    runtime: TurnSnapshotRuntime
    projection: TurnSnapshotProjection


@dataclass(frozen=True)
class TurnSnapshotPolicy:
    snapshot_owner: object
    time_context_owner: object
    user_adaptation_scope: str = "runtime-user"


def ensure_turn_context_snapshot(
    services: TurnSnapshotServices,
    policy: TurnSnapshotPolicy,
    body: dict[str, Any],
    *,
    request_id: str,
    user_message: str,
) -> dict[str, Any]:
    """为本轮所有消费者冻结一份由服务端拥有的上下文视图。"""

    existing = body.get("_turnContextSnapshot")
    if (
        body.get("_turnContextSnapshotOwner") is policy.snapshot_owner
        and isinstance(existing, dict)
        and existing
    ):
        return existing
    services.cognitive.recover_projections(services.sources.load_settings())
    context = services.sources.read_context()
    # 待处理外部动作以 SQLite 事务记录。会话 JSON 可能尚未看到刚完成的轮次，
    # 因此把实时记录合入本轮所有解析器共同使用的唯一冻结快照。
    live_pending_action = services.sources.read_pending_action()
    if isinstance(live_pending_action, Mapping) and live_pending_action:
        context["pendingAction"] = dict(live_pending_action)
    active_epoch = safe_nonnegative_int(context.get("conversationEpoch"))
    supplied_epoch = body.get("contextEpoch")
    request_epoch = safe_nonnegative_int(
        supplied_epoch if supplied_epoch is not None else active_epoch
    )
    if supplied_epoch is not None and request_epoch != active_epoch:
        raise RequestValidationError(
            "stale_conversation_epoch",
            "这轮请求属于已经结束的对话，未进入附件、工具或模型处理。",
        )
    expected_conversation_id = str(
        context.get("conversationId") or conversation_id_for_epoch(request_epoch)
    )
    supplied_conversation_id = str(body.get("conversationId") or "").strip()
    if supplied_conversation_id and supplied_conversation_id != expected_conversation_id:
        raise RequestValidationError(
            "stale_conversation_id",
            "这轮请求属于已经结束的对话，旧消息没有进入附件、工具或模型处理。",
        )
    if supplied_conversation_id:
        # 新客户端会给每条历史附上代际或会话身份。在信任边界直接删除旧会话历史，
        # 不能只靠提示词要求模型忽略它。
        trusted_messages: list[dict[str, Any]] = []
        for item in body.get("messages", []):
            if not isinstance(item, dict):
                continue
            metadata = item.get("meta") if isinstance(item.get("meta"), Mapping) else {}
            item_conversation_id = str(
                item.get("conversationId") or metadata.get("conversationId") or ""
            ).strip()
            item_epoch = metadata.get("contextEpoch", item.get("contextEpoch"))
            same_conversation = (
                item_conversation_id == expected_conversation_id
                if item_conversation_id
                else item_epoch is not None
                and safe_nonnegative_int(item_epoch) == request_epoch
            )
            if same_conversation:
                trusted_messages.append(item)
        body["messages"] = trusted_messages
    # 普通轮次只使用紧凑工作集。只有显式回复可能指向已压缩消息，才读取持久账本。
    raw_reply_anchor = body.get("replyAnchor")
    turns = (
        [
            item
            for item in services.sources.read_archive()
            if turn_conversation_id(item) == expected_conversation_id
        ]
        if raw_reply_anchor not in (None, {}, "")
        else []
    )
    for item in context.get("recentTurns", []):
        if (
            not isinstance(item, dict)
            or turn_conversation_id(item) != expected_conversation_id
        ):
            continue
        item_id = str(item.get("turnId") or "")
        if item_id and not any(
            str(turn.get("turnId") or "") == item_id for turn in turns
        ):
            turns.append(item)
    try:
        snapshot = build_turn_context_snapshot(
            request_id=request_id,
            epoch=request_epoch,
            conversation_id=expected_conversation_id,
            user_text=user_message,
            user_message_id=str(
                body.get("userMessageId") or body.get("clientMessageId") or ""
            ),
            raw_reply_anchor=raw_reply_anchor,
            turns=turns,
            conversation_focus=body.get("conversationFocus"),
            visible_messages=body.get("messages") or [],
            generations={
                "memoryGeneration": safe_nonnegative_int(
                    context.get("memoryGeneration")
                ),
                "relationshipGeneration": safe_nonnegative_int(
                    context.get("relationshipGeneration")
                ),
                "conversationGeneration": safe_nonnegative_int(
                    context.get("conversationGeneration")
                ),
            },
        )
    except ReplyAnchorError as error:
        raise RequestValidationError("invalid_reply_anchor", str(error)) from error
    # 下列冻结输入只供后端使用，不进入公开结果。所有后续解析器读取同一份副本，
    # 避免单轮同时看到压缩前后的混合状态。
    snapshot["_contextState"] = deepcopy(context)
    snapshot["_relationshipState"] = deepcopy(services.sources.read_relationship())
    with services.cognitive.state_database() as connection:
        snapshot["_cognitiveState"] = load_cognitive_state(
            connection,
            expected_conversation_id,
        )
        adaptation_generation = services.cognitive.memory_generation()
        user_adaptations = load_user_adaptations(
            connection,
            scope_id=policy.user_adaptation_scope,
            memory_generation=adaptation_generation,
        )
        snapshot["_cognitiveState"].setdefault("preferences", {}).update(
            {
                "userAdaptations": user_adaptations["records"],
                "userAdaptationRevision": user_adaptations["revision"],
                "userAdaptationGeneration": adaptation_generation,
                "userAdaptationScopeId": policy.user_adaptation_scope,
                # 用户目录跨会话与清空代际保持权威，不能恢复旧的本地提案副本。
                "userProposals": [],
            }
        )
        execution_rows = read_cognitive_execution_receipts(
            connection,
            expected_conversation_id,
        )
        body["_cognitiveFailedRequests"] = read_failed_requests(
            connection,
            expected_conversation_id,
        )
        previous_turn = str(
            (snapshot["_cognitiveState"].get("lastDecisionDigest") or {}).get(
                "turnId"
            )
            or ""
        )
        if previous_turn:
            rows = load_cognitive_trace_receipts(
                connection,
                expected_conversation_id,
                [previous_turn],
            )
            recent_evidence = {}
            for row in rows:
                receipts = row.get("toolReceipts")
                if isinstance(receipts, list):
                    recent_evidence.update(
                        {
                            ref: {
                                **entry,
                                "historical": True,
                                "sourceTurnId": previous_turn,
                                "observedAt": row.get("createdAt"),
                            }
                            for ref, entry in collect_evidence(receipts).items()
                        }
                    )
            body["_cognitiveEvidence"] = recent_evidence
        for row in execution_rows:
            recovered = collect_evidence(row["toolReceipts"])
            body["_cognitiveEvidence"] = {
                **(body.get("_cognitiveEvidence") or {}),
                **{
                    ref: {
                        **entry,
                        "historical": True,
                        "sourceTurnId": row["turnId"],
                        "observedAt": row["createdAt"],
                        "answerCommitted": row["answerCommitted"],
                    }
                    for ref, entry in recovered.items()
                },
            }
        body["_cognitiveExecutionHistory"] = [
            {
                **{
                    key: row[key]
                    for key in (
                        "turnId",
                        "userRequest",
                        "createdAt",
                        "answerCommitted",
                    )
                },
                "executionPlans": row.get("executionPlans", []),
            }
            for row in execution_rows
        ]
    body["_cognitiveEvidence"] = {
        **collect_user_adaptation_evidence(
            snapshot["_cognitiveState"].get("preferences") or {}
        ),
        **(body.get("_cognitiveEvidence") or {}),
        **collect_current_user_evidence(snapshot),
    }
    body["_turnContextSnapshot"] = snapshot
    body["_turnContextSnapshotOwner"] = policy.snapshot_owner
    body["_conversationEpoch"] = request_epoch
    services.runtime.write_log(
        "turn_context_snapshot_created",
        requestId=request_id,
        rootSnapshotId=snapshot["rootSnapshotId"],
        conversationId=snapshot["conversationId"],
        conversationEpoch=request_epoch,
        replyAnchorResolved=bool(snapshot.get("resolvedAnchor")),
        embeddingSkippedReason=str(
            (snapshot.get("resolvedAnchor") or {}).get("embeddingSkippedReason") or ""
        ),
    )
    return snapshot


def cognitive_turn_state(body: Mapping[str, Any]) -> dict[str, Any]:
    """读取当前轮次生效的认知状态，覆盖层优先于冻结基线。"""

    overlay = body.get("_cognitiveOverlay")
    if isinstance(overlay, Mapping) and isinstance(overlay.get("state"), dict):
        return overlay["state"]
    if isinstance(body.get("_cognitiveBase"), dict):
        return body["_cognitiveBase"]
    snapshot = body.get("_turnContextSnapshot")
    if isinstance(snapshot, Mapping) and isinstance(
        snapshot.get("_cognitiveState"), dict
    ):
        return snapshot["_cognitiveState"]
    return {}


def bind_turn_context_to_result(
    services: TurnSnapshotServices,
    policy: TurnSnapshotPolicy,
    body: Mapping[str, Any],
    result: dict[str, Any],
    *,
    bind_cognition: bool = True,
) -> dict[str, Any]:
    """让所有持久化路径携带同一份冻结身份和回复锚点。"""

    # 对外时间来源由后端拥有，模型 JSON 中同名字段一律先删除。
    result.pop("timeContext", None)
    if (
        body.get("_timeContextOwner") is policy.time_context_owner
        and body.get("_timeContext")
    ):
        result["timeContext"] = deepcopy(body["_timeContext"])
    for tool_result in result.get("_conversationToolResults") or []:
        if (
            isinstance(tool_result, Mapping)
            and tool_result.get("name") == "get_system_time"
            and tool_result.get("ok") is True
            and tool_result.get("iso")
        ):
            result["timeContext"] = {
                "source": "get_system_time",
                "iso": tool_result["iso"],
                "display": tool_result.get("display", ""),
                "precision": "second",
                "toolCallId": tool_result.get("toolCallId", ""),
                "ok": True,
            }
    result["_nativeVisualInputIds"] = list(body.get("_nativeVisualInputIds") or [])
    snapshot = body.get("_turnContextSnapshot")
    if not isinstance(snapshot, Mapping) or not snapshot:
        return result
    if bind_cognition:
        services.projection.bind_cognitive_result(body, result)
    current = snapshot.get("currentUserMessage")
    current = current if isinstance(current, Mapping) else {}
    resolved = snapshot.get("resolvedAnchor")
    resolved = resolved if isinstance(resolved, Mapping) else {}
    public_anchor = {
        key: value for key, value in resolved.items() if key not in {"messages", "selectedText"}
    }
    metrics = result.setdefault("_chatMetrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
        result["_chatMetrics"] = metrics
    metrics.update(
        {
            "conversationEpoch": safe_nonnegative_int(snapshot.get("epoch")),
            "conversationId": str(snapshot.get("conversationId") or ""),
            "rootSnapshotId": str(snapshot.get("rootSnapshotId") or ""),
            "currentTurnId": str(snapshot.get("currentTurnId") or ""),
            "userMessageId": str(current.get("messageId") or ""),
            "replyAnchor": public_anchor,
        }
    )
    control = snapshot.get("_contextControl")
    if isinstance(control, Mapping):
        metrics["contextControl"] = deepcopy(dict(control))
    context_resolution = body.get("_contextResolution")
    context_resolution = (
        context_resolution if isinstance(context_resolution, Mapping) else {}
    )
    reference_resolution = context_resolution.get("referenceResolution")
    reference_resolution = (
        reference_resolution if isinstance(reference_resolution, Mapping) else {}
    )
    selected_message_ids = [
        str(item)
        for item in reference_resolution.get("antecedentMessageIds", [])
        if str(item)
    ]
    if not selected_message_ids:
        selected_message_ids = [
            str(item.get("messageId") or "")
            for item in context_resolution.get("referenceEvidence", [])
            if isinstance(item, Mapping) and str(item.get("messageId") or "")
        ]
    if not selected_message_ids and isinstance(control, Mapping):
        selected_message_ids = [
            str(item) for item in control.get("preferredMessageIds", []) if str(item)
        ]
    metrics["resolutionRecord"] = {
        "resolutionId": f"res_{str(snapshot.get('currentTurnId') or '')}_reference",
        "conversationId": str(snapshot.get("conversationId") or ""),
        "sourceTurnId": str(snapshot.get("currentTurnId") or ""),
        "kind": "reference_resolution",
        "selectedMessageIds": selected_message_ids,
        "alternativeMessageIds": [
            str(item.get("messageId") or "")
            for item in (
                (control or {}).get("recalledMessages", [])
                if isinstance(control, Mapping)
                else []
            )
            if isinstance(item, Mapping)
            and str(item.get("messageId") or "") not in selected_message_ids
        ],
        "method": (
            "explicit_user_context_control"
            if isinstance(control, Mapping) and control.get("directives")
            else "discourse_reference"
        ),
        "status": "active",
    }
    result["turnContextSnapshotId"] = str(snapshot.get("rootSnapshotId") or "")
    result["conversationId"] = str(snapshot.get("conversationId") or "")
    result["currentUserMessageId"] = str(current.get("messageId") or "")
    if public_anchor:
        result["replyAnchor"] = public_anchor
    return result


def frozen_turn_context_state(body: Mapping[str, Any]) -> dict[str, Any] | None:
    """返回控制规则处理后的冻结会话状态；没有控制结果时返回原快照。"""

    snapshot = body.get("_turnContextSnapshot")
    if not isinstance(snapshot, Mapping):
        return None
    context = snapshot.get("_resolvedContextState") or snapshot.get("_contextState")
    return context if isinstance(context, dict) else None


def frozen_turn_relationship_state(body: Mapping[str, Any]) -> dict[str, Any] | None:
    snapshot = body.get("_turnContextSnapshot")
    if not isinstance(snapshot, Mapping):
        return None
    relationship = snapshot.get("_relationshipState")
    return relationship if isinstance(relationship, dict) else None
