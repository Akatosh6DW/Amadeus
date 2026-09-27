"""开发者独立记录器：按原顺序追加已提交轮次，Release 始终关闭。

记录目录不属于聊天/记忆清除范围；锁在此唯一创建，路径由运行资料作用域提供。
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
import hmac
import json
import os
import threading
from backend.common.values import safe_nonnegative_int as _safe_nonnegative_int

LOCK = threading.RLock()

@dataclass(frozen=True)
class RecorderPorts:
    release_mode: Callable[[], bool]
    conversation_lock: Callable[[], Any]
    private_dir: Callable[[], Path]
    state_path: Callable[[], Path]
    transcript_path: Callable[[], Path]
    read_archive: Callable[..., Any]
    now: Callable[[], str]
    log: Callable[..., Any]

class SalieriRecorder:
    def __init__(self, ports: RecorderPorts) -> None:
        self.ports = ports
        self.lock = LOCK

    def read_salieri_state(self) -> dict[str, Any]:
        """Read the recorder switch from its private, non-memory storage."""

        if self.ports.release_mode():
            return {
                "schemaVersion": 1,
                "enabled": False,
                "enabledAt": "",
                "backfilledTurns": 0,
            }
        with self.lock:
            try:
                value = json.loads(self.ports.state_path().read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
                value = {}
            return {
                "schemaVersion": 1,
                "enabled": bool(value.get("enabled")),
                "enabledAt": str(value.get("enabledAt") or "")[:80],
                "backfilledTurns": _safe_nonnegative_int(value.get("backfilledTurns")),
            }


    def salieri_enabled(self) -> bool:
        return bool(self.read_salieri_state().get("enabled"))


    def format_salieri_turn(self, turn: Mapping[str, Any]) -> str:
        timestamp = str(turn.get("at") or self.ports.now()).strip()[:80]
        turn_id = str(turn.get("turnId") or turn.get("requestId") or "").strip()[:180]
        origin = str(turn.get("origin") or "chat").strip()[:40]
        user_text = str(turn.get("user") or "").replace("\x00", "").strip()
        assistant_text = str(turn.get("assistant") or "").replace("\x00", "").strip()
        blocks = [f"[{timestamp}] [TURN {turn_id or '-'}] [来源: {origin or 'chat'}]"]
        if user_text:
            blocks.extend(["用户:", user_text])
        if assistant_text:
            blocks.extend(["Amadeus:", assistant_text])
        return "\n".join(blocks) + "\n\n"


    def append_salieri_turn(self, turn: Mapping[str, Any]) -> bool:
        """Append one finalized dialogue turn without consulting memory state."""

        if not self.salieri_enabled():
            return False
        text = self.format_salieri_turn(turn)
        if not text.strip():
            return False
        with self.lock:
            self.ports.private_dir().mkdir(parents=True, exist_ok=True)
            with self.ports.transcript_path().open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        return True


    def record_salieri_turn_safely(self, turn: Mapping[str, Any]) -> bool:
        try:
            return self.append_salieri_turn(turn)
        except Exception as error:
            self.ports.log(
                "salieri_transcript_append_failed",
                turnId=str(turn.get("turnId") or turn.get("requestId") or ""),
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
            return False


    def enable_salieri_recorder(self, code: Any) -> dict[str, Any]:
        """Unlock an append-only transcript and backfill the surviving archive once."""

        if self.ports.release_mode():
            return {
                "ok": False,
                "enabled": False,
                "error": "not_available_in_release",
            }
        supplied = str(code or "").strip().casefold()
        if not hmac.compare_digest(supplied, "salieri"):
            return {"ok": False, "enabled": False, "error": "access_denied"}
        with self.ports.conversation_lock():
            with self.lock:
                state = self.read_salieri_state()
                if not state.get("enabled"):
                    self.ports.private_dir().mkdir(parents=True, exist_ok=True)
                    archive_turns = self.ports.read_archive()
                    if not self.ports.transcript_path().exists():
                        header = "Amadeus / SALIERI DIALOGUE RECORD\n"
                        header += f"记录启用时间: {self.ports.now()}\n"
                        header += "此文件独立于聊天、短期记忆和长期记忆的清除操作。\n\n"
                        with self.ports.transcript_path().open(
                            "w", encoding="utf-8", newline="\n"
                        ) as stream:
                            stream.write(header)
                            for turn in archive_turns:
                                stream.write(self.format_salieri_turn(turn))
                            stream.flush()
                            os.fsync(stream.fileno())
                    state = {
                        "schemaVersion": 1,
                        "enabled": True,
                        "enabledAt": self.ports.now(),
                        "backfilledTurns": len(archive_turns),
                    }
                    temporary = self.ports.state_path().with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    os.replace(temporary, self.ports.state_path())
        return self.salieri_transcript_payload()


    def salieri_transcript_payload(self, max_bytes: int = 262_144) -> dict[str, Any]:
        if self.ports.release_mode():
            return {
                "ok": False,
                "enabled": False,
                "error": "not_available_in_release",
            }
        state = self.read_salieri_state()
        payload: dict[str, Any] = {"ok": True, **state}
        if not state.get("enabled"):
            return payload
        with self.lock:
            try:
                size = self.ports.transcript_path().stat().st_size
                limit = max(8_192, min(int(max_bytes), 1_048_576))
                with self.ports.transcript_path().open("rb") as stream:
                    start = max(0, size - limit)
                    stream.seek(start)
                    raw = stream.read(limit)
                payload.update({
                    "content": raw.decode("utf-8", errors="replace"),
                    "truncated": start > 0,
                    "fileBytes": size,
                    "updatedAt": datetime.fromtimestamp(
                        self.ports.transcript_path().stat().st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                    "filePath": str(self.ports.transcript_path()),
                })
            except FileNotFoundError:
                payload.update({
                    "content": "",
                    "truncated": False,
                    "fileBytes": 0,
                    "updatedAt": "",
                    "filePath": str(self.ports.transcript_path()),
                })
        return payload
