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
from agent.epistemic_architecture import research_receipt_from_tool_results
from agent.participant_realization import strip_participant_realization_metadata
from agent.persona_context import PersonaContextSnapshot
from agent.proactive_control import PROACTIVE_DIRECTIVES
from agent.research_architecture import EpistemicPlan, create_epistemic_plan
from agent.visual_grounding import has_current_visual_evidence as has_typed_visual_evidence
from backend.chat.delivery import require_delivery
from backend.chat.presentation import speech_language_profile
from backend.chat.reply_fields import normalize_proactive_directive, resolve_proactive_directive
from backend.common.text import unique_strings
from backend.providers.errors import UpstreamProtocolError

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
    enforce_speaker_attribution_grounding: Callable[..., Any]


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

    def _repair_missing_speech_units(
        self,
        settings: dict[str, Any],
        result: dict[str, Any],
        *,
        request_id: str = "",
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> dict[str, Any]:
        """补齐 accepted stream 中缺失的日语单元，同时冻结中文轨道。

        快路径的中文单元已经通过 stream delivery 放行，不能再用整轮重写来
        修复一个缺失的日语字段。这里逐个调用既有的有界日语对齐器；任何一
        个单元修复失败都转为明确的 text-only 结果，避免把部分日语误当成完整
        音频计划。
        """

        if speech_language_profile(settings)["speechLanguage"] != "ja-JP":
            return result
        # Voice-disabled turns must not add a new remote alignment request just
        # because the accepted stream omitted an optional Japanese field.
        if not settings.get("voiceEnabled", True):
            return result
        source_units = result.get("_modelSpeechUnits")
        if not isinstance(source_units, list) or not source_units:
            return result
        if not any(
            isinstance(item, Mapping)
            and not str(item.get("spokenTextJa") or item.get("ja") or "").strip()
            for item in source_units
        ):
            return result

        alignment_persona_snapshot = (
            persona_snapshot
            if persona_snapshot is not None and persona_snapshot.uses_hybrid_provider_context
            else None
        )
        repaired_units: list[dict[str, Any]] = []
        failed_indexes: list[int] = []
        repaired_count = 0
        for index, raw_unit in enumerate(source_units):
            unit = dict(raw_unit) if isinstance(raw_unit, Mapping) else {}
            text_zh = str(unit.get("textZh") or unit.get("zh") or "").strip()
            spoken_ja = str(unit.get("spokenTextJa") or unit.get("ja") or "").strip()
            if not text_zh:
                unit["textOnlyDelivery"] = True
                unit["speechUnavailableReason"] = "spoken_alignment_failed"
                unit["ttsUnavailableReason"] = "spoken_alignment_failed"
                failed_indexes.append(index)
                repaired_units.append(unit)
                continue
            if spoken_ja:
                unit["textZh"] = text_zh
                unit["spokenTextJa"] = spoken_ja
                unit["textOnlyDelivery"] = False
                unit.pop("speechUnavailableReason", None)
                unit.pop("ttsUnavailableReason", None)
                repaired_units.append(unit)
                continue

            unit_result = {
                **result,
                "reply": text_zh,
                "textZh": text_zh,
                "spokenTextJa": "",
                "spokenTextJaMissingFromModel": True,
            }
            try:
                aligned = (
                    self.presentation.align_spoken_text_ja(
                        settings,
                        unit_result,
                        persona_snapshot=alignment_persona_snapshot,
                    )
                    if alignment_persona_snapshot is not None
                    else self.presentation.align_spoken_text_ja(settings, unit_result)
                )
            except Exception as error:
                self.write_log(
                    "spoken_ja_unit_repair_failed",
                    requestId=request_id,
                    unitIndex=index,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                )
                aligned = {}
            fixed = str(aligned.get("spokenTextJa") or "").strip() if isinstance(aligned, Mapping) else ""
            if fixed and not str(aligned.get("speechUnavailableReason") or "").strip():
                unit["textZh"] = text_zh
                unit["spokenTextJa"] = fixed
                unit["textOnlyDelivery"] = False
                unit.pop("speechUnavailableReason", None)
                unit.pop("ttsUnavailableReason", None)
                repaired_count += 1
            else:
                unit["textZh"] = text_zh
                unit["spokenTextJa"] = ""
                unit["textOnlyDelivery"] = True
                unit["speechUnavailableReason"] = "spoken_alignment_failed"
                unit["ttsUnavailableReason"] = "spoken_alignment_failed"
                failed_indexes.append(index)
            repaired_units.append(unit)

        result["_modelSpeechUnits"] = repaired_units
        if failed_indexes:
            playable_spoken = [
                str(item.get("spokenTextJa") or "").strip()
                for item in repaired_units
                if str(item.get("textZh") or "").strip()
                and str(item.get("spokenTextJa") or "").strip()
                and not bool(item.get("textOnlyDelivery"))
            ]
            if playable_spoken:
                # A failed unit is a local text-only hole in the ordered plan.
                # Keep the valid units in the canonical Japanese projection so
                # the buffered stream and final contract can deliver them.
                result["spokenTextJa"] = "\n\n".join(playable_spoken)
                result["speechText"] = result["spokenTextJa"]
                result["textOnlyDelivery"] = False
                result["speechPartialDelivery"] = True
                result["speechPartialFailureUnitIndexes"] = failed_indexes
                result["speechUnavailableReason"] = "spoken_alignment_partial_failure"
                result["ttsUnavailableReason"] = "spoken_alignment_partial_failure"
                result["spokenTextJaMissingFromModel"] = True
                result["spokenTextJaRepaired"] = bool(repaired_count)
                result["spokenTextJaRepairMode"] = "per_unit_partial"
                result["spokenTextJaRepairFailedUnitIndexes"] = failed_indexes
                return result

            # No unit has a usable Japanese track.  Keep the canonical answer
            # visible, but make the whole turn explicitly text-only.
            result["spokenTextJa"] = ""
            result["speechText"] = str(result.get("reply") or "")
            result["textOnlyDelivery"] = True
            result.pop("speechPartialDelivery", None)
            result.pop("speechPartialFailureUnitIndexes", None)
            result["speechUnavailableReason"] = "spoken_alignment_failed"
            result["ttsUnavailableReason"] = "spoken_alignment_failed"
            result["spokenTextJaMissingFromModel"] = True
            result["spokenTextJaRepaired"] = bool(repaired_count)
            result["spokenTextJaRepairMode"] = "per_unit_failed"
            result["spokenTextJaRepairFailedUnitIndexes"] = failed_indexes
            return result

        result["spokenTextJa"] = "\n\n".join(
            str(item.get("spokenTextJa") or "").strip() for item in repaired_units
        )
        result["spokenTextJaMissingFromModel"] = False
        result["spokenTextJaRepaired"] = bool(repaired_count)
        result["spokenTextJaRepairMode"] = "per_unit_alignment"
        result["textOnlyDelivery"] = False
        result.pop("speechPartialDelivery", None)
        result.pop("speechPartialFailureUnitIndexes", None)
        result.pop("speechUnavailableReason", None)
        result.pop("ttsUnavailableReason", None)
        return result

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
        result = require_delivery(model_result, settings=settings, logger=self.write_log, request_id=request_id)
        canonical_reply = result["reply"]
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
        source_messages = None
        if isinstance(cognitive_body, Mapping):
            source_messages = [dict(row) for row in cognitive_body.get('messages', []) if isinstance(row, Mapping)]
            current_id = str(chat_metrics.get('userMessageId') or cognitive_body.get('userMessageId') or '')
            if current_id and not any(str(row.get('messageId') or row.get('id') or '') == current_id for row in source_messages):
                source_messages.append({'messageId': current_id, 'role': 'user', 'content': user_message})
        conversation_tool_results = (
            result.get("_conversationToolResults")
            if isinstance(result.get("_conversationToolResults"), list)
            else []
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
        result["groundingGate"] = {"status": "NOT_EVALUATED", "authority": "generation_context_and_host_receipts"}
        postprocess_text_mutations: list[dict[str, Any]] = []
        if not cognitive_final:
            result = self.evidence.enforce_external_action_receipt(result, conversation_tool_results)
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
        if preserve_accepted_text:
            try:
                result = self._repair_missing_speech_units(
                    settings,
                    result,
                    request_id=request_id,
                    persona_snapshot=persona_snapshot,
                )
            except Exception as error:
                warn("spoken_ja_unit_repair", error)
                result["spokenTextJa"] = ""
                result["speechText"] = str(result.get("reply") or "")
                result["textOnlyDelivery"] = True
                result["speechUnavailableReason"] = "spoken_alignment_failed"
                result["ttsUnavailableReason"] = "spoken_alignment_failed"
                result["spokenTextJaRepairMode"] = "per_unit_failed"
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

        result["reply"] = canonical_reply
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
        try:
            generated_files = self.persistence.materialize_generated_files(result)
        except Exception as error:
            generated_files = []
            warn("generated_files", error)
        if generated_files:
            result["files"] = generated_files
        try:
            result = self.presentation.speech_unit_contract_for_result(result, request_id=request_id)
        except Exception as error:
            warn("speech_units", error)
            result["speechUnavailableReason"] = "speech_projection_failed"
            result.pop("speechUnits", None)
        result["reply"] = canonical_reply
        result = require_delivery(result, settings=settings, logger=self.write_log, request_id=request_id)
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
                "sourceMessages": source_messages,
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
            source_messages=source_messages,
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
