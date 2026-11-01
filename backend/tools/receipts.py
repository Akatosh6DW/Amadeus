"""外部工具回执的权威投影与声明修复规则。

工具执行器记录每次尝试，模型结果只拥有声明权。这里按动作身份选择
权威回执，并在最终回复发布前把 Todo 与浏览器的成功、失败和待澄清
状态绑定到真实回执；不执行工具、不读写数据库，也不依赖 ``server``。
"""
from __future__ import annotations

from typing import Any, Mapping

from backend.tools.outcomes import effective_tool_results


def action_receipts_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(item["actionReceipt"])
        for item in effective_tool_results(results)
        if isinstance(item, dict)
        and isinstance(item.get("actionReceipt"), dict)
        and item["actionReceipt"].get("actionId")
    ]


def enforce_external_action_receipt(
    result: dict[str, Any],
    tool_results: list[dict[str, Any]] | None = None,
    *, schedule_decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project host receipts; prose keywords cannot create or erase an action."""
    projected = dict(result)
    receipts = action_receipts_from_results(tool_results or [])
    # Candidate-supplied receipts are declarations, never execution evidence.
    projected.pop("actionReceipt", None)
    projected.pop("actionReceipts", None)
    if receipts:
        projected["actionReceipt"] = receipts[-1]
        projected["actionReceipts"] = receipts
        projected["operationStatus"] = str(receipts[-1].get("status") or "unknown")
    elif any(isinstance(item, Mapping) and item.get("attempted") for item in tool_results or []):
        projected["operationStatus"] = "unknown"
    else:
        projected["operationStatus"] = "not_attempted"
    projected["receiptAuthority"] = "host_tool_results"
    return projected
