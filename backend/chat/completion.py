"""回复完成核验与有限改写。

本模块决定候选是否满足本轮交付契约，不执行工具，也不提交会话或认知状态。
模型审计、附件事实与应用配置通过窄接口读取；已通过专用核验的事实边界继续
由其原核验结果负责，不能被通用语义审计覆盖。最多改写两次后明确失败。
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

from agent.action_grounding import PreparedTurn
from agent.attachment_editing import ATTACHMENT_ACTION_RULE
from agent.cognitive_brain import CognitiveProtocolError
from agent.cognitive_finalization import FinalProjectionAuditRepairError
from agent.dialogue_architecture import (
    TurnContract, UtteranceInterpretation, bind_missing_input_clarification_contract,
    contract_completion_issues, final_turn_envelope,
)
from agent.dialogue_waiting import synchronize_response_waiting
from agent.fingerprints import runtime_fingerprint
from agent.persona_context import PersonaContextSnapshot
from backend.attachments.storage import safe_attachment_name
from backend.chat.parsing import parse_json_object
from backend.chat.reply import EMPTY_REPLY_FALLBACK
from backend.common.values import clamp_float
from backend.providers.errors import UpstreamProtocolError


class CompletionModel(Protocol):
    """只允许审计和改写候选；不提供工具执行或提交能力。"""

    def audit(self, settings, interpretation, contract, candidate, **kwargs) -> dict[str, Any]: ...
    def rewrite(self, settings, interpretation, contract, candidate, audit, **kwargs) -> dict[str, Any]: ...


class CompletionAttachments(Protocol):
    """在检查时读取本轮附件与任务事实，不缓存易变的认知投影。"""

    def issues(self, candidate: dict[str, Any], body: Mapping[str, Any] | None) -> list[str]: ...
    def capabilities(self, body: Mapping[str, Any]) -> list[dict[str, Any]]: ...
    def has_cognitive_state(self, body: Mapping[str, Any]) -> bool: ...


class CompletionRuntime(Protocol):
    """应用的模型配置与低风险完成判断，由入口在调用时装配。"""

    def configured(self, settings: dict[str, Any]) -> bool: ...
    def can_confirm_core(self, candidate: dict[str, Any], contract: TurnContract, *,
                         text_mutated: bool, upstream_finish_reason: str) -> bool: ...



def reply_integrity_issues(value: Any) -> list[str]:
    """Detect transport/normalization fragments without judging the topic."""

    parsed = parse_json_object(value) if isinstance(value, str) else value if isinstance(value, dict) else {}
    reply = str((parsed or {}).get("reply") or value or "").strip()
    if not reply:
        return ["empty_answer"]
    issues: list[str] = []
    if re.fullmatch(
        r"(?:\s*\[S\d+\]\s*)+(?:[。.!！？，,；;：:]\s*)?",
        reply,
        re.I,
    ):
        issues.append("citation_only_answer")
    elif re.search(r"^(?:\[S\d+\]\s*)+[。.!！？，,；;：:]", reply, re.I):
        issues.append("orphaned_leading_citation")
    if re.search(r"^[。！？；：、]", reply):
        issues.append("orphaned_leading_punctuation")
    if re.search(
        r'["\'](?:spokenTextJa|emotion|gesture|voiceStyle|knowledgeConfidence|researchNeeded)["\']\s*:',
        reply,
        flags=re.I,
    ):
        issues.append("structured_transport_leak")
    return issues


def visual_grounding_text_fingerprint(candidate: Mapping[str, Any]) -> str:
    payload = "\x1f".join((
        str(candidate.get("reply") or ""),
        str(candidate.get("spokenTextJa") or ""),
    ))
    return runtime_fingerprint(payload, length=32)


def completion_risk_reasons(
    result: dict[str, Any],
    contract: TurnContract,
    *,
    text_mutated: bool = False,
) -> list[str]:
    reasons = list(contract_completion_issues(result, contract))
    reply_length = len(re.sub(r"\s+", "", str(result.get("reply") or "")))
    if len(contract.required_acts) > 1 and reply_length < 16:
        reasons.append("reply_too_short_for_required_acts")
    spoken_length = len(re.sub(r"\s+", "", str(result.get("spokenTextJa") or "")))
    bilingual_ratio = spoken_length / max(1, reply_length)
    if bilingual_ratio < 0.18 or bilingual_ratio > 5.5:
        reasons.append("bilingual_core_alignment_risk")
    if result.get("structuredOutputInvalid") and not result.get("structuredOutputResolved"):
        reasons.append("structured_output_invalid")
    if result.get("streamProtocolIssue"):
        reasons.append("stream_protocol_issue")
    finish_reason = str(result.get("upstreamFinishReason") or "").strip().lower()
    if finish_reason and finish_reason not in {"stop", "end_turn", "tool_calls"}:
        reasons.append("abnormal_upstream_finish_reason:" + finish_reason)
    if result.get("nicknameReactionGroundingViolation"):
        reasons.append("nickname_reaction_grounding_violation")
    action_receipt = (
        result.get("actionReceipt")
        if isinstance(result.get("actionReceipt"), Mapping)
        else {}
    )
    action_receipts = (
        [item for item in result.get("actionReceipts", []) if isinstance(item, Mapping)]
        if isinstance(result.get("actionReceipts"), list)
        else []
    )
    if (
        result.get("_fileOutputExpected")
        and not (
            isinstance(result.get("_fileOutputs"), list)
            and result.get("_fileOutputs")
        )
        and not (
            isinstance(result.get("files"), list)
            and result.get("files")
        )
    ):
        reasons.append("required_generated_file_missing")
    memory_action = str(result.get("memoryAction") or "none").strip().lower()
    if (
        memory_action != "none"
        and not result.get("structuredSideEffectVerified")
    ):
        # A state-changing memory decision is not a low-risk presentation
        # field. The compact protocol may contain two fluent but contradictory
        # language tracks (or a reply unrelated to the declared save). Route
        # it through the semantic judge instead of guessing from local words.
        reasons.append("structured_side_effect_requires_semantic_audit")
    if (
        (action_receipt or action_receipts)
        and not result.get("externalActionOutcomeVerified")
    ):
        reasons.append("external_action_outcome_requires_semantic_audit")
    for flag in (
        "replyIntegrityUnverified",
        "taskCompletionUnverified",
        "contextGroundingUnverified",
        "toolGroundingUnverified",
        "webSynthesisUnverified",
    ):
        if result.get(flag):
            reasons.append(flag)
    if text_mutated:
        reasons.append("postprocess_text_mutation")
    return list(dict.fromkeys(reasons))


def backend_can_confirm_core_dialogue_completion(
    result: dict[str, Any],
    contract: TurnContract,
    *,
    text_mutated: bool,
    upstream_finish_reason: str,
    conditional_semantic_audit_enabled: bool,
    integrity_checker: Callable[[Any], list[str]],
) -> bool:
    """Confirm a low-risk one-act dialogue from backend-owned evidence."""

    if text_mutated:
        return False
    if str(result.get("semanticKind") or "") in {"clarification", "clarify"}:
        # A clarification is a different terminal outcome from completing the
        # original task. It must pass the missing-input semantic audit and bind
        # its own contract before the backend may confirm completion.
        return False
    if not result.get("coreDialogueProtocol"):
        if not conditional_semantic_audit_enabled:
            return False
        recovery_mode = str(result.get("semanticRecoveryMode") or "")
        low_risk_recovery = recovery_mode in {
            "strict_json",
            "fenced_json",
            "embedded_json",
        }
        ordinary_semantic_stream = bool(
            str(result.get("source") or "") in {"api", "api-stream"}
            and not result.get("streamProtocolIssue")
            and not result.get("plainTextSalvaged")
        )
        if not (low_risk_recovery or ordinary_semantic_stream):
            return False
    text_only_delivery = bool(result.get("textOnlyDelivery"))
    if result.get("dialogueFieldRepairFailed") and not text_only_delivery:
        return False
    finish_reason = str(upstream_finish_reason or result.get("upstreamFinishReason") or "").strip().lower()
    if finish_reason not in {"stop", "end_turn"}:
        return False
    if not str(result.get("reply") or "").strip():
        return False
    if not str(result.get("spokenTextJa") or "").strip() and not text_only_delivery:
        return False
    if integrity_checker(result):
        return False
    if len(contract.required_acts) > 1:
        return False
    if "complete_requested_task" in contract.required_acts:
        # A fluent acknowledgement or request for the missing material is not
        # proof that the task itself was completed. Request turns therefore use
        # the short semantic audit, which can confirm completion or transition
        # to the audited missing-input contract.
        return False
    if contract.primary_act == "respond_other":
        # ``other`` is deliberately the low-confidence catch-all speech act.
        # Structural fluency cannot prove that a request synonym was not
        # missed or that a question-shaped reply is not awaiting input.
        return False
    if result.get("toolUsed"):
        # Tool/research turns used to rely on model-authored completion flags
        # from the heavy JSON protocol. In semantic-v3 the backend can confirm
        # a one-act turn after receipts and grounding gates have succeeded.
        failed_visual_outcome = bool(
            contract.required_acts
            == ("explain_current_visual_observation_failure",)
            and str(result.get("toolStatus") or "") in {
                "failed", "error", "partial",
            }
        )
        if str(result.get("toolStatus") or "") != "ok" and not failed_visual_outcome:
            return False
    allowed_format_recovery = str(result.get("semanticRecoveryMode") or "") in {
        "strict_json",
        "fenced_json",
        "embedded_json",
    }
    if any(result.get(flag) for flag in (
        "replyIntegrityUnverified",
        "taskCompletionUnverified",
        "contextGroundingUnverified",
        "toolGroundingUnverified",
        "webSynthesisUnverified",
    )):
        return False
    if result.get("streamProtocolIssue") and not allowed_format_recovery:
        return False
    return True


def generic_missing_input_clarification_candidate(
    result: Mapping[str, Any],
    contract: TurnContract | None = None,
) -> bool:
    """Return true only for an ordinary typed clarification awaiting audit.

    Schedule/Todo and browser continuations already own typed pending state and
    must not be rebound by the generic dialogue contract.
    """

    semantic_kind = str(result.get("semanticKind") or "")
    typed_clarification = semantic_kind in {"clarification", "clarify"}
    request_contract = bool(
        contract is not None
        and "complete_requested_task" in contract.required_acts
    )
    expected_response_type = str(result.get("expectedResponseType") or "")
    if not typed_clarification and not request_contract:
        return False
    if (
        typed_clarification
        and (
            result.get("requiresUserResponse") is not True
            or expected_response_type not in {"free_text", "confirmation", "choice"}
        )
    ):
        return False
    if (
        expected_response_type == "browser_target"
        or str(result.get("clarificationActionId") or "").strip()
    ):
        return False
    schedule = (
        result.get("scheduleDecision")
        if isinstance(result.get("scheduleDecision"), Mapping)
        else {}
    )
    intent = schedule.get("intent") if isinstance(schedule.get("intent"), Mapping) else {}
    policy = schedule.get("policy") if isinstance(schedule.get("policy"), Mapping) else {}
    if (
        str(intent.get("kind") or "") == "clarification"
        or str(policy.get("action") or "") in {"clarify", "request_clarification"}
        or bool(schedule.get("workflow"))
    ):
        return False
    return True


def clarification_context_for_turn(
    result: Mapping[str, Any],
    prepared_turn: PreparedTurn,
    chat_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a bounded evidence view for the missing-input semantic audit."""

    attachments = (
        [item for item in result.get("attachments", []) if isinstance(item, Mapping)]
        if isinstance(result.get("attachments"), list)
        else []
    )
    tool_results = (
        [item for item in result.get("_conversationToolResults", []) if isinstance(item, Mapping)]
        if isinstance(result.get("_conversationToolResults"), list)
        else []
    )
    successful_tools = []
    for item in tool_results[:12]:
        status = str(item.get("status") or item.get("toolStatus") or "").strip().lower()
        if not status and item.get("ok") is True:
            status = "ok"
        if status not in {"ok", "success", "accepted", "completed"}:
            continue
        successful_tools.append({
            "tool": str(item.get("tool") or item.get("name") or "")[:80],
            "status": status,
            "hasContent": bool(
                item.get("content") or item.get("text") or item.get("result")
                or item.get("extractedText") or item.get("imageDescription")
                or item.get("contentPreview") or item.get("visualCaption")
            ),
            "hasArtifact": bool(
                item.get("path") or item.get("storedPath") or item.get("artifactId")
                or item.get("attachmentId") or item.get("imageArtifact")
            ),
        })
    capability = dict(prepared_turn.capability_envelope or {})
    context_resolution = (
        chat_metrics.get("contextResolution")
        if isinstance(chat_metrics.get("contextResolution"), Mapping)
        else {}
    )
    return {
        "boundAttachments": [{
            "name": str(item.get("name") or item.get("filename") or "")[:160],
            "contentType": str(
                item.get("mimeType") or item.get("contentType") or item.get("type") or ""
            )[:100],
            "hasExtractedContent": bool(
                item.get("text") or item.get("content") or item.get("extractedText")
            ),
        } for item in attachments[:8]],
        "successfulToolResults": successful_tools,
        "fileOutputCount": max(
            len(result.get("_fileOutputs", []))
            if isinstance(result.get("_fileOutputs"), list) else 0,
            len(result.get("files", []))
            if isinstance(result.get("files"), list) else 0,
        ),
        "resolvedSubject": str(context_resolution.get("resolvedSubject") or "")[:500],
        "resolvedQuestion": str(context_resolution.get("resolvedQuestion") or "")[:500],
        "allowedTools": list(capability.get("allowed_tools") or capability.get("allowedTools") or [])[:30],
        "requiredTools": list(capability.get("required_tools") or capability.get("requiredTools") or [])[:20],
        "modelMayRequestTools": bool(
            capability.get("may_escalate", capability.get("mayEscalate", False))
        ),
    }


def authoritative_outcome_for_result(result: Mapping[str, Any], *, max_generated_files: int) -> Any:
    """Return bounded state/action/artifact outcomes without generated bodies."""

    actions = (
        [dict(item) for item in result["actionReceipts"] if isinstance(item, Mapping)]
        if isinstance(result.get("actionReceipts"), list) and result["actionReceipts"]
        else [dict(result["actionReceipt"])]
        if isinstance(result.get("actionReceipt"), Mapping)
        else []
    )
    generated_files: list[dict[str, Any]] = []
    outputs = result.get("_fileOutputs")
    if isinstance(outputs, list):
        for item in outputs[:max_generated_files]:
            if not isinstance(item, Mapping) or not isinstance(item.get("content"), str):
                continue
            content = str(item.get("content") or "")
            generated_files.append({
                "status": "staged",
                "name": safe_attachment_name(
                    item.get("fileName") or item.get("name"),
                    "output.txt",
                ),
                "size": len(content.encode("utf-8")),
            })
    if not generated_files and isinstance(result.get("files"), list):
        generated_files = [{
            "status": "published",
            "name": str(item.get("name") or "output.txt")[:180],
            "size": int(item.get("size") or 0),
            "id": str(item.get("id") or "")[:80],
        } for item in result["files"][:max_generated_files] if isinstance(item, Mapping)]
    memory_action = str(result.get("memoryAction") or "none").strip().lower()
    memory_outcome: dict[str, Any] = {}
    if memory_action != "none":
        updates = result.get("memoryUpdates")
        bounded_updates = [
            str(item.get("content") or item.get("text") or item)
            if isinstance(item, Mapping) else str(item)
            for item in (updates[:4] if isinstance(updates, list) else [])
        ]
        memory_outcome = {
            "action": memory_action,
            "status": "accepted_for_persistence",
            "updates": [item[:500] for item in bounded_updates if item.strip()],
        }
    if generated_files or memory_outcome:
        return {
            **({"actions": actions} if actions else {}),
            **({"memory": memory_outcome} if memory_outcome else {}),
            **({"generatedFiles": generated_files} if generated_files else {}),
        }
    if result.get("_fileOutputExpected"):
        return {
            "generatedFiles": [],
            "requiredArtifactStatus": "missing",
        }
    if len(actions) == 1:
        return actions[0]
    return actions


def apply_missing_input_clarification_audit(
    interpretation: UtteranceInterpretation,
    contract: TurnContract,
    candidate: dict[str, Any],
    audit: Mapping[str, Any],
) -> tuple[TurnContract, dict[str, Any], bool]:
    """Apply the same audited contract transition before or after a rewrite."""

    if (
        not audit.get("clarificationPasses")
        or bool(candidate.get("emptyReply"))
        or str(candidate.get("reply") or "").strip() == EMPTY_REPLY_FALLBACK
        or str(candidate.get("source") or "") == "local-empty-recovery"
        or bool(candidate.get("emptyReplyRecoveryReason"))
    ):
        return contract, candidate, False
    rebound = bind_missing_input_clarification_contract(
        contract,
        semantic_text=interpretation.semantic_text,
    )
    accepted = dict(candidate)
    accepted.update({
        "completedActs": list(rebound.required_acts),
        "completedActsProvided": True,
        "standalone": True,
        "standaloneProvided": True,
        "dependsOnUndeliveredPrefix": False,
        "dependsOnUndeliveredPrefixProvided": True,
        "turnComplete": True,
        "turnCompleteProvided": True,
        "requiresUserResponse": True,
        "requiresUserResponseProvided": True,
        "expectedResponseType": str(accepted.get("expectedResponseType") or "free_text"),
        "semanticKind": "clarification",
        "turnContractVerification": "semantic_missing_input_clarification",
        "missingInputClarificationAudit": {
            "missingInput": str(audit.get("missingInput") or "")[:500],
            "reason": str(audit.get("reason") or "")[:500],
            "confidence": clamp_float(audit.get("confidence"), 0, 1, 0),
        },
    })
    return rebound, accepted, True


def ensure_turn_contract_completion(
    settings: dict[str, Any],
    interpretation: UtteranceInterpretation,
    contract: TurnContract,
    result: dict[str, Any],
    *,
    request_id: str,
    text_mutated: bool = False,
    allow_rewrite: bool = True,
    upstream_finish_reason: str = "",
    persona_snapshot: PersonaContextSnapshot | None = None,
    clarification_context: Mapping[str, Any] | None = None,
    task_dialogue: Sequence[Mapping[str, Any]] = (),
    cognitive_body: Mapping[str, Any] | None = None,
    model: CompletionModel,
    attachments: CompletionAttachments,
    runtime: CompletionRuntime,
    log_event: Callable[..., Any],
) -> dict[str, Any]:
    """按附件、专用核验、语义审计和有限改写顺序完成交付检查。"""
    candidate = synchronize_response_waiting(result)
    attachment_issues = attachments.issues(candidate, cognitive_body)
    if candidate.get("_fileOutputExpected") and not candidate.get("_fileOutputs") and not candidate.get("files"):
        attachment_issues.append("required_generated_file_missing")
    if attachment_issues:
        repair_report = {"problems": list(dict.fromkeys(attachment_issues)), "repair": ATTACHMENT_ACTION_RULE,
                         "attachmentCapabilities": attachments.capabilities(cognitive_body or {})}
        if isinstance(cognitive_body, dict) and attachments.has_cognitive_state(cognitive_body):
            raise FinalProjectionAuditRepairError(candidate, repair_report, repair_report)
        if allow_rewrite:
            candidate = model.rewrite(settings, interpretation, contract, candidate, repair_report,
                request_id=request_id, persona_snapshot=persona_snapshot, task_dialogue=task_dialogue)
            return ensure_turn_contract_completion(settings, interpretation, contract, candidate,
                model=model, attachments=attachments, runtime=runtime, log_event=log_event,
                request_id=request_id, allow_rewrite=False, persona_snapshot=persona_snapshot,
                cognitive_body=cognitive_body, upstream_finish_reason=upstream_finish_reason,
                clarification_context=clarification_context, task_dialogue=task_dialogue)
        raise CognitiveProtocolError("attachment_delivery_invalid:" + ",".join(attachment_issues))
    verification = candidate.get("_cognitiveOutcomeVerification") or {}
    if verification.get("valid") is True:
        if not str(candidate.get("reply") or "").strip():
            raise CognitiveProtocolError("final_answer_missing")
        if (candidate.get("_cognitiveOutcome") or candidate.get("outcome") or {}).get("status") == "needs_input":
            contract = bind_missing_input_clarification_contract(contract, semantic_text=interpretation.semantic_text)
        # Structural outcome validation does not claim semantic correctness.
        # Preserve partial/failed/needs_input task status during delivery.
        completed_acts = tuple(contract.required_acts)
        verification_method = "cognitive_outcome_validation"
        if completed_acts is not None:
            candidate.update({
                "outcome": deepcopy(candidate.get("_cognitiveOutcome") or candidate.get("outcome") or {}),
                "outcomeVerification": deepcopy(verification),
                "completedActs": list(completed_acts),
                "completedActsProvided": True,
                "turnContractVerification": verification_method,
                "turnContractSemanticAuditCalls": 0,
                "turnContractRewriteCalls": 0,
                "turnContract": contract.to_dict(),
                "utteranceInterpretation": interpretation.to_dict(),
                "standalone": True,
                "standaloneProvided": True,
                "turnComplete": True,
                "turnCompleteProvided": True,
                "dependsOnUndeliveredPrefix": False,
                "dependsOnUndeliveredPrefixProvided": True,
            })
            candidate["finalTurnEnvelope"] = final_turn_envelope(
                candidate, contract, turn_id=f"{request_id}:turn",
                delivery_path="atomic" if allow_rewrite else "stream",
                upstream_finish_reason=upstream_finish_reason or None,
            ).to_dict()
            return candidate
    if upstream_finish_reason:
        candidate["upstreamFinishReason"] = upstream_finish_reason
    source = str(candidate.get("source") or "")
    trusted_local = bool(
        not runtime.configured(settings)
        or (
            source
            and source not in {"api", "api-stream", "local-empty-recovery"}
            and not candidate.get("_fileOutputExpected")
        )
    )
    visual_terminal_boundary = bool(candidate.get("visualGroundingTerminalBoundary"))
    grounding_terminal_boundary = bool(candidate.get("groundingGateTerminalBoundary"))
    evidence_limited_precision_audited = bool(
        candidate.get("evidenceLimitedPrecisionAudited")
        and not candidate.get("webSynthesisUnverified")
        and not candidate.get("toolGroundingUnverified")
    )
    visual_grounding_audit = (
        candidate.get("visualGroundingAudit")
        if isinstance(candidate.get("visualGroundingAudit"), dict)
        else {}
    )
    verified_visual_answer = bool(
        visual_grounding_audit.get("passes")
        and str(candidate.get("visualGroundingTextFingerprint") or "")
        == visual_grounding_text_fingerprint(candidate)
    )
    if visual_terminal_boundary:
        # This system-owned boundary is the completed safe outcome of a visual
        # task whose factual candidate failed evidence review.  Sending it to a
        # generic contract rewriter would discard the visual packet and could
        # recreate the very hallucination that was just blocked.
        candidate.update({
            "completedActs": list(contract.required_acts),
            "completedActsProvided": True,
            "standalone": True,
            "standaloneProvided": True,
            "dependsOnUndeliveredPrefix": False,
            "dependsOnUndeliveredPrefixProvided": True,
            "turnComplete": True,
            "turnCompleteProvided": True,
            "turnContractVerification": "visual_fail_closed_boundary",
        })
    elif grounding_terminal_boundary:
        # The grounding gate owns this fact-free report.  A generic model audit
        # has no access to the failed evidence receipt and could reintroduce the
        # discarded claim, so preserve the typed safe outcome as-is.
        candidate.update({
            "completedActs": list(contract.required_acts),
            "completedActsProvided": True,
            "standalone": True,
            "standaloneProvided": True,
            "dependsOnUndeliveredPrefix": False,
            "dependsOnUndeliveredPrefixProvided": True,
            "turnComplete": True,
            "turnCompleteProvided": True,
            "turnContractVerification": "grounding_fail_closed_boundary",
        })
    elif verified_visual_answer:
        # The exact bilingual text already passed the dedicated visual audit.
        # A generic contract rewrite cannot see that evidence packet and must
        # not overwrite an accepted answer with an unrelated hallucination.
        candidate.update({
            "completedActs": list(contract.required_acts),
            "completedActsProvided": True,
            "standalone": True,
            "standaloneProvided": True,
            "dependsOnUndeliveredPrefix": False,
            "dependsOnUndeliveredPrefixProvided": True,
            "turnComplete": True,
            "turnCompleteProvided": True,
            "turnContractVerification": "verified_visual_grounding",
        })
    elif evidence_limited_precision_audited:
        # The evidence-limited precision pass is the semantic owner of this
        # web answer.  Re-auditing it with the generic contract prompt would
        # lose its source scope and spend another model call for the same text.
        candidate.update({
            "completedActs": list(contract.required_acts),
            "completedActsProvided": True,
            "standalone": True,
            "standaloneProvided": True,
            "dependsOnUndeliveredPrefix": False,
            "dependsOnUndeliveredPrefixProvided": True,
            "turnComplete": True,
            "turnCompleteProvided": True,
            "turnContractVerification": "evidence_limited_precision_audit",
        })
    elif trusted_local:
        candidate.update({
            "completedActs": list(contract.required_acts),
            "completedActsProvided": True,
            "standalone": True,
            "standaloneProvided": True,
            "dependsOnUndeliveredPrefix": False,
            "dependsOnUndeliveredPrefixProvided": True,
            "turnComplete": True,
            "turnCompleteProvided": True,
            "turnContractVerification": "trusted_local_route",
        })
    elif runtime.can_confirm_core(
        candidate,
        contract,
        text_mutated=text_mutated,
        upstream_finish_reason=upstream_finish_reason,
    ):
        candidate.update({
            "completedActs": list(contract.required_acts),
            "completedActsProvided": True,
            "standalone": True,
            "standaloneProvided": True,
            "dependsOnUndeliveredPrefix": False,
            "dependsOnUndeliveredPrefixProvided": True,
            "turnComplete": True,
            "turnCompleteProvided": True,
            "turnContractVerification": "backend_core_dialogue",
        })
    reasons = completion_risk_reasons(
        candidate,
        contract,
        text_mutated=text_mutated and not trusted_local,
    )
    if (
        trusted_local
        or visual_terminal_boundary
        or grounding_terminal_boundary
        or verified_visual_answer
        or evidence_limited_precision_audited
    ):
        # Local simulation and typed server-owned outcomes have no need for a
        # generic semantic rewrite. Their completion metadata was established
        # above from backend or dedicated evidence; do not turn a presentation
        # heuristic into a call that can replace the accepted answer.
        reasons = []
    audit: dict[str, Any] = {}
    semantic_audit_calls = 0
    contract_rewrite_calls = 0
    # These reasons describe backend-observable facts, not semantic quality.
    # A model judge must never waive them merely by declaring that the prose
    # answered the user: either the artifact exists or it does not.
    hard_grounding_reasons = {
        "nickname_reaction_grounding_violation",
        "required_generated_file_missing",
    }
    if reasons:
        log_event(
            "turn_contract_semantic_audit_started",
            requestId=request_id,
            reasons=reasons,
            coreDialogueProtocol=bool(candidate.get("coreDialogueProtocol")),
            fieldRepairAttempted=bool(candidate.get("dialogueFieldRepairAttempted")),
        )
        semantic_audit_calls += 1
        audit = model.audit(
            settings,
            interpretation,
            contract,
            candidate,
            request_id=request_id,
            clarification_context=clarification_context,
            task_dialogue=task_dialogue,
        )
        contract, candidate, clarification_accepted = apply_missing_input_clarification_audit(
            interpretation,
            contract,
            candidate,
            audit,
        )
        if clarification_accepted:
            reasons = []
        elif audit.get("passes"):
            if (
                isinstance(candidate.get("actionReceipt"), Mapping)
                or isinstance(candidate.get("actionReceipts"), list)
            ):
                candidate["externalActionOutcomeVerified"] = True
            if str(candidate.get("memoryAction") or "none").strip().lower() != "none":
                candidate["structuredSideEffectVerified"] = True
            candidate.update({
                "completedActs": list(audit.get("completedActs") or contract.required_acts),
                "completedActsProvided": True,
                "standalone": True,
                "standaloneProvided": True,
                "dependsOnUndeliveredPrefix": False,
                "dependsOnUndeliveredPrefixProvided": True,
                "turnComplete": True,
                "turnCompleteProvided": True,
                "turnContractVerification": "semantic_audit",
            })
            reasons = [item for item in reasons if item in hard_grounding_reasons]
    max_contract_rewrite_calls = 2
    while (
        reasons
        and allow_rewrite
        and contract_rewrite_calls < max_contract_rewrite_calls
    ):
        contract_rewrite_calls += 1
        candidate = model.rewrite(
            settings,
            interpretation,
            contract,
            candidate,
            audit,
            request_id=request_id,
            persona_snapshot=persona_snapshot,
            task_dialogue=task_dialogue,
        )
        reasons = completion_risk_reasons(candidate, contract)
        if not reasons:
            break
        if reasons:
            semantic_audit_calls += 1
            audit = model.audit(
                settings,
                interpretation,
                contract,
                candidate,
                request_id=request_id,
                clarification_context=clarification_context,
                task_dialogue=task_dialogue,
            )
            contract, candidate, clarification_accepted = apply_missing_input_clarification_audit(
                interpretation,
                contract,
                candidate,
                audit,
            )
            if clarification_accepted:
                reasons = []
            elif audit.get("passes"):
                if (
                    isinstance(candidate.get("actionReceipt"), Mapping)
                    or isinstance(candidate.get("actionReceipts"), list)
                ):
                    candidate["externalActionOutcomeVerified"] = True
                if str(candidate.get("memoryAction") or "none").strip().lower() != "none":
                    candidate["structuredSideEffectVerified"] = True
                candidate.update({
                    "completedActs": list(audit.get("completedActs") or contract.required_acts),
                    "completedActsProvided": True,
                    "standalone": True,
                    "standaloneProvided": True,
                    "dependsOnUndeliveredPrefix": False,
                    "dependsOnUndeliveredPrefixProvided": True,
                    "turnComplete": True,
                    "turnCompleteProvided": True,
                    "turnContractVerification": "model_rewrite_and_audit",
                })
                reasons = [item for item in reasons if item in hard_grounding_reasons]
    if reasons:
        log_event(
            "turn_contract_rejected",
            requestId=request_id,
            contract=contract.to_dict(),
            reasons=reasons,
            audit=audit,
        )
        raise UpstreamProtocolError(
            "model reply did not complete the current Turn Contract: "
            + ",".join(reasons)
        )
    final_attachment_issues = attachments.issues(candidate, cognitive_body)
    if final_attachment_issues:
        raise CognitiveProtocolError("attachment_delivery_invalid:" + ",".join(final_attachment_issues))
    candidate["turnContract"] = contract.to_dict()
    candidate["turnContractSemanticAuditCalls"] = semantic_audit_calls
    candidate["turnContractRewriteCalls"] = contract_rewrite_calls
    candidate["utteranceInterpretation"] = interpretation.to_dict()
    envelope = final_turn_envelope(
        candidate,
        contract,
        turn_id=f"{request_id or uuid.uuid4().hex}:turn",
        delivery_path="atomic" if allow_rewrite else "stream",
        upstream_finish_reason=upstream_finish_reason or None,
    )
    candidate["finalTurnEnvelope"] = envelope.to_dict()
    log_event(
        "turn_contract_completed",
        requestId=request_id,
        verification=str(candidate.get("turnContractVerification") or "model_declaration"),
        envelope=envelope.to_dict(),
    )
    return candidate
