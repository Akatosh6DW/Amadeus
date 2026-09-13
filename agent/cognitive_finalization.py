"""Typed failures raised while checking the final delivered turn."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping

from agent.cognitive_brain import CognitiveProtocolError


class FinalProjectionAuditRepairError(CognitiveProtocolError):
    """The final, post-processed candidate needs one bounded delivery repair.

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
            "repair": "最终交付未通过附件或执行结果校验；依据原始用户要求、真实回执和当前状态修正，不能声称交付了不存在的文件。",
        }, ensure_ascii=False))
