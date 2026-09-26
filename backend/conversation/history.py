"""会话历史目录、恢复选择与公开查询规则。

本模块只拥有历史读取和公开投影的连续规则；context epoch、archive 文件、
SQLite turn index 与主动联系状态仍由各自服务持有。所有入口都通过窄端口
读取，使旧的 storage scope 和测试替换点继续指向同一实际状态所有者。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Callable, Mapping

from agent.context_architecture import canonical_turn_id
from agent.fingerprints import runtime_fingerprint
from agent.turn_context import (
    conversation_id_for_epoch,
    legacy_message_id,
    message_content_hash,
)
from backend.common.values import safe_nonnegative_int
from backend.context.conversation import turn_conversation_epoch, turn_conversation_id
from backend.conversation.records import public_attachment_metadata


@dataclass(frozen=True)
class ConversationHistoryPorts:
    """历史服务需要的动态读取与纯投影规则。"""

    read_context: Callable[[], dict[str, Any]]
    read_sessions: Callable[[], dict[str, dict[str, Any]]]
    read_archive: Callable[[int], list[dict[str, Any]]]
    read_turn_records: Callable[[int], list[dict[str, Any]]]
    turn_record_count: Callable[[], int]
    normalize_proactive_control: Callable[[Any], dict[str, Any]]
    max_chat_attachments: int
    storage_health: Callable[[], dict[str, Any]]
    recoverable_recent: Callable[..., tuple[list[dict[str, Any]], str]]


class ConversationHistoryService:
    """拥有会话目录、恢复优先级与对外历史字段裁剪。"""

    def __init__(self, ports: ConversationHistoryPorts) -> None:
        self.ports = ports

    def session_summaries(self) -> dict[str, Any]:
        """List durable conversations without loading their text into a prompt."""

        context = self.ports.read_context()
        active_id = str(
            context.get("conversationId")
            or conversation_id_for_epoch(safe_nonnegative_int(context.get("conversationEpoch")))
        )
        snapshots = self.ports.read_sessions()
        grouped: dict[str, dict[str, Any]] = {
            conversation_id: {
                "conversationId": conversation_id,
                "conversationEpoch": safe_nonnegative_int(snapshot.get("conversationEpoch")),
                "title": "",
                "preview": "",
                "createdAt": str(snapshot.get("createdAt") or snapshot.get("updatedAt") or "")[:80],
                "updatedAt": str(snapshot.get("updatedAt") or "")[:80],
                "turnCount": 0,
            }
            for conversation_id, snapshot in snapshots.items()
        }
        for turn in self.ports.read_archive(0):
            conversation_id = turn_conversation_id(turn)
            if not conversation_id:
                continue
            entry = grouped.setdefault(conversation_id, {
                "conversationId": conversation_id,
                "conversationEpoch": turn_conversation_epoch(turn),
                "title": "",
                "preview": "",
                "createdAt": "",
                "updatedAt": "",
                "turnCount": 0,
            })
            at = str(turn.get("at") or "")[:80]
            user = str(turn.get("user") or "").strip()
            assistant = str(turn.get("assistant") or "").strip()
            entry["turnCount"] = int(entry["turnCount"]) + 1
            if at and not entry["createdAt"]:
                entry["createdAt"] = at
            if at:
                entry["updatedAt"] = at
            if user and not entry["title"]:
                entry["title"] = re.sub(r"\s+", " ", user).strip()[:80]
            latest_visible = assistant or user
            if latest_visible:
                entry["preview"] = re.sub(r"\s+", " ", latest_visible).strip()[:180]

        active = grouped.setdefault(active_id, {
            "conversationId": active_id,
            "conversationEpoch": safe_nonnegative_int(context.get("conversationEpoch")),
            "title": "新对话",
            "preview": "还没有消息",
            "createdAt": str(context.get("updatedAt") or "")[:80],
            "updatedAt": str(context.get("updatedAt") or "")[:80],
            "turnCount": 0,
        })
        active["active"] = True
        for conversation_id, entry in grouped.items():
            entry.setdefault("active", conversation_id == active_id)
            entry["active"] = conversation_id == active_id
            if not entry.get("title"):
                entry["title"] = "新对话"
            if not entry.get("preview"):
                entry["preview"] = "还没有消息"
        conversations = sorted(
            grouped.values(),
            key=lambda item: (
                str(item.get("updatedAt") or item.get("createdAt") or ""),
                safe_nonnegative_int(item.get("conversationEpoch")),
            ),
            reverse=True,
        )
        return {
            "ok": True,
            "activeConversationId": active_id,
            "conversations": conversations,
        }

    def storage_health(self) -> dict[str, Any]:
        archive_turns = self.ports.read_archive(0)
        indexed_turns = self.ports.read_turn_records(1)
        context = self.ports.read_context()
        recent = [item for item in context.get("recentTurns", []) if isinstance(item, dict)]
        latest_archive = archive_turns[-1] if archive_turns else {}
        latest_indexed = indexed_turns[-1] if indexed_turns else {}
        archive_count = len(archive_turns)
        indexed_count = self.ports.turn_record_count()
        suspicious_reset = bool(
            archive_count > 0
            and not recent
            and not indexed_turns
        )
        latest_mismatch = bool(
            latest_archive
            and latest_indexed
            and (
                str(latest_archive.get("at") or "") != str(latest_indexed.get("at") or "")
                or str(latest_archive.get("assistant") or "") != str(latest_indexed.get("assistant") or "")
            )
        )
        count_mismatch = bool(
            archive_count > 0
            and indexed_count != archive_count
        )
        return {
            "archiveTurnCount": archive_count,
            "indexedTurnCount": indexed_count,
            "recentTurnCount": len(recent),
            "latestArchiveAt": str(latest_archive.get("at") or ""),
            "latestIndexedAt": str(latest_indexed.get("at") or ""),
            "suspiciousReset": suspicious_reset,
            "countMismatch": count_mismatch,
            "latestMismatch": latest_mismatch,
            "recoveryRequired": bool(
                suspicious_reset
                or latest_mismatch
                or count_mismatch
            ),
        }

    def recoverable_recent(
        self,
        limit: int,
        *,
        include_inactive_epochs: bool = False,
        prefer_working_set: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        """Read the durable suffix or compacted prompt working set without mutation."""

        maximum = max(1, int(limit))
        context = self.ports.read_context()
        active_id = str(
            context.get("conversationId")
            or conversation_id_for_epoch(safe_nonnegative_int(context.get("conversationEpoch")))
        )

        def visible(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if include_inactive_epochs:
                return values
            return [item for item in values if turn_conversation_id(item) == active_id]

        context_turns = [
            item for item in context.get("recentTurns", []) if isinstance(item, dict)
        ]
        context_turns = visible(context_turns)[-maximum:]
        indexed_count = self.ports.turn_record_count()
        indexed_turns = visible(
            self.ports.read_turn_records(max(maximum, indexed_count))
        )[-maximum:]
        archive_turns = self.ports.read_archive(0)
        visible_archive_turns = visible(archive_turns)[-maximum:]
        if prefer_working_set and context_turns:
            latest_durable = (
                visible_archive_turns[-1]
                if visible_archive_turns and len(archive_turns) > indexed_count
                else indexed_turns[-1]
                if indexed_turns
                else visible_archive_turns[-1]
                if visible_archive_turns
                else None
            )
            if latest_durable is None or (
                canonical_turn_id(context_turns[-1])
                == canonical_turn_id(latest_durable)
            ):
                return context_turns, "conversation_working_set"
        if archive_turns and len(archive_turns) > indexed_count:
            return visible_archive_turns, "archive_readonly"
        if indexed_turns:
            return indexed_turns, "state_database"
        if context_turns:
            return context_turns, "legacy_context"
        if visible_archive_turns:
            return visible_archive_turns, "archive_readonly"
        return [], "empty"

    def public_history(
        self,
        limit: int = 120,
        *,
        include_inactive_epochs: bool = True,
        storage_health: Callable[[], dict[str, Any]] | None = None,
        recoverable_recent: Callable[..., tuple[list[dict[str, Any]], str]] | None = None,
    ) -> dict[str, Any]:
        """Return the server-owned transcript with bounded public metadata."""

        context = self.ports.read_context()
        health = (storage_health or self.storage_health)()
        # Read-only disaster recovery keeps history visible without replacing
        # any user file. Building a new index remains an explicit operation.
        turns, recovery_source = (recoverable_recent or self.recoverable_recent)(
            limit,
            include_inactive_epochs=include_inactive_epochs,
        )
        messages: list[dict[str, Any]] = []
        for turn in turns:
            at = str(turn.get("at") or "")[:80]
            user = str(turn.get("user") or "").strip()
            assistant = str(turn.get("assistant") or "").strip()
            turn_id = str(turn.get("turnId") or "")
            conversation_id = str(
                turn.get("conversationId")
                or conversation_id_for_epoch(turn_conversation_epoch(turn))
            )
            reply_anchor = (
                dict(turn.get("replyAnchor"))
                if isinstance(turn.get("replyAnchor"), dict) else {}
            )
            if user and str(turn.get("origin") or "") != "touch":
                user_meta: dict[str, Any] = {
                    "at": at,
                    "restored": True,
                    "contextEpoch": turn_conversation_epoch(turn),
                    "conversationId": conversation_id,
                    "turnId": turn_id,
                    "messageId": str(
                        turn.get("userMessageId")
                        or legacy_message_id(turn_id, "user")
                    ),
                    "contentHash": str(
                        turn.get("userContentHash") or message_content_hash(user)
                    ),
                }
                if reply_anchor:
                    user_meta["replyAnchor"] = reply_anchor
                if isinstance(turn.get("attachments"), list):
                    user_meta["attachments"] = [
                        public_attachment_metadata(item)
                        for item in turn["attachments"][: self.ports.max_chat_attachments]
                        if isinstance(item, dict) and str(item.get("id") or "").strip()
                    ]
                messages.append({"role": "user", "content": user, "meta": user_meta})
            if assistant:
                assistant_meta = {
                    "at": at,
                    "restored": True,
                    "contextEpoch": turn_conversation_epoch(turn),
                    "emotion": str(turn.get("emotion") or "neutral")[:40],
                    "toolUsed": str(turn.get("toolUsed") or "")[:160],
                    "toolStatus": str(turn.get("toolStatus") or "")[:40],
                    "conversationId": conversation_id,
                    "turnId": turn_id,
                    "messageId": str(
                        turn.get("assistantMessageId")
                        or legacy_message_id(turn_id, "assistant")
                    ),
                    "contentHash": str(
                        turn.get("assistantContentHash") or message_content_hash(assistant)
                    ),
                }
                if isinstance(turn.get("dialoguePolicy"), dict):
                    assistant_meta["dialoguePolicy"] = dict(turn["dialoguePolicy"])
                if isinstance(turn.get("dialogueValidation"), dict):
                    assistant_meta["dialogueValidation"] = dict(turn["dialogueValidation"])
                if isinstance(turn.get("actionReceipt"), dict):
                    receipt = turn["actionReceipt"]
                    assistant_meta["actionReceipt"] = {
                        key: receipt.get(key)
                        for key in (
                            "actionId", "tool", "status", "requestedTarget",
                            "resolvedTarget", "adapter", "error", "timestamp", "action",
                            "taskId", "taskVersionBefore", "taskVersionAfter",
                            "bookmarkId", "name", "versionBefore", "versionAfter", "requestId", "timeFacts", "timeSpec",
                        )
                        if receipt.get(key) not in (None, "")
                    }
                if isinstance(turn.get("actionReceipts"), list):
                    assistant_meta["actionReceipts"] = [
                        dict(receipt)
                        for receipt in turn["actionReceipts"][:12]
                        if isinstance(receipt, dict)
                    ]
                if isinstance(turn.get("todoContext"), dict):
                    assistant_meta["todoContext"] = dict(turn["todoContext"])
                if isinstance(turn.get("sources"), list):
                    assistant_meta["sources"] = [
                        {
                            "id": str(item.get("id") or "")[:20],
                            "title": str(item.get("title") or "")[:500],
                            "url": str(item.get("url") or "")[:2000],
                            "domain": str(item.get("domain") or "")[:200],
                        }
                        for item in turn["sources"][:8]
                        if isinstance(item, dict)
                        and re.match(r"^https?://", str(item.get("url") or "").strip(), re.I)
                    ]
                if str(turn.get("origin") or "") == "touch":
                    assistant_meta["kind"] = "touch"
                    assistant_meta["source"] = "touch"
                    assistant_meta["touchZone"] = str((turn.get("interactionEvent") or {}).get("zone") or "")
                if str(turn.get("origin") or "") == "proactive":
                    assistant_meta["kind"] = "proactive"
                    assistant_meta["source"] = "proactive"
                    assistant_meta["proactiveEventId"] = str(turn.get("proactiveEventId") or "")[:100]
                    assistant_meta["proactiveKind"] = str(turn.get("proactiveKind") or "")[:40]
                    assistant_meta["proactiveSubject"] = str(turn.get("proactiveSubject") or "")[:240]
                    assistant_meta["proactivePersisted"] = True
                messages.append({
                    "role": "assistant",
                    "content": assistant,
                    "meta": assistant_meta,
                })
        return {
            "ok": True,
            "archiveCount": max(
                int(context.get("archiveCount") or 0),
                int(health.get("archiveTurnCount") or 0),
            ),
            "updatedAt": str(context.get("updatedAt") or ""),
            "conversationEpoch": max(0, int(context.get("conversationEpoch") or 0)),
            "conversationId": str(
                context.get("conversationId")
                or conversation_id_for_epoch(int(context.get("conversationEpoch") or 0))
            ),
            "proactiveControl": self.ports.normalize_proactive_control(
                context.get("proactiveControl")
            ),
            "storageHealth": health,
            "waitingForUser": bool(context.get("waitingForUser") or context.get("pendingAssistantQuestion")),
            "recoveryRequired": bool(health.get("recoveryRequired")),
            "recoverySource": recovery_source,
            "compatibilityRecovery": context.get("compatibilityRecovery") or {},
            "messages": messages,
        }

    @staticmethod
    def normalize_search_text(value: Any) -> str:
        """Normalize user-visible transcript text without changing stored history."""

        return unicodedata.normalize("NFKC", str(value or "")).casefold()

    @staticmethod
    def search_result_id(turn: Mapping[str, Any]) -> str:
        stable_material = "\0".join([
            str(turn.get("at") or ""),
            str(turn.get("user") or ""),
            str(turn.get("assistant") or ""),
            str(turn.get("requestId") or turn.get("proactiveEventId") or ""),
        ])
        return "history_" + runtime_fingerprint(stable_material, length=24)

    def search(
        self,
        query: Any,
        *,
        role: str = "all",
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search the append-only archive without injecting it into model context."""

        raw_query = re.sub(r"\s+", " ", str(query or "")).strip()[:120]
        normalized_query = self.normalize_search_text(raw_query)
        selected_role = str(role or "all").strip().lower()
        if selected_role not in {"all", "user", "assistant"}:
            selected_role = "all"
        page_limit = max(1, min(50, int(limit or 30)))
        page_offset = max(0, min(20_000, int(offset or 0)))
        matches: list[dict[str, Any]] = []
        for turn in self.ports.read_archive(0):
            user = str(turn.get("user") or "").strip()
            assistant = str(turn.get("assistant") or "").strip()
            searchable = {
                "user": self.normalize_search_text(user),
                "assistant": self.normalize_search_text(assistant),
            }
            matched_roles = [
                item_role
                for item_role in ("user", "assistant")
                if (selected_role in {"all", item_role})
                and (
                    not normalized_query
                    or normalized_query in searchable[item_role]
                )
                and bool(user if item_role == "user" else assistant)
            ]
            if not matched_roles:
                continue
            matches.append({
                "id": self.search_result_id(turn),
                "at": str(turn.get("at") or "")[:80],
                "user": user[:12_000],
                "assistant": assistant[:12_000],
                "matchRoles": matched_roles,
                "origin": (
                    "proactive"
                    if str(turn.get("origin") or "") == "proactive"
                    else "chat"
                ),
            })
        matches.reverse()
        total = len(matches)
        page = matches[page_offset:page_offset + page_limit]
        return {
            "ok": True,
            "query": raw_query,
            "role": selected_role,
            "offset": page_offset,
            "limit": page_limit,
            "total": total,
            "hasMore": page_offset + len(page) < total,
            "results": page,
        }
