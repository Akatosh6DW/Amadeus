"""Bind model-declared retries to backend execution identities."""
from typing import Any, Mapping


def browser_retry_identity(request: Mapping[str, Any], evidence: Mapping[str, Any], *, required: bool = False) -> dict[str, str]:
    selection = request.get("selection") or {}
    if not isinstance(selection, Mapping):
        raise ValueError("browser_action_selection_invalid")
    relation = selection.get("actionRelation")
    target = str(selection.get("retryOfActionId") or "").strip()
    receipts = []
    for entry in evidence.values():
        result = entry.get("receipt") if isinstance(entry, Mapping) else None
        receipt = result.get("actionReceipt") if isinstance(result, Mapping) else None
        if isinstance(receipt, Mapping) and receipt.get("tool") == "open_browser":
            receipts.append(receipt)
    if relation is None and not target and (not required or not any(receipt.get("status") == "rejected" for receipt in receipts)):
        return {}
    if relation == "new" and not target:
        return {}
    if relation == "retry" and not receipts:
        # A plan rejected before dispatch has no browser action identity.
        # Continuing that request is its first execution, even if the model
        # called it a retry. Never require an ID that the backend did not issue.
        return {}
    if relation != "retry" or not target:
        raise ValueError('browser_action_relation_required: calls item needs selection:{"actionRelation":"retry","retryOfActionId":"REAL_FAILED_ACTION_ID"} or selection:{"actionRelation":"new"}; the call has not executed yet')
    matches = [receipt for receipt in receipts if receipt.get("actionId") == target]
    if not matches:
        raise ValueError("browser_retry_requires_known_failed_action: no failed browser receipt matches this ID; the current call has not executed")
    if any(receipt.get("status") != "rejected" for receipt in matches):
        raise ValueError("browser_retry_requires_known_failed_action: the referenced action is not a failed attempt; reuse accepted receipts")
    groups = {str(receipt.get("actionGroupId") or target) for receipt in matches}
    if len(groups) != 1:
        raise ValueError("browser_retry_identity_conflict")
    group = groups.pop()
    if any(receipt.get("status") == "accepted" and str(receipt.get("actionGroupId") or receipt.get("actionId")) == group for receipt in receipts):
        raise ValueError("browser_retry_already_succeeded: reuse the successful receipt")
    return {"_actionGroupId": group, "_retryOfActionId": target}
