"""会话 turn 的认知事务提交边界。

本模块只负责把 reducer 状态、会话 turn 行、pending 失效和投影待办放进
同一个 SQLite 事务。会话工作集和摘要仍由 :mod:`backend.conversation.writer`
拥有；存储连接、记忆清除代际和 turn 行写入由入口通过窄端口提供。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ContextManager, Mapping, Protocol

from agent.cognitive_commit import CognitiveCommitError, commit_turn


class CognitiveCommitStore(Protocol):
    """认知事务实际需要的存储能力。"""

    def memory_generation(self) -> int: ...

    def state_database(self) -> ContextManager[Any]: ...

    def write_turn_record(
        self,
        turn: dict[str, Any],
        *,
        connection: Any,
    ) -> None: ...


@dataclass(frozen=True)
class CognitivePersistenceServices:
    store: CognitiveCommitStore


def clear_pending_from_context(
    context: dict[str, Any],
    overlay: Mapping[str, Any] | None,
) -> None:
    """仅在本轮明确失效当前 action 时清除 pending。"""

    invalidated_pending_ids = {
        str(item).strip()
        for item in (
            overlay.get("invalidatedPendingActionIds")
            if isinstance(overlay, Mapping) else []
        )
        if str(item).strip()
    }
    pending_in_context = context.get("pendingAction")
    if (
        invalidated_pending_ids
        and isinstance(pending_in_context, Mapping)
        and str(pending_in_context.get("actionId") or "") in invalidated_pending_ids
    ):
        context["pendingAction"] = {}


def commit_turn_to_sqlite(
    services: CognitivePersistenceServices,
    turn: dict[str, Any],
    result: dict[str, Any],
    overlay: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
) -> Any:
    """原子提交 reducer、turn 行、pending 失效和投影待办。"""

    projection_payloads = result.get("_cognitiveProjectionPayloads")
    projection_payloads = (
        dict(projection_payloads)
        if isinstance(projection_payloads, Mapping) else {}
    )
    projection_payloads["conversation_context"] = {
        "context": dict(context),
    }
    user_adaptation_snapshot = None
    if any(
        patch.get("operation") in {
            "PROPOSE_USER_INTERACTION_PREFERENCE",
            "PROPOSE_USER_EXPERIENCE",
            "PROMOTE_USER_ADAPTATION",
            "REVOKE_USER_ADAPTATION",
        }
        for patch in overlay.get("patches", [])
        if isinstance(patch, Mapping)
    ) or any(
        record.get("revokedByTurnId") == overlay.get("turnId")
        for record in (
            overlay.get("state", {})
            .get("preferences", {})
            .get("userAdaptations") or []
        )
        if isinstance(record, Mapping)
    ):
        preferences = overlay["state"]["preferences"]
        user_adaptation_snapshot = {
            "scopeId": preferences["userAdaptationScopeId"],
            "memoryGeneration": preferences["userAdaptationGeneration"],
            "revision": preferences["userAdaptationRevision"],
            "records": preferences.get("userAdaptations", []),
        }
        if (
            services.store.memory_generation()
            != user_adaptation_snapshot["memoryGeneration"]
        ):
            raise CognitiveCommitError(
                "user memory was cleared after the adaptation snapshot"
            )
    with services.store.state_database() as connection:
        committed = commit_turn(
            connection,
            turn=turn,
            overlay=overlay,
            digest=(
                result.get("_cognitiveDigest")
                if isinstance(result.get("_cognitiveDigest"), Mapping) else {}
            ),
            trace=(
                result.get("_cognitiveTrace")
                if isinstance(result.get("_cognitiveTrace"), Mapping) else {}
            ),
            projection_payloads=projection_payloads,
            user_adaptation_snapshot=user_adaptation_snapshot,
            turn_writer=lambda persisted_turn, active_connection: (
                services.store.write_turn_record(
                    dict(persisted_turn),
                    connection=active_connection,
                )
            ),
        )
        if (
            user_adaptation_snapshot is not None
            and services.store.memory_generation()
            != user_adaptation_snapshot["memoryGeneration"]
        ):
            raise CognitiveCommitError(
                "user memory was cleared during adaptation commit"
            )
    turn.update({
        "cognitiveCommitId": committed.commit_id,
        "cognitiveStateRevision": committed.state_revision,
        "cognitiveDigestRef": committed.commit_id,
        "cognitiveTraceRef": (
            committed.commit_id if result.get("_cognitiveTrace") else ""
        ),
    })
    result["_cognitiveCommitId"] = committed.commit_id
    result["_cognitiveStateRevision"] = committed.state_revision
    return committed
