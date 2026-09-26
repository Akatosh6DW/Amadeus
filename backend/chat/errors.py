"""对话交付控制异常；入口与恢复流程共享同一异常类型。"""

from typing import Any


class GroundingGateError(RuntimeError):
    """完整候选在交付前需要补充证据；保留原异常身份与错误码。"""

    def __init__(self, decision: Any) -> None:
        self.decision = decision
        self.code = "GROUNDING_GATE"
        super().__init__(
            "grounded claim gate requires internal recovery: "
            + str(getattr(decision, "status", "BLOCK"))
        )
