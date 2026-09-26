"""日程决定到本地工具请求的纯投影规则。

模型只产生 ScheduleIntent；本模块根据已授权的 policy 投影读取/写入调用，
并把同一工具的原始模型 step 与规范化参数合并。它不访问 SQLite、模型或
当前会话状态，也不根据自然语言补充日程意图。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from agent.cognitive_tools import canonical_tool_arguments


def schedule_tool_requests(
    schedule_decision: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Translate an authorized semantic decision into concrete local calls."""

    decision = schedule_decision if isinstance(schedule_decision, Mapping) else {}
    intent = decision.get("intent") if isinstance(decision.get("intent"), Mapping) else {}
    policy = decision.get("policy") if isinstance(decision.get("policy"), Mapping) else {}
    action = str(policy.get("action") or "")
    if action == "read":
        commands = intent.get("commands") if isinstance(intent.get("commands"), list) else []
        list_command = next(
            (
                item for item in commands
                if isinstance(item, Mapping) and str(item.get("action") or "") == "list"
            ),
            {},
        )
        return [{
            "name": "todo_list",
            "args": {"scope": str(list_command.get("scope") or "active")},
            "reason": "model-decided schedule query",
            "selection": {"toolAuthorization": "read", "targetDomain": "local_schedule"},
        }]
    if action != "execute" or not bool(policy.get("allowed")):
        return []
    commands = intent.get("commands") if isinstance(intent.get("commands"), list) else []
    return [
        {
            "name": "todo_manage",
            "args": dict(command),
            "reason": "authorized ScheduleIntent command",
            "selection": {"toolAuthorization": "write", "targetDomain": "local_schedule"},
        }
        for command in commands
        if isinstance(command, Mapping)
    ]


def merge_schedule_tool_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    schedule_decision: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Make ScheduleIntent commands authoritative while retaining other tools."""

    scheduled = schedule_tool_requests(schedule_decision)
    if not scheduled:
        policy = (schedule_decision or {}).get("policy") or {}
        return [
            dict(item) for item in (requests or ())
            if isinstance(item, Mapping)
            and not (
                item.get("name") == "todo_manage"
                and policy
                and (policy.get("action") != "execute" or not policy.get("allowed"))
            )
        ]
    scheduled_names = {str(item.get("name") or "") for item in scheduled}
    originals = [dict(item) for item in (requests or ()) if isinstance(item, Mapping)]
    merged = [
        item for item in originals
        if str(item.get("name") or "") not in scheduled_names
    ]
    available = [
        item for item in originals
        if str(item.get("name") or "") in scheduled_names
    ]
    for scheduled_call in scheduled:
        for index, original in enumerate(available):
            if original.get("name") != scheduled_call["name"]:
                continue
            if scheduled_call["name"] == "todo_manage":
                try:
                    same_operation = (
                        canonical_tool_arguments("todo_manage", original.get("args") or {})
                        == canonical_tool_arguments("todo_manage", scheduled_call["args"])
                    )
                except ValueError:
                    same_operation = False
            else:
                same_operation = original.get("args") == scheduled_call["args"]
            if same_operation:
                # Keep the model's bound step and exact executable arguments.
                # Schema defaults in ScheduleIntent do not create a new action.
                scheduled_call = {
                    **deepcopy(original),
                    "reason": scheduled_call["reason"],
                    "selection": {
                        **dict(original.get("selection") or {}),
                        **scheduled_call["selection"],
                    },
                }
                available.pop(index)
                break
        merged.append(scheduled_call)
    return merged
