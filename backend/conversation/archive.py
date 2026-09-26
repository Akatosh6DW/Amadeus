"""会话归档 JSONL 与恢复数据库的持久化边界。

本模块拥有 append-only archive 的读取、追加、显式替换和独立 recovery
database 重建。上下文 epoch、session snapshot 和清空/切换仍归
``context_store``；归档服务只通过端口取得同一把 CONVERSATION_LOCK、
pending outbox 与 turn index，避免生成第二份会话状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class ConversationArchivePorts:
    """归档文件、pending projection 与状态库的窄端口。"""

    lock: Any
    archive_path: Callable[[], Path]
    state_database_path: Callable[[], Path]
    state_database: Callable[[], Any]
    pending_projections: Callable[[str], list[dict[str, Any]]]
    write_turn_record: Callable[[dict[str, Any]], Any]
    read_archive: Callable[[int], list[dict[str, Any]]]
    turn_conversation_id: Callable[[Mapping[str, Any]], str]
    recovery_dir: Callable[[], Path]
    now_local: Callable[[], datetime]
    legacy_fingerprint: Callable[..., str]


class ConversationArchiveService:
    """拥有会话 archive 与恢复 DB 的 I/O 顺序，不拥有 context epoch。"""

    def __init__(self, ports: ConversationArchivePorts) -> None:
        self.ports = ports

    def append(
        self,
        turn: dict[str, Any],
        *,
        write_turn_record: bool = True,
    ) -> None:
        path = self.ports.archive_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(turn, ensure_ascii=False, separators=(",", ":")) + "\n"
        cognitive_commit_id = str(turn.get("cognitiveCommitId") or "").strip()
        with self.ports.lock:
            if cognitive_commit_id and path.exists():
                marker = f'"cognitiveCommitId":"{cognitive_commit_id}"'
                try:
                    with path.open("r", encoding="utf-8", newline="\n") as archive:
                        if any(marker in existing_line for existing_line in archive):
                            return
                except OSError:
                    # The append below will surface a real filesystem error; a
                    # read failure must never silently claim an already projected
                    # commit.
                    pass
            with path.open("a", encoding="utf-8", newline="\n") as archive:
                archive.write(line)
                archive.flush()
                os.fsync(archive.fileno())
        if write_turn_record:
            self.ports.write_turn_record(turn)

    def read(self, limit: int = 0) -> list[dict[str, Any]]:
        path = self.ports.archive_path()
        turns: list[dict[str, Any]] = []
        # An absent archive can be the first failed file projection after a
        # successful database commit. Its turn is still available in the outbox.
        with path.open("r", encoding="utf-8") if path.exists() else nullcontext(()) as archive:
            for line in archive:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(value, dict)
                    and str(value.get("at") or "").strip()
                    and (
                        str(value.get("user") or "").strip()
                        or str(value.get("assistant") or "").strip()
                    )
                ):
                    turns.append(value)
        pending = self.ports.pending_projections("conversation_archive")
        known = {
            str(item.get("turnId") or item.get("cognitiveCommitId") or "")
            for item in turns
        }
        for payload in pending:
            turn = payload.get("turn")
            ident = (
                str(turn.get("turnId") or turn.get("cognitiveCommitId") or "")
                if isinstance(turn, dict) else ""
            )
            if ident and ident not in known:
                turns.append(turn)
                known.add(ident)
        if pending:
            turns.sort(key=lambda item: str(item.get("at") or ""))
        return turns[-limit:] if limit > 0 else turns

    def replace(self, turns: Sequence[Mapping[str, Any]]) -> None:
        """Atomically replace the durable transcript after explicit deletion."""

        path = self.ports.archive_path()
        temporary = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not turns:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        with temporary.open("w", encoding="utf-8", newline="\n") as archive:
            for turn in turns:
                archive.write(json.dumps(dict(turn), ensure_ascii=False, separators=(",", ":")))
                archive.write("\n")
            archive.flush()
            os.fsync(archive.fileno())
        os.replace(temporary, path)

    def delete_indexed(self, conversation_id: str) -> int:
        """Delete only index rows whose stored metadata belongs to one conversation."""

        path = self.ports.state_database_path()
        if not path.exists():
            return 0
        deleted_ids: list[str] = []
        with self.ports.state_database() as connection:
            rows = connection.execute(
                "SELECT turn_id, metadata_json FROM conversation_turns WHERE session_id = 'default'"
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(str(row["metadata_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if (
                    isinstance(metadata, dict)
                    and self.ports.turn_conversation_id(metadata) == conversation_id
                ):
                    deleted_ids.append(str(row["turn_id"]))
            if deleted_ids:
                connection.executemany(
                    "DELETE FROM conversation_turns WHERE turn_id = ?",
                    [(turn_id,) for turn_id in deleted_ids],
                )
        return len(deleted_ids)

    def rebuild(self, output_path: Path) -> dict[str, Any]:
        """Build a separate recovery database; never replace the active store."""

        output = output_path.resolve()
        active = self.ports.state_database_path().resolve()
        if output == active:
            raise ValueError("Recovery output must not replace the active state database.")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise FileExistsError(str(output))
        turns = self.ports.read_archive(0)
        connection = sqlite3.connect(output)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                """
            CREATE TABLE conversation_turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
            )
            written = 0
            seen: set[str] = set()
            for turn in turns:
                stable_material = "\0".join([
                    str(turn.get("at") or ""),
                    str(turn.get("user") or ""),
                    str(turn.get("assistant") or ""),
                    str(turn.get("proactiveEventId") or ""),
                ])
                turn_id = str(
                    turn.get("turnId")
                    or "turn_" + self.ports.legacy_fingerprint(
                        stable_material, length=24
                    )
                )
                if turn_id in seen:
                    continue
                seen.add(turn_id)
                metadata = {
                    key: value for key, value in turn.items()
                    if key not in {"user", "assistant"}
                }
                connection.execute(
                    """
                INSERT INTO conversation_turns(
                    turn_id, session_id, created_at, user_text, assistant_text, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        turn_id,
                        str(turn.get("sessionId") or "default"),
                        str(turn.get("at") or ""),
                        str(turn.get("user") or ""),
                        str(turn.get("assistant") or ""),
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                written += 1
            connection.commit()
        finally:
            connection.close()
        return {
            "ok": True,
            "outputPath": str(output),
            "archiveTurnCount": len(turns),
            "writtenTurnCount": written,
            "activeDatabaseUntouched": not active.exists() or output != active,
        }

    def build_recovery_candidate(self) -> dict[str, Any]:
        recovery_dir = self.ports.recovery_dir()
        token = self.ports.now_local().strftime("%Y%m%d-%H%M%S-%f")
        output = recovery_dir / f"amadeus_state.rebuilt.{token}.sqlite3"
        return self.rebuild(output)
