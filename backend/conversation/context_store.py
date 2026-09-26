"""会话上下文持久化与生命周期服务。

本模块是 conversation_context.json、历史会话索引和上下文 epoch 的唯一
编排者。它不执行模型生成；旧格式恢复、SQLite pending projection、文件
存储和 turn/transient 清理通过窄端口接入，保证同一把 CONVERSATION_LOCK、
当前 storage scope 与原有清理顺序不被 HTTP 或聊天入口复制。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import os
import sqlite3
from typing import Any, Callable, Mapping

from agent.context_compatibility import (
    assert_writable as assert_context_writable,
    check_version as check_context_version,
    ContextCompatibilityError,
    load_context as load_compatible_context,
)
from agent.dialogue_waiting import refresh_waiting_context
from agent.discourse_reference import DiscourseLedger
from agent.turn_context import conversation_id_for_epoch
from backend.common.values import safe_nonnegative_int
from backend.context.conversation import turn_conversation_id
from backend.runtime.requests import RequestValidationError


@dataclass(frozen=True)
class ConversationContextPorts:
    """入口提供的动态边界；这些回调不复制上下文状态。"""

    lock: Any
    context_path: Callable[[], Path]
    archive_path: Callable[[], Path]
    sessions_path: Callable[[], Path]
    state_database_path: Callable[[], Path]
    pending_projection_payloads: Callable[..., list[dict[str, Any]]]
    legacy_reconstructor: Callable[[str, dict[str, Any]], str]
    write_log: Callable[..., Any]
    now_iso: Callable[[], str]
    now_utc: Callable[[], datetime]
    session_id: Callable[[], str]
    request_started_at_ms: Callable[[str], int | None]
    default_context: Callable[[], dict[str, Any]]
    normalize_proactive_control: Callable[[Any], dict[str, Any]]
    read_context: Callable[[], dict[str, Any]]
    write_context: Callable[[dict[str, Any]], dict[str, Any]]
    read_session_snapshots: Callable[[], dict[str, dict[str, Any]]]
    write_session_snapshots: Callable[[Mapping[str, Mapping[str, Any]]], None]
    save_session_snapshot: Callable[[Mapping[str, Any]], None]
    clear_turn_records: Callable[[], None]
    clear_transient_records: Callable[[], None]
    read_archive_turns: Callable[[int], list[dict[str, Any]]]
    recent_limit: int


class ConversationContextStore:
    """拥有上下文文件、会话索引与清空/切换 epoch 的连续责任。"""

    def __init__(self, ports: ConversationContextPorts) -> None:
        self.ports = ports

    def sessions_path(self) -> Path:
        """Expose the scoped session index path for the legacy entry point."""

        return self.ports.sessions_path()

    def default_context(self) -> dict[str, Any]:
        return {
            "version": 10,
            "updatedAt": self.ports.now_iso(),
            "archiveCount": 0,
            "conversationEpoch": 0,
            "conversationId": conversation_id_for_epoch(0),
            "conversationGeneration": 0,
            "relationshipGeneration": 0,
            "deliveryGeneration": 0,
            "conversationState": {
                "currentTopic": "",
                "recentTopicHistory": [],
                "pendingAssistantQuestionId": "",
                "unresolvedReferenceIds": [],
                "currentRelationshipContext": "",
                "currentContextEpoch": 0,
                "activeEpisodeId": "",
                "lastUserTurnId": "",
                "lastAssistantTurnId": "",
            },
            "contextPressure": {},
            "backgroundJobCircuits": {},
            "recentTurns": [],
            "summaries": [],
            "activeTask": {},
            "screenCaptures": [],
            "activeVisualArtifact": {},
            "pendingAssistantQuestion": {},
            "waitingForUser": False,
            "assistantQuestionHistory": [],
            "proactiveEventIds": [],
            "proactiveControl": {
                "status": "allowed",
                "resumeMode": "",
                "reason": "",
                "updatedAt": "",
                "sourceTurnId": "",
            },
            "sourceBatches": [],
            "pendingAction": {},
            "actionReceipts": [],
            "preparedTurns": [],
            "claimLedger": [],
            "researchReceipts": [],
            "deliveredTurnEvents": [],
            "referenceLedger": DiscourseLedger().to_dict(),
            "resolutionRecords": [],
            "contextDirectives": [],
            "correctionConstraints": [],
            "recallHints": [],
            "retrievalTraces": [],
        }

    def pending_projections(
            self, kind: str, *, conversation_id: str = "", latest_only: bool = False,
        ) -> list[dict[str, Any]]:
        """Read the committed fallback without initializing schemas on every read."""
        path = self.ports.state_database_path()
        if not path.exists():
            return []
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
        try:
            return self.ports.pending_projection_payloads(connection, kind=kind, conversation_id=conversation_id,
                                               latest_only=latest_only)
        finally:
            connection.close()

    def read_context(self) -> dict[str, Any]:
        path = self.ports.context_path()
        with self.ports.lock:
            value = load_compatible_context(path, self.ports.default_context(),
                invoke=self.ports.legacy_reconstructor, log=self.ports.write_log)
        epoch = safe_nonnegative_int(value.get("conversationEpoch"))
        conversation_id = str(value.get("conversationId") or conversation_id_for_epoch(epoch))
        pending = self.pending_projections(
            "conversation_context", conversation_id=conversation_id, latest_only=True,
        )
        candidate = pending[0].get("context") if pending else None
        if (isinstance(candidate, dict) and candidate.get("conversationId") == conversation_id
                and safe_nonnegative_int(candidate.get("conversationEpoch")) == epoch
                and safe_nonnegative_int(candidate.get("archiveCount")) > safe_nonnegative_int(value.get("archiveCount"))):
            check_context_version(candidate, self.ports.default_context()["version"])
            value = candidate
        defaults = self.ports.default_context()
        for key, fallback in defaults.items():
            value.setdefault(key, fallback)
        # In-memory migration is lossless; the next ordinary atomic context write
        # persists the current schema without a separate migration writer racing a
        # live append or clear operation.
        value["version"] = int(defaults["version"])
        value["recentTurns"] = value["recentTurns"] if isinstance(value.get("recentTurns"), list) else []
        value["conversationEpoch"] = max(0, int(value.get("conversationEpoch") or 0))
        value["conversationId"] = str(
            value.get("conversationId")
            or conversation_id_for_epoch(value["conversationEpoch"])
        )
        value["conversationGeneration"] = max(0, int(value.get("conversationGeneration") or 0))
        value["relationshipGeneration"] = max(0, int(value.get("relationshipGeneration") or 0))
        value["deliveryGeneration"] = max(0, int(value.get("deliveryGeneration") or 0))
        value["deliveredTurnEvents"] = (
            value["deliveredTurnEvents"]
            if isinstance(value.get("deliveredTurnEvents"), list) else []
        )
        value["conversationState"] = (
            value["conversationState"] if isinstance(value.get("conversationState"), dict) else {}
        )
        for key, fallback in defaults["conversationState"].items():
            value["conversationState"].setdefault(key, fallback)
        value["conversationState"]["currentContextEpoch"] = value["conversationEpoch"]
        value["contextPressure"] = (
            value["contextPressure"] if isinstance(value.get("contextPressure"), dict) else {}
        )
        value["backgroundJobCircuits"] = (
            value["backgroundJobCircuits"]
            if isinstance(value.get("backgroundJobCircuits"), dict) else {}
        )
        value["summaries"] = value["summaries"] if isinstance(value.get("summaries"), list) else []
        value["activeTask"] = value["activeTask"] if isinstance(value.get("activeTask"), dict) else {}
        value["screenCaptures"] = value["screenCaptures"] if isinstance(value.get("screenCaptures"), list) else []
        value["activeVisualArtifact"] = value["activeVisualArtifact"] if isinstance(value.get("activeVisualArtifact"), dict) else {}
        value["pendingAssistantQuestion"] = (
            value["pendingAssistantQuestion"]
            if isinstance(value.get("pendingAssistantQuestion"), dict)
            else {}
        )
        value["proactiveEventIds"] = (
            value["proactiveEventIds"]
            if isinstance(value.get("proactiveEventIds"), list)
            else []
        )
        legacy_question_wait = (value.get("proactiveControl") or {}).get("reason") == "unanswered_question_no_nudge"
        value["waitingForUser"] = value.get("waitingForUser") is True or legacy_question_wait
        if legacy_question_wait:
            value["proactiveControl"] = {"status": "allowed"}
        value["proactiveControl"] = self.ports.normalize_proactive_control(
            value.get("proactiveControl")
        )
        value["sourceBatches"] = (
            value["sourceBatches"]
            if isinstance(value.get("sourceBatches"), list)
            else []
        )
        value["pendingAction"] = (
            value["pendingAction"]
            if isinstance(value.get("pendingAction"), dict)
            else {}
        )
        value["actionReceipts"] = (
            value["actionReceipts"]
            if isinstance(value.get("actionReceipts"), list)
            else []
        )
        value["preparedTurns"] = (
            value["preparedTurns"]
            if isinstance(value.get("preparedTurns"), list)
            else []
        )
        value["claimLedger"] = (
            value["claimLedger"]
            if isinstance(value.get("claimLedger"), list)
            else []
        )
        value["researchReceipts"] = (
            value["researchReceipts"]
            if isinstance(value.get("researchReceipts"), list)
            else []
        )
        value["referenceLedger"] = DiscourseLedger.from_dict(
            value.get("referenceLedger")
            if isinstance(value.get("referenceLedger"), Mapping) else None
        ).to_dict()
        return refresh_waiting_context(value, now=self.ports.now_utc(), session_id=self.ports.session_id())

    def read_session_snapshots(self) -> dict[str, dict[str, Any]]:
        path = self.ports.sessions_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise ContextCompatibilityError("历史会话索引无法读取，已保留原件并停止覆盖；当前记录恢复不会清空其他会话。") from error
        check_context_version(value, 1)
        if not isinstance(value, dict) or not isinstance(value.get("sessions"), dict):
            raise ContextCompatibilityError("历史会话索引格式无法识别，原文件未修改。")
        sessions = value.get("sessions") if isinstance(value, dict) else {}
        return {
            str(key): dict(item)
            for key, item in (sessions.items() if isinstance(sessions, dict) else [])
            if str(key).strip() and isinstance(item, dict)
        }

    def write_session_snapshots(
            self, sessions: Mapping[str, Mapping[str, Any]],
        ) -> None:
        path = self.ports.sessions_path()
        self.ports.read_session_snapshots()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "version": 1,
            "updatedAt": self.ports.now_iso(),
            "sessions": {str(key): dict(value) for key, value in sessions.items()},
        }, ensure_ascii=False, indent=2)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)

    def save_session_snapshot(self, context: Mapping[str, Any]) -> None:
        conversation_id = str(context.get("conversationId") or "").strip()
        if not conversation_id:
            return
        sessions = self.ports.read_session_snapshots()
        sessions[conversation_id] = deepcopy(dict(context))
        self.ports.write_session_snapshots(sessions)

    def next_conversation_epoch(self, current: Mapping[str, Any] | None = None) -> int:
        return safe_nonnegative_int((current or {}).get("conversationEpoch")) + 1

    def write_context(self, context: dict[str, Any]) -> dict[str, Any]:
        path = self.ports.context_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        context["updatedAt"] = self.ports.now_iso()
        payload = json.dumps(context, ensure_ascii=False, indent=2)
        temporary = path.with_suffix(".tmp")
        with self.ports.lock:
            # Some background callers recover from read exceptions. Never let their
            # fallback empty context overwrite a file whose recovery has failed.
            assert_context_writable(path, self.ports.default_context())
            check_context_version(context, self.ports.default_context()["version"])
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
            self.ports.save_session_snapshot(context)
        return context

    def clear_context(self) -> dict[str, Any]:
        with self.ports.lock:
            current = self.ports.read_context()
            next_epoch = self.next_conversation_epoch(current)
            archive_path = self.ports.archive_path()
            try:
                archive_path.unlink()
            except FileNotFoundError:
                pass
            self.ports.clear_turn_records()
            self.ports.clear_transient_records()
            sessions_path = self.ports.sessions_path()
            try:
                sessions_path.unlink()
            except FileNotFoundError:
                pass
            cleared = self.ports.default_context()
            cleared["conversationEpoch"] = next_epoch
            cleared["conversationId"] = conversation_id_for_epoch(next_epoch)
            cleared["conversationGeneration"] = int(current.get("conversationGeneration") or 0) + 1
            cleared["conversationState"]["currentContextEpoch"] = next_epoch
            return self.ports.write_context(cleared)

    def clear_short_term(self) -> dict[str, Any]:
        with self.ports.lock:
            current = self.ports.read_context()
            self.ports.save_session_snapshot(current)
            cleared = self.ports.default_context()
            next_epoch = self.next_conversation_epoch(current)
            # Archive/SQLite remain the immutable audit bottom.  Prompt retrieval
            # is epoch-filtered, so old turns are visible only to explicit history.
            cleared["archiveCount"] = int(current.get("archiveCount") or 0)
            cleared["conversationEpoch"] = next_epoch
            cleared["conversationId"] = conversation_id_for_epoch(next_epoch)
            cleared["conversationGeneration"] = int(current.get("conversationGeneration") or 0) + 1
            cleared["relationshipGeneration"] = int(current.get("relationshipGeneration") or 0)
            cleared["conversationState"]["currentContextEpoch"] = next_epoch
            self.ports.clear_transient_records()
            return self.ports.write_context(cleared)

    def start_new_conversation(self) -> dict[str, Any]:
        """Start a clean context epoch while retaining global memory and history."""

        context = self.clear_short_term()
        self.ports.write_log(
            "new_conversation_started",
            conversationId=str(context.get("conversationId") or ""),
            conversationEpoch=safe_nonnegative_int(context.get("conversationEpoch")),
            retainedArchiveCount=safe_nonnegative_int(context.get("archiveCount")),
        )
        return context

    def switch_conversation(self, conversation_id: Any) -> dict[str, Any]:
        """Activate a durable conversation and rebuild its prompt working set."""

        requested_id = str(conversation_id or "").strip()[:180]
        if not requested_id:
            raise RequestValidationError("missing_conversation_id", "没有指定要打开的对话。")
        with self.ports.lock:
            current = self.ports.read_context()
            current_id = str(current.get("conversationId") or "")
            if requested_id == current_id:
                return current
            self.ports.save_session_snapshot(current)
            sessions = self.ports.read_session_snapshots()
            turns = [turn for turn in self.ports.read_archive_turns() if turn_conversation_id(turn) == requested_id]
            snapshot = sessions.get(requested_id)
            if not isinstance(snapshot, dict) and not turns:
                raise RequestValidationError("conversation_not_found", "这个历史对话不存在或还没有保存消息。")
            activation_epoch = self.next_conversation_epoch(current)
            restored = deepcopy(snapshot) if isinstance(snapshot, dict) else self.ports.default_context()
            defaults = self.ports.default_context()
            for key, fallback in defaults.items():
                restored.setdefault(key, deepcopy(fallback))
            if not isinstance(snapshot, dict):
                restored["recentTurns"] = turns[-self.ports.recent_limit:]
            restored["archiveCount"] = max(
                safe_nonnegative_int(current.get("archiveCount")),
                len(self.ports.read_archive_turns()),
            )
            restored["conversationEpoch"] = activation_epoch
            restored["conversationId"] = requested_id
            restored["conversationGeneration"] = safe_nonnegative_int(
                current.get("conversationGeneration")
            ) + 1
            restored["relationshipGeneration"] = safe_nonnegative_int(
                current.get("relationshipGeneration")
            )
            restored["preparedTurns"] = []
            restored["pendingAction"] = {}
            restored["backgroundJobCircuits"] = {}
            restored["conversationState"]["currentContextEpoch"] = activation_epoch
            self.ports.clear_transient_records()
            restored = self.ports.write_context(restored)
        self.ports.write_log(
            "conversation_switched",
            conversationId=requested_id,
            conversationEpoch=activation_epoch,
            restoredTurnCount=len(restored["recentTurns"]),
        )
        return restored

    def normalize_proactive_control(self, value: Any) -> dict[str, Any]:
        raw = value if isinstance(value, Mapping) else {}
        status = "paused" if str(raw.get("status") or "").strip().lower() == "paused" else "allowed"
        return {
            "status": status,
            "resumeMode": "model_decides" if status == "paused" else "",
            "reason": str(raw.get("reason") or "")[:160] if status == "paused" else "",
            "updatedAt": str(raw.get("updatedAt") or "")[:80],
            "sourceTurnId": str(raw.get("sourceTurnId") or "")[:120],
            "sourceUserMessage": str(raw.get("sourceUserMessage") or "")[:1200] if status == "paused" else "",
            "sourceRequestStartedAtMs": safe_nonnegative_int(raw.get("sourceRequestStartedAtMs"))
            or self.ports.request_started_at_ms(str(raw.get("sourceTurnId") or "")) or 0,
        }
