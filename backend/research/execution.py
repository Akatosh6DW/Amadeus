"""研究工具执行与恢复协调：独立操作回执不能被合并证据取代。

从同一状态库恢复本轮回执，维持既有工具身份、指纹与预算，再交给研究 loop。
"""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Callable, Mapping
import json
import time
import uuid
from agent.fingerprints import legacy_persistent_fingerprint
from agent.cognitive_runtime import collect_evidence
from agent.cognitive_tools import tool_step_binding
from agent.cognitive_evidence_journal import read_receipts as read_cognitive_execution_receipts
from agent.research_architecture import ResearchDecision, CapabilityEnvelope
from tools.safe_web import canonicalize_public_url, SafeWebError
from backend.tools.validation import tool_request_signature
from backend.chat.upstream_pipeline import tool_result_was_attempted
from backend.research import loop as research_loop, current_info as research_current_info

@dataclass(frozen=True)
class ResearchExecutionPorts:
    database: Callable[..., Any]
    state: Callable[..., dict[str, Any]]
    turn_state: Callable[..., dict[str, Any]]
    record: Callable[..., dict[str, Any]]
    bind_reused: Callable[..., Any]
    web_search: Callable[..., dict[str, Any]]
    read_page: Callable[..., dict[str, Any]]
    log: Callable[..., Any]
    rounds: Callable[[], Any]
    store: Callable[[], Any]
    checks: Callable[[], Any]

class ResearchExecution:
    def __init__(self, ports: ResearchExecutionPorts) -> None:
        self.ports = ports

    def bounded_research_answer_scope(
        self,
        plan: Mapping[str, Any],
        cognitive_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """保留认知状态的当前读取入口，纯范围规则交给 temporal 模块。"""
        plan_scope = plan.get("answerScope") if isinstance(plan.get("answerScope"), Mapping) else {}
        state = self.ports.turn_state(cognitive_body or {})
        context = state.get("context") if isinstance(state.get("context"), Mapping) else {}
        state_scope = context.get("answerScope") if isinstance(context.get("answerScope"), Mapping) else {}
        return research_current_info.normalize_research_answer_scope(plan_scope, state_scope)


    def execute_research_tool(
        self,
        name: str, settings: dict[str, Any], args: dict[str, Any], *,
        cognitive_body: dict[str, Any] | None = None, request_id: str = "",
    ) -> dict[str, Any]:
        """Keep each research operation's identity separate from merged web evidence."""
        state = self.ports.state(cognitive_body or {})
        target_key = "query" if name == "web_search" else "url"
        def target_identity(value: Any) -> str:
            target = str(value or "").strip()
            try:
                return canonicalize_public_url(target) if name == "read_web_page" else target
            except (SafeWebError, ValueError, TypeError):
                return target

        # A research plan adds ranking/provenance hints to the concrete model call.
        # Retain that original call (including explicit freshness/provider options)
        # when its target is unique; extra research queries remain unbound.
        candidates = [step for step in (state.get("task") or {}).get("plan", [])
                      if step.get("name") == name
                      and target_identity((step.get("args") or {}).get(target_key)) == target_identity(args.get(target_key))
                      and step.get("status") not in {"cancelled", "invalidated"}]
        request = {"name": name, "args": {target_key: args[target_key]}}
        if len(candidates) == 1:
            request.update(args=deepcopy(candidates[0]["args"]), stepId=candidates[0]["stepId"])
        signature = tool_request_signature(request)
        if cognitive_body is not None:
            for entry in (cognitive_body.get("_cognitiveEvidence") or {}).values():
                receipt = entry.get("receipt") or {}
                if (not entry.get("historical") and tool_result_was_attempted(receipt)
                        and receipt.get("_cognitiveRequestSignature") == signature):
                    # Reuse the actual operation result, never mint a new receipt
                    # or attach its identity to an aggregate result.
                    self.ports.bind_reused(cognitive_body, state, [request], [receipt])
                    if state:
                        self.ports.turn_state(cognitive_body)["task"] = deepcopy(state["task"])
                    return {key: deepcopy(value) for key, value in receipt.items()
                            if not key.startswith("_cognitive") and key != "toolCallId"}
        executor = self.ports.web_search if name == "web_search" else self.ports.read_page
        try:
            result = executor(settings, {**args, **request["args"]})
        except Exception as error:
            result = {"name": name, "ok": False, "error": "tool_execution_exception",
                      "errorType": type(error).__name__, "message": str(error)[:500], "attempted": True}
        if cognitive_body is not None and state:
            receipt = {**deepcopy(result), "name": name, "toolCallId": f"tool_{uuid.uuid4().hex}",
                       "_cognitiveRequestSignature": signature}
            binding = tool_step_binding(state.get("task") or {}, request)
            if binding:
                receipt["_cognitiveStepBinding"] = binding
            self.ports.record(receipt, state, request_id=request_id)
            cognitive_body.setdefault("_cognitiveEvidence", {}).update(collect_evidence([receipt]))
            self.ports.log("research_tool_receipt_recorded", requestId=request_id, tool=name,
                             toolCallId=receipt["toolCallId"], ok=bool(receipt.get("ok")),
                             taskId=binding.get("taskId"), stepId=binding.get("stepId"))
        return result


    def run_research_query_loop(
        self,
        settings: dict[str, Any],
        user_message: str,
        messages: list[dict[str, Any]] | None,
        initial_plan: dict[str, Any],
        resolution: dict[str, Any] | None,
        decision: ResearchDecision,
        envelope: CapabilityEnvelope,
        *,
        request_id: str = "",
        cognitive_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
        """恢复认知回执并固定历史身份，再交由 loop 维护研究轮次。"""

        resolution = resolution or {}
        execution_state = self.ports.state(cognitive_body or {})
        if cognitive_body is not None:
            if execution_state.get("_executionRootSnapshotId"):
                with self.ports.database() as connection:
                    recorded = read_cognitive_execution_receipts(
                        connection, execution_state["conversationId"],
                        turn_ids=[execution_state["_executionTurnId"]],
                    )
                receipts = [receipt for turn in recorded for receipt in turn["toolReceipts"]]
                cognitive_body.setdefault("_cognitiveEvidence", {}).update(collect_evidence(receipts))
        task = execution_state.get("task") or {}
        # 此字段已持久化；保持输入与 JSON 编码字节不变，避免升级后重复工具调用。
        initial_fingerprint = legacy_persistent_fingerprint(json.dumps({
            "plan": {key: value for key, value in initial_plan.items() if not key.startswith("_")},
            "sourcePolicyId": decision.source_policy_id,
            "unknownEntities": decision.unknown_entities,
            "verificationTargets": decision.verification_targets,
            "taskId": task.get("taskId"), "planRevision": task.get("planRevision"),
            "toolRequests": [tool_request_signature(step) for step in task.get("plan", [])
                             if step.get("name") in {"web_search", "read_web_page"}],
        }, ensure_ascii=False, sort_keys=True).encode("utf-8"), length=64)
        answer_scope = self.bounded_research_answer_scope(initial_plan, cognitive_body)
        return research_loop.run_research_query_loop(
            settings, user_message, messages, initial_plan, resolution, decision, envelope,
            prepared=research_loop.PreparedResearchLoop(initial_fingerprint, answer_scope),
            rounds=self.ports.rounds(), store=self.ports.store(), checks=self.ports.checks(),
            log_event=self.ports.log, perf_counter=time.perf_counter,
            request_id=request_id, cognitive_body=cognitive_body,
        )
