"""已接受对话的关系更新应用，规则与持久层之间只传一份状态。

外层更新继续使用存储拥有的同一可重入锁；清空记忆的边界重置保留原字段范围。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from backend.common.text import unique_strings
from backend.common.values import clamp_float
from backend.relationship.rules import grounded_relation_event, relationship_commitment_from_turn, relationship_stage_for

@dataclass(frozen=True)
class StateOperations:
    read_relationship_state: Callable[..., Any]
    write_relationship_state: Callable[..., Any]
    lock: Callable[..., Any]

class RelationshipApplication:
    def __init__(self, state: StateOperations, *, now: Callable[[], str]) -> None:
        self.state = state
        self.now = now

    def _update_relationship_state_locked(
        self,
        user_message: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        state = self.state.read_relationship_state()
        text = str(user_message or "")
        emotion = str(result.get("emotion") or "neutral")
        state["interactionCount"] = int(state.get("interactionCount") or 0) + 1
        state["familiarity"] = clamp_float(state.get("familiarity"), 0, 1, 0.58) + 0.0025
        relation_event = grounded_relation_event(text, result)
        relation_signals = set(relation_event["signals"])
        if "shared_routine" in relation_signals:
            state["sharedRoutineEvents"] = int(state.get("sharedRoutineEvents") or 0) + 1
            state["stability"] = clamp_float(state.get("stability"), 0, 1, 0.42) + 0.003
        if "positive" in relation_signals:
            state["positiveEvents"] = int(state.get("positiveEvents") or 0) + 1
            state["trust"] = clamp_float(state.get("trust"), 0, 1, 0.54) + 0.006
            state["careLevel"] = clamp_float(state.get("careLevel"), 0, 1, 0.6) + 0.005
            state["lastEmotionalEvent"] = "用户表达了亲近、感谢或肯定。"
        elif "distress" in relation_signals:
            state["trust"] = clamp_float(state.get("trust"), 0, 1, 0.54) + 0.004
            state["careLevel"] = clamp_float(state.get("careLevel"), 0, 1, 0.6) + 0.007
            state["teasingLevel"] = clamp_float(state.get("teasingLevel"), 0, 1, 0.56) - 0.015
            state["lastEmotionalEvent"] = "用户处于需要安定和陪伴的状态。"
        if "repair" in relation_signals:
            state["repairEvents"] = int(state.get("repairEvents") or 0) + 1
            state["stability"] = clamp_float(state.get("stability"), 0, 1, 0.42) + 0.012
        elif emotion in {"annoyed", "teasing", "amused", "laughing", "shy", "flustered", "affectionate", "skeptical", "startled"}:
            state["teasingLevel"] = clamp_float(state.get("teasingLevel"), 0, 1, 0.56) + 0.003
        elif emotion in {"thinking", "focused", "excited", "proud"}:
            state["familiarity"] = clamp_float(state.get("familiarity"), 0, 1, 0.58) + 0.003
        commitment = relationship_commitment_from_turn(user_message, result)
        if commitment:
            source_turn_id = str(result.get("requestId") or result.get("_requestId") or "").strip()
            state.update({
                "commitmentStatus": commitment["status"],
                "commitmentLabel": commitment["label"],
                "commitmentConfirmedAt": (
                    str(state.get("commitmentConfirmedAt") or "") or self.now()
                ),
                "commitmentSourceTurnIds": unique_strings([
                    *(state.get("commitmentSourceTurnIds") or []),
                    *([source_turn_id] if source_turn_id else []),
                ])[-24:],
                "lastEmotionalEvent": commitment["milestone"],
            })
            state["milestones"] = unique_strings([
                *(state.get("milestones") or []),
                commitment["milestone"],
            ])[-24:]
        previous_stage = str(state.get("stage") or "close_companion")
        state["stage"] = relationship_stage_for(state)
        if state["stage"] != previous_stage:
            milestone = {
                "trusted_intimacy": "长期互动形成了可以依赖的信任。",
                "committed_partner": "共同经历和分歧修复让关系变得稳定。",
                "life_partner": "共同计划与日常习惯已经自然交织。",
                "old_married": "多年般的默契已经成为相处的默认状态。",
            }.get(state["stage"])
            if milestone:
                state["milestones"] = unique_strings([*(state.get("milestones") or []), milestone])[-24:]
                state["lastEmotionalEvent"] = milestone
        for key in ("familiarity", "trust", "teasingLevel", "careLevel", "stability"):
            state[key] = clamp_float(state.get(key), 0, 1, 0.5)
        return self.state.write_relationship_state(state)


    def update_relationship_state(self, user_message: str, result: dict[str, Any]) -> dict[str, Any]:
        """Apply one relationship event as one in-process transaction.

        Reading and writing were previously protected, at most, as independent
        file operations.  Concurrent turns could therefore all read generation N
        and overwrite each other with different versions of N+1.  Keep the full
        read-modify-write section under the same re-entrant lock.
        """

        with self.state.lock():
            return self._update_relationship_state_locked(user_message, result)


    def reset_dignity_boundary_after_full_memory_clear(self) -> dict[str, Any]:
        state = self.state.read_relationship_state()
        state.update({
            "dignityViolationCount": 0,
            "dignityLocked": False,
            "coldShoulder": False,
            "coldShoulderReason": "",
            "lastBoundaryAssessment": "none",
        })
        return self.state.write_relationship_state(state)
