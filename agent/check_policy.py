"""Capabilities for checks: a style diagnosis cannot veto delivery.

The registry governs decisions, not user language. Host permissions remain in
their executors; model-provided authorization and success flags grant nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable


class Category(str, Enum):
    HARD_PERMISSION = "hard_permission"
    HARD_PROTOCOL = "hard_protocol"
    HARD_LEAK = "hard_leak"
    SOFT_QUALITY = "soft_quality"
    PRESENTATION = "presentation"
    DIAG = "diag"


class Action(str, Enum):
    BLOCK_TOOL = "block_tool"
    BLOCK_DELIVERY = "block_delivery"
    REWRITE_ONCE = "rewrite_once"
    DEGRADE_FIELD = "degrade_field"
    ANNOTATE = "annotate"
    LOG = "log"


ALLOWED = {
    Category.HARD_PERMISSION: frozenset({Action.BLOCK_TOOL, Action.LOG}),
    # regex-consumer: protocol.envelope
    Category.HARD_PROTOCOL: frozenset({Action.BLOCK_DELIVERY, Action.REWRITE_ONCE, Action.LOG}),
    # regex-consumer: outbound.private_content
    Category.HARD_LEAK: frozenset({Action.BLOCK_DELIVERY, Action.LOG}),
    Category.SOFT_QUALITY: frozenset({Action.ANNOTATE, Action.REWRITE_ONCE, Action.LOG}),
    Category.PRESENTATION: frozenset({Action.DEGRADE_FIELD, Action.LOG}),
    Category.DIAG: frozenset({Action.LOG}),
}


@dataclass(frozen=True)
class CheckSpec:
    id: str
    owner: str
    category: Category
    scope: str
    guards: str
    retire_when: str | None = None


class RegistryViolation(ValueError):
    pass


SPECS = {
    spec.id: spec for spec in (
        CheckSpec("protocol.envelope", "backend.chat.delivery", Category.HARD_PROTOCOL,
                  "envelope", "Deliver only a complete, located reply; never raw protocol"),
        CheckSpec("outbound.private_content", "backend.security.outbound", Category.HARD_LEAK,
                  "reply_or_query", "Do not transmit actual secrets or private prompt spans"),
        CheckSpec("permission.executor", "backend.tools.validation", Category.HARD_PERMISSION,
                  "tool_call", "Host permission, account, path and target boundaries"),
        CheckSpec("permission.untrusted_target", "backend.tools.trust", Category.HARD_PERMISSION,
                  "tool_call", "Untrusted material cannot introduce a new side-effect target"),
        CheckSpec("legacy.context_grounding", "backend.chat.upstream_pipeline", Category.SOFT_QUALITY,
                  "reply", "Context relevance", "Offline contextual accuracy evaluation"),
        CheckSpec("legacy.task_completion", "backend.chat.completion", Category.SOFT_QUALITY,
                  "reply", "Answer obligations", "Structured task/outcome state"),
        CheckSpec("legacy.tool_grounding", "backend.tools.grounding_validation", Category.SOFT_QUALITY,
                  "reply", "Using supplied evidence", "Host attachment and tool observations"),
        CheckSpec("legacy.claim_grounding", "backend.chat.finalization", Category.SOFT_QUALITY,
                  "reply", "Source and epistemic attribution", "Structured claims and receipts"),
        CheckSpec("legacy.dialogue_style", "backend.chat.finalization", Category.SOFT_QUALITY,
                  "reply", "Persona and concision", "Prompt/field evaluation"),
        CheckSpec("presentation.speech", "backend.tts", Category.PRESENTATION,
                  "speech", "Unavailable audio must not discard text"),
        CheckSpec("presentation.metadata", "backend.chat.delivery", Category.PRESENTATION,
                  "emotion_gesture_pace", "Optional fields use defaults"),
        CheckSpec("receipt.projection", "backend.tools.receipts", Category.DIAG,
                  "operation_status", "Operation status comes only from host receipts"),
        CheckSpec("input.transform", "backend.context", Category.DIAG,
                  "context", "Record input transformations without storing private content"),
    )
}


def enforce(check_id: str, action: Action, *, strict: bool = True) -> Action:
    spec = SPECS.get(check_id)
    if spec is None or action not in ALLOWED[spec.category]:
        if strict:
            raise RegistryViolation(f"{check_id}:{action.value}")
        return Action.LOG
    return action


def record(check_id: str, action: Action, *, logger: Callable | None = None,
           request_id: str = "", reason: str = "", decisive: bool = False,
           strict: bool = False) -> dict:
    accepted = enforce(check_id, action, strict=strict)
    spec = SPECS.get(check_id)
    event = {
        "checkId": check_id, "category": spec.category.value if spec else "unregistered",
        "requestedAction": action.value, "action": accepted.value,
        "registryViolation": accepted != action, "reason": reason,
        # regex-consumer: protocol.envelope
        "decisive": decisive and accepted == Action.BLOCK_DELIVERY,
    }
    if logger:
        try:
            logger("delivery_check", requestId=request_id, **event)
        except Exception:
            # Diagnostics have no authority to turn an answer into a failure.
            pass
    return event


@dataclass
class RepairBudget:
    """One shared protocol repair; soft findings do not spend model calls."""

    remaining: int = 1

    def claim(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def soft_findings(items: Iterable[tuple[str, str]], *, logger=None, request_id="") -> list[dict]:
    return [record(check_id, Action.ANNOTATE, reason=reason, logger=logger,
                   request_id=request_id) for check_id, reason in items]
