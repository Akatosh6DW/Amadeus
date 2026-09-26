"""从不可变会话记录补齐已有双方确认的关系状态；单方断言不能升级关系。

重复运行优先读取已确认状态；写入使用原关系存储和带来源的记忆入口，保留原顺序。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from backend.relationship.rules import relationship_commitment_from_turn, relationship_stage_for
from backend.common.text import unique_strings

@dataclass(frozen=True)
class RelationshipHistoryPorts:
    read_state: Callable[[], dict[str, Any]]
    read_archive: Callable[..., Any]
    write_state: Callable[..., dict[str, Any]]
    merge_memory: Callable[..., Any]
    now: Callable[[], str]
    log: Callable[..., Any]

class RelationshipHistory:
    def __init__(self, ports: RelationshipHistoryPorts) -> None:
        self.ports = ports

    def reconcile_relationship_commitment_history(
        self,
        settings: dict[str, Any],
        *,
        limit: int = 96,
    ) -> dict[str, Any]:
        """Backfill a missing commitment from the immutable conversation ledger.

        Earlier builds archived both sides of the confirmation but had no typed
        relationship extractor.  This migration is idempotent, requires mutual
        evidence from one archived turn, and never promotes a user assertion by
        itself.
        """

        state = self.ports.read_state()
        if str(state.get("commitmentStatus") or "") == "confirmed":
            return state
        for turn in reversed(self.ports.read_archive(max(1, int(limit)))):
            user_text = str(turn.get("user") or "").strip()
            assistant_text = str(turn.get("assistant") or "").strip()
            commitment = relationship_commitment_from_turn(
                user_text,
                {
                    "reply": assistant_text,
                    "relationEvent": turn.get("relationEvent"),
                    "relationEventProvided": bool(turn.get("relationEventProvided")),
                },
            )
            if not commitment:
                continue
            source_turn_id = str(
                turn.get("requestId") or turn.get("turnId") or "relationship-history"
            ).strip()
            timestamp = str(turn.get("at") or "").strip() or self.ports.now()
            state.update({
                "commitmentStatus": commitment["status"],
                "commitmentLabel": commitment["label"],
                "commitmentConfirmedAt": timestamp,
                "commitmentSourceTurnIds": unique_strings([
                    *(state.get("commitmentSourceTurnIds") or []),
                    source_turn_id,
                ])[-24:],
                "lastEmotionalEvent": commitment["milestone"],
            })
            state["milestones"] = unique_strings([
                *(state.get("milestones") or []),
                commitment["milestone"],
            ])[-24:]
            state["stage"] = relationship_stage_for(state)
            state = self.ports.write_state(state)
            self.ports.merge_memory(
                [commitment["memory"]],
                user_message=user_text,
                settings=settings,
                source_turn_id=source_turn_id,
                update_types={commitment["memory"]: "relationship_state"},
            )
            self.ports.log(
                "relationship_commitment_history_reconciled",
                requestId=source_turn_id,
                confirmedAt=timestamp,
                label=commitment["label"],
            )
            return state
        return state
