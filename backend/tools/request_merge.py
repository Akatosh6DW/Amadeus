"""执行前合并工具细化：保留独立只读请求，避免竞争的副作用调用。"""
from __future__ import annotations
from typing import Any, Mapping, Sequence
from backend.tools.validation import tool_request_signature, structured_tool_authorization, structured_browser_authorized
from backend.tools.browser_rules import browser_request_resolution_rank

def merge_preexecution_tool_requests(
    existing: Sequence[Mapping[str, Any]],
    refinements: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge refinements without executing competing side-effect calls.

    Concrete call signatures still distinguish independent read-only work. A
    browser escalation refines the one turn-level navigation action, so a
    grounded URL replaces an unresolved parser proposal instead of running in
    parallel with it.
    """

    merged = [dict(item) for item in existing if isinstance(item, Mapping)]
    structured_bookmark_write = any(
        str(item.get("name") or "") == "bookmark_manage"
        and structured_tool_authorization(item) == "write"
        for item in refinements
        if isinstance(item, Mapping)
    )
    structured_navigation = any(
        structured_browser_authorized(item)
        for item in refinements
        if isinstance(item, Mapping)
    )
    if structured_bookmark_write and not structured_navigation:
        # The semantic model has interpreted the whole utterance as a bookmark
        # mutation.  A lexical hint such as “以后打开这个称呼” must not survive
        # as an immediate browser action unless the same structured plan also
        # explicitly authorizes navigation.
        merged = [
            item for item in merged
            if str(item.get("name") or "") != "open_browser"
        ]
    signatures = {tool_request_signature(item) for item in merged}
    for raw in refinements:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        signature = tool_request_signature(item)
        if signature in signatures:
            continue
        if str(item.get("name") or "") == "open_browser":
            browser_indexes = [
                index
                for index, current in enumerate(merged)
                if str(current.get("name") or "") == "open_browser"
            ]
            if browser_indexes:
                best_index = max(
                    browser_indexes,
                    key=lambda index: browser_request_resolution_rank(merged[index]),
                )
                if browser_request_resolution_rank(item) >= browser_request_resolution_rank(
                    merged[best_index]
                ):
                    first_index = browser_indexes[0]
                    merged = [
                        current
                        for index, current in enumerate(merged)
                        if index not in browser_indexes
                    ]
                    merged.insert(first_index, item)
                    signatures = {
                        tool_request_signature(current) for current in merged
                    }
                continue
        merged.append(item)
        signatures.add(signature)
    return merged
