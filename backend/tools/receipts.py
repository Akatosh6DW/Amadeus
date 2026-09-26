"""外部工具回执的权威投影与声明修复规则。

工具执行器记录每次尝试，模型结果只拥有声明权。这里按动作身份选择
权威回执，并在最终回复发布前把 Todo 与浏览器的成功、失败和待澄清
状态绑定到真实回执；不执行工具、不读写数据库，也不依赖 ``server``。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

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
    """Prevent prose from claiming that an external action succeeded without a receipt."""

    repaired = dict(result)
    receipt_results = list(effective_tool_results(tool_results or []))
    if isinstance(result.get("actionReceipt"), dict):
        receipt_results.append({
            "name": str(result["actionReceipt"].get("tool") or "external_action"),
            "ok": result["actionReceipt"].get("status") == "accepted",
            "actionReceipt": dict(result["actionReceipt"]),
        })
    receipts = action_receipts_from_results(receipt_results)
    latest = receipts[-1] if receipts else {}
    browser_receipt = next(
        (item for item in reversed(receipts) if item.get("tool") == "open_browser"),
        {},
    )
    if len(receipts) > 1:
        repaired["actionReceipts"] = receipts
    reply = str(repaired.get("reply") or "").strip()
    todo_receipt = next(
        (item for item in reversed(receipts) if item.get("tool") == "todo_manage"),
        {},
    )
    todo_attempts = [
        item for item in (tool_results or [])
        if isinstance(item, Mapping) and item.get("name") == "todo_manage"
    ]
    todo_attempt_receipts = [
        dict(item["actionReceipt"])
        for item in todo_attempts
        if isinstance(item.get("actionReceipt"), Mapping)
    ]
    todo_batch_accepted = bool(todo_attempts) and (
        len(todo_attempt_receipts) == len(todo_attempts)
        and all(item.get("status") == "accepted" for item in todo_attempt_receipts)
    )
    decision = schedule_decision or result.get("scheduleDecision") or {}
    intent = decision.get("intent") if isinstance(decision, Mapping) else None
    if not isinstance(intent, Mapping):
        intent = result.get("scheduleIntent")
    schedule_excluded = bool(
        isinstance(intent, Mapping)
        and intent.get("kind") == "none" and intent.get("relation") == "unrelated"
        and not intent.get("commands") and not todo_attempts and not todo_receipt
    )
    todo_success_claim = bool(re.search(
        r"(?:我|已经|已|刚刚|这就).{0,12}(?:替你|帮你|给你)?(?:记住|记下|安排|加入|添加|创建|修改|改成|改到|完成|删除|推迟|打勾|打上勾|勾掉|勾上)(?:了|好)?|"
        r"(?:待办|任务|日程|提醒).{0,10}(?:已经|已)(?:添加|创建|修改|完成|删除|推迟|打勾|打上勾|勾掉|勾上)|"
        r"到时候我会提醒你|我会.{0,16}提醒你",
        reply,
        re.I,
    )) and not bool(re.search(
        r"(?:没有|并未|尚未|未能|不能|无法).{0,14}"
        r"(?:记住|记下|安排|加入|添加|创建|修改|改成|改到|完成|删除|推迟|打勾|打上勾|勾掉|勾上|提醒)",
        reply,
        re.I,
    ))
    # A completed semantic no-schedule decision owns task scope. Words such
    # as "remembered" or "changed" in ordinary conversation cannot turn it
    # into a failed calendar write. Actual write attempts still need receipts.
    if todo_success_claim and not schedule_excluded:
        if todo_batch_accepted or (
            not todo_attempts and todo_receipt.get("status") == "accepted"
        ):
            repaired["actionReceipt"] = todo_receipt
            if len(todo_attempt_receipts) > 1:
                repaired["actionReceipts"] = todo_attempt_receipts
        else:
            cleaned = re.sub(
                r"[^。！？!?\n]*(?:记住|记下|安排|加入|添加|创建|修改|改成|改到|完成|删除|推迟|打勾|打上勾|勾掉|勾上|到时候我会提醒你|我会.{0,16}提醒你)"
                r"[^。！？!?\n]*[。！？!?]?",
                "",
                reply,
                flags=re.I,
            ).strip()
            repaired["reply"] = (
                "这次没有取得日程写入成功回执，所以我不能说已经修改。"
                + (f"\n\n{cleaned}" if cleaned else "")
            )
            repaired["actionClaimRepaired"] = True
            repaired["requiresUserResponse"] = False
            repaired["requiresUserResponseProvided"] = True
        return repaired
    if browser_receipt:
        accepted = (
            browser_receipt.get("tool") == "open_browser"
            and browser_receipt.get("status") == "accepted"
        )
        if accepted:
            repaired["actionReceipt"] = browser_receipt
            repaired["requiresUserResponse"] = False
            repaired["requiresUserResponseProvided"] = True
            return repaired

        error = str(browser_receipt.get("error") or "")
        target = str(
            browser_receipt.get("requestedTarget")
            or browser_receipt.get("resolvedTarget")
            or ""
        ).strip()[:120]
        unresolved_errors = {
            "target_confirmation_required",
            "unresolved_browser_target",
            "missing_url",
            "no_verified_source_to_open",
            "ambiguous_browser_target",
        }
        if error in unresolved_errors:
            repaired["reply"] = (
                f"我不确定你说的“{target}”具体是哪个网站。"
                "你可以告诉我它的正式名称，或者直接把网址发给我。"
                if target else
                "我还不知道你想打开哪个网站。告诉我网站名称，或者直接把网址发给我吧。"
            )
            repaired["requiresUserResponse"] = True
            repaired["expectedResponseType"] = "browser_target"
            repaired["browserTargetClarificationAdded"] = True
            repaired["clarificationActionId"] = str(
                browser_receipt.get("actionId") or ""
            )[:100]
        elif error == "permission_denied":
            repaired["reply"] = "我现在没有打开浏览器的权限，所以这次没能替你打开。"
            repaired["requiresUserResponse"] = False
        else:
            target_label = f"“{target}”" if target else "这个网页"
            repaired["reply"] = f"我刚才没能打开{target_label}。你要我再试一次吗？"
            repaired["requiresUserResponse"] = True
            repaired["expectedResponseType"] = "confirmation"
        repaired["actionReceipt"] = browser_receipt
        repaired["actionClaimRepaired"] = True
        repaired["requiresUserResponseProvided"] = True
        return repaired
    browser_success_claim = re.search(
        r"(?:已经|已|刚刚|现在)?(?:替你|帮你|给你|为你)?"
        r"(?:打开|启动|唤起)(?:了|好)?(?:默认)?浏览器|"
        r"(?:页面|网页|链接)(?:已经|已)(?:打开|加载)|"
        r"你(?:现在|应该|很快)?(?:能|会)?看到(?:页面|网页|浏览器)|"
        r"(?:稍等|等一下)[，,\s]*(?:我)?(?:来|现在)?(?:替你|帮你|给你|为你)?"
        r"(?:打开|启动|唤起)|"
        r"(?:我)?(?:正在|这就|马上|现在)(?:替你|帮你|给你|为你)?"
        r"(?:打开|启动|唤起)|"
        r"(?:已经|已)(?:把)?打开请求交给(?:系统|浏览器)",
        reply,
        re.I,
    )
    if not browser_success_claim:
        if latest:
            repaired["actionReceipt"] = latest
        return repaired
    repaired["reply"] = "我刚才没能确认网页是否真的打开了。你要我再试一次吗？"
    repaired["actionClaimRepaired"] = True
    repaired["requiresUserResponse"] = True
    repaired["requiresUserResponseProvided"] = True
    repaired["expectedResponseType"] = "confirmation"
    return repaired
