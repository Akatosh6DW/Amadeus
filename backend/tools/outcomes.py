"""工具尝试到实际动作结果的统一投影；不执行工具、不读写状态。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


# These are host-owned lifecycle values.  They are deliberately structural;
# this projection must not infer recoverability from a tool name or from an
# error string supplied by the model/tool adapter.
_INDETERMINATE_STATES = frozenset({
    "unknown",
    "indeterminate",
    "sending",
    "side_effect_unknown",
})


def _cognitive_binding(item: Mapping[str, Any]) -> tuple[str, str, str, int] | None:
    """Return the host plan identity carried by one executed result.

    A status projection is allowed to reconcile only results that the host
    associated with a concrete task step and plan revision.  In particular,
    top-level model fields are not accepted as a substitute for the binding.
    """

    binding = item.get("_cognitiveStepBinding")
    if not isinstance(binding, Mapping):
        return None
    task_id = str(binding.get("taskId") or "").strip()
    step_id = str(binding.get("stepId") or "").strip()
    tool = str(binding.get("tool") or item.get("name") or "").strip()
    revision = binding.get("planRevision")
    if not task_id or not step_id or not tool or revision is None or isinstance(revision, bool):
        return None
    try:
        revision_number = int(str(revision).strip())
    except (TypeError, ValueError):
        return None
    if revision_number < 0:
        return None
    return task_id, step_id, tool, revision_number


def _is_real_success(item: Mapping[str, Any]) -> bool:
    """Recognize only a host result, never a model completion claim."""

    if item.get("ok") is True:
        return True
    receipt = item.get("actionReceipt")
    return (
        isinstance(receipt, Mapping)
        and str(receipt.get("status") or "").strip().lower() == "accepted"
    )


def _mapping_has_indeterminate_state(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for field in ("status", "executionStatus", "deliveryState", "outcome", "operationStatus"):
        state = str(value.get(field) or "").strip().lower()
        if state in _INDETERMINATE_STATES:
            return True
    return False


def _is_indeterminate_failure(item: Mapping[str, Any]) -> bool:
    """Keep failures whose side-effect state is not proven terminal.

    ``attempted=True`` is intentionally conservative: a failed attempted
    external action may have happened even when no receipt was returned.  The
    legacy todo validation result that motivated this projection has no
    ``attempted`` marker and has a normal envelope ``status=error``; it is
    therefore eligible for a newer bound plan result.  Explicit lifecycle
    states always win over that legacy fallback.
    """

    for value in (
        item,
        item.get("actionReceipt"),
        item.get("_toolResultEnvelope"),
    ):
        if _mapping_has_indeterminate_state(value):
            return True
        if isinstance(value, Mapping):
            if _mapping_has_indeterminate_state(value.get("error")):
                return True
            if _mapping_has_indeterminate_state(value.get("payload")):
                return True
    if item.get("attempted") is True or item.get("sent") is True:
        return True
    return False


def _is_supersedable_bound_failure(item: Mapping[str, Any]) -> bool:
    """Return whether a newer bound success may remove this item from status."""

    return (
        item.get("ok") is False
        and not _is_real_success(item)
        and not _is_indeterminate_failure(item)
    )


# 同一外部动作一旦 accepted 就是终态；后续失败重试不能覆盖已经发生的副作用。
# `effective_tool_results` remains the lossless evidence/receipt projection;
# status-only reconciliation is intentionally isolated below.
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


def effective_tool_results_for_status(
    results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project terminal tool status without rewriting execution evidence.

    Receipt identity remains the first projection, as before.  A second,
    status-only projection may hide a deterministic validation failure when a
    later host result for the *same* bound task step has a strictly higher
    cognitive plan revision and is a real success.  Missing bindings, equal
    revisions, different task/step/tool identities, and uncertain failures
    remain visible.  The original input sequence is never mutated and callers
    that need evidence continue to use :func:`effective_tool_results`.
    """

    projected = effective_tool_results(results)
    bindings = [_cognitive_binding(item) for item in projected]
    successful_revisions: dict[tuple[str, str, str], set[int]] = {}
    for item, binding in zip(projected, bindings):
        if binding is None or not _is_real_success(item):
            continue
        task_id, step_id, tool, revision = binding
        successful_revisions.setdefault((task_id, step_id, tool), set()).add(revision)

    if not successful_revisions:
        return projected

    selected: list[dict[str, Any]] = []
    for item, binding in zip(projected, bindings):
        if binding is None or not _is_supersedable_bound_failure(item):
            selected.append(item)
            continue
        task_id, step_id, tool, revision = binding
        newer_success = any(
            candidate_revision > revision
            for candidate_revision in successful_revisions.get((task_id, step_id, tool), ())
        )
        if not newer_success:
            selected.append(item)
    return selected
