"""Provider-independent lifecycle and result envelopes for one dialogue turn.

The objects in this module deliberately do not know about HTTP, DeepSeek,
Live2D, or a concrete tool executor.  They are the shared contract used by the
legacy server while the monolithic request path is migrated incrementally.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Any, Mapping, Sequence


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TurnPhase(str, Enum):
    RECEIVED = "RECEIVED"
    PREPARING = "PREPARING"
    PREPARED = "PREPARED"
    WAITING_MODEL = "WAITING_MODEL"
    PARSING_MODEL_OUTPUT = "PARSING_MODEL_OUTPUT"
    RECOVERING_MODEL_OUTPUT = "RECOVERING_MODEL_OUTPUT"
    REPAIRING_FIELDS = "REPAIRING_FIELDS"
    VALIDATING_REPLY = "VALIDATING_REPLY"
    WAITING_TOOL = "WAITING_TOOL"
    WAITING_PERMISSION = "WAITING_PERMISSION"
    VALIDATING = "VALIDATING"
    WAITING_TTS = "WAITING_TTS"
    READY_TO_DELIVER = "READY_TO_DELIVER"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INDETERMINATE = "INDETERMINATE"
    PARTIAL = "PARTIAL"


class TerminalReason(str, Enum):
    END_TURN = "end_turn"
    CLARIFICATION_REQUIRED = "clarification_required"
    PERMISSION_REQUIRED = "permission_required"
    USER_CANCELLED = "user_cancelled"
    MAX_TOOL_ROUNDS = "max_tool_rounds"
    NO_PROGRESS = "no_progress"
    PROMPT_TOO_LONG = "prompt_too_long"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    UPSTREAM_FAILURE = "upstream_failure"
    DELIVERY_FAILURE = "delivery_failure"
    CRASH_RECOVERY_INDETERMINATE = "crash_recovery_indeterminate"


TERMINAL_PHASES = frozenset({
    TurnPhase.COMPLETED,
    TurnPhase.FAILED,
    TurnPhase.CANCELLED,
    TurnPhase.INDETERMINATE,
    TurnPhase.PARTIAL,
})


_ALLOWED_TRANSITIONS: dict[TurnPhase, frozenset[TurnPhase]] = {
    TurnPhase.RECEIVED: frozenset({
        TurnPhase.PREPARING,
        TurnPhase.PREPARED,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.PREPARING: frozenset({
        TurnPhase.PREPARED,
        TurnPhase.WAITING_MODEL,
        TurnPhase.WAITING_TOOL,
        TurnPhase.WAITING_PERMISSION,
        TurnPhase.VALIDATING_REPLY,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.PREPARED: frozenset({
        TurnPhase.WAITING_MODEL,
        TurnPhase.WAITING_TOOL,
        TurnPhase.WAITING_PERMISSION,
        TurnPhase.VALIDATING,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.WAITING_MODEL: frozenset({
        TurnPhase.PARSING_MODEL_OUTPUT,
        TurnPhase.WAITING_TOOL,
        TurnPhase.WAITING_PERMISSION,
        TurnPhase.VALIDATING,
        TurnPhase.VALIDATING_REPLY,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.PARSING_MODEL_OUTPUT: frozenset({
        TurnPhase.RECOVERING_MODEL_OUTPUT,
        TurnPhase.REPAIRING_FIELDS,
        TurnPhase.VALIDATING_REPLY,
        TurnPhase.WAITING_MODEL,
        TurnPhase.WAITING_TOOL,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.RECOVERING_MODEL_OUTPUT: frozenset({
        TurnPhase.REPAIRING_FIELDS,
        TurnPhase.VALIDATING_REPLY,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.REPAIRING_FIELDS: frozenset({
        TurnPhase.VALIDATING_REPLY,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.WAITING_TOOL: frozenset({
        TurnPhase.WAITING_MODEL,
        TurnPhase.WAITING_PERMISSION,
        TurnPhase.VALIDATING,
        TurnPhase.VALIDATING_REPLY,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.WAITING_PERMISSION: frozenset({
        TurnPhase.WAITING_TOOL,
        TurnPhase.WAITING_MODEL,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.VALIDATING: frozenset({
        TurnPhase.WAITING_MODEL,
        TurnPhase.WAITING_TOOL,
        TurnPhase.READY_TO_DELIVER,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.VALIDATING_REPLY: frozenset({
        TurnPhase.WAITING_MODEL,
        TurnPhase.WAITING_TOOL,
        TurnPhase.WAITING_TTS,
        TurnPhase.READY_TO_DELIVER,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.WAITING_TTS: frozenset({
        TurnPhase.READY_TO_DELIVER,
        TurnPhase.DELIVERING,
        TurnPhase.PARTIAL,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.READY_TO_DELIVER: frozenset({
        TurnPhase.DELIVERING,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
    }),
    TurnPhase.DELIVERING: frozenset({
        TurnPhase.COMPLETED,
        TurnPhase.CANCELLED,
        TurnPhase.FAILED,
        TurnPhase.INDETERMINATE,
        TurnPhase.PARTIAL,
    }),
    TurnPhase.COMPLETED: frozenset(),
    TurnPhase.FAILED: frozenset(),
    TurnPhase.CANCELLED: frozenset(),
    TurnPhase.INDETERMINATE: frozenset(),
    TurnPhase.PARTIAL: frozenset(),
}


@dataclass(frozen=True)
class TurnEvent:
    sequence: int
    phase: TurnPhase
    at: str
    reason: str = ""
    inferred: bool = False

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["phase"] = self.phase.value
        return value


@dataclass
class TurnState:
    """Single-owner state machine with an append-only transition history."""

    request_id: str
    turn_id: str
    phase: TurnPhase = TurnPhase.RECEIVED
    terminal_reason: TerminalReason | None = None
    model_rounds: int = 0
    tool_rounds: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    events: list[TurnEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.events:
            self.events.append(TurnEvent(1, self.phase, self.created_at, "request_claimed"))

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def transition(
        self,
        phase: TurnPhase | str,
        *,
        reason: str = "",
        terminal_reason: TerminalReason | str | None = None,
        inferred: bool = False,
    ) -> None:
        target = phase if isinstance(phase, TurnPhase) else TurnPhase(str(phase))
        if target == self.phase:
            return
        if self.terminal:
            raise ValueError(f"terminal turn cannot transition: {self.phase.value} -> {target.value}")
        if target not in _ALLOWED_TRANSITIONS[self.phase]:
            raise ValueError(f"invalid turn transition: {self.phase.value} -> {target.value}")
        parsed_terminal: TerminalReason | None = None
        if terminal_reason is not None:
            parsed_terminal = (
                terminal_reason
                if isinstance(terminal_reason, TerminalReason)
                else TerminalReason(str(terminal_reason))
            )
        if target in TERMINAL_PHASES and parsed_terminal is None:
            raise ValueError("terminal transition requires terminal_reason")
        if target not in TERMINAL_PHASES and parsed_terminal is not None:
            raise ValueError("non-terminal transition cannot set terminal_reason")
        self.phase = target
        self.terminal_reason = parsed_terminal
        self.updated_at = utc_now_iso()
        self.events.append(TurnEvent(
            len(self.events) + 1,
            target,
            self.updated_at,
            str(reason or "")[:240],
            inferred,
        ))

    def advance_to(self, phase: TurnPhase | str, *, reason: str = "") -> None:
        """Advance through legal bridge states when a legacy caller is coarse.

        A bridge is recorded as inferred, so diagnostics can distinguish an
        observed lifecycle event from compatibility migration plumbing.
        """

        target = phase if isinstance(phase, TurnPhase) else TurnPhase(str(phase))
        if target == self.phase:
            return
        if target in TERMINAL_PHASES:
            raise ValueError("use finish() for terminal phases")
        path = _shortest_transition_path(self.phase, target)
        if not path:
            raise ValueError(f"no legal turn path: {self.phase.value} -> {target.value}")
        for index, item in enumerate(path):
            self.transition(
                item,
                reason=reason if index == len(path) - 1 else f"bridge_to:{target.value}",
                inferred=index != len(path) - 1,
            )

    def note_model_round(self) -> None:
        self.model_rounds += 1

    def note_tool_round(self) -> None:
        self.tool_rounds += 1

    def finish(
        self,
        phase: TurnPhase | str,
        terminal_reason: TerminalReason | str,
        *,
        reason: str = "",
    ) -> None:
        target = phase if isinstance(phase, TurnPhase) else TurnPhase(str(phase))
        if target not in TERMINAL_PHASES:
            raise ValueError("finish target must be terminal")
        if self.terminal:
            if self.phase == target:
                return
            raise ValueError(f"turn already terminal: {self.phase.value}")
        bridge = {
            TurnPhase.COMPLETED: TurnPhase.DELIVERING,
            TurnPhase.INDETERMINATE: TurnPhase.DELIVERING,
            TurnPhase.PARTIAL: TurnPhase.DELIVERING,
        }.get(target)
        if bridge is not None and self.phase != bridge:
            self.advance_to(bridge, reason=f"finish_bridge:{target.value}")
        self.transition(target, reason=reason, terminal_reason=terminal_reason)

    def to_dict(self) -> dict[str, object]:
        return {
            "requestId": self.request_id,
            "turnId": self.turn_id,
            "phase": self.phase.value,
            "terminal": self.terminal,
            "terminalReason": self.terminal_reason.value if self.terminal_reason else "",
            "modelRounds": self.model_rounds,
            "toolRounds": self.tool_rounds,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "events": [item.to_dict() for item in self.events],
        }


def _shortest_transition_path(start: TurnPhase, target: TurnPhase) -> list[TurnPhase]:
    queue: deque[tuple[TurnPhase, list[TurnPhase]]] = deque([(start, [])])
    visited = {start}
    while queue:
        current, path = queue.popleft()
        for next_phase in _ALLOWED_TRANSITIONS[current]:
            if next_phase in TERMINAL_PHASES or next_phase in visited:
                continue
            next_path = [*path, next_phase]
            if next_phase == target:
                return next_path
            visited.add(next_phase)
            queue.append((next_phase, next_path))
    return []


_PROGRESS_PHASES: tuple[tuple[tuple[str, ...], TurnPhase], ...] = (
    (("permission",), TurnPhase.WAITING_PERMISSION),
    (("semantic-recovery", "protocol-recovery"), TurnPhase.RECOVERING_MODEL_OUTPUT),
    (("field-repair", "repairing-fields"), TurnPhase.REPAIRING_FIELDS),
    (("model-output-parsing", "semantic-parsing", "finalizing"), TurnPhase.PARSING_MODEL_OUTPUT),
    (("tool-executing", "researching", "web-searching"), TurnPhase.WAITING_TOOL),
    (("contract-validat", "postprocess"), TurnPhase.VALIDATING_REPLY),
    (("validat", "ground", "audit"), TurnPhase.VALIDATING),
    (("tts", "audio-prepar"), TurnPhase.WAITING_TTS),
    (("deliver", "audio-playing"), TurnPhase.DELIVERING),
    (("model", "generat", "alignment", "rewrite", "synthes", "repairing"), TurnPhase.WAITING_MODEL),
    (("prepare", "context", "classif", "route", "understand", "attachment"), TurnPhase.PREPARING),
)


def phase_for_progress_stage(stage: str) -> TurnPhase | None:
    normalized = str(stage or "").strip().lower()
    if normalized == "received":
        return TurnPhase.RECEIVED
    for fragments, phase in _PROGRESS_PHASES:
        if any(fragment in normalized for fragment in fragments):
            return phase
    return None


@dataclass(frozen=True)
class ToolResultEnvelope:
    call_id: str
    turn_id: str
    tool_id: str
    status: str
    started_at: str
    completed_at: str
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]
    error: Mapping[str, Any] | None = None

    @classmethod
    def from_legacy_result(
        cls,
        result: Mapping[str, Any],
        *,
        turn_id: str,
        started_at: str | None = None,
        completed_at: str | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> "ToolResultEnvelope":
        ok = bool(result.get("ok"))
        error_name = str(result.get("error") or "")
        status = "success" if ok else (
            "permission_denied" if error_name == "permission_denied" else "error"
        )
        payload = {
            str(key): value
            for key, value in result.items()
            if key not in {"name", "ok", "toolCallId", "callId", "_toolResultEnvelope"}
        }
        error = None if ok else {
            "code": error_name or "tool_failed",
            "message": str(result.get("message") or result.get("reason") or "")[:1000],
            "retryable": bool(result.get("retryable")),
        }
        return cls(
            call_id=str(result.get("toolCallId") or result.get("callId") or ""),
            turn_id=str(turn_id or ""),
            tool_id=str(result.get("name") or ""),
            status=status,
            started_at=started_at or utc_now_iso(),
            completed_at=completed_at or utc_now_iso(),
            payload=payload,
            provenance=dict(provenance or {}),
            error=error,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "callId": self.call_id,
            "turnId": self.turn_id,
            "toolId": self.tool_id,
            "status": self.status,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "payload": dict(self.payload),
            "provenance": dict(self.provenance),
            "error": dict(self.error) if self.error else None,
        }


@dataclass(frozen=True)
class MessageSnapshot:
    revision: int
    messages: tuple[Mapping[str, Any], ...]
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "createdAt": self.created_at,
            "messages": [dict(item) for item in self.messages],
        }


class DoubleBufferedConversation:
    """Keep the active prompt immutable while the next message set is staged."""

    def __init__(self, messages: Sequence[Mapping[str, Any]] = ()) -> None:
        self._lock = threading.RLock()
        self._revision = 0
        self._active = MessageSnapshot(0, tuple(dict(item) for item in messages))
        self._staged: MessageSnapshot | None = None

    def active(self) -> MessageSnapshot:
        with self._lock:
            return self._active

    def stage(self, messages: Sequence[Mapping[str, Any]]) -> MessageSnapshot:
        with self._lock:
            self._revision += 1
            self._staged = MessageSnapshot(
                self._revision,
                tuple(dict(item) for item in messages),
            )
            return self._staged

    def commit(self, revision: int) -> MessageSnapshot:
        with self._lock:
            if self._staged is None or self._staged.revision != int(revision):
                raise ValueError("staged conversation revision does not match")
            self._active = self._staged
            self._staged = None
            return self._active

    def discard(self, revision: int) -> None:
        with self._lock:
            if self._staged is not None and self._staged.revision == int(revision):
                self._staged = None
