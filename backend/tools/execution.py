"""执行已验证的工具计划，保留并发分区与逐项持久化顺序。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping, Sequence

from agent.tool_architecture import ToolRegistry
from agent.turn_runtime import ToolResultEnvelope, utc_now_iso

from .validation import AUDIT_EXECUTOR_ID, ToolExecutionPlan


def execute_prepared_agent_tool(
    prepared: dict[str, Any],
    settings: dict[str, Any],
    user_question: str,
    request_id: str,
    *,
    registry: ToolRegistry,
    resolve_executor: Callable[[str], Callable[..., Any] | None],
    todo_manage: Callable[..., Any],
    bookmark_manage: Callable[..., Any],
    prepare_usage_alias: Callable[[str], str],
    write_log: Callable[..., Any],
) -> dict[str, Any]:
    """单项分派；浏览器隐藏作用域沿用请求 ID 防止恢复重放。"""

    name = str(prepared["name"])
    args = prepared["args"]
    execution_args = dict(args)
    if name == "open_browser" and request_id:
        execution_args["_requestId"] = request_id
        execution_args["_rememberUsage"] = True
    if name == "open_browser" and not execution_args.get("_usageAlias"):
        usage_alias = str(prepare_usage_alias(user_question) or "").strip()
        if usage_alias:
            execution_args["_usageAlias"] = usage_alias[:240]
    tool_call_id = str(prepared["toolCallId"])
    tool_started_at = utc_now_iso()
    definition = registry.get(name)
    write_log(
        "tool_execution_started",
        requestId=request_id,
        toolCallId=tool_call_id,
        toolName=name,
        family=definition.family if definition else "",
    )
    executor = resolve_executor(name)
    try:
        result: Any = (
            todo_manage(settings, execution_args, source_turn_id=request_id,
                        _time_anchor=prepared.get("timeAnchor"))
            if name == "todo_manage"
            else bookmark_manage(settings, execution_args, source_turn_id=request_id)
            if name == "bookmark_manage"
            else executor(settings, execution_args, user_question)
            if executor is not None
            else None
        )
    except Exception as error:
        result = {"name": name, "ok": False, "error": "tool_execution_exception",
                  "errorType": type(error).__name__, "message": str(error)[:500],
                  "toolCallId": tool_call_id, "attempted": True}
    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("name", name)
        result.setdefault("toolCallId", tool_call_id)
        provenance = {
            "executor": AUDIT_EXECUTOR_ID,
            "toolFamily": definition.family if definition else "",
        }
    else:
        result = {
            "name": name,
            "ok": False,
            "error": "tool_returned_invalid_result",
            "toolCallId": tool_call_id,
        }
        provenance = {"executor": AUDIT_EXECUTOR_ID}
    result["_toolResultEnvelope"] = ToolResultEnvelope.from_legacy_result(
        result,
        turn_id=f"{request_id}:turn" if request_id else "",
        started_at=tool_started_at,
        provenance=provenance,
    ).to_dict()
    write_log(
        "tool_result_received",
        requestId=request_id,
        toolCallId=tool_call_id,
        toolName=name,
        ok=bool(result.get("ok")),
        error=str(result.get("error") or ""),
    )
    return result


def execute_plan(
    plan: ToolExecutionPlan,
    settings: dict[str, Any],
    user_question: str,
    request_id: str,
    cognitive_state: Mapping[str, Any] | None,
    *,
    registry: ToolRegistry,
    execute_prepared: Callable[[dict[str, Any], dict[str, Any], str, str], dict[str, Any]],
    execute_todo_batch: Callable[..., list[dict[str, Any]]],
    record_cognitive_result: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """返回请求序结果；worker 内先保存结果，再读取任何 Future。

    todo 批量事务先于普通操作执行，即使普通操作在请求中排得更早。
    这是既有副作用顺序，不能由结果列表的顺序反推执行顺序。
    """

    if plan.early_results is not None:
        return plan.early_results
    operations = plan.operations

    def record_result(item: dict[str, Any]) -> dict[str, Any]:
        call_id = str(item.get("toolCallId") or "")
        observation_target = plan.validated_observation_targets.get(call_id)
        if observation_target:
            item["_validatedObservationTarget"] = observation_target
        signature = plan.cognitive_request_signatures.get(call_id)
        if signature:
            item["_cognitiveRequestSignature"] = signature
        binding = plan.cognitive_bindings.get(call_id)
        if binding:
            item["_cognitiveStepBinding"] = binding
        return record_cognitive_result(
            item, cognitive_state, request_id=request_id, user_question=user_question,
        )

    def execute_and_record(operation: dict[str, Any]) -> dict[str, Any]:
        # 一个后续工具失败时，已完成工具的收据仍必须先落盘。
        return record_result(execute_prepared(operation, settings, user_question, request_id))

    ordered_results: list[dict[str, Any] | None] = [None] * len(operations)
    todo_indexes = [
        index for index, operation in enumerate(operations)
        if operation.get("kind") == "execute" and operation.get("name") == "todo_manage"
    ]
    if len(todo_indexes) > 1:
        todo_results = execute_todo_batch(
            [operations[index] for index in todo_indexes], settings, request_id=request_id,
        )
        for operation_index, result in zip(todo_indexes, todo_results):
            ordered_results[operation_index] = record_result(result)
            operations[operation_index] = {"kind": "result", "result": result}
    index = 0
    while index < len(operations):
        operation = operations[index]
        if operation["kind"] == "result":
            # 保留批量 todo 在预处理和顺序遍历时各记录一次的现有行为。
            ordered_results[index] = record_result(operation["result"])
            index += 1
            continue
        if not registry.concurrency_safe([operation["name"]]):
            ordered_results[index] = execute_and_record(operation)
            index += 1
            continue
        partition_end = index + 1
        while (
            partition_end < len(operations)
            and operations[partition_end]["kind"] == "execute"
            and registry.concurrency_safe([operations[partition_end]["name"]])
        ):
            partition_end += 1
        partition = operations[index:partition_end]
        if len(partition) == 1:
            ordered_results[index] = execute_and_record(partition[0])
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(partition))) as executor:
                futures = [executor.submit(execute_and_record, item) for item in partition]
                for offset, future in enumerate(futures):
                    ordered_results[index + offset] = future.result()
        index = partition_end
    return [item for item in ordered_results if isinstance(item, dict)]
