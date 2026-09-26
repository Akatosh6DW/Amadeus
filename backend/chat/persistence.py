"""已冻结回复的保存应用服务。

本模块拥有记忆/关系准备、会话提交与提交后投影的顺序，不修改可见回复。
普通保存可记录降级；认知补丁要求提交和投影成功，否则明确抛错。
数据库事务、清空代际和后台观察队列继续归各领域服务，不在此复制状态。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.cognitive_commit import (
    CognitiveCommitError, build_memory_projection_payload,
    has_cognitive_overlay, has_revision_overlay,
)
from agent.context_compatibility import ContextCompatibilityError
from agent.dialogue_architecture import UtteranceInterpretation
from agent.participant_realization import strip_participant_realization_metadata
from backend.common.text import unique_strings

@dataclass(frozen=True)
class MemoryOperations:
    """记忆候选与观察任务；继续由原仓储拥有代际和后台任务。"""

    grounded_memory_candidates: Callable[..., Any]
    recover_declared_memory_action_with_model: Callable[..., Any]
    read_memory: Callable[..., Any]
    schedule_memory_observer: Callable[..., Any]
    memory_clear_generation: Callable[..., Any]
    memory_path: Callable[..., Any]


@dataclass(frozen=True)
class RelationshipOperations:
    """关系状态读取和更新；候选准备与提交后的投影保持区分。"""

    relationship_commitment_from_turn: Callable[..., Any]
    read_relationship_state: Callable[..., Any]
    update_relationship_state: Callable[..., Any]
    default_relationship_state: Callable[..., Any]


@dataclass(frozen=True)
class ConversationOperations:
    """会话提交和兼容恢复；提交失败不能伪造认知修订成功。"""

    context_protection_for_turn: Callable[..., Any]
    append_conversation_turn: Callable[..., Any]
    read_conversation_context: Callable[..., Any]
    default_conversation_context: Callable[..., Any]
    normalize_proactive_control: Callable[..., Any]


@dataclass(frozen=True)
class ProjectionOperations:
    """认知 outbox 投影；复用原提交身份，失败保留可重试记录。"""

    _run_cognitive_projection: Callable[..., Any]
    _project_cognitive_memory_records: Callable[..., Any]
    _project_cognitive_relationship_state: Callable[..., Any]


class ReplyPersistence:
    def __init__(
        self, memory: MemoryOperations, relationship: RelationshipOperations,
        conversation: ConversationOperations, projection: ProjectionOperations,
        *, write_log: Callable[..., None],
    ) -> None:
        self.memory = memory
        self.relationship = relationship
        self.conversation = conversation
        self.projection = projection
        self.write_log = write_log

    def persist(
        self,
        settings: dict[str, Any],
        user_message: str,
        result: dict[str, Any],
        *,
        request_id: str,
        interpretation: UtteranceInterpretation,
        chat_metrics: dict[str, Any],
        cold_shoulder_short_circuit: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        """Persist a frozen answer without changing any user-visible speech.

        Fast streaming calls this only after its validated utterances have been
        emitted.  Legacy JSON calls invoke it inline for response compatibility.
        """

        value = dict(result)
        cognitive_overlay = value.get("_cognitiveOverlay")
        cognitive_envelope = has_cognitive_overlay(cognitive_overlay)
        cognitive_has_patch = has_revision_overlay(cognitive_overlay)
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

        updates: list[dict[str, Any]] = []
        try:
            memory_update_types = value.pop("_memoryUpdateTypes", {})
            if not isinstance(memory_update_types, dict):
                memory_update_types = {}
            raw_model_candidates = value.pop("_memoryCandidates", [])
            if not isinstance(raw_model_candidates, list):
                raw_model_candidates = []
            if not raw_model_candidates and isinstance(value.get("memoryUpdates"), list):
                raw_model_candidates = list(value.get("memoryUpdates") or [])
            memory_updates_provided = bool(value.get("memoryUpdatesProvided"))
            memory_action = str(value.get("memoryAction") or "none").strip().lower()
            memory_action_provided = bool(value.get("memoryActionProvided"))
            model_updates = (
                []
                if cognitive_envelope or cold_shoulder_short_circuit or not memory_updates_provided else
                self.memory.grounded_memory_candidates(raw_model_candidates, user_message)
            )
            memory_action_recovered = False
            if (
                not cognitive_envelope
                and not cold_shoulder_short_circuit
                and memory_action_provided
                and memory_action == "save"
                and not model_updates
            ):
                try:
                    raw_model_candidates = self.memory.recover_declared_memory_action_with_model(
                        settings,
                        user_message,
                        value,
                        request_id=request_id,
                    )
                    model_updates = self.memory.grounded_memory_candidates(
                        raw_model_candidates,
                        user_message,
                    )
                    memory_action_recovered = bool(model_updates)
                except Exception as error:
                    warn("memory_action_structured_repair", error)
            commitment = (
                None
                if cold_shoulder_short_circuit else
                self.relationship.relationship_commitment_from_turn(user_message, value)
            )
            relationship_updates = ([{
                "content": commitment["memory"],
                "type": "relationship_state",
                "importance": 1.0,
                "shouldSave": True,
                "subject": "user_and_assistant",
                "predicate": "relationship_commitment",
                "object": commitment["label"],
                "polarity": "positive",
                "modality": "asserted",
                "temporal": "current",
                "sourceSpan": user_message,
                "userPinned": True,
            }] if commitment else [])
            if commitment:
                memory_update_types[commitment["memory"]] = "relationship_state"
                self.write_log(
                    "relationship_commitment_detected",
                    requestId=request_id,
                    label=commitment["label"],
                    memoryType="relationship_state",
                )
            updates = (
                []
                if cold_shoulder_short_circuit else
                [
                    *model_updates,
                    *relationship_updates,
                ]
            )
            value["memoryDecisionSource"] = (
                "cold_shoulder_suppressed"
                if cold_shoulder_short_circuit else
                "model_semantic_action_repair"
                if memory_action_recovered else
                "model_structured"
                if memory_updates_provided else
                "model_protocol_missing_no_write"
            )
            value["_contextProtection"] = self.conversation.context_protection_for_turn(
                user_message,
                value,
                [item for item in updates if isinstance(item, Mapping)],
                interpretation,
            )
            memories = self.memory.read_memory(settings)
            if not cognitive_envelope:
                self.memory.schedule_memory_observer(
                    updates,
                    user_message=user_message,
                    settings=settings,
                    source_turn_id=request_id,
                    update_types=memory_update_types,
                )
            if not isinstance(memories, list):
                raise TypeError(f"memory persistence returned {type(memories).__name__}")
        except Exception as error:
            if cognitive_has_patch:
                raise CognitiveCommitError(
                    "validated cognitive revision memory preparation failed"
                ) from error
            warn("memory_persistence", error)
            try:
                memories = self.memory.read_memory(settings)
            except Exception:
                memories = []

        try:
            relationship = (
                self.relationship.read_relationship_state()
                if cold_shoulder_short_circuit or cognitive_envelope else
                self.relationship.update_relationship_state(
                    user_message,
                    {**value, "_requestId": request_id},
                )
            )
            if not isinstance(relationship, dict):
                raise TypeError(f"relationship persistence returned {type(relationship).__name__}")
        except Exception as error:
            if cognitive_has_patch:
                raise CognitiveCommitError(
                    "validated cognitive revision relationship preparation failed"
                ) from error
            warn("relationship_persistence", error)
            try:
                relationship = self.relationship.read_relationship_state()
            except Exception:
                relationship = self.relationship.default_relationship_state()

        if cognitive_envelope:
            # The cognitive result is bound before finalisation.  Persist the
            # answer actually accepted by this function so a later audit/repair
            # cannot leave a draft in the digest or trace.
            final_reply = str(value.get("reply") or "").strip()
            final_spoken = str(value.get("spokenTextJa") or "").strip()
            final_speech = str(value.get("speechText") or "").strip()
            digest = (
                deepcopy(value.get("_cognitiveDigest"))
                if isinstance(value.get("_cognitiveDigest"), Mapping) else {}
            )
            digest["answerCommitments"] = unique_strings([
                final_reply,
                final_spoken,
                final_speech,
            ])
            value["_cognitiveDigest"] = digest
            trace = (
                deepcopy(value.get("_cognitiveTrace"))
                if isinstance(value.get("_cognitiveTrace"), Mapping) else {}
            )
            trace["finalAnswer"] = {
                "reply": final_reply,
                "spokenTextJa": final_spoken,
                "speechText": final_speech,
            }
            value["_cognitiveTrace"] = trace
            relationship_projection_result = {
                key: deepcopy(value.get(key))
                for key in (
                    "reply", "spokenTextJa", "speechText", "emotion", "gesture",
                    "relationEvent", "relationEventProvided", "proactiveDirective",
                    "proactiveDirectiveProvided", "turnComplete",
                )
                if key in value
            }
            value["_cognitiveProjectionPayloads"] = {
                "memory_records": {
                    **build_memory_projection_payload(
                        cognitive_overlay,
                        updates,
                        source_turn_id=request_id,
                    ),
                    "userMessage": user_message,
                    "memoryClearGeneration": self.memory.memory_clear_generation(settings),
                    "updateTypes": dict(memory_update_types),
                },
                "relationship_state": {
                    "userMessage": user_message,
                    "result": relationship_projection_result,
                },
            }

        try:
            conversation = self.conversation.append_conversation_turn(user_message, value, settings, chat_metrics)
            if not isinstance(conversation, dict):
                raise TypeError(f"conversation persistence returned {type(conversation).__name__}")
        except ContextCompatibilityError:
            raise
        except Exception as error:
            if cognitive_has_patch:
                raise CognitiveCommitError(
                    "validated cognitive revision persistence failed; answer not committed"
                ) from error
            warn("conversation_persistence", error)
            try:
                conversation = self.conversation.read_conversation_context()
            except ContextCompatibilityError:
                raise
            except Exception:
                conversation = self.conversation.default_conversation_context()

        if cognitive_envelope:
            commit_id = str(value.get("_cognitiveCommitId") or "").strip()
            if not commit_id:
                raise CognitiveCommitError(
                    "cognitive envelope reached finalisation without a commit id"
                )
            try:
                self.projection._run_cognitive_projection(
                    commit_id,
                    {
                        "memory_records": lambda payload: self.projection._project_cognitive_memory_records(
                            payload,
                            settings=settings,
                        ),
                        "relationship_state": self.projection._project_cognitive_relationship_state,
                    },
                    kinds=("memory_records", "relationship_state"),
                )
                memories = self.memory.read_memory(settings)
                relationship = self.relationship.read_relationship_state()
            except Exception as error:
                raise CognitiveCommitError(
                    "cognitive projection failed; retryable outbox remains"
                ) from error

        value["memoryUpdates"] = unique_strings([
            item.get("content") if isinstance(item, Mapping) else item
            for item in updates
        ])
        value["memories"] = memories
        value["relationshipUpdated"] = bool(relationship)
        value["relationshipTone"] = str(relationship.get("stage") or "close_companion")
        value["conversationArchiveCount"] = int(conversation.get("archiveCount") or 0)
        value["proactiveControl"] = self.conversation.normalize_proactive_control(
            conversation.get("proactiveControl")
        )
        value["memoryFile"] = str(self.memory.memory_path(settings))
        value.pop("_contextProtection", None)
        value.pop("_referenceLedger", None)
        for private_key in (
            "_cognitiveOverlay",
            "_cognitiveDigest",
            "_cognitiveTrace",
            "_cognitiveProjectionPayloads",
            "_cognitiveCommitId",
            "_cognitiveStateRevision",
        ):
            value.pop(private_key, None)
        return strip_participant_realization_metadata(value), warnings
