"""关系状态的唯一进程锁、scope 读取与原子文件替换。

路径和请求 scope 仍由运行环境按调用提供；开发者只读查看使用 token/finally 恢复原域。
generation 在写入时递增，临时文件替换与原实现共用同一锁，避免出现半份 JSON。
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import threading
import uuid
from typing import Any, Callable
from backend.common.values import clamp_float, clamp_int, safe_nonnegative_int as _safe_nonnegative_int
from backend.common.text import unique_strings

RELATIONSHIP_LOCK = threading.RLock()

class RelationshipStateStore:
    def __init__(self, *, state_path: Callable[[], Path], release_data_dir: Callable[[], Path],
                 scoped_path: Callable[[Path], Path], scope_var: Callable[[], Any],
                 normalize_scope: Callable[[Any], str], read_json: Callable[..., Any],
                 now: Callable[[], str]) -> None:
        self.state_path = state_path
        self.release_data_dir = release_data_dir
        self.scoped_path = scoped_path
        self.scope_var = scope_var
        self.normalize_scope = normalize_scope
        self.read_json = read_json
        self.now = now

    def default_relationship_state(self) -> dict[str, Any]:
        return {
            "version": 4,
            "generation": 0,
            "updatedAt": self.now(),
            "stage": "close_companion",
            "interactionCount": 0,
            "positiveEvents": 0,
            "sharedRoutineEvents": 0,
            "repairEvents": 0,
            "familiarity": 0.58,
            "trust": 0.54,
            "teasingLevel": 0.56,
            "careLevel": 0.6,
            "stability": 0.42,
            "milestones": [],
            "lastEmotionalEvent": "",
            # Aggregate familiarity scores describe how the relationship feels;
            # an explicit, mutually accepted commitment is a different kind of
            # evidence and must not be reconstructed from those scores.
            "commitmentStatus": "unconfirmed",
            "commitmentLabel": "",
            "commitmentConfirmedAt": "",
            "commitmentSourceTurnIds": [],
            # This counter is intentionally separate from ordinary annoyed/furious
            # emotions. Only model-confirmed severe personal degradation increments
            # it, and only an explicit full long-term-memory clear resets it.
            "dignityViolationCount": 0,
            "dignityLocked": False,
            "coldShoulder": False,
            "coldShoulderReason": "",
            "lastBoundaryAssessment": "none",
        }


    def normalize_relationship_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict):
            state = self.default_relationship_state()
        else:
            state = dict(state)
        defaults = self.default_relationship_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        state["version"] = 4
        return {
            **state,
            "familiarity": clamp_float(state.get("familiarity"), 0, 1, defaults["familiarity"]),
            "trust": clamp_float(state.get("trust"), 0, 1, defaults["trust"]),
            "teasingLevel": clamp_float(state.get("teasingLevel"), 0, 1, defaults["teasingLevel"]),
            "careLevel": clamp_float(state.get("careLevel"), 0, 1, defaults["careLevel"]),
            "stability": clamp_float(state.get("stability"), 0, 1, defaults["stability"]),
            "interactionCount": clamp_int(state.get("interactionCount"), 0, 1000000, 0),
            "positiveEvents": clamp_int(state.get("positiveEvents"), 0, 1000000, 0),
            "sharedRoutineEvents": clamp_int(state.get("sharedRoutineEvents"), 0, 1000000, 0),
            "repairEvents": clamp_int(state.get("repairEvents"), 0, 1000000, 0),
            "dignityViolationCount": clamp_int(state.get("dignityViolationCount"), 0, 5, 0),
            "dignityLocked": bool(state.get("dignityLocked")),
            "coldShoulder": bool(state.get("coldShoulder")),
            "coldShoulderReason": str(state.get("coldShoulderReason") or "")[:160],
            "lastBoundaryAssessment": str(state.get("lastBoundaryAssessment") or "none")[:48],
            "commitmentStatus": (
                "confirmed"
                if str(state.get("commitmentStatus") or "").strip().lower() == "confirmed"
                else "unconfirmed"
            ),
            "commitmentLabel": str(state.get("commitmentLabel") or "")[:80],
            "commitmentConfirmedAt": str(state.get("commitmentConfirmedAt") or "")[:80],
            "commitmentSourceTurnIds": unique_strings(
                state.get("commitmentSourceTurnIds")
                if isinstance(state.get("commitmentSourceTurnIds"), list)
                else []
            )[-24:],
            "milestones": unique_strings(state.get("milestones") if isinstance(state.get("milestones"), list) else [])[-24:],
        }


    def read_relationship_state(self) -> dict[str, Any]:
        with RELATIONSHIP_LOCK:
            state = self.read_json(
                self.scoped_path(self.state_path()),
                self.default_relationship_state(),
            )
        return self.normalize_relationship_state(state)


    def read_relationship_state_for_scope(self, scope: str) -> dict[str, Any]:
        """Read one relationship domain without changing the request's active domain.

        The developer dashboard uses this as a read-only inspection boundary.  A
        normal-mode snapshot must never become the active developer relationship
        state merely because it was rendered in the diagnostics UI.
        """

        requested_scope = self.normalize_scope(scope)
        token = self.scope_var().set(requested_scope)
        try:
            return self.read_relationship_state()
        finally:
            self.scope_var().reset(token)


    def read_release_relationship_state(self) -> dict[str, Any]:
        """Read the installed release profile without activating or modifying it."""

        path = self.release_data_dir() / "relationship_state.json"
        return self.normalize_relationship_state(self.read_json(path, self.default_relationship_state()))


    def write_relationship_state(self, state: dict[str, Any]) -> dict[str, Any]:
        with RELATIONSHIP_LOCK:
            path = self.scoped_path(self.state_path())
            path.parent.mkdir(parents=True, exist_ok=True)
            state["generation"] = _safe_nonnegative_int(state.get("generation")) + 1
            state["updatedAt"] = self.now()
            payload = json.dumps(state, ensure_ascii=False, indent=2)
            temporary = path.with_name(
                f".{path.name}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
            )
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)
        return state
