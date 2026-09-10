"""Typed failures raised while checking the final delivered turn."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping

from agent.cognitive_brain import CognitiveProtocolError


class FinalProjectionAuditRepairError(CognitiveProtocolError):
    """The final, post-processed candidate needs one bounded semantic repair.

    The candidate is captured before the finalizer unwinds so a caller can run
    the existing repair route without trusting a later reconstruction of the
    answer.  The packet/report are diagnostics only; they never authorize a
    tool call or a state commit.
    """

    def __init__(
        self,
        candidate: Mapping[str, Any],
        packet: Mapping[str, Any],
        report: Mapping[str, Any],
    ) -> None:
        self.candidate = deepcopy(dict(candidate))
        self.packet = deepcopy(dict(packet))
        self.report = deepcopy(dict(report))
        super().__init__(json.dumps({
            "code": "final_projection_semantic_repair_required",
            "violations": list(self.report.get("problems") or []),
            "repair": "最终投影的实际台词未通过独立语义核对；依据原始用户要求、真实回执和当前状态重新分析，必要时补查，再重新生成并复核。",
        }, ensure_ascii=False))
