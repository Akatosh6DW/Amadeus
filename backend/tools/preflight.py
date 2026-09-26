"""工具预检、待办相关性与能力合同的应用边界。

本模块把同一轮的附件、工作区、视觉、日程和 Todo 证据合并到一个
``TurnPreflight``，并只把明确的模型决定投影为工具请求。状态读取、
请求缓存和诊断日志通过窄端口提供；工具 schema、联网规则和纯提示
直接复用各自领域 owner，不反向导入 server。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from agent.cognitive_brain import normalize_brain_container
from agent.cognitive_outcome import current_state_evidence_requirement
from agent.research_architecture import CapabilityEnvelope, ResearchDecision
from agent.tool_architecture import (
    DEFAULT_TOOL_REGISTRY,
    PerceptionNeed,
    TurnPreflight,
    assemble_tool_pool,
    build_turn_preflight,
)
from agent.todo_architecture import (
    TODO_NOOP_REQUESTABLE_FIELDS,
    todo_mutation_prohibited,
    todo_noop_target_matches,
    normalize_todo_noop_target,
)
from backend.research.intent import user_explicitly_requests_web_research
from backend.tools.validation import SENSOR_TOOL_NAMES
from todo_store import match_active_todos_to_dialogue, read_todo


@dataclass(frozen=True)
class ToolPreflightPorts:
    """预检服务需要的动态状态、附件规则和诊断端口。"""

    state_database: Callable[[], Any]
    todo_reconciliation: Callable[[dict[str, Any], str, Sequence[dict[str, Any]] | None], dict[str, Any]]
    current_pending_action: Callable[[], dict[str, Any]]
    cognitive_turn_state: Callable[[Mapping[str, Any]], dict[str, Any]]
    infer_workspace_current_state_request: Callable[[str, Mapping[str, Any] | None], Mapping[str, Any] | None]
    is_attachment_followup_query: Callable[[str], bool]
    write_log: Callable[..., Any]
    image_attachment_extensions: Callable[[], frozenset[str]]


class ToolPreflightService:
    """Owns one-turn preflight/reconciliation mutation and its model review projection."""

    def __init__(self, ports: ToolPreflightPorts) -> None:
        self.ports = ports


    def body_tool_preflight(
        self,
        body: dict[str, Any],
        user_message: str,
        messages: Sequence[dict[str, Any]] | None,
        *,
        reply_mode: str = "normal",
        speech_act: str = "conversation",
        settings: Mapping[str, Any] | None = None,
    ) -> TurnPreflight:
        """Create one semantic tool preflight shared by routing and execution."""

        raw_attachments: list[dict[str, Any]] = []
        for key in ("_processedAttachments", "attachments", "_continuedAttachments"):
            value = body.get(key)
            if isinstance(value, list):
                raw_attachments.extend(item for item in value if isinstance(item, dict))
        has_image = any(
            str(item.get("mimeType") or item.get("type") or "").lower().startswith("image/")
            or Path(str(item.get("name") or "")).suffix.lower() in self.ports.image_attachment_extensions()
            for item in raw_attachments
        )
        has_document = any(
            not (
                str(item.get("mimeType") or item.get("type") or "").lower().startswith("image/")
                or Path(str(item.get("name") or "")).suffix.lower() in self.ports.image_attachment_extensions()
            )
            for item in raw_attachments
        )
        artifact_id = next(
            (
                str(item.get("id") or item.get("attachmentId") or "").strip()
                for item in reversed(raw_attachments)
                if str(item.get("id") or item.get("attachmentId") or "").strip()
            ),
            None,
        )
        active = build_turn_preflight(
            user_message,
            messages,
            reply_mode=reply_mode,
            speech_act=speech_act,
            has_image_attachment=has_image,
            has_document_attachment=has_document,
            existing_artifact_id=artifact_id,
            schedule_intent=(
                body.get("_scheduleDecision")
                if isinstance(body.get("_scheduleDecision"), Mapping)
                else None
            ),
        )
        if settings is not None:
            workspace_request = self.ports.infer_workspace_current_state_request(
                user_message,
                body.get("_discourseLedger")
                if isinstance(body.get("_discourseLedger"), Mapping)
                else None,
            )
            workspace_path = str(
                ((workspace_request or {}).get("args") or {}).get("path") or ""
            ).strip()
            attachment_names = {
                Path(str(item.get("name") or item.get("fileName") or "")).name.casefold()
                for item in raw_attachments
                if str(item.get("name") or item.get("fileName") or "").strip()
            }
            attachment_target = bool(raw_attachments) and (
                self.ports.is_attachment_followup_query(user_message)
                or workspace_path in {"", "."}
                or Path(workspace_path).name.casefold() in attachment_names
            )
            requirement = (
                current_state_evidence_requirement(workspace_request)
                if workspace_request is not None
                and bool(settings.get("permissionFileRead"))
                and not attachment_target
                else None
            )
            if requirement is None:
                body.pop("_currentStateEvidenceRequirement", None)
            else:
                requirement_payload = requirement.to_dict()
                body["_currentStateEvidenceRequirement"] = requirement_payload
                active = replace(
                    active,
                    candidate_capabilities=tuple(dict.fromkeys((
                        *active.candidate_capabilities,
                        requirement.tool_name,
                    ))),
                    mandatory_capabilities=tuple(dict.fromkeys((
                        *active.mandatory_capabilities,
                        requirement.tool_name,
                    ))),
                    current_state_evidence_requirement=requirement_payload,
                    likely_no_tool=False,
                    reasons=tuple(dict.fromkeys((
                        *active.reasons,
                        "current_workspace_state_requires_current_receipt",
                    ))),
                )
        semantic_source = (body.get("_artifactTaskContext") or {}).get("contextSource")
        if semantic_source in {"dialogue", "new_screen", "new_camera"}:
            visual_names = {"read_image", *SENSOR_TOOL_NAMES}
            active = replace(
                active,
                candidate_capabilities=tuple(name for name in active.candidate_capabilities if name not in visual_names),
                mandatory_capabilities=tuple(name for name in active.mandatory_capabilities if name not in visual_names),
                perception=PerceptionNeed(),
            )
            if semantic_source != "dialogue":
                name = "capture_screen" if semantic_source == "new_screen" else "capture_camera"
                active = replace(
                    active, candidate_capabilities=(*active.candidate_capabilities, name),
                    mandatory_capabilities=(*active.mandatory_capabilities, name),
                    perception=PerceptionNeed(required=True, target_domain="screen" if semantic_source == "new_screen" else "physical_user", freshness="current"),
                    likely_no_tool=False,
                )
        continued_visuals = body.get("_continuedVisualArtifacts") or []
        if continued_visuals:
            selected = continued_visuals[-1]
            names = tuple(dict.fromkeys(str(item["toolName"]) for item in continued_visuals))
            active = replace(
                active,
                candidate_capabilities=tuple(dict.fromkeys((*active.candidate_capabilities, *names))),
                mandatory_capabilities=tuple(dict.fromkeys((
                    *(name for name in active.mandatory_capabilities if name not in SENSOR_TOOL_NAMES), *names,
                ))),
                perception=PerceptionNeed(
                    required=True, target_domain=selected["targetDomain"],
                    freshness="existing_artifact", existing_artifact_id=str(selected["id"]),
                ),
                likely_no_tool=False,
                reasons=(*active.reasons, "semantic_task_bound_saved_visual_artifact"),
            )
        todo_reconciliation = self.ports.todo_reconciliation(
            body,
            user_message,
            messages,
        )
        pending_todo = self.ports.current_pending_action()
        if (
            pending_todo.get("type") == "todo_mutation"
            and str(pending_todo.get("status") or "") != "paused"
            and not self.ports.cognitive_turn_state(body)
            and not todo_mutation_prohibited(user_message)
        ):
            # The previous turn already authorized a Todo mutation but could not
            # safely execute it without more information.  Re-open the capability
            # contract for the next reply; the model still has to decide whether
            # that reply semantically continues the old request.
            active = replace(
                active,
                candidate_capabilities=tuple(dict.fromkeys((
                    *active.candidate_capabilities,
                    "todo_search",
                    "todo_manage",
                ))),
                mandatory_capabilities=tuple(dict.fromkeys((
                    *active.mandatory_capabilities,
                    "todo_manage",
                ))),
                likely_no_tool=False,
                reasons=tuple(dict.fromkeys((
                    *active.reasons,
                    "todo_continuation:awaiting_semantic_decision",
                ))),
            )
            self.ports.write_log(
                "todo_continuation_preflight_activated",
                requestId=str(body.get("requestId") or "")[:120],
                actionId=str(pending_todo.get("actionId") or "")[:100],
                status=str(pending_todo.get("status") or "")[:40],
            )
        body["_toolPreflight"] = active
        return active

    def body_todo_reconciliation(
        self,
        body: dict[str, Any],
        user_message: str,
        messages: Sequence[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Cache the read-only Todo/dialogue relevance pass for this turn."""

        cached = body.get("_todoReconciliation")
        if isinstance(cached, dict):
            return cached
        try:
            with self.ports.state_database() as connection:
                reconciliation = match_active_todos_to_dialogue(
                    connection,
                    user_message,
                    messages,
                )
        except (OSError, sqlite3.Error, RuntimeError) as error:
            reconciliation = {
                "active": False,
                "activeTaskCount": 0,
                "strongMatch": False,
                "requiresModelDecision": False,
                "matchSource": "unavailable",
                "candidates": [],
                "recentDialogue": [],
                "error": type(error).__name__,
            }
        body["_todoReconciliation"] = reconciliation
        self.ports.write_log(
            "todo_reconciliation_prefilter",
            requestId=str(body.get("requestId") or ""),
            activeTaskCount=int(reconciliation.get("activeTaskCount") or 0),
            strongMatch=bool(reconciliation.get("strongMatch")),
            matchSource=str(reconciliation.get("matchSource") or "none"),
            candidateTaskIds=[
                str(item.get("taskId") or "")
                for item in reconciliation.get("candidates", [])
                if isinstance(item, Mapping)
            ],
        )
        return reconciliation

    def apply_model_todo_review(self, value: dict[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
        """Translate the model's explicit completion decisions into ordinary calls.

        No interpretation of user/reply text, database writes, or model retries here.
        Only a typed model decision can assert that the current user goal is already
        satisfied; candidate identity/version and the fresh SQLite row remain the
        backend binding and authority.
        """

        # A retry candidate must prove its own current readback.  Never let an
        # accepted private marker from an earlier candidate authorize a later one.
        if isinstance(body, dict):
            body.pop("_cognitiveTodoNoOp", None)
        directive = value.get("cognitive")
        reviews = directive.get("todoReview") if isinstance(directive, Mapping) else None
        if not isinstance(reviews, list):
            return value
        value = normalize_brain_container(value)
        if value.get("kind") not in {"turn", "tool_use"}:
            return value
        candidates = {item.get("taskId"): item for item in (body.get("_todoReconciliation") or {}).get("candidates", [])
                      if isinstance(item, Mapping) and item.get("status") in {"pending", "in_progress"}}
        calls = value.get("calls") or value.get("toolRequests") or []
        if not isinstance(calls, list):
            return value
        satisfied_reviews = [
            review for review in reviews
            if isinstance(review, Mapping)
            and review.get("decision") == "desired_target_already_satisfied"
        ]
        if satisfied_reviews and len(satisfied_reviews) == len(reviews) and not calls:
            # Only this explicit typed outcome can be terminal.  Ordinary
            # ``keep`` is merely a model observation and must still pass the
            # normal cognitive task-revision gate.  The target is resolved
            # from the current user goal; it is not permission to copy an
            # arbitrary candidate snapshot.
            marker_items: list[dict[str, Any]] = []
            seen_task_ids: set[str] = set()
            reconciliation = body.get("_todoReconciliation")
            match_source = (
                str(reconciliation.get("matchSource") or "")
                if isinstance(reconciliation, Mapping)
                else ""
            )
            try:
                with self.ports.state_database() as connection:
                    for review in satisfied_reviews:
                        task_id = review.get("taskId")
                        version = review.get("version")
                        target = review.get("desiredTarget")
                        requested_fields = review.get("requestedFields")
                        cached = candidates.get(task_id) if isinstance(task_id, str) else None
                        match_type = str((cached or {}).get("matchType") or "")
                        match_score = int((cached or {}).get("matchScore") or 0)
                        bound_to_current_goal = (
                            (
                                match_source == "current_utterance"
                                and match_type in {
                                    "exact_title", "title_contains", "query_contains_title",
                                    "details_contains", "similar_title",
                                }
                                and match_score >= 500
                            )
                            or (
                                match_source == "recent_task_reference"
                                and match_type == "recent_task_reference"
                            )
                        )
                        valid_requested_fields = (
                            isinstance(requested_fields, list)
                            and bool(requested_fields)
                            and all(
                                isinstance(field, str)
                                and field in TODO_NOOP_REQUESTABLE_FIELDS
                                for field in requested_fields
                            )
                            and len(requested_fields) == len(set(requested_fields))
                        )
                        if (
                            not isinstance(task_id, str) or not task_id.strip()
                            or isinstance(version, bool) or not isinstance(version, int) or version <= 0
                            or task_id in seen_task_ids
                            or review.get("targetSource") != "current_user_goal"
                            or not bound_to_current_goal
                            or not valid_requested_fields
                            or not isinstance(target, Mapping)
                            or normalize_todo_noop_target(target) is None
                            or not cached or cached.get("version") != version
                        ):
                            marker_items = []
                            break
                        current = read_todo(connection, task_id)
                        if (
                            not current
                            or current.get("status") not in {"pending", "in_progress"}
                            or current.get("version") != version
                            or not todo_noop_target_matches(current, target)
                        ):
                            marker_items = []
                            break
                        seen_task_ids.add(task_id)
                        marker_items.append({
                            "taskId": task_id,
                            "version": version,
                            "decision": "desired_target_already_satisfied",
                            "targetSource": "current_user_goal",
                            "requestedFields": list(requested_fields),
                            "desiredTarget": dict(normalize_todo_noop_target(target) or {}),
                        })
            except (OSError, sqlite3.Error, RuntimeError):
                marker_items = []
            if marker_items and len(marker_items) == len(satisfied_reviews) and isinstance(body, dict):
                body["_cognitiveTodoNoOp"] = {
                    "source": "authoritative_todo_readback",
                    "items": marker_items,
                }
                self.ports.write_log(
                    "cognitive_todo_noop_accepted",
                    requestId=body.get("requestId"),
                    taskIds=[item["taskId"] for item in marker_items],
                    versions=[item["version"] for item in marker_items],
                )
            return value
        handled = {str((item.get("args") or {}).get("taskId")
                       or ((item.get("args") or {}).get("targetRef") or {}).get("taskId") or "") for item in calls
                   if isinstance(item, Mapping) and item.get("name") == "todo_manage"}
        # A follow-up must not replay an earlier decision after either success or
        # failure. A model can still request an explicit retry through normal calls.
        for entry in (body.get("_cognitiveEvidence") or {}).values():
            if not isinstance(entry, Mapping) or entry.get("historical"):
                continue
            receipt = (entry.get("receipt") or {}).get("actionReceipt") or {}
            if receipt.get("tool") == "todo_manage" and receipt.get("action") == "complete":
                handled.add(str(receipt.get("taskId") or ""))
        added = []
        for review in reviews:
            if not isinstance(review, Mapping) or review.get("decision") != "complete":
                continue
            task_id = review.get("taskId")
            if not isinstance(task_id, str) or task_id in handled:
                continue
            task = candidates.get(task_id)
            if not task or not isinstance(task.get("version"), int):
                continue
            added.append({"name": "todo_manage", "selection": {"toolAuthorization": "write"},
                          "args": {"action": "complete", "taskId": task_id,
                                   "expectedVersion": task["version"], "commitIntent": True}})
            handled.add(task_id)
        if not added:
            return value
        result = deepcopy(value)
        result.update(kind="tool_use", calls=[*calls, *added])
        # This draft precedes execution. Only the tool continuation may speak about
        # its result, using the actual receipt.
        for key in ("toolRequests", "outcome", "speechUnits", "reply", "zh", "ja"):
            result.pop(key, None)
        self.ports.write_log("cognitive_todo_review_materialized", requestId=body.get("requestId"),
                         taskIds=[item["args"]["taskId"] for item in added], addedModelCalls=0)
        return result


def todo_reconciliation_prompt(value: Any) -> str:
    """Give the model candidates and a decision contract, never a write verdict."""

    reconciliation = value if isinstance(value, Mapping) else {}
    if not reconciliation.get("candidates") and not reconciliation.get("previousReminder"):
        return ""
    payload = {
        "matchSource": reconciliation.get("matchSource"),
        "candidates": reconciliation.get("candidates", []),
        "recentDialogue": reconciliation.get("recentDialogue", []),
        "activeTaskCount": reconciliation.get("activeTaskCount", 0),
        "truncated": bool(reconciliation.get("truncated")),
        "previousReminder": reconciliation.get("previousReminder"),
    }
    return (
        "[活动中日程的对话核对]\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n这是本轮从待办数据库读取的活动任务快照；历史提醒仅用于关联目标，版本和状态以此快照为准。"
        "待办 taskId 与 packet.task.taskId 属于不同状态：前者是用户事项，后者是助手的工具执行计划。"
        "用户事项尚未完成，不表示以前已经结束的助手计划需要恢复，也不要因此把 outcome.taskStatus 改为 active。"
        "候选列表不代表任务已经完成，也不要求你调用工具；active_inventory 只是少量待办背景，并不表示当前话题与它们相关。"
        "结合用户当前话语、紧邻对话及任务 title/details 自主判断是否有真实进度反馈。"
        "若用户明确反馈自己的对应任务目标已达成，应调用 todo_manage(action=complete)，无需再让用户说‘标记完成’或确认一次。"
        "到场/开始类提醒在用户已到场/已开始时即可完成；整项工作或课程的完成任务则不能因刚开始就标完成。"
        "previousReminder.recurrenceAdvanced 表示那次重复提醒已结束并顺延；用户回应那次提醒不能把下一次也标完成。"
        "计划、否定、假设、他人的进度或无关闲聊都不应触发完成。"
        "目标明确时直接用此快照中的 taskId、version 作为 expectedVersion，设置 commitIntent=true 和 selection.toolAuthorization=write；"
        "不必为了形式重复 todo_search。缺少目标或快照不足时再检索；结合对话仍有歧义才请用户选择。"
        "先在 cognitive.todoReview 中逐项给出候选的简短判断："
        "[{taskId:真实编号,version:当前版本,decision:'complete'|'keep'|'desired_target_already_satisfied',reason:结合当前原话和任务目标的简短理由}]。"
        "普通 keep 只是‘本轮不修改’的观察，永远不能绕过 TASK_REVISION，也不能凭复制快照终止当前目标。"
        "只有用户本轮明确要求修改某个任务参数，并且你从当前用户目标解析出的结果已经与数据库当前状态一致时，才可使用"
        "decision='desired_target_already_satisfied'；这不是 complete，也不是对无关任务、他人进度或假设问题的判断。"
        "该决定必须同时包含 targetSource='current_user_goal'、非空 requestedFields（只能是 title、details、status、priority、dueAt、dueDate、timezone、remindBeforeMinutes、recurrenceRule、snoozedUntil），"
        "以及完整 desiredTarget。desiredTarget 必须包含 title、details、status、priority、dueAt、dueDate、allDay、timezone、remindBeforeMinutes、recurrenceRule、snoozedUntil 十一个字段；"
        "用户没有改动的字段从当前候选快照继承，用户本轮要求的字段按当前目标解析，不能把‘复制快照’当成用户目标。"
        "示例："
        '{"taskId":"候选中的taskId","version":1,"decision":"desired_target_already_satisfied","targetSource":"current_user_goal","requestedFields":["remindBeforeMinutes"],"desiredTarget":{"title":"原标题","details":"原详情","status":"pending","priority":"normal","dueAt":"","dueDate":"","allDay":true,"timezone":"Europe/London","remindBeforeMinutes":0,"recurrenceRule":"","snoozedUntil":""},"reason":"用户要求不提前提醒，目标与当前权威状态逐项一致"}。'
        "缺少 targetSource/requestedFields/version/desiredTarget、字段不全、目标任一字段不同、当前任务绑定不明确或版本过期时，不能使用该终态决定；它会继续经过普通认知校验。"
        "complete 是你决定执行标记完成，keep 是不修改；无关、他人、假设、尚未达成或目标仍需变化时选择 keep 或正常工具调用。"
        "这是同一次输出的判断，不另行调用审核模型；上次的理解或旧回执不能代替当前数据库状态。"
        "判断后再选择 kind：有 complete 时使用 kind=tool_use，并在 calls 中调用 todo_manage；"
        "你已明确选择 complete 但漏写 calls 时，后端会用此快照补齐同一调用，仍走原有权限和版本检查。"
        "单写 scheduleIntent、outcome 或认知任务状态不会代替工具执行。"
        "历史创建/修改回执不能证明本轮完成操作成功。先请求调用，再根据新回执回复。"
        "任何成功表述都必须以 todo_manage 的 accepted actionReceipt 为依据。"
        "\n若判断应完成，调用容器结构如下（taskId 和 expectedVersion 替换为所选任务的真实值）："
        '{"kind":"tool_use","cognitive":{"interactionAct":"EXECUTION_FEEDBACK",'
        '"understoodGoal":"根据用户进度更新对应待办","patches":[]},'
        '"calls":[{"name":"todo_manage","selection":{"toolAuthorization":"write"},'
        '"args":{"action":"complete","taskId":"候选中的taskId","expectedVersion":1,"commitIntent":true}}]}。'
        "若判断无须更新，按普通对话容器回复即可；后端只会在本轮再次读取数据库、核对当前用户目标的完整 desiredTarget、版本和明确任务绑定后把该 typed decision 视为终态 no-op。"
    )

def capability_with_tool_preflight(
    capability: CapabilityEnvelope,
    preflight: TurnPreflight,
    settings: dict[str, Any],
    *,
    extra_tool_ids: Sequence[str] = (),
) -> CapabilityEnvelope:
    """Merge non-web tools into the same per-turn capability envelope."""

    pool_preflight = preflight
    if (
        preflight.perception.target_domain == "unknown_visual"
        and preflight.perception.confidence < 0.65
        and not preflight.mandatory_capabilities
    ):
        # Ambiguous, low-confidence mentions remain capability-index hints.
        # Loading full camera/screen schemas here made those hints look like a
        # command to the model and caused non-visual turns to call vision.
        pool_preflight = replace(
            preflight,
            candidate_capabilities=(),
            likely_no_tool=True,
        )
    pool = assemble_tool_pool(
        pool_preflight,
        research_policy=capability.research_policy,
        extra_tool_ids=extra_tool_ids,
    )
    current_requirement = (
        preflight.current_state_evidence_requirement
        if isinstance(preflight.current_state_evidence_requirement, Mapping)
        else {}
    )
    current_tool_id = str(current_requirement.get("toolName") or "")
    current_tool = DEFAULT_TOOL_REGISTRY.get(current_tool_id)
    current_tool_permitted = bool(
        current_tool is not None
        and (
            not current_tool.permission_key
            or bool(settings.get(current_tool.permission_key))
        )
    )
    # 当前状态读取不依赖十项通用池的截断顺序；只有已授权的精确只读
    # schema 才进入本轮模型可调用集合。
    pool_ids = tuple(dict.fromkeys((
        *((current_tool_id,) if current_tool_permitted else ()),
        *(item.tool_id for item in pool),
    )))
    permitted_ids = tuple(
        item.tool_id
        for item in pool
        if not item.permission_key or bool(settings.get(item.permission_key))
    )
    allowed = tuple(dict.fromkeys([
        *capability.allowed_tools,
        *((current_tool_id,) if current_tool_permitted else ()),
        *permitted_ids,
    ]))
    required = tuple(dict.fromkeys([
        *capability.required_tools,
        *preflight.mandatory_capabilities,
    ]))
    if preflight.mandatory_capabilities:
        tool_policy = "required"
    elif preflight.perception.target_domain == "unknown_visual":
        tool_policy = "clarify_or_model_decides"
    else:
        tool_policy = "model_decides"
    return replace(
        capability,
        allowed_tools=allowed,
        required_tools=required,
        may_escalate=bool(capability.may_escalate or preflight.tool_escalation_allowed),
        max_tool_rounds=max(2, capability.max_tool_rounds),
        tool_policy=tool_policy,
        candidate_tool_families=tuple(dict.fromkeys(item.family for item in pool)),
        initially_loaded_tool_ids=pool_ids,
        may_search_tools=True,
        perception_target=preflight.perception.target_domain,
        ambiguities=preflight.perception.ambiguities,
    )

def reconcile_research_with_tool_preflight(
    decision: ResearchDecision,
    preflight: TurnPreflight,
    user_message: str,
) -> ResearchDecision:
    """Keep private/local perception out of the public-web research policy."""

    if (
        preflight.perception.required
        and preflight.perception.target_domain != "none"
        and not user_explicitly_requests_web_research(user_message)
    ):
        return replace(
            decision,
            policy="forbidden",
            reasons=tuple(dict.fromkeys([
                "local_perception_not_public_research",
                *decision.reasons,
            ])),
            unknown_entities=(),
            unstable_claims=(),
            verification_targets=(),
            primary_source_required=False,
            source_policy_id=None,
        )
    return decision

def tool_preflight_prompt(
    preflight: TurnPreflight,
    capability: CapabilityEnvelope,
) -> str:
    current_requirement = (
        preflight.current_state_evidence_requirement
        if isinstance(preflight.current_state_evidence_requirement, Mapping)
        else None
    )
    current_definition = DEFAULT_TOOL_REGISTRY.get(
        str((current_requirement or {}).get("toolName") or "")
    )
    current_state_tool = (
        {
            "requirement": dict(current_requirement),
            "toolDefinition": current_definition.to_dict(),
        }
        if current_requirement is not None and current_definition is not None
        else None
    )
    return (
        "[统一工具选择前置]\n"
        + json.dumps(
            {
                "preflight": preflight.to_dict(),
                "capabilityEnvelope": capability.to_dict(),
                "capabilityIndex": [
                    {"id": item.tool_id, "family": item.family, "summary": item.description}
                    for item in DEFAULT_TOOL_REGISTRY.all()
                ],
                "toolDefinitions": [
                    definition.to_dict()
                    for tool_id in capability.initially_loaded_tool_ids
                    for definition in (DEFAULT_TOOL_REGISTRY.get(tool_id),)
                    if definition is not None
                ],
                "currentStateEvidenceTool": current_state_tool,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n规则：分类结果只是工具候选提示，不是禁止调用工具的闸门。"
        "是否联网必须由你结合本轮完整语义决定，不能因为出现“今天、现在、最近、研究、项目”"
        "或任何单个词就调用联网工具。个人感受、关系交流、创作和可自包含推理通常无需联网；"
        "只有回答确实依赖当前公开事实、指定网页或可核验来源时才发出 web tool_use。"
        "不得在没有充分网页证据回执时输出 [S1] 一类引用。"
        "requiredTools 非空时，必须先取得对应工具证据再作答。"
        "前置感知目标为 unknown_visual 时，它只是模型判断前的低置信候选，不是最终否决；"
        "你必须结合本轮完整语义自行解析目标。能解析时用 tool_use.targetDomain 明确给出目标并选择匹配工具；"
        "只有完整语义仍不足以区分屏幕、用户本人或已上传图片时，才自然地追问一个最小澄清问题。"
        "若回答过程中才发现缺少外部事实、画面、文件或动作能力，可通过 toolRequests 请求已加载工具；"
        "currentStateEvidenceTool 非空时，当前本地状态必须由其中精确工具和参数的新成功回执支持；"
        "历史回执、失败回执、其他路径或其他工具不能替代。没有取得回执时只能如实说明尚未完成，不能给出精确状态断言。"
        "未加载完整定义时，用 capabilityQuery 请求能力搜索。找不到合适工具时应说明缺少的能力类别，"
        "而不是编造观察或执行结果。没有取得 capture_camera/capture_screen 的成功工具结果时，"
        "不得用‘我看看、我看一眼、正在看’之类的人格台词冒充已经开始或完成了实时观察；"
        "若确实需要看，必须先输出结构化 tool_use。"
        "当答案正确性依赖精确字符、子串、文本长度或基础算术时，可调用 local_compute；"
        "普通闲聊和不依赖精确数值的表达不需要调用。工具返回后以其 result 为准，不要再凭语言重新心算。"
    )

def reconcile_research_decision_with_tool_requests(
    decision: ResearchDecision,
    tool_requests: Sequence[dict[str, Any]],
) -> ResearchDecision:
    """Allow a grounded external action to resolve its public target."""

    browser_resolution = any(
        str(item.get("name") or "") == "web_search"
        and isinstance(item.get("args"), dict)
        and item["args"].get("browserTargetResolution")
        for item in tool_requests
        if isinstance(item, dict)
    )
    if not browser_resolution or decision.policy != "forbidden":
        return decision
    return replace(
        decision,
        policy="required",
        reasons=tuple(dict.fromkeys(
            (*decision.reasons, "browser_target_requires_verified_public_url")
        )),
        verification_targets=tuple(dict.fromkeys(
            (*decision.verification_targets, "verified direct public URL")
        )),
        confidence=min(decision.confidence, 0.85),
    )
