"""工具尝试到实际动作结果的统一投影；不执行工具、不读写状态。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# 同一外部动作一旦 accepted 就是终态；后续失败重试不能覆盖已经发生的副作用。
def effective_tool_results(
    results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """按工具及动作身份选出权威结果，保留无回执结果的独立顺序。

    执行日志记录全部尝试；提示、状态与回执消费同一动作结果。
    已接受操作不会被后续失败重试抹掉，避免把已发生的操作误判为未执行。
    """

    normalized = [dict(item) for item in results if isinstance(item, Mapping)]
    selected_indexes: dict[tuple[str, str], int] = {}
    for index, item in enumerate(normalized):
        receipt = item.get("actionReceipt")
        if not isinstance(receipt, Mapping):
            continue
        action_id = str(receipt.get("actionId") or "").strip()
        if not action_id:
            continue
        tool = str(receipt.get("tool") or item.get("name") or "").strip()
        key = (tool, str(receipt.get("actionGroupId") or action_id))
        previous_index = selected_indexes.get(key)
        if previous_index is None:
            selected_indexes[key] = index
            continue
        previous_receipt = normalized[previous_index].get("actionReceipt")
        previous_status = str(
            previous_receipt.get("status")
            if isinstance(previous_receipt, Mapping)
            else ""
        )
        current_status = str(receipt.get("status") or "")
        if previous_status != "accepted" or current_status == "accepted":
            selected_indexes[key] = index

    selected = set(selected_indexes.values())
    return [
        item
        for index, item in enumerate(normalized)
        if not (
            isinstance(item.get("actionReceipt"), Mapping)
            and str(item["actionReceipt"].get("actionId") or "").strip()
        )
        or index in selected
    ]
