"""Executable task steps: explicit bindings, never inferred from user wording."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping, Sequence
from agent.tool_arguments import TRANSPORT_ARGUMENT_FIELDS, file_tool_argument_error
from agent.tool_architecture import DEFAULT_TOOL_REGISTRY
from agent.todo_architecture import TodoTargetRef
from tools.safe_web import canonicalize_public_url


def _bounded_integer(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (ValueError, TypeError, OverflowError):
        return default


def canonical_tool_arguments(name: str, value: Any) -> dict[str, Any]:
    """Compare the operation after schema defaults, before transport identity.

    This is also used by server call signatures. Defaults never grant a
    permission or change the path/content selected by the model.
    """
    ignored = TRANSPORT_ARGUMENT_FIELDS
    args = {key: val for key, val in value.items() if key not in ignored} if isinstance(value, Mapping) else {}
    if file_tool_argument_error(name, args):
        return args  # Retain invalid input for diagnostics; never substitute a different target.
    if name in {"get_system_time", "capture_screen", "capture_camera"}:
        return {}
    if name == "todo_search":
        try:
            target = TodoTargetRef.from_mapping(args.get("targetRef") or args.get("target_ref") or args)
        except ValueError:
            return args
        return {"targetRef": target.to_dict(),
                "limit": _bounded_integer(args.get("limit") or 8, 1, 20, 8)}
    if name == "bookmark_list":
        return {"query": str(args.get("query") or args.get("name") or "").strip()[:240],
                "limit": _bounded_integer(args.get("limit"), 1, 200, 200)}
    if name == "bookmark_manage":
        action = str(args.get("action") or "create").strip().lower()
        operation = {
            "action": "create" if action == "save" else action,
            "bookmarkId": str(args.get("bookmarkId") or args.get("bookmark_id") or "").strip()[:100],
            "name": " ".join(str(args.get("name") or args.get("alias") or "").split())[:120],
            "expectedVersion": _bounded_integer(args.get("expectedVersion"), 0, 2_000_000_000, 0),
            "commitIntent": bool(args.get("commitIntent") or args.get("commit_intent")),
        }
        url = str(args.get("url") or "").strip()
        if url:
            try:
                url = canonicalize_public_url(url)
            except ValueError:
                pass
            operation["url"] = url
        return operation
    if name == "open_browser":
        # Resolver labels describe the destination; they do not change the
        # navigation selected by the model or establish a new executable step.
        operation = {key: val for key, val in args.items()
                     if key not in {"sourceTitle", "matchedAlias", "destinationId", "bookmarkId"}}
        if isinstance(operation.get("url"), str):
            try:
                operation["url"] = canonicalize_public_url(operation["url"])
            except ValueError:
                pass  # Keep malformed URLs for the existing argument validator.
        return operation
    if name == "list_directory":
        try:
            depth = max(1, min(4, int(args.get("maxDepth"))))
        except (ValueError, TypeError, OverflowError):
            depth = 2
        return {"path": str(args.get("path") or ".").strip(),
                "recursive": bool(args.get("recursive")), "maxDepth": depth}
    if name == "create_directory":
        return {"path": str(args.get("path") or ".").strip(),
                "createParents": bool(args.get("createParents", True))}
    if name == "read_text_file":
        return {"path": str(args.get("path") or ".").strip()}
    if name == "write_text_file":
        return {"path": str(args.get("path") or ".").strip(), "content": str(args.get("content") or ""),
                "mode": str(args.get("mode") or "create").strip().lower(),
                "createParents": bool(args.get("createParents"))}
    return args


def tool_arguments_match(name: str, supplied: Any, planned: Any) -> bool:
    """Match a selected operation while allowing its backend-owned resolution.

    Search history IDs and a URL resolved from an exact bookmark name are
    filled by the server constraint layer. They must not invalidate a plan
    which left those values unspecified. Concrete execution signatures still
    use canonical_tool_arguments, including the resolved IDs and URL.
    """
    actual = canonical_tool_arguments(name, supplied)
    expected = canonical_tool_arguments(name, planned)
    if name == "todo_search" and isinstance(planned, Mapping):
        target = planned.get("targetRef") or planned.get("target_ref") or planned
        if (isinstance(target, Mapping)
                and not any(key in target for key in ("recentTaskIds", "recent_task_ids"))
                and isinstance(actual.get("targetRef"), Mapping)
                and isinstance(expected.get("targetRef"), Mapping)):
            expected["targetRef"]["recentTaskIds"] = actual["targetRef"].get("recentTaskIds", [])
    if (name == "bookmark_manage" and expected.get("action") == "create"
            and expected.get("name") and not expected.get("url") and actual.get("url")):
        expected["url"] = actual["url"]
    return actual == expected


def normalized_tool_plan(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("tool plan must be a list of {stepId,name,args}")
    steps = []
    ids: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError("each executable step must be an object")
        name = str(raw.get("name") or raw.get("tool") or "").strip()
        args = raw.get("args", raw.get("arguments", {}))
        step_id = str(raw.get("stepId") or raw.get("id") or f"step-{index + 1}").strip()
        if not name or not isinstance(args, Mapping) or not step_id or step_id in ids:
            raise ValueError("executable steps need a tool name, argument object and unique stepId")
        ids.add(step_id)
        # Only backend receipts establish completion. Replacing a plan creates
        # pending steps rather than importing model-asserted execution history.
        steps.append({"stepId": step_id, "name": name, "args": deepcopy(dict(args)), "status": "pending"})
    return steps


def tool_plan_identity(value: Any) -> list[dict[str, Any]]:
    """Return the safe same-turn identity for an executable tool plan.

    Plan storage keeps the model's argument spelling so later diagnostics can
    explain it.  Same-turn replay compares the executable identity instead:
    schema defaults and transport fields are canonicalized, while step ID,
    tool name, and order remain part of the identity.  A plan with an unknown
    tool schema or invalid file-tool arguments cannot be treated as a replay.
    """
    plan = normalized_tool_plan(value)
    identity: list[dict[str, Any]] = []
    for step in plan:
        name = str(step["name"])
        if DEFAULT_TOOL_REGISTRY.get(name) is None:
            raise ValueError(f"tool schema is unknown: {name}")
        if file_tool_argument_error(name, step["args"]):
            raise ValueError(f"tool arguments are invalid: {name}")
        identity.append({
            "stepId": step["stepId"],
            "name": name,
            "args": canonical_tool_arguments(name, step["args"]),
        })
    return identity


def selected_tool_step(plan: Sequence[Any], selector: Mapping[str, Any]) -> dict[str, Any]:
    step_id = str(selector.get("stepId") or "")
    name = str(selector.get("tool") or selector.get("name") or "")
    candidates = [step for step in plan if isinstance(step, dict) and step.get("name")
                  and (not step_id or str(step.get("stepId")) == step_id)
                  and (not name or step.get("name") == name)]
    if len(candidates) != 1:
        raise ValueError("select one existing executable step by stepId; create REPLACE_TOOL_PLAN first if absent")
    return candidates[0]


def tool_plan_violations(task: Mapping[str, Any], requests: Sequence[Mapping[str, Any]]) -> list[str]:
    steps = [step for step in task.get("plan", []) if isinstance(step, Mapping) and step.get("name")]
    violations = []
    for request in requests:
        name = str(request.get("name") or request.get("tool") or "")
        step_id = str(request.get("stepId") or "")
        candidates = [step for step in steps if (str(step.get("stepId")) == step_id if step_id else step.get("name") == name)]
        if not candidates:
            if step_id or steps:
                violations.append("tool_not_in_current_plan:" + name)
            continue
        # Transport-owned identity may be attached between planning and actual
        # execution; all executable arguments must still match the new plan.
        supplied = request.get("args") or {}
        matches = [step for step in candidates if step.get("name") == name
                   and isinstance(supplied, Mapping)
                   and tool_arguments_match(name, supplied, step.get("args"))]
        if not matches:
            violations.append("tool_arguments_differ_from_current_plan:" + name)
        elif len(matches) > 1:
            violations.append("tool_step_ambiguous:" + name)
        elif not any(step.get("status", "pending") in {"pending", "failed"} for step in matches):
            violations.append("tool_step_not_executable:" + name)
    return violations


def tool_step_binding(task: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, Any]:
    if tool_plan_violations(task, [request]):
        return {}
    candidates = [step for step in task.get("plan", []) if isinstance(step, Mapping)
                  and step.get("name") == request.get("name")
                  and (not request.get("stepId") or request["stepId"] == step.get("stepId"))
                  and tool_arguments_match(str(step.get("name")), request.get("args"), step.get("args"))]
    if len(candidates) != 1:
        return {}
    return {"taskId": task.get("taskId"), "planRevision": task.get("planRevision", 0),
            "stepId": candidates[0]["stepId"]}


def receipt_matches_request(receipt: Mapping[str, Any], request: Mapping[str, Any]) -> bool:
    """Match an actual operation, including the public attachment reader alias."""
    if receipt.get("attempted") is False or receipt.get("error") == "cognitive_task_constraint":
        return False
    name = request.get("name")
    args = request.get("args") or {}
    if not isinstance(args, Mapping):
        return False
    if (name == "read_attachment" and receipt.get("name") in {"read_file", "read_image"}
            or name == "read_image" and receipt.get("name") == "read_image"):
        if set(args) - {"attachmentId", "id", "fileName", "name"}:
            return False
        target = args.get("attachmentId") or args.get("id")
        label = args.get("fileName") or args.get("name")
        return ((not target or target == receipt.get("attachmentId"))
                and (not label or label == receipt.get("fileName")))
    try:
        original = json.loads(receipt.get("_cognitiveRequestSignature") or "{}")
    except (TypeError, ValueError):
        return False
    return (receipt.get("name") == name == original.get("name")
            and tool_arguments_match(str(name), original.get("args"), args))


def apply_reused_step_binding(state: Mapping[str, Any], binding: Mapping[str, Any],
                              receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Project observed progress without changing an immutable execution receipt."""
    result = deepcopy(dict(state))
    task = result.get("task") or {}
    if binding.get("taskId") != task.get("taskId") or binding.get("planRevision") != task.get("planRevision", 0):
        return result
    request = binding.get("request") or {}
    for step in task.get("plan", []):
        if (step.get("stepId") != binding.get("stepId") or step.get("name") != request.get("name")
                or not tool_arguments_match(str(step.get("name")), request.get("args"), step.get("args"))
                or step.get("status") in {"invalidated", "cancelled", "completed"}):
            continue
        ids = binding.get("receiptIds") or []
        by_id = {receipt.get("toolCallId"): receipt for receipt in receipts}
        if not ids or any(ident not in by_id or not receipt_matches_request(by_id[ident], request) for ident in ids):
            continue
        step.update(status="completed" if all(by_id[ident].get("ok") for ident in ids) else "failed",
                    receiptId=ids[0], receiptIds=list(ids))
    return result


def project_tool_plan_receipts(state: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Apply actual current-turn receipts to a private task projection."""
    result = deepcopy(dict(state))
    task = result.get("task") or {}
    for entry in evidence.values():
        if not isinstance(entry, Mapping) or entry.get("historical"):
            continue
        receipt = entry.get("receipt") or {}
        if isinstance(receipt, Mapping) and receipt.get("attempted") is False:
            continue
        binding = receipt.get("_cognitiveStepBinding") if isinstance(receipt, Mapping) else None
        if not isinstance(binding, Mapping) or binding.get("taskId") != task.get("taskId"):
            continue
        if binding.get("planRevision") != task.get("planRevision", 0):
            continue
        for step in task.get("plan", []):
            if not isinstance(step, dict) or step.get("stepId") != binding.get("stepId") or step.get("name") != receipt.get("name"):
                continue
            if step.get("status") in {"invalidated", "cancelled"}:
                continue
            # A later rejected retry cannot undo an already successful action.
            if step.get("status") != "completed":
                step["status"] = "completed" if receipt.get("ok") else "failed"
                step["receiptId"] = receipt.get("toolCallId")
    return result


def project_durable_tool_progress(connection: Any, state: Mapping[str, Any], *,
                                 turn_id: str | None = None) -> dict[str, Any]:
    """Read execution progress independently of whether an answer committed.

    The journal is written by the executor, not model patches. Its progress
    shares the task's exact plan identity without advancing cognitive revision
    or promoting uncommitted beliefs. Ordinary historical evidence stays old.
    """
    task = state.get("task") or {}
    if not task.get("taskId") or not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_execution_evidence'"
    ).fetchone():
        return deepcopy(dict(state))
    rows = connection.execute("""SELECT receipt_json FROM cognitive_execution_evidence
        WHERE conversation_id=? AND json_extract(receipt_json, '$._cognitiveStepBinding.taskId')=?
        AND json_extract(receipt_json, '$._cognitiveStepBinding.planRevision')=?
        AND (? IS NULL OR turn_id=?)
        ORDER BY evidence_id""", (
            state.get("conversationId"), task["taskId"], task.get("planRevision", 0),
            turn_id, turn_id,
        ))
    evidence = {}
    for encoded, in rows:
        receipt = json.loads(encoded)
        if not receipt.get("toolCallId") or receipt.get("attempted") is False:
            continue
        try:
            request = json.loads(receipt.get("_cognitiveRequestSignature") or "{}")
        except (TypeError, ValueError):
            continue
        binding = receipt.get("_cognitiveStepBinding") or {}
        if not any(
            step.get("stepId") == binding.get("stepId") and step.get("name") == receipt.get("name") == request.get("name")
            and tool_arguments_match(str(step.get("name")), request.get("args"), step.get("args"))
            for step in task.get("plan", []) if isinstance(step, Mapping)
        ):
            continue
        evidence[receipt["toolCallId"]] = {"receipt": receipt}
    result = project_tool_plan_receipts(state, evidence)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_reused_step_bindings'").fetchone():
        rows = connection.execute("""SELECT turn_id, binding_json FROM cognitive_reused_step_bindings
            WHERE conversation_id=? AND task_id=? AND plan_revision=?
            AND (? IS NULL OR turn_id=?) ORDER BY binding_id""",
            (state.get("conversationId"), task["taskId"], task.get("planRevision", 0), turn_id, turn_id))
        for source_turn_id, encoded in rows:
            binding = json.loads(encoded)
            receipts = [json.loads(row[0]) for row in connection.execute("""SELECT receipt_json
                FROM cognitive_execution_evidence WHERE conversation_id=? AND turn_id=?""",
                (state.get("conversationId"), source_turn_id))]
            result = apply_reused_step_binding(result, binding, receipts)
    return result
