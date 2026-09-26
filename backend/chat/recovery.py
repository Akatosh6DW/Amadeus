"""对话交付前的有限恢复流程，不实现模型请求、字段规范化或持久化。

保留原有场景策略：正常候选先收尾；投影审核失败重新准备并再次收尾；
只有获准联网且本轮尚未尝试时，证据不足才返回生成流程查证一次。
"""

from typing import Any, Callable

from agent.cognitive_finalization import FinalProjectionAuditRepairError
from .errors import GroundingGateError


def finalize_with_recovery(
    settings: dict[str, Any],
    user_message: str,
    result: dict[str, Any],
    *,
    body: dict[str, Any],
    request_id: str,
    started_at: float,
    prepared_turn: Any,
    stream_preamble: dict[str, Any] | None,
    progress_callback: Any,
    persona_snapshot: Any,
    finalize: Callable[..., dict[str, Any]],
    repair_projection: Callable[..., dict[str, Any]],
    prepare_turn: Callable[..., Any],
    generate: Callable[..., dict[str, Any]],
    log_event: Callable[..., Any],
) -> dict[str, Any]:
    """交付或按原规则恢复；未被允许的错误原样向上抛出。

    prepared_turn 属于当前候选；恢复后须从 body 重新取得，不能继续使用
    旧计划。回调在调用时由入口提供，恢复模块不反向访问服务器全局状态。
    """
    try:
        finalized = finalize(
            settings,
            user_message,
            result,
            request_id=request_id,
            started_at=started_at,
            stream_preamble=stream_preamble,
            prepared_turn=prepared_turn,
            persona_snapshot=persona_snapshot,
            cognitive_body=body,
        )
        finalized.pop("_fileOutputExpected", None)
        return finalized
    except FinalProjectionAuditRepairError as audit_error:
        recovered_result = repair_projection(
            body,
            settings,
            audit_error,
            request_id=request_id,
            progress_callback=progress_callback,
            persona_snapshot=persona_snapshot,
        )
        recovered_prepared = prepare_turn(
            body,
            user_message,
            request_id=request_id,
        )
        finalized = finalize(
            settings,
            user_message,
            recovered_result,
            request_id=request_id,
            started_at=started_at,
            stream_preamble=stream_preamble,
            prepared_turn=recovered_prepared,
            persona_snapshot=persona_snapshot,
            cognitive_body=body,
        )
        finalized.pop("_fileOutputExpected", None)
        return finalized
    except GroundingGateError as gate_error:
        decision = gate_error.decision
        if (
            getattr(decision, "status", "") != "RESEARCH_ESCALATION"
            or not settings.get("permissionWebSearch")
            or body.get("_groundingRecoveryAttempted")
        ):
            raise
        body["_groundingRecoveryAttempted"] = True
        body["_researchEscalation"] = {
            "reason": "grounding_gate_research_escalation",
            "queries": [],
            "unknownEntities": [],
            "verificationTargets": list(
                getattr(decision, "missing_verification_targets", ())
                or ("unsupported external claims",)
            ),
            "confidence": 0.45,
            "origin": "final_grounding_gate",
        }
        log_event(
            "grounding_gate_returned_to_query_loop",
            requestId=request_id,
            gateDecision=decision.to_dict(),
        )
        recovered_result = generate(
            body,
            progress_callback=progress_callback,
            persona_snapshot=persona_snapshot,
        )
        recovered_prepared = prepare_turn(
            body,
            user_message,
            request_id=request_id,
        )
        try:
            return finalize(
                settings,
                user_message,
                recovered_result,
                request_id=request_id,
                started_at=started_at,
                stream_preamble=stream_preamble,
                prepared_turn=recovered_prepared,
                persona_snapshot=persona_snapshot,
                cognitive_body=body,
            )
        except FinalProjectionAuditRepairError as audit_error:
            # 查证重试仍属于本轮；再次出现投影问题时沿用同一受限恢复路径，
            # 不能交付旧候选，也不能转到无关的补救流程。
            repaired_result = repair_projection(
                body,
                settings,
                audit_error,
                request_id=request_id,
                progress_callback=progress_callback,
                persona_snapshot=persona_snapshot,
            )
            repaired_prepared = prepare_turn(
                body,
                user_message,
                request_id=request_id,
            )
            return finalize(
                settings,
                user_message,
                repaired_result,
                request_id=request_id,
                started_at=started_at,
                stream_preamble=stream_preamble,
                prepared_turn=repaired_prepared,
                persona_snapshot=persona_snapshot,
                cognitive_body=body,
            )
