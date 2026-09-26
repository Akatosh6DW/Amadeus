"""已接受回复的统一收尾应用服务。

依次核对任务/事实、保留引用与身份、完成语音和文件交付，再持久化或暂缓提交。
请求 body 与领域仓储仍是原状态所有者；本服务只持有当前候选的临时副本。
模型/文件/存储副作用通过按职责分组的端口进入原服务，不由 HTTP 层执行。
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping, Sequence

from agent.action_grounding import PreparedTurn
from agent.dialogue_architecture import (
    TurnContract, UtteranceInterpretation, bind_current_visual_failure_contract,
    bind_current_visual_observation_contract, build_turn_contract, choose_turn_policy,
)
from agent.epistemic_architecture import grounded_claim_gate_decision, research_receipt_from_tool_results
from agent.participant_realization import strip_participant_realization_metadata
from agent.persona_context import PersonaContextSnapshot
from agent.proactive_control import PROACTIVE_DIRECTIVES
from agent.research_architecture import EpistemicPlan, create_epistemic_plan
from agent.visual_grounding import has_current_visual_evidence as has_typed_visual_evidence
from backend.chat.completion import visual_grounding_text_fingerprint
from backend.chat.errors import GroundingGateError
from backend.chat.grounding import (
    hard_fact_gate_requires_terminal_boundary, soft_fact_gate_can_degrade,
    soft_grounding_boundary_reply, terminal_grounding_boundary_reply,
)
from backend.chat.presentation import SPEECH_PACE_SPEED, requested_speech_pace
from backend.chat.reply_fields import normalize_proactive_directive, resolve_proactive_directive
from backend.common.text import unique_strings
from backend.providers.errors import UpstreamProtocolError
from backend.visual.grounding import current_visual_grounding_verified, native_visual_input_delivered

@dataclass(frozen=True)
class ContextOperations:
    """当前任务解释和快照绑定；不持有另一份会话。"""

    prepare_action_turn: Callable[..., Any]
    utterance_interpretation_from_prepared: Callable[..., Any]
    bind_turn_context_to_result: Callable[..., Any]
    clarification_context_for_turn: Callable[..., Any]
    current_task_semantic_request: Callable[..., Any]


@dataclass(frozen=True)
class EvidenceOperations:
    """既有事实、引用、视觉与完成核验；失败与修复次数保持原合同。"""

    apply_attachment_read_failure_boundary: Callable[..., Any]
    attach_verified_web_sources: Callable[..., Any]
    rewrite_reply_for_grounding_gate: Callable[..., Any]
    enforce_external_action_receipt: Callable[..., Any]
    repair_reference_attribution_once: Callable[..., Any]
    refresh_visual_grounding_for_current_text: Callable[..., Any]
    ensure_turn_contract_completion: Callable[..., Any]


@dataclass(frozen=True)
class PresentationOperations:
    """回复表现与语音交付装配；模型参数与人格快照由调用参数约束。"""

    dialogue_validation_for_result: Callable[..., Any]
    enforce_nickname_reaction_grounding: Callable[..., Any]
    align_spoken_text_ja: Callable[..., Any]
    punctuation_fallback_speech_units: Callable[..., Any]
    speech_unit_contract_for_result: Callable[..., Any]
    enforce_identity_grounding: Callable[..., Any]
    enforce_speaker_attribution_grounding: Callable[..., Any]
    enforce_direct_social_request: Callable[..., Any]
    enforce_explicit_supportive_tone: Callable[..., Any]
    enforce_distress_support_tone: Callable[..., Any]
    enforce_explicit_laugh_request: Callable[..., Any]
    enforce_explicit_choice_grounding: Callable[..., Any]


@dataclass(frozen=True)
class PersistenceOperations:
    """生成文件和最终状态保存；事务与失败回滚由各自服务拥有。"""

    materialize_generated_files: Callable[..., Any]
    persist_finalized_chat_state: Callable[..., Any]


class ReplyFinalizer:
    def __init__(
        self, context: ContextOperations, evidence: EvidenceOperations,
        presentation: PresentationOperations, persistence: PersistenceOperations, *,
        finish_reason: Callable[[], str], configured: Callable[[], bool],
        write_log: Callable[..., None], update_progress: Callable[..., None],
    ) -> None:
        self.context = context
        self.evidence = evidence
        self.presentation = presentation
        self.persistence = persistence
        self.finish_reason = finish_reason
        self.configured = configured
        self.write_log = write_log
        self.update_progress = update_progress

    def finalize(
        self,
        settings: dict[str, Any],
        user_message: str,
        model_result: dict[str, Any],
        *,
        request_id: str = "",
        started_at: float | None = None,
        preserve_accepted_text: bool = False,
        stream_preamble: dict[str, Any] | None = None,
        prepared_turn: PreparedTurn | None = None,
        defer_persistence_until_after_utterance: bool = False,
        persona_snapshot: PersonaContextSnapshot | None = None,
        resolved_interpretation: UtteranceInterpretation | None = None,
        resolved_contract: TurnContract | None = None,
        resolved_dialogue: Sequence[Mapping[str, Any]] | None = None,
        cognitive_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enrich an accepted model reply without letting local persistence hide it."""

        if not isinstance(model_result, dict):
            raise UpstreamProtocolError("normalized upstream reply is not an object")
        result = dict(model_result)
        if cognitive_body is not None:
            self.context.bind_turn_context_to_result(cognitive_body, result)
        cognitive_final = (result.get("_cognitiveOutcomeVerification") or {}).get("valid") is True
        if cognitive_final:
            preserve_accepted_text = True
        saved_task_dialogue = result.pop("_taskDialogue", [])
        task_dialogue = list(resolved_dialogue if resolved_dialogue is not None else saved_task_dialogue)
        semantic_task_request = str(
            result.pop("_semanticTaskRequest", "")
            or (resolved_interpretation.semantic_text if resolved_interpretation else user_message)
        ).strip()[:1200]
        if cognitive_body is not None and cognitive_body.get("_cognitiveRepairRoute"):
            semantic_task_request = self.context.current_task_semantic_request(cognitive_body, user_message)
        initial_proactive_directive = normalize_proactive_directive(
            result.get("proactiveDirective")
        )
        initial_proactive_directive_provided = (
            bool(result.get("proactiveDirectiveProvided"))
            if "proactiveDirectiveProvided" in result
            else str(model_result.get("proactiveDirective") or "").strip().lower()
            in PROACTIVE_DIRECTIVES
        )
        upstream_finish = str(
            result.pop("_upstreamFinishReason", "")
            or self.finish_reason()
            or ""
        )[:80]
        prepared_for_contract = prepared_turn or self.context.prepare_action_turn(
            user_message,
            pending_action={},
            request_id=request_id,
        )
        interpretation = resolved_interpretation or (
            result.pop("_utteranceInterpretation")
            if isinstance(result.get("_utteranceInterpretation"), UtteranceInterpretation)
            else self.context.utterance_interpretation_from_prepared(prepared_for_contract)
        )
        inferred_policy = choose_turn_policy(
            user_message,
            has_tools=bool(result.get("toolUsed") or result.get("_conversationToolResults")),
            has_attachments=bool(result.get("attachments")),
            file_output_expected=bool(
                result.get("_fileOutputExpected") or result.get("_fileOutputs")
            ),
            interpretation=interpretation,
        )
        effective_cognitive_contract = (
            cognitive_body.get("_cognitiveTurnContract")
            if cognitive_final and isinstance(cognitive_body, Mapping) else None
        )
        saved_contract = result.pop("_turnContract", None)
        contract = (effective_cognitive_contract if isinstance(effective_cognitive_contract, TurnContract) else None) or resolved_contract or (
            saved_contract
            if isinstance(saved_contract, TurnContract)
            else build_turn_contract(interpretation, inferred_policy)
        )
        cold_shoulder_short_circuit = str(result.get("source") or "") == "furious-cold-shoulder"
        chat_metrics = result.pop("_chatMetrics", {})
        if not isinstance(chat_metrics, dict):
            chat_metrics = {}
        chat_metrics["requestId"] = request_id
        conversation_tool_results = (
            result.get("_conversationToolResults")
            if isinstance(result.get("_conversationToolResults"), list)
            else []
        )
        result = self.evidence.apply_attachment_read_failure_boundary(
            result,
            conversation_tool_results,
        )
        receipt = research_receipt_from_tool_results(
            request_id,
            conversation_tool_results,
        )
        epistemic_plan = (
            EpistemicPlan.from_dict(prepared_for_contract.epistemic_plan)
            if isinstance(prepared_for_contract.epistemic_plan, dict)
            else None
        )
        if epistemic_plan is None:
            epistemic_plan = create_epistemic_plan(
                request_id,
                interpretation,
            )
        visual_attempt_results = [
            item for item in conversation_tool_results
            if isinstance(item, Mapping)
            and str(item.get("name") or "") in {
                "capture_screen", "capture_camera", "read_image",
            }
        ]
        if has_typed_visual_evidence(conversation_tool_results):
            if not cognitive_final or contract.required_acts != ("answer_from_current_visual_observation",):
                contract = bind_current_visual_observation_contract(
                    contract,
                    semantic_text=semantic_task_request,
                )
            epistemic_plan = epistemic_plan.with_current_visual_observation(
                subject=semantic_task_request,
            )
        elif visual_attempt_results:
            if not cognitive_final or contract.required_acts != ("explain_current_visual_observation_failure",):
                contract = bind_current_visual_failure_contract(
                    contract,
                    semantic_text=semantic_task_request,
                )
            epistemic_plan = epistemic_plan.with_current_visual_observation(
                subject=semantic_task_request,
                observation_available=False,
            )
        if isinstance(cognitive_body, dict):
            cognitive_body["_cognitiveTurnContract"] = contract
        epistemic_state = epistemic_plan.epistemic_state()
        effective_research_policy = (
            epistemic_plan.research_policy
            if settings.get("permissionWebSearch")
            and not result.get("webSearchFailed")
            and not result.get("webEvidenceLimited")
            else "forbidden"
        )
        if not cognitive_final:
            if hard_fact_gate_requires_terminal_boundary(epistemic_state, receipt):
                result = terminal_grounding_boundary_reply(result)
                self.write_log(
                    "grounded_claim_gate_hard_terminal_boundary_applied",
                    requestId=request_id,
                    planId=epistemic_plan.plan_id,
                    planRevision=epistemic_plan.revision,
                )
            # Field repair in call_upstream may have changed Japanese after the first
            # visual audit. Renew that receipt BEFORE the generic gate consumes it.
            result = self.evidence.refresh_visual_grounding_for_current_text(
                settings, semantic_task_request, result, conversation_tool_results,
                request_id=request_id, phase="pre_claim_gate",
                task_dialogue=task_dialogue,
            )
            gate_decision = grounded_claim_gate_decision(
                str(result.get("reply") or ""),
                epistemic_state,
                receipt,
                research_policy=effective_research_policy,
                visual_evidence_verified=(current_visual_grounding_verified(result, conversation_tool_results)
                                          or native_visual_input_delivered(result, conversation_tool_results)),
            )
            self.write_log(
                "grounded_claim_gate_decision",
                requestId=request_id,
                planId=epistemic_plan.plan_id,
                planRevision=epistemic_plan.revision,
                decision=gate_decision.to_dict(),
            )
            if (
                gate_decision.status in {"REWRITE_WITH_UNCERTAINTY", "BLOCK"}
                and self.configured(settings)
            ):
                self.update_progress(
                    request_id,
                    "grounding-rewrite",
                    "正在收敛缺少证据的断言",
                )
                result = self.evidence.rewrite_reply_for_grounding_gate(
                    settings,
                    user_message,
                    result,
                    gate_decision,
                    epistemic_plan,
                    request_id=request_id,
                    persona_snapshot=persona_snapshot,
                )
                result = self.evidence.refresh_visual_grounding_for_current_text(
                    settings, semantic_task_request, result, conversation_tool_results,
                    request_id=request_id, phase="post_claim_rewrite",
                    task_dialogue=task_dialogue,
                )
                gate_decision = grounded_claim_gate_decision(
                    str(result.get("reply") or ""),
                    epistemic_state,
                    receipt,
                    research_policy=effective_research_policy,
                    visual_evidence_verified=(current_visual_grounding_verified(result, conversation_tool_results)
                                              or native_visual_input_delivered(result, conversation_tool_results)),
                )
                self.write_log(
                    "grounded_claim_gate_rewrite_decision",
                    requestId=request_id,
                    planId=epistemic_plan.plan_id,
                    planRevision=epistemic_plan.revision,
                    decision=gate_decision.to_dict(),
                )
            if gate_decision.status != "PASS":
                self.write_log(
                    "grounded_claim_gate_model_rewrite_rejected",
                    requestId=request_id,
                    violations=list(gate_decision.reasons),
                    gateStatus=gate_decision.status,
                    planId=epistemic_plan.plan_id,
                    planRevision=epistemic_plan.revision,
                    claimType="epistemic_or_research_claim",
                )
                if soft_fact_gate_can_degrade(epistemic_state, gate_decision):
                    for cleanup_pass in range(1, 3):
                        soft_boundary_targets = list(
                            getattr(gate_decision, "missing_verification_targets", ()) or ()
                        )
                        result = soft_grounding_boundary_reply(result, gate_decision)
                        self.write_log(
                            "grounded_claim_gate_soft_boundary_applied",
                            requestId=request_id,
                            planId=epistemic_plan.plan_id,
                            planRevision=epistemic_plan.revision,
                            cleanupPass=cleanup_pass,
                            targets=soft_boundary_targets,
                        )
                        gate_decision = grounded_claim_gate_decision(
                            str(result.get("reply") or ""),
                            epistemic_state,
                            receipt,
                            research_policy=effective_research_policy,
                            visual_evidence_verified=False,
                        )
                        if gate_decision.status == "PASS":
                            break
                        if not soft_fact_gate_can_degrade(epistemic_state, gate_decision):
                            break
                else:
                    result = terminal_grounding_boundary_reply(result)
                    gate_decision = grounded_claim_gate_decision(
                        str(result.get("reply") or ""),
                        epistemic_state,
                        receipt,
                        research_policy=effective_research_policy,
                        visual_evidence_verified=False,
                    )
                self.write_log(
                    "grounded_claim_gate_terminal_boundary_decision",
                    requestId=request_id,
                    planId=epistemic_plan.plan_id,
                    planRevision=epistemic_plan.revision,
                    decision=gate_decision.to_dict(),
                )
            if gate_decision.status != "PASS":
                self.write_log(
                    "grounded_claim_gate_rejected",
                    requestId=request_id,
                    violations=list(gate_decision.reasons),
                    gateStatus=gate_decision.status,
                    planId=epistemic_plan.plan_id,
                    planRevision=epistemic_plan.revision,
                    claimType="epistemic_or_research_claim",
                )
                raise GroundingGateError(gate_decision)
        if receipt:
            result["researchReceipt"] = receipt.to_dict()
            self.write_log(
                "research_receipt_created",
                requestId=request_id,
                researchId=receipt.research_id,
                status=receipt.status,
                executedQueries=list(receipt.executed_queries),
                sourceCount=len(receipt.source_uids),
                pagesReadCount=len(receipt.pages_read),
                evidenceSufficient=receipt.evidence_sufficient,
            )
        result["epistemicState"] = epistemic_state.to_dict()
        result["epistemicPlan"] = epistemic_plan.to_dict()
        result["groundingGate"] = ({"status": "PASS", "authority": "main_model_outcome", "checks": "references_and_receipts"}
                                   if cognitive_final else gate_decision.to_dict())
        postprocess_text_mutations: list[dict[str, Any]] = []

        def apply_validating_transform(
            stage: str,
            transformer,
            current: dict[str, Any],
        ) -> dict[str, Any]:
            before_reply = str(current.get("reply") or "")
            before_spoken = str(current.get("spokenTextJa") or "")
            transformed = transformer(user_message, current)
            if not isinstance(transformed, dict):
                raise TypeError(f"{stage} returned {type(transformed).__name__}")
            changed_fields: list[str] = []
            if str(transformed.get("reply") or "") != before_reply:
                changed_fields.append("reply")
                transformed["reply"] = before_reply
            if str(transformed.get("spokenTextJa") or "") != before_spoken:
                changed_fields.append("spokenTextJa")
                transformed["spokenTextJa"] = before_spoken
            if changed_fields:
                postprocess_text_mutations.append({
                    "stage": stage,
                    "changedFields": changed_fields,
                    "accepted": False,
                    "reason": "postprocessor_may_validate_metadata_but_must_not_author_prose",
                })
                transformed["postprocessTextMutationRejected"] = True
            return transformed

        before_external = {
            "reply": str(result.get("reply") or ""),
            "spokenTextJa": str(result.get("spokenTextJa") or ""),
        }
        result = result if cognitive_final else self.evidence.enforce_external_action_receipt(result, conversation_tool_results)
        external_changed = [
            key for key in ("reply", "spokenTextJa")
            if str(result.get(key) or "") != before_external[key]
        ]
        if external_changed:
            postprocess_text_mutations.append({
                "stage": "external_action_receipt",
                "changedFields": external_changed,
                "accepted": True,
                "reason": "tool_receipt_truth_grounding_requires_contract_revalidation",
            })
        result = self.presentation.enforce_nickname_reaction_grounding(
            user_message,
            result,
            reaction_key=request_id,
            persona_snapshot=persona_snapshot,
        )
        if not cognitive_final and isinstance(result.get("_referenceLedger"), Mapping):
            result = self.evidence.repair_reference_attribution_once(
                settings,
                result,
                result["_referenceLedger"],
                request_id=request_id,
                persona_snapshot=persona_snapshot,
            )
        if (
            conversation_tool_results
            and not isinstance(result.get("sources"), list)
            and any(
                isinstance(item, dict)
                and item.get("ok")
                and item.get("name") in {"web_search", "read_web_page"}
                for item in conversation_tool_results
            )
        ):
            result = self.evidence.attach_verified_web_sources(result, conversation_tool_results)
        if not isinstance(result.get("dialogueValidation"), dict):
            validation = self.presentation.dialogue_validation_for_result(result, inferred_policy)
            result["dialoguePolicy"] = inferred_policy.to_dict()
            result["dialogueValidation"] = validation.to_dict()
            chat_metrics.update({
                "replyMode": inferred_policy.reply_mode,
                "actionBudget": inferred_policy.action_budget,
                "turnPolicyReason": inferred_policy.reason,
                "turnPolicy": inferred_policy.to_dict(),
                "replySentences": validation.sentence_count,
                "replyCharacters": validation.character_count,
                "assistantPhraseHits": list(validation.forbidden_phrase_hits),
                "dialogueValidationIssues": list(validation.issues),
                "dialogueStylePostDeliveryViolation": bool(
                    preserve_accepted_text and validation.needs_rewrite
                ),
            })
        warnings: list[str] = []

        def warn(stage: str, error: Exception) -> None:
            warnings.append(stage)
            self.write_log(
                "chat_postprocess_degraded",
                requestId=request_id,
                stage=stage,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )

        transformer_stages = () if (cold_shoulder_short_circuit or preserve_accepted_text) else (
            ("identity_grounding", self.presentation.enforce_identity_grounding),
            ("speaker_attribution", self.presentation.enforce_speaker_attribution_grounding),
            ("direct_social_request", self.presentation.enforce_direct_social_request),
            ("explicit_supportive_tone", self.presentation.enforce_explicit_supportive_tone),
            ("distress_support_tone", self.presentation.enforce_distress_support_tone),
            ("explicit_laugh_request", self.presentation.enforce_explicit_laugh_request),
            ("explicit_choice_grounding", self.presentation.enforce_explicit_choice_grounding),
        )
        for stage, transformer in transformer_stages:
            try:
                result = apply_validating_transform(stage, transformer, result)
            except Exception as error:
                warn(stage, error)

        requested_pace = "" if cold_shoulder_short_circuit else requested_speech_pace(user_message)
        if requested_pace:
            result["speechPace"] = requested_pace
            result["speechSpeed"] = SPEECH_PACE_SPEED[requested_pace]
            result["speechPaceSource"] = "user_request"

        align_started = time.perf_counter()
        if not preserve_accepted_text:
            try:
                before_spoken_alignment = str(result.get("spokenTextJa") or "")
                alignment_persona_snapshot = (
                    persona_snapshot
                    if persona_snapshot is not None and persona_snapshot.uses_hybrid_provider_context
                    else None
                )
                if result.get("dialogueFieldRepairAttempted"):
                    aligned = (
                        self.presentation.align_spoken_text_ja(
                            settings,
                            result,
                            allow_remote_repair=False,
                            persona_snapshot=alignment_persona_snapshot,
                        )
                        if alignment_persona_snapshot is not None else
                        self.presentation.align_spoken_text_ja(settings, result, allow_remote_repair=False)
                    )
                else:
                    aligned = (
                        self.presentation.align_spoken_text_ja(
                            settings,
                            result,
                            persona_snapshot=alignment_persona_snapshot,
                        )
                        if alignment_persona_snapshot is not None else
                        self.presentation.align_spoken_text_ja(settings, result)
                    )
                if not isinstance(aligned, dict):
                    raise TypeError(f"spoken_ja_alignment returned {type(aligned).__name__}")
                result = aligned
                if str(result.get("spokenTextJa") or "") != before_spoken_alignment:
                    postprocess_text_mutations.append({
                        "stage": "spoken_ja_alignment",
                        "changedFields": ["spokenTextJa"],
                        "accepted": True,
                        "reason": "model_generated_bilingual_alignment_requires_contract_revalidation",
                    })
            except Exception as error:
                warn("spoken_ja_alignment", error)
                if not str(result.get("spokenTextJa") or "").strip():
                    # A generic Japanese sentence is not a translation of the
                    # accepted Chinese reply.  Preserve the text and make speech
                    # unavailability explicit instead of playing unrelated audio.
                    result["speechUnavailableReason"] = "spoken_alignment_failed"
                    result["ttsUnavailableReason"] = "spoken_alignment_failed"
        spoken_align_ms = round((time.perf_counter() - align_started) * 1000)

        if isinstance(stream_preamble, dict):
            preamble_zh = str(stream_preamble.get("textZh") or "").strip()
            preamble_ja = str(stream_preamble.get("spokenTextJa") or "").strip()
            if preamble_zh or preamble_ja:
                # Progress is an ephemeral UI/TTS event, not part of the assistant's
                # final answer.  Folding it back into reply/history caused repeated
                # "我查一下" openings and let a tool-use round contaminate memory.
                result["streamPreamble"] = dict(stream_preamble)
                result["streamPreambleExcludedFromFinal"] = True

        if not cognitive_final:
            result = self.evidence.refresh_visual_grounding_for_current_text(
                settings, semantic_task_request, result, conversation_tool_results,
                request_id=request_id, phase="final_text",
                task_dialogue=task_dialogue,
            )

        result = self.evidence.ensure_turn_contract_completion(
            settings,
            interpretation,
            contract,
            result,
            request_id=request_id,
            text_mutated=bool(postprocess_text_mutations),
            allow_rewrite=not preserve_accepted_text,
            upstream_finish_reason=upstream_finish,
            persona_snapshot=persona_snapshot,
            task_dialogue=task_dialogue,
            cognitive_body=cognitive_body,
            clarification_context=self.context.clarification_context_for_turn(
                result,
                prepared_for_contract,
                chat_metrics,
            ),
        )
        if not cognitive_final and has_typed_visual_evidence(conversation_tool_results):
            contracted_hash = visual_grounding_text_fingerprint(result)
            if str(result.get("visualGroundingTextFingerprint") or "") != contracted_hash:
                result = self.evidence.refresh_visual_grounding_for_current_text(
                    settings, semantic_task_request, result, conversation_tool_results,
                    request_id=request_id, phase="post_contract",
                    task_dialogue=task_dialogue,
                )
                if result.get("visualGroundingTerminalBoundary"):
                    result = self.evidence.ensure_turn_contract_completion(
                        settings,
                        interpretation,
                        contract,
                        result,
                        request_id=request_id,
                        text_mutated=False,
                        allow_rewrite=False,
                        upstream_finish_reason=upstream_finish,
                        persona_snapshot=persona_snapshot,
                        task_dialogue=task_dialogue,
                        cognitive_body=cognitive_body,
                        clarification_context=self.context.clarification_context_for_turn(
                            result,
                            prepared_for_contract,
                            chat_metrics,
                        ),
                    )
        if persona_snapshot is not None:
            result["personaContextSnapshotId"] = persona_snapshot.snapshot_id
        if (
            not result.get("_modelSpeechUnits")
            and (
                result.get("structuredOutputMissingFromModel")
                or result.get("plainTextSalvaged")
            )
        ):
            fallback_units = self.presentation.punctuation_fallback_speech_units(
                result.get("reply") or result.get("textZh"),
                result.get("spokenTextJa"),
            )
            if fallback_units:
                result["_modelSpeechUnits"] = fallback_units
                result["speechSegmentCount"] = len(fallback_units)
                result["speechSegmentCountMissingFromModel"] = False
                result["speechSegmentCountSource"] = "punctuation_plain_text_fallback"
                result["visibleSegmentationSource"] = "punctuation_plain_text_fallback"
                self.write_log(
                    "plain_text_punctuation_plan",
                    requestId=request_id,
                    unitCount=len(fallback_units),
                    ellipsisIsBoundary=False,
                )
        generated_files = self.persistence.materialize_generated_files(result)
        if generated_files:
            result["files"] = generated_files
        result = self.presentation.speech_unit_contract_for_result(result, request_id=request_id)
        chat_metrics.update({
            "turnContractVerification": result.get("turnContractVerification") or "",
            "turnContractSemanticAuditCalls": int(result.get("turnContractSemanticAuditCalls") or 0),
            "turnContractRewriteCalls": int(result.get("turnContractRewriteCalls") or 0),
            "speechUnitCount": len(result.get("speechUnits") or []),
            "speechUnitContractHash": str(
                (result.get("speechUnitContract") or {}).get("contractFingerprint") or ""
            ),
        })
        if postprocess_text_mutations:
            result["postprocessTextMutations"] = postprocess_text_mutations

        # A rewrite/alignment/recovery pass may replace the response object, but it
        # has no authority to silently erase the original turn-level outreach
        # decision. A provided structured decision remains authoritative; missing
        # metadata preserves state instead of guessing from local keywords.
        current_proactive_provided = (
            bool(result.get("proactiveDirectiveProvided"))
            if "proactiveDirectiveProvided" in result
            else str(result.get("proactiveDirective") or "").strip().lower()
            in PROACTIVE_DIRECTIVES
        )
        directive_candidate = (
            initial_proactive_directive
            if initial_proactive_directive_provided
            else result.get("proactiveDirective")
            if current_proactive_provided
            else "unchanged"
        )
        resolved_proactive_directive, proactive_directive_source = (
            resolve_proactive_directive(
                user_message,
                directive_candidate,
                model_directive_provided=(
                    initial_proactive_directive_provided or current_proactive_provided
                ),
            )
        )
        result["proactiveDirective"] = resolved_proactive_directive
        result["proactiveDirectiveProvided"] = True
        result["proactiveDirectiveSource"] = proactive_directive_source

        if cognitive_body is not None:
            # Rewriters construct fresh dictionaries. Restore the accepted turn
            # overlay at the final persistence boundary, using the actual final
            # answer for its digest/trace rather than an earlier draft.
            self.context.bind_turn_context_to_result(cognitive_body, result)
            if (result.get("_cognitiveOverlay") or {}).get("patches"):
                defer_persistence_until_after_utterance = False

        if defer_persistence_until_after_utterance:
            result["ok"] = True
            result["requestId"] = request_id
            result["latencyMs"] = round(
                (time.time() - (started_at or time.time())) * 1000
            )
            if warnings:
                result["pipelineWarnings"] = list(warnings)
            result["pipelineMetrics"] = {
                **chat_metrics,
                "spokenJaRepairMs": spoken_align_ms,
                "spokenJaRepairMode": result.get("spokenTextJaRepairMode") or "none",
                "postprocessDegraded": bool(warnings),
                "serverTotalMs": result["latencyMs"],
                "persistenceDeferredUntilAfterUtterance": True,
            }
            result["_deferredPersistence"] = {
                "chatMetrics": dict(chat_metrics),
                "coldShoulderShortCircuit": cold_shoulder_short_circuit,
            }
            return result

        result, persistence_warnings = self.persistence.persist_finalized_chat_state(
            settings,
            user_message,
            result,
            request_id=request_id,
            interpretation=interpretation,
            chat_metrics=chat_metrics,
            cold_shoulder_short_circuit=cold_shoulder_short_circuit,
        )
        warnings = unique_strings([*warnings, *persistence_warnings])
        result["ok"] = True
        result["requestId"] = request_id
        result["latencyMs"] = round((time.time() - (started_at or time.time())) * 1000)
        if warnings:
            result["pipelineWarnings"] = warnings
        result["pipelineMetrics"] = {
            **chat_metrics,
            "spokenJaRepairMs": spoken_align_ms,
            "spokenJaRepairMode": result.get("spokenTextJaRepairMode") or "none",
            "postprocessDegraded": bool(warnings),
            "serverTotalMs": result["latencyMs"],
        }
        result.pop("_contextProtection", None)
        result.pop("_conversationToolResults", None)
        return strip_participant_realization_metadata(result)
