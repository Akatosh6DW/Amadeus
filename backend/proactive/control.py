"""主动联系许可的唯一状态转换：忽略过期请求，保留暂停原因和用户来源。

调用者传入当前会话；该服务不单独保存许可副本，也不因为进程重启自动恢复联系。
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping
import json
from backend.chat.reply_fields import normalize_proactive_directive
from backend.common.values import safe_nonnegative_int as _safe_nonnegative_int
from backend.common.text import unique_strings

@dataclass(frozen=True)
class ProactiveControlPorts:
    read_context: Callable[[], dict[str, Any]]
    normalize: Callable[..., dict[str, Any]]
    now: Callable[[], str]
    log: Callable[..., Any]

class ProactiveControl:
    def __init__(self, ports: ProactiveControlPorts) -> None:
        self.ports = ports

    def proactive_control_for_model(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Keep the active request visible even after its chat turn is compacted."""

        control = self.ports.normalize(context.get("proactiveControl"))
        if control["status"] == "paused" and not control["sourceUserMessage"]:
            for turn in reversed(context.get("recentTurns") or []):
                if isinstance(turn, Mapping) and str(turn.get("turnId") or "") == control["sourceTurnId"]:
                    control["sourceUserMessage"] = str(turn.get("user") or "")[:1200]
                    break
        return control


    def proactive_control_prompt(self, context: Mapping[str, Any] | None = None) -> str:
        active = context if isinstance(context, Mapping) else self.ports.read_context()
        return "[主动联系状态：由模型结合语境决定暂停或恢复]\n" + json.dumps(
            {"proactiveControl": self.proactive_control_for_model(active)}, ensure_ascii=False,
        )


    def apply_proactive_directive(
        self,
        context: dict[str, Any],
        directive: Any,
        *,
        user_initiated: bool,
        source_turn_id: str,
        request_started_at_ms: int | None = None,
        pause_reason: str = "user_requested_silence",
        source_user_message: str = "",
    ) -> dict[str, Any]:
        """Apply a semantic outreach decision to authoritative conversation state."""

        current = self.proactive_control_for_model(context)
        previous = dict(current)
        normalized_directive = normalize_proactive_directive(directive)
        pause_updated_at_ms = 0
        try:
            pause_updated_at_ms = int(
                datetime.fromisoformat(str(current.get("updatedAt") or "")).timestamp() * 1000
            )
        except (TypeError, ValueError):
            pause_updated_at_ms = 0
        current_started_at_ms = current["sourceRequestStartedAtMs"] or pause_updated_at_ms
        stale_user_turn = bool(
            user_initiated and request_started_at_ms is not None
            and current_started_at_ms > 0
            and int(request_started_at_ms) < current_started_at_ms
        )
        effective_directive = "unchanged" if stale_user_turn else normalized_directive
        if effective_directive in {"pause", "pause_until_next_user_message"}:
            normalized_pause_reason = (
                str(pause_reason or "").strip()
                if str(pause_reason or "").strip() in {
                    "user_requested_silence",
                    "model_inferred_unavailability",
                    "unanswered_sequence_complete",
                }
                else "user_requested_silence"
            )
            current = {
                "status": "paused",
                "resumeMode": "model_decides",
                "reason": normalized_pause_reason,
                "updatedAt": self.ports.now(),
                "sourceTurnId": str(source_turn_id or "")[:120],
                "sourceUserMessage": str(source_user_message or "")[:1200],
                "sourceRequestStartedAtMs": _safe_nonnegative_int(request_started_at_ms),
            }
        elif effective_directive == "resume":
            current = {
                "status": "allowed",
                "resumeMode": "",
                "reason": "",
                "updatedAt": self.ports.now(),
                "sourceTurnId": str(source_turn_id or "")[:120],
                "sourceUserMessage": "",
                "sourceRequestStartedAtMs": _safe_nonnegative_int(request_started_at_ms),
            }
        context["proactiveControl"] = current
        if current != previous or normalized_directive != "unchanged":
            self.ports.log(
                "proactive_control_transition",
                directive=normalized_directive,
                previousStatus=previous["status"],
                status=current["status"],
                resumeMode=current["resumeMode"],
                reason=current["reason"],
                sourceTurnId=str(source_turn_id or "")[:120],
                staleUserTurnIgnored=stale_user_turn,
            )
        return current


    def recent_proactive_texts(
        self,
        messages: list[dict[str, Any]] | None = None,
        supplied: list[Any] | None = None,
        limit: int = 18,
    ) -> list[str]:
        """Collect persisted and client-visible proactive speech for semantic de-duplication."""

        context = self.ports.read_context()
        values = [
            str(turn.get("assistant") or "").strip()
            for turn in context.get("recentTurns", [])
            if isinstance(turn, dict)
            and str(turn.get("origin") or "") == "proactive"
            and str(turn.get("assistant") or "").strip()
        ]
        values.extend(
            str(item.get("content") or "").strip()
            for item in (messages or [])
            if isinstance(item, dict)
            and item.get("role") == "assistant"
            and (
                str((item.get("meta") or {}).get("kind") or "") == "proactive"
                if isinstance(item.get("meta"), dict)
                else False
            )
            and str(item.get("content") or "").strip()
        )
        values.extend(str(item).strip() for item in (supplied or []) if str(item).strip())
        return unique_strings(values)[-max(1, limit):]
