"""会话删除与代际替换的持久化顺序。

删除同时更新 session catalog、append-only archive、SQLite turn index 和当前
context。该事务边界继续由入口提供的同一把 CONVERSATION_LOCK 保护；本模块
不复制 context store，也不拥有关系、记忆或主动联系状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from backend.common.values import safe_nonnegative_int
from backend.runtime.requests import RequestValidationError


@dataclass(frozen=True)
class ConversationLifecyclePorts:
    """删除闭环所需的窄 I/O 端口。"""

    lock: Any
    read_context: Callable[[], dict[str, Any]]
    read_sessions: Callable[[], dict[str, dict[str, Any]]]
    write_sessions: Callable[[Mapping[str, Mapping[str, Any]]], None]
    read_archive: Callable[[int], list[dict[str, Any]]]
    replace_archive: Callable[[Sequence[Mapping[str, Any]]], None]
    delete_indexed: Callable[[str], int]
    turn_conversation_id: Callable[[Mapping[str, Any]], str]
    next_epoch: Callable[[Mapping[str, Any] | None], int]
    default_context: Callable[[], dict[str, Any]]
    conversation_id_for_epoch: Callable[[int], str]
    clear_transient: Callable[[], None]
    write_context: Callable[[dict[str, Any]], dict[str, Any]]
    catalog: Callable[[], dict[str, Any]]
    write_log: Callable[..., Any]


class ConversationLifecycleService:
    """拥有删除闭环，不拥有 context/session/archive 的底层存储。"""

    def __init__(self, ports: ConversationLifecyclePorts) -> None:
        self.ports = ports

    def delete(self, conversation_id: Any) -> dict[str, Any]:
        """Delete one conversation while retaining global memory and relationship state."""

        requested_id = str(conversation_id or "").strip()[:180]
        if not requested_id:
            raise RequestValidationError("missing_conversation_id", "没有指定要删除的对话。")
        with self.ports.lock:
            current = self.ports.read_context()
            current_id = str(
                current.get("conversationId")
                or self.ports.conversation_id_for_epoch(
                    safe_nonnegative_int(current.get("conversationEpoch"))
                )
            )
            sessions = self.ports.read_sessions()
            archived = self.ports.read_archive(0)
            deleted_turns = [
                turn for turn in archived
                if self.ports.turn_conversation_id(turn) == requested_id
            ]
            if (
                requested_id != current_id
                and requested_id not in sessions
                and not deleted_turns
            ):
                raise RequestValidationError(
                    "conversation_not_found",
                    "这个对话不存在，可能已经被删除。",
                )

            sessions.pop(requested_id, None)
            self.ports.write_sessions(sessions)
            retained_turns = [
                turn for turn in archived
                if self.ports.turn_conversation_id(turn) != requested_id
            ]
            self.ports.replace_archive(retained_turns)
            deleted_indexed_turns = self.ports.delete_indexed(requested_id)

            active_deleted = requested_id == current_id
            if active_deleted:
                next_epoch = self.ports.next_epoch(current)
                replacement = self.ports.default_context()
                replacement["archiveCount"] = len(retained_turns)
                replacement["conversationEpoch"] = next_epoch
                replacement["conversationId"] = self.ports.conversation_id_for_epoch(next_epoch)
                replacement["conversationGeneration"] = (
                    safe_nonnegative_int(current.get("conversationGeneration")) + 1
                )
                replacement["relationshipGeneration"] = safe_nonnegative_int(
                    current.get("relationshipGeneration")
                )
                replacement["conversationState"]["currentContextEpoch"] = next_epoch
                self.ports.clear_transient()
                active_context = self.ports.write_context(replacement)
            else:
                current["archiveCount"] = len(retained_turns)
                active_context = self.ports.write_context(current)

        catalog = self.ports.catalog()
        self.ports.write_log(
            "conversation_deleted",
            conversationId=requested_id,
            activeDeleted=active_deleted,
            deletedArchiveTurnCount=len(deleted_turns),
            deletedIndexedTurnCount=deleted_indexed_turns,
            replacementConversationId=str(active_context.get("conversationId") or ""),
        )
        return {
            "ok": True,
            "deletedConversationId": requested_id,
            "deletedTurnCount": max(len(deleted_turns), deleted_indexed_turns),
            "activeDeleted": active_deleted,
            "context": active_context,
            "activeConversationId": catalog.get("activeConversationId"),
            "conversations": catalog.get("conversations", []),
        }
