"""运行日志和测试诊断落盘，复用同一日志锁；正式模式不保存模型原始正文。"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
import json
import re
from agent.fingerprints import runtime_fingerprint

@dataclass(frozen=True)
class DiagnosticPorts:
    server_log: Callable[[], Path]
    network_log: Callable[[], Path]
    data_dir: Callable[[], Path]
    runtime_mode: Callable[[], str]
    finish_reason_context: Callable[[], Any]
    now: Callable[[], str]
    lock: Any

class DiagnosticLog:
    def __init__(self, ports: DiagnosticPorts) -> None:
        self.ports = ports

    def write_server_log(self, event: str, **details: Any) -> None:
        """Persist compact diagnostics without prompts, API keys, or response bodies."""

        record = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": str(event),
            **details,
        }
        try:
            self.ports.server_log().parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            with self.ports.lock:
                with self.ports.server_log().open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
        except OSError:
            pass


    def write_network_incident(self, **details: Any) -> None:
        """Persist one redacted upstream transport failure for field diagnosis."""

        record = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **details,
        }
        try:
            self.ports.network_log().parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            with self.ports.lock:
                with self.ports.network_log().open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
        except OSError:
            pass


    def record_cognitive_format_failure(self, body: dict[str, Any], content: str, diagnostic: Mapping[str, Any], *, request_id: str, secrets: tuple[str, ...] = ()) -> None:
        record = {"at": self.ports.now(), "requestId": request_id, "diagnostic": dict(diagnostic),
                  "characters": len(content), "contentFingerprint": runtime_fingerprint(content, length=24),
                  "finishReason": self.ports.finish_reason_context().get()}
        self.write_server_log("cognitive_json_parse_failed", **record)
        if self.ports.runtime_mode() != "test":
            return
        # One bounded local Test diagnostic, never prompts, credentials or image
        # bytes. Keep both failed generations instead of losing the first cause.
        safe = content
        for secret in secrets:
            if secret:
                safe = safe.replace(secret, "[REDACTED]")
        safe = re.sub(r"(?i)\bsk-[a-z0-9_-]+|Bearer\s+[a-z0-9._-]+", "[REDACTED]", safe)
        safe = re.sub(r"data:image/[^\s\"']+", "[IMAGE OMITTED]", safe)
        attempts = body.setdefault("_cognitiveFormatFailures", [])
        attempts.append({**record, "rawResponse": safe[:131072], "truncated": len(safe) > 131072})
        del attempts[:-4]
        try:
            path = self.ports.data_dir() / "logs" / "cognitive_format_failure_latest.json"
            with self.ports.lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"requestId": request_id, "attempts": attempts}, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
