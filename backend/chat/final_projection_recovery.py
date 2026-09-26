"""最终投影未通过语义校验时的有界恢复流程。

本模块只拥有一次最终投影修复的预算判断、诊断交接和结果投影。认知状态仍
保存在调用方传入的请求 body 中；认知模型执行和工具循环续接分别由窄端口
提供，因此这里既不持久化会话，也不拥有工具权限或模型传输。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol

from agent.cognitive_brain import (
    MAX_STAGE_REVISIONS,
    MAX_TURN_REPAIRS,
    REPAIR_BUDGET_KEYS,
    CognitiveProtocolError,
)
from agent.cognitive_finalization import FinalProjectionAuditRepairError
from agent.cognitive_repair_routing import supports_pro_repair
from agent.deepseek_vision import VISION_MODEL
from agent.persona_context import PersonaContextSnapshot
from backend.chat.parsing import parse_json_object
from backend.tools.outcomes import effective_tool_results


class FinalProjectionCognitiveOperations(Protocol):
    """读取本轮认知状态，并执行既有认知修复状态机。"""

    def turn_state(self, *args: Any, **kwargs: Any) -> Any: ...
    def resolve_response(self, *args: Any, **kwargs: Any) -> Any: ...


class FinalProjectionPipelineOperations(Protocol):
    """把需要工具的修复候选续接到既有上游工具循环。"""

    def continue_upstream(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class FinalProjectionRecoveryServices:
    cognitive: FinalProjectionCognitiveOperations
    pipeline: FinalProjectionPipelineOperations


def recover_final_projection_candidate(
    services: FinalProjectionRecoveryServices,
    body: dict[str, Any],
    settings: Mapping[str, Any],
    error: FinalProjectionAuditRepairError,
    *,
    request_id: str,
    progress_callback: Any | None = None,
    persona_snapshot: PersonaContextSnapshot | None = None,
) -> dict[str, Any]:
    """运行一次既有 Pro/Flash 修复，再续接正常工具路径。

    返回值仍是尚未持久化的模型结果。调用方必须再次运行
    ``finalize_chat_response``，让修复后的最终投影在提交前重新完成后处理和
    校验。
    """

    if not isinstance(error, FinalProjectionAuditRepairError):
        raise TypeError("final projection recovery requires its typed audit error")
    if not services.cognitive.turn_state(body):
        raise error
    budget = body.setdefault("_cognitiveCallBudget", {})
    existing_route = body.get("_cognitiveRepairRoute")
    resumed_repair = (
        isinstance(existing_route, Mapping)
        and existing_route.get("phase") == "resume"
        and int(budget.get("repairHandoffCalls", 0) or 0) >= 1
        and supports_pro_repair(settings, {"model": settings.get("apiModel")})
    )
    if (
        body.get("_cognitiveFinalProjectionRecoveryAttempted")
        or sum(int(budget.get(key, 0) or 0) for key in REPAIR_BUDGET_KEYS)
        >= MAX_TURN_REPAIRS
        or (
            int(budget.get("repairRevisionCalls", 0) or 0) >= MAX_STAGE_REVISIONS
            if resumed_repair
            else (
                int(budget.get("repairHandoffCalls", 0) or 0) >= 1
                or int(budget.get("revisionCalls", 0) or 0) >= MAX_STAGE_REVISIONS
            )
        )
    ):
        raise CognitiveProtocolError(
            "final_projection_semantic_repair_budget_exhausted"
        )
    if (
        isinstance(existing_route, Mapping)
        and str(existing_route.get("phase") or "") in {"analysis", "resume"}
        and not resumed_repair
    ):
        raise CognitiveProtocolError(
            "final_projection_semantic_repair_route_already_active"
        )

    body["_cognitiveFinalProjectionRecoveryAttempted"] = True
    route = deepcopy(dict(existing_route)) if isinstance(existing_route, Mapping) else {}
    route.update(
        {
            "reason": "final_projection_validation",
            "phase": "resume" if resumed_repair else "trigger",
            "normalModel": str(settings.get("apiModel") or ""),
            "source": "final_projection_validation",
        }
    )
    body["_cognitiveRepairRoute"] = route

    candidate = deepcopy(error.candidate)
    # 审计结果只作为后端数据放在被拒绝的草稿旁，并通过既有“不可信草稿”交接
    # 传给修复模型；它不能成为状态补丁，也不能授予工具权限。
    candidate["_finalProjectionValidation"] = {
        "packet": deepcopy(error.packet),
        "report": deepcopy(error.report),
        "source": "backend_delivery_validation",
    }
    prior_results = candidate.get("_conversationToolResults")
    if not isinstance(prior_results, list):
        recovered_receipts: list[dict[str, Any]] = []
        for entry in (body.get("_cognitiveEvidence") or {}).values():
            if not isinstance(entry, Mapping) or entry.get("historical"):
                continue
            receipt = entry.get("receipt")
            if isinstance(receipt, Mapping) and receipt.get("name"):
                recovered_receipts.append(deepcopy(dict(receipt)))
        prior_results = effective_tool_results(recovered_receipts)
    prior_results = [
        dict(item) for item in prior_results if isinstance(item, Mapping)
    ]

    payload = {
        "model": str(settings.get("apiModel") or VISION_MODEL),
        "messages": deepcopy(
            body.get("messages") if isinstance(body.get("messages"), list) else []
        ),
    }
    body["_cognitiveFinalProjectionValidation"] = {
        "packet": deepcopy(error.packet),
        "report": deepcopy(error.report),
    }
    try:
        resolved = services.cognitive.resolve_response(
            body,
            dict(settings),
            payload,
            json.dumps(candidate, ensure_ascii=False),
            request_id=request_id,
            initial_failure=error if resumed_repair else None,
        )
    finally:
        body.pop("_cognitiveFinalProjectionValidation", None)
    parsed = parse_json_object(resolved) or {}
    if not isinstance(parsed, Mapping):
        raise CognitiveProtocolError(
            "final_projection_semantic_repair_returned_non_object"
        )
    requests = parsed.get("calls") or parsed.get("toolRequests") or []
    if str(parsed.get("kind") or "").lower() == "tool_use" and requests:
        return services.pipeline.continue_upstream(
            body,
            progress_callback=progress_callback,
            persona_snapshot=persona_snapshot,
            continuation={
                "content": resolved,
                "toolResults": prior_results,
                "toolRounds": body.get("_cognitiveToolRoundsUsed", 0),
            },
        )

    repaired = deepcopy(dict(parsed))
    # 修复后的语音由 Flash 决定。这里只携带最终化器所需的本请求执行信息和账务
    # 字段；被拒绝的回复、语音文本和已接受状态绝不能从旧候选恢复。
    for key in (
        "_conversationToolResults",
        "_referenceLedger",
        "_fileOutputExpected",
        "_chatMetrics",
        "_utteranceInterpretation",
        "_turnContract",
        "_taskDialogue",
        "_upstreamFinishReason",
        "attachments",
        "files",
    ):
        if key not in repaired and key in error.candidate:
            repaired[key] = deepcopy(error.candidate[key])
    if prior_results and not isinstance(
        repaired.get("_conversationToolResults"), list
    ):
        repaired["_conversationToolResults"] = prior_results
    return repaired
