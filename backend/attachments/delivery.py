"""回复修订后的附件交付事实：保留完成结果，撤销不再交付的文件。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


def preserve_attachment_delivery(source: Mapping[str, Any], rewritten: dict[str, Any]) -> None:
    """Style rewrites retain delivery facts; changed decisions cannot resurrect files."""
    action = rewritten.get("attachmentAction") or source.get("attachmentAction")
    if not isinstance(action, Mapping):
        return
    rewritten["attachmentAction"] = deepcopy(dict(action))
    if action == source.get("attachmentAction"):
        for key in ("outcome", "_cognitiveOutcome", "requiresUserResponse"):
            if key in source:
                rewritten[key] = deepcopy(source[key])
    deliver = action.get("kind") in {"edit", "create"} and action.get("status") == "completed"
    rewritten["_fileOutputExpected"] = deliver
    if not deliver:
        rewritten.pop("_fileOutputs", None)
        rewritten.pop("files", None)
