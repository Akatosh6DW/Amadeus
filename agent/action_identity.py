"""Bind browser retries to backend execution identities.

The action relation is an execution identity detail, not a semantic gate.  A
new target starts a new action by default; an identical target after an actual
failed browser attempt can safely reuse that failed action group.  Explicit
retry IDs remain checked against real host receipts.
"""
import json
from typing import Any, Mapping


def _browser_target(args: Mapping[str, Any] | None) -> str:
    values = args if isinstance(args, Mapping) else {}
    for key in ("url", "query", "resolveQuery"):
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalized_target(value: Any) -> str:
    target = str(value or "").strip()
    if not target:
        return ""
    # The browser executor canonicalizes a bare host with a trailing slash;
    # make the comparison stable across the pre- and post-canonical forms.
    if "://" in target:
        try:
            from urllib.parse import urlsplit, urlunsplit

            parsed = urlsplit(target)
            if parsed.scheme and parsed.netloc:
                path = parsed.path.rstrip("/") or "/"
                return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, parsed.fragment))
        except ValueError:
            pass
    return target


def _browser_parameters(args: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    values = args if isinstance(args, Mapping) else {}
    keys = ("url", "query", "resolveQuery", "bookmarkId", "destinationId", "browserIntent", "needsConfirmation")
    result: list[tuple[str, str]] = []
    for key in keys:
        if key not in values:
            continue
        value = values.get(key)
        if key in {"url", "query", "resolveQuery"}:
            normalized = _normalized_target(value)
        elif isinstance(value, bool):
            normalized = "true" if value else "false"
        else:
            normalized = str(value or "").strip()
        if normalized:
            result.append((key, normalized))
    return tuple(result)


def _receipt_attempted(receipt: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    return receipt.get("attempted") is not False and result.get("attempted") is not False


def _same_browser_request(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    """Compare the model call with the recorded call when a signature exists."""

    raw_signature = str(result.get("_cognitiveRequestSignature") or "").strip()
    if raw_signature:
        try:
            recorded = json.loads(raw_signature)
        except (TypeError, ValueError, json.JSONDecodeError):
            recorded = None
        if isinstance(recorded, Mapping):
            recorded_args = recorded.get("args")
            if isinstance(recorded_args, Mapping):
                return _browser_parameters(request.get("args")) == _browser_parameters(recorded_args)
    requested = _normalized_target(_browser_target(request.get("args")))
    recorded_target = _normalized_target(receipt.get("requestedTarget"))
    return bool(requested and recorded_target and requested == recorded_target)


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
            receipts.append((receipt, result if isinstance(result, Mapping) else {}))
    if relation not in {None, "new", "retry"}:
        raise ValueError("browser_action_relation_invalid")
    if relation == "new" and not target:
        return {}
    failed_receipts = [
        (receipt, result)
        for receipt, result in receipts
        if receipt.get("status") == "rejected" and _receipt_attempted(receipt, result)
    ]
    # No relation means a new action.  The one narrow exception is an exact
    # repeat of an actually attempted failed browser call; bind it to the
    # existing group so callers can retry without manufacturing an ID.
    if relation in {None, "retry"} and not target:
        same_failed = next(
            (
                (receipt, result)
                for receipt, result in reversed(failed_receipts)
                if _same_browser_request(request, result, receipt)
            ),
            None,
        )
        if same_failed is None:
            return {}
        failed_receipt, _ = same_failed
        group = str(failed_receipt.get("actionGroupId") or failed_receipt.get("actionId") or "").strip()
        action_id = str(failed_receipt.get("actionId") or "").strip()
        if not group or not action_id:
            return {}
        if any(
            receipt.get("status") == "accepted"
            and str(receipt.get("actionGroupId") or receipt.get("actionId")) == group
            for receipt, _ in receipts
        ):
            return {}
        return {"_actionGroupId": group, "_retryOfActionId": action_id}
    if relation == "retry" and not receipts:
        # A plan rejected before dispatch has no browser action identity.
        # Continuing that request is its first execution, even if the model
        # called it a retry. Never require an ID that the backend did not issue.
        return {}
    if relation != "retry" or not target:
        if relation is None:
            return {}
        raise ValueError('browser_action_relation_required: calls item needs selection:{"actionRelation":"retry","retryOfActionId":"REAL_FAILED_ACTION_ID"} or selection:{"actionRelation":"new"}; the call has not executed yet')
    matches = [receipt for receipt, result in receipts if receipt.get("actionId") == target]
    if not matches:
        raise ValueError("browser_retry_requires_known_failed_action: no failed browser receipt matches this ID; the current call has not executed")
    if any(receipt.get("status") != "rejected" for receipt in matches):
        raise ValueError("browser_retry_requires_known_failed_action: the referenced action is not a failed attempt; reuse accepted receipts")
    groups = {str(receipt.get("actionGroupId") or target) for receipt in matches}
    if len(groups) != 1:
        raise ValueError("browser_retry_identity_conflict")
    group = groups.pop()
    if any(
        receipt.get("status") == "accepted"
        and str(receipt.get("actionGroupId") or receipt.get("actionId")) == group
        for receipt, _ in receipts
    ):
        raise ValueError("browser_retry_already_succeeded: reuse the successful receipt")
    return {"_actionGroupId": group, "_retryOfActionId": target}
