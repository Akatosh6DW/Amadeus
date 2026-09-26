"""会话工作集与持久化写入服务。

ConversationWriter 只拥有一次 turn 的上下文工作集、等待状态和摘要调度顺序；
真实 SQLite commit、JSONL archive、private recorder 通过窄端口注入，保持
认知提交 -> outbox projection -> private recorder 的原副作用顺序。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from agent.cognitive_commit import has_cognitive_overlay
from agent.context_architecture import canonical_turn_id
from agent.context_pressure import conversation_working_set_pressure, select_conversation_collapse_indexes
from agent.dialogue_waiting import update_waiting_after_turn
from agent.discourse_reference import append_turn_to_ledger
from backend.common.text import unique_strings
from backend.common.values import safe_nonnegative_int
from .records import TurnRecordAssembler
from .summaries import SummaryJobRegistry


@dataclass(frozen=True)
class ConversationWriterRules:
    recent_limit: int
    summary_limit: int
    compress_batch: int
    recent_raw_min: int
    recent_raw_max: int
    recent_raw_token_budget: int
    screen_capture_history_limit: int
    source_batch_history_limit: int


@dataclass(frozen=True)
class ConversationWriterPorts:
    assembler: TurnRecordAssembler
    rules: ConversationWriterRules
    read_context: Callable[[], dict[str, Any]]
    write_context: Callable[[dict[str, Any]], Any]
    append_archive: Callable[..., Any]
    write_action_receipt: Callable[[dict[str, Any]], Any]
    read_relationship_state: Callable[[], dict[str, Any]]
    record_salieri: Callable[[dict[str, Any]], Any]
    write_log: Callable[..., None]
    configured: Callable[[dict[str, Any]], bool]
    now_iso: Callable[[], str]
    current_scope: Callable[[], str]
    request_started_at_ms: Callable[[str], int | None]
    session_started_iso: Callable[[], str]
    commit_context_control: Callable[..., dict[str, Any]]
    apply_proactive_directive: Callable[..., dict[str, Any]]
    should_persist_assistant_question: Callable[..., bool]
    assistant_question_from_text: Callable[[Any], str]
    clear_cognitive_pending: Callable[..., None]
    commit_cognitive: Callable[..., Any]
    run_cognitive_projection: Callable[..., None]
    project_cognitive_context: Callable[..., None]
    circuit: Any
    compact_artifact: Callable[[list[dict[str, Any]]], Any]
    refine_summary: Callable[..., None]
    summary_jobs: SummaryJobRegistry


class ConversationWriter:
    """Append a frozen answer while retaining one shared conversation lock.

    The caller owns the re-entrant lock. This method deliberately does not
    acquire a second lock, so read/clear/archive and append remain serialized
    by the same server compatibility alias.
    """

    def __init__(self, ports: ConversationWriterPorts) -> None:
        self.ports = ports

    def append_locked(
        self,
        user_message: str,
        result: dict[str, Any],
        settings: dict[str, Any],
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assembly = self.ports.assembler.assemble(
            user_message, result, diagnostics,
        )
        turn = assembly.turn
        source_batch = assembly.source_batch
        screen_captures = assembly.screen_captures
        visual_artifacts = assembly.visual_artifacts
        receipt = assembly.receipt
        claim_records = assembly.claim_records
        context_resolution = assembly.context_resolution
        reference_ledger_after_turn = assembly.reference_ledger
        context_protection = assembly.context_protection
        origin = assembly.origin
        proactive_event_id = assembly.proactive_event_id
        metrics = diagnostics if isinstance(diagnostics, dict) else {}
        expected_epoch = safe_nonnegative_int(turn.get("conversationEpoch"))
        request_id = str(turn.get("requestId") or "").strip()[:120]
        cognitive_commit_result = None
        cognitive_overlay = result.get("_cognitiveOverlay")
        if not has_cognitive_overlay(cognitive_overlay):
            self.ports.append_archive(turn)
            # The private recorder must never make an ordinary conversation fail.
            self.ports.record_salieri(turn)
        context = self.ports.read_context()
        context["archiveCount"] = int(context.get("archiveCount") or 0) + 1
        if expected_epoch != max(0, int(context.get("conversationEpoch") or 0)):
            # A request that began before clear may finish afterwards.  Preserve
            # it in the raw audit ledger under its original epoch, but never make
            # it visible to the new conversation working set.
            self.ports.write_log(
                "conversation_turn_archived_in_inactive_epoch",
                turnId=turn["turnId"],
                turnEpoch=expected_epoch,
                activeEpoch=int(context.get("conversationEpoch") or 0),
            )
            if has_cognitive_overlay(cognitive_overlay):
                self.ports.clear_cognitive_pending(context, cognitive_overlay)
                cognitive_commit_result = self.ports.commit_cognitive(
                    turn,
                    result,
                    cognitive_overlay,
                    context=context,
                )
                self.ports.run_cognitive_projection(
                    cognitive_commit_result.commit_id,
                    {
                        "conversation_archive": lambda payload: self.ports.append_archive(
                            dict(payload.get("turn") or {}),
                            write_turn_record=False,
                        ),
                        "conversation_context": self.ports.project_cognitive_context,
                    },
                    kinds=("conversation_archive", "conversation_context"),
                    raise_on_error=False,
                )
                self.ports.record_salieri(turn)
            else:
                self.ports.write_context(context)
            return context
        context = self.ports.commit_context_control(
            context,
            metrics.get("contextControl"),
            resolution_record=(
                metrics.get("resolutionRecord")
                if isinstance(metrics.get("resolutionRecord"), Mapping) else None
            ),
        )
        if reference_ledger_after_turn is None:
            reference_ledger_after_turn = append_turn_to_ledger(
                context.get("referenceLedger"),
                turn_id=turn["turnId"],
                user_text=turn["user"],
                assistant_text=turn["assistant"],
                created_at=turn["at"],
                user_message_id=turn["userMessageId"],
                assistant_message_id=turn["assistantMessageId"],
            )
        context["referenceLedger"] = reference_ledger_after_turn.to_dict()
        if source_batch:
            batches = [
                item for item in context.get("sourceBatches", [])
                if isinstance(item, dict)
                and item.get("batchId") != source_batch.get("batchId")
            ]
            batches.append(source_batch)
            context["sourceBatches"] = batches[-self.ports.rules.source_batch_history_limit:]
        if isinstance(result.get("actionReceipt"), dict) and result["actionReceipt"].get("actionId"):
            self.ports.write_action_receipt(dict(result["actionReceipt"]))
        claims = [
            item
            for item in context.get("claimLedger", [])
            if isinstance(item, dict) and str(item.get("claim_id") or item.get("claimId") or "")
        ]
        claims.extend(item.to_dict() for item in claim_records)
        deduplicated_claims: dict[str, dict[str, Any]] = {}
        for item in claims:
            claim_id = str(item.get("claim_id") or item.get("claimId") or "")
            if claim_id:
                deduplicated_claims[claim_id] = item
        context["claimLedger"] = list(deduplicated_claims.values())[-320:]
        if receipt:
            receipts = [
                item
                for item in context.get("researchReceipts", [])
                if isinstance(item, dict)
                and str(item.get("research_id") or item.get("researchId") or "")
                != receipt.research_id
            ]
            receipts.append(receipt.to_dict())
            context["researchReceipts"] = receipts[-120:]
        recent = [item for item in context.get("recentTurns", []) if isinstance(item, dict)]
        recent.append(turn)
        original_recent_count = len(recent)
        compressed: list[dict[str, Any]] = []
        working_set_budget = 24_000
        working_pressure = conversation_working_set_pressure(
            recent,
            budget_tokens=working_set_budget,
        )
        working_set_tokens = round(working_pressure * working_set_budget)
        storage_safety_trigger = original_recent_count > self.ports.rules.recent_limit
        pending_summary_exists = any(
            isinstance(item, dict)
            and item.get("status") == "pending"
            and bool(item.get("pendingCompaction"))
            for item in context.get("summaries", [])
        )
        pending_summary_id = ""
        pending_summary_turns: list[dict[str, Any]] = []
        if pending_summary_exists:
            pending_item = next(
                item for item in context.get("summaries", [])
                if isinstance(item, dict)
                and item.get("status") == "pending"
                and bool(item.get("pendingCompaction"))
            )
            pending_summary_id = str(pending_item.get("id") or "")
            pending_source_ids = tuple(
                str(item) for item in pending_item.get("sourceTurnIds", [])
                if str(item)
            )
            pending_summary_turns = recent[:len(pending_source_ids)]
            if tuple(
                canonical_turn_id(item, index)
                for index, item in enumerate(pending_summary_turns)
            ) != pending_source_ids:
                pending_summary_turns = []
        should_compact = (
            (working_pressure >= 0.82 or storage_safety_trigger)
            and not pending_summary_exists
        )
        if should_compact:
            target_tokens_to_free = (
                working_set_tokens
                if storage_safety_trigger and working_pressure < 0.82
                else max(
                    1,
                    working_set_tokens - round(working_set_budget * 0.68),
                )
            )
            active_conversation_state = (
                context.get("conversationState")
                if isinstance(context.get("conversationState"), dict) else {}
            )
            dynamically_protected_turn_ids = unique_strings([
                str(active_conversation_state.get("pendingAssistantQuestionId") or ""),
                *(
                    active_conversation_state.get("unresolvedReferenceIds")
                    if isinstance(
                        active_conversation_state.get("unresolvedReferenceIds"), list
                    ) else []
                ),
            ])
            collapse_indexes = select_conversation_collapse_indexes(
                recent,
                batch_size=self.ports.rules.compress_batch,
                target_tokens_to_free=target_tokens_to_free,
                protected_turn_ids=dynamically_protected_turn_ids,
                min_recent_turns=self.ports.rules.recent_raw_min,
                max_recent_turns=self.ports.rules.recent_raw_max,
                recent_token_budget=self.ports.rules.recent_raw_token_budget,
            )
            compressed = [recent[index] for index in collapse_indexes]
        if compressed:
            summary_id = f"{int(time.time() * 1000)}-{context['archiveCount']}"
            summaries = [item for item in context.get("summaries", []) if isinstance(item, dict)]
            compact_artifact = self.ports.compact_artifact(compressed)
            compact_text = compact_artifact.text
            model_summary_pending = self.ports.configured(settings)
            if not model_summary_pending:
                recent = recent[len(compressed):]
            summaries.append({
                "id": summary_id,
                "createdAt": self.ports.now_iso(),
                "text": compact_text,
                "source": "pending_model" if model_summary_pending else "local_fallback",
                "status": "pending" if model_summary_pending else "complete",
                "pendingCompaction": model_summary_pending,
                "summaryPurpose": "dialogue_continuity_only",
                "longTermMemoryWritten": False,
                "conversationEpoch": expected_epoch,
                "sourceTurnIds": [
                    canonical_turn_id(item, index)
                    for index, item in enumerate(compressed)
                ],
                "workingSetPressure": round(working_pressure, 6),
                "artifact": compact_artifact.to_dict(),
            })
            context["summaries"] = summaries[-self.ports.rules.summary_limit:]
        # Never satisfy the storage fallback by silently dropping protected turns.
        # If every old turn is a correction/pending/unresolved anchor, keep the
        # overflow visible and report it rather than manufacturing a lossy summary.
        protected_overflow = (
            len(recent) > self.ports.rules.recent_limit
            and not compressed
            and not pending_summary_exists
        )
        context["recentTurns"] = recent
        context["conversationGeneration"] = int(context.get("conversationGeneration") or 0) + 1
        context["contextPressure"] = {
            "workingSetPressure": round(working_pressure, 6),
            "workingSetTokens": working_set_tokens,
            "workingSetBudgetTokens": working_set_budget,
            "targetTokensToFree": (
                target_tokens_to_free if should_compact else 0
            ),
            "trigger": (
                "token_pressure" if working_pressure >= 0.82 else
                "storage_safety_limit" if storage_safety_trigger else
                "none"
            ),
            "collapsedTurnCount": (
                len(compressed) if compressed and not self.ports.configured(settings) else 0
            ),
            "scheduledTurnCount": len(compressed) if self.ports.configured(settings) else 0,
            "summaryPending": bool(
                pending_summary_exists or (compressed and self.ports.configured(settings))
            ),
            "hardProtectedOverflow": protected_overflow,
            "retainedTurnCount": len(recent),
            "updatedAt": self.ports.now_iso(),
        }
        capture_history = [
            item for item in context.get("screenCaptures", [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        capture_history.extend(screen_captures)
        deduplicated_captures: dict[str, dict[str, Any]] = {}
        for item in capture_history:
            deduplicated_captures[str(item.get("id"))] = item
        context["screenCaptures"] = list(deduplicated_captures.values())[-self.ports.rules.screen_capture_history_limit:]
        if visual_artifacts:
            selected_visual = dict(visual_artifacts[-1])
            previous_visual = context.get("activeVisualArtifact") or {}
            same_visual = selected_visual["id"] == previous_visual.get("id")
            selected_visual["sourceTurnId"] = previous_visual.get("sourceTurnId", turn["turnId"]) if same_visual else turn["turnId"]
            selected_visual["originUserMessage"] = previous_visual.get("originUserMessage", user_message) if same_visual else user_message
            selected_visual["lastReferencedAt"] = turn["at"]
            context["activeVisualArtifact"] = selected_visual
        pending_question = self.ports.assistant_question_from_text(turn["assistant"])
        if result.get("requiresUserResponseProvided") and result.get("requiresUserResponse"):
            pending_question = str(turn["assistant"] or "").strip()
        if not self.ports.should_persist_assistant_question(user_message, turn["assistant"], pending_question, result):
            pending_question = ""
        task = ((result.get("_cognitiveOverlay") or {}).get("state") or {}).get("task") or {}
        update_waiting_after_turn(context, turn={**turn, "origin": origin or "chat", "proactiveEventId": proactive_event_id},
            result=result, question=pending_question, now=datetime.now(timezone.utc),
            session_id=self.ports.session_started_iso(),
            task_id=str(task.get("taskId") or "") if task.get("status") in {"active", "paused"} else "")
        if context_resolution.get("active"):
            context["activeTask"] = {
                key: context_resolution.get(key)
                for key in (
                    "kind", "subject", "intent", "resolvedQuestion", "matchup", "eventYear",
                    "requestedAction", "constraints", "entities", "analysisFocus", "userPosition", "personaModules",
                )
                if context_resolution.get(key) not in (None, "", [], {})
            }
            context["activeTask"]["updatedAt"] = self.ports.now_iso()
        elif context_resolution.get("clearActiveTask"):
            context["activeTask"] = {}
        conversation_state = (
            context.get("conversationState")
            if isinstance(context.get("conversationState"), dict) else {}
        )
        subject = str(context_resolution.get("subject") or context_resolution.get("resolvedQuestion") or "").strip()[:240]
        topic_history = [str(item)[:240] for item in conversation_state.get("recentTopicHistory", []) if str(item).strip()]
        if subject and (not topic_history or topic_history[-1] != subject):
            topic_history.append(subject)
        protection_reasons = {
            str(item)
            for item in [
                *(
                    context_protection.get("reasons")
                    if isinstance(context_protection.get("reasons"), list) else []
                ),
                *(
                    context_protection.get("transientReasons")
                    if isinstance(context_protection.get("transientReasons"), list) else []
                ),
            ]
            if str(item)
        }
        unresolved_reference_ids = [
            str(item)[:120]
            for item in conversation_state.get("unresolvedReferenceIds", [])
            if str(item).strip()
        ]
        if "unresolvedReference" in protection_reasons:
            unresolved_reference_ids = unique_strings([
                *unresolved_reference_ids,
                str(turn["turnId"]),
            ])[-24:]
        resolved_reference_ids = {
            str(item)
            for item in (
                result.get("resolvedReferenceIds")
                if isinstance(result.get("resolvedReferenceIds"), list) else []
            )
            if str(item)
        }
        if resolved_reference_ids:
            unresolved_reference_ids = [
                item for item in unresolved_reference_ids
                if item not in resolved_reference_ids
            ]
        memory_types = {
            str(item)
            for item in (
                context_protection.get("memoryTypes")
                if isinstance(context_protection.get("memoryTypes"), list) else []
            )
            if str(item)
        }
        active_episode_id = str(conversation_state.get("activeEpisodeId") or "")[:120]
        if memory_types & {"episode", "episodic_archive", "shared_reference"}:
            active_episode_id = str(turn["turnId"])
        conversation_state.update({
            "currentTopic": subject or str(conversation_state.get("currentTopic") or ""),
            "recentTopicHistory": topic_history[-12:],
            "pendingAssistantQuestionId": (
                str((context.get("pendingAssistantQuestion") or {}).get("questionId") or "")
            ),
            "unresolvedReferenceIds": unresolved_reference_ids,
            "currentRelationshipContext": str(self.ports.read_relationship_state().get("stage") or "")[:80],
            "currentContextEpoch": expected_epoch,
            "activeEpisodeId": active_episode_id,
            "lastUserTurnId": str(turn["turnId"]) if turn["user"] else str(conversation_state.get("lastUserTurnId") or ""),
            "lastAssistantTurnId": str(turn["turnId"]) if turn["assistant"] else str(conversation_state.get("lastAssistantTurnId") or ""),
        })
        context["conversationState"] = conversation_state
        proactive_control = self.ports.apply_proactive_directive(
            context,
            result.get("proactiveDirective"),
            user_initiated=bool(turn["user"]),
            source_turn_id=str(turn["turnId"]),
            request_started_at_ms=self.ports.request_started_at_ms(request_id),
            source_user_message=str(turn["user"] or ""),
            pause_reason=str(
                result.get("_proactivePauseReason") or "user_requested_silence"
            ),
        )
        result["proactiveControl"] = dict(proactive_control)
        context["relationshipGeneration"] = safe_nonnegative_int(
            self.ports.read_relationship_state().get("generation")
        )
        context["backgroundJobCircuits"] = self.ports.circuit.snapshot()
        if has_cognitive_overlay(cognitive_overlay):
            self.ports.clear_cognitive_pending(context, cognitive_overlay)
            cognitive_commit_result = self.ports.commit_cognitive(
                turn,
                result,
                cognitive_overlay,
                context=context,
            )
            self.ports.run_cognitive_projection(
                cognitive_commit_result.commit_id,
                {
                    "conversation_archive": lambda payload: self.ports.append_archive(
                        dict(payload.get("turn") or {}),
                        write_turn_record=False,
                    ),
                    "conversation_context": self.ports.project_cognitive_context,
                },
                kinds=("conversation_archive", "conversation_context"),
                raise_on_error=False,
            )
            # The private recorder must run only after the authoritative SQLite
            # commit, so it cannot become the first durable representation.
            self.ports.record_salieri(turn)
        else:
            self.ports.write_context(context)
        summary_job_id = summary_id if compressed and self.ports.configured(settings) else pending_summary_id
        summary_job_turns = compressed if compressed and self.ports.configured(settings) else pending_summary_turns
        summary_job_key = (self.ports.current_scope(), summary_job_id)
        schedule_summary_job = False
        if summary_job_id and summary_job_turns:
            schedule_summary_job = self.ports.summary_jobs.reserve(summary_job_key)
        if schedule_summary_job:
            threading.Thread(
                target=self.ports.refine_summary,
                args=(
                    summary_job_id,
                    summary_job_turns,
                    dict(settings),
                    self.ports.current_scope(),
                    expected_epoch,
                ),
                daemon=True,
                name="amadeus-memory-compressor",
            ).start()
        return context
