"""会话 turn 的纯装配规则。

本模块只把已冻结的模型结果和诊断组装成可持久化 turn；SQLite、上下文
文件和后台任务仍由 writer/summaries 通过显式端口负责。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.discourse_reference import DiscourseLedger, append_turn_to_ledger
from agent.epistemic_architecture import (
    build_epistemic_turn_state, claim_records_from_turn,
    research_receipt_from_tool_results,
)
from agent.fingerprints import legacy_persistent_fingerprint
from agent.dialogue_waiting import synchronize_response_waiting
from agent.turn_context import conversation_id_for_epoch, legacy_message_id, message_content_hash
from agent.visual_artifact_cache import visual_artifact_descriptor, visual_artifacts_from_result
from backend.chat.reply_fields import (
    normalize_proactive_directive, normalize_relation_event,
    resolve_proactive_directive,
)
from backend.common.text import unique_strings
from backend.common.values import clamp_float, clamp_int, safe_nonnegative_int
from backend.tools.todo_actions import public_todo_candidate


@dataclass(frozen=True)
class TurnRecordPorts:
    """Only server-owned boundaries needed by record assembly."""

    now_iso: Callable[[], str]
    current_epoch: Callable[[], int]
    write_log: Callable[..., None]
    freeze_source_batch: Callable[..., dict[str, Any]]
    screen_capture_memories_from_result: Callable[[dict[str, Any]], list[dict[str, Any]]]
    max_chat_attachments: int


@dataclass(frozen=True)
class TurnAssembly:
    turn: dict[str, Any]
    source_batch: dict[str, Any]
    screen_captures: list[dict[str, Any]]
    visual_artifacts: list[dict[str, Any]]
    receipt: Any
    claim_records: list[Any]
    context_resolution: dict[str, Any]
    reference_ledger: DiscourseLedger | None
    context_protection: dict[str, Any]
    origin: str
    proactive_event_id: str


def public_attachment_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(metadata.get("id") or ""),
        "name": str(metadata.get("name") or "attachment"),
        "mimeType": str(metadata.get("mimeType") or "application/octet-stream"),
        "size": int(metadata.get("size") or 0),
        "kind": str(metadata.get("kind") or "file"),
    }


class TurnRecordAssembler:
    """Freeze one turn without reading or writing persistent state."""

    def __init__(self, ports: TurnRecordPorts) -> None:
        self.ports = ports

    def assemble(
        self,
        user_message: str,
        result: dict[str, Any],
        diagnostics: dict[str, Any] | None = None,
    ) -> TurnAssembly:
        result.update(synchronize_response_waiting(result))
        metrics = diagnostics if isinstance(diagnostics, dict) else {}
        model_proactive_directive = normalize_proactive_directive(
            result.get("proactiveDirective")
        )
        proactive_directive, proactive_directive_source = resolve_proactive_directive(
            user_message,
            result.get("proactiveDirective"),
            model_directive_provided=bool(result.get("proactiveDirectiveProvided")),
        )
        result["proactiveDirective"] = proactive_directive
        result["proactiveDirectiveSource"] = proactive_directive_source
        if proactive_directive != model_proactive_directive:
            self.ports.write_log(
                "proactive_directive_reconciled",
                requestId=str(metrics.get("requestId") or "")[:120],
                modelDirective=model_proactive_directive,
                resolvedDirective=proactive_directive,
                source=proactive_directive_source,
            )
        active_epoch_at_append = self.ports.current_epoch()
        expected_epoch = (
            safe_nonnegative_int(metrics.get("conversationEpoch"))
            if metrics.get("conversationEpoch") is not None
            else active_epoch_at_append
        )
        turn = {
            "schemaVersion": 1,
            "at": self.ports.now_iso(),
            "user": str(user_message or "").strip(),
            "assistant": str(result.get("reply") or "").strip(),
            "emotion": str(result.get("emotion") or "neutral"),
            "conversationEpoch": expected_epoch,
        }
        for key in ("requiresUserResponse", "requiresUserResponseProvided", "expectedResponseType", "semanticKind", "questionRelation"):
            if key in result:
                turn[key] = deepcopy(result[key])
        if isinstance(result.get("_cognitiveOutcome") or result.get("outcome"), Mapping):
            turn["outcome"] = deepcopy(result.get("_cognitiveOutcome") or result["outcome"])
        if bool(result.get("relationEventProvided")):
            turn["relationEvent"] = normalize_relation_event(result.get("relationEvent"))
            turn["relationEventProvided"] = True
        context_protection = (
            result.get("_contextProtection")
            if isinstance(result.get("_contextProtection"), Mapping) else {}
        )
        if context_protection:
            turn["contextProtection"] = {
                "version": safe_nonnegative_int(context_protection.get("version")) or 1,
                "reasons": unique_strings(
                    context_protection.get("reasons")
                    if isinstance(context_protection.get("reasons"), list) else []
                )[:12],
                "transientReasons": unique_strings(
                    context_protection.get("transientReasons")
                    if isinstance(context_protection.get("transientReasons"), list) else []
                )[:12],
                "claims": unique_strings(
                    context_protection.get("claims")
                    if isinstance(context_protection.get("claims"), list) else []
                )[:8],
                "memoryTypes": unique_strings(
                    context_protection.get("memoryTypes")
                    if isinstance(context_protection.get("memoryTypes"), list) else []
                )[:8],
                "hardProtected": bool(context_protection.get("hardProtected")),
            }
        stable_material = "\0".join([
            turn["at"],
            turn["user"],
            turn["assistant"],
            str(metrics.get("proactiveEventId") or ""),
            str(expected_epoch),
        ])
        turn["turnId"] = "turn_" + legacy_persistent_fingerprint(
            stable_material, length=24
        )
        frozen_turn_id = str(metrics.get("currentTurnId") or "").strip()[:180]
        if frozen_turn_id:
            turn["turnId"] = frozen_turn_id
        turn["conversationId"] = str(
            metrics.get("conversationId")
            or conversation_id_for_epoch(expected_epoch)
        )[:180]
        turn["rootSnapshotId"] = str(metrics.get("rootSnapshotId") or "")[:180]
        turn["userMessageId"] = str(
            metrics.get("userMessageId")
            or legacy_message_id(turn["turnId"], "user")
        )[:240]
        turn["assistantMessageId"] = legacy_message_id(turn["turnId"], "assistant")[:240]
        turn["userContentHash"] = message_content_hash(turn["user"])
        turn["assistantContentHash"] = message_content_hash(turn["assistant"])
        reply_anchor = metrics.get("replyAnchor")
        if isinstance(reply_anchor, Mapping) and reply_anchor:
            turn["replyAnchor"] = dict(reply_anchor)
        context_control = metrics.get("contextControl")
        if isinstance(context_control, Mapping) and context_control.get("directives"):
            turn["contextDirectiveDelta"] = {
                "directives": deepcopy(list(context_control.get("directives") or [])),
                "rejectedResolutionIds": list(context_control.get("rejectedResolutionIds") or []),
                "rejectedMessageIds": list(context_control.get("rejectedMessageIds") or []),
                "preferredMessageIds": list(context_control.get("preferredMessageIds") or []),
                "ambiguous": bool(context_control.get("ambiguous")),
            }
        result["turnIdentity"] = {
            "conversationId": turn["conversationId"],
            "turnId": turn["turnId"],
            "userMessageId": turn["userMessageId"],
            "assistantMessageId": turn["assistantMessageId"],
            "userContentHash": turn["userContentHash"],
            "assistantContentHash": turn["assistantContentHash"],
            "rootSnapshotId": turn["rootSnapshotId"],
        }
        request_id = str(metrics.get("requestId") or "").strip()[:120]
        if request_id:
            turn["requestId"] = request_id
        origin = str(metrics.get("conversationOrigin") or "").strip()
        proactive_event_id = str(metrics.get("proactiveEventId") or "").strip()
        proactive_kind = str(metrics.get("proactiveKind") or "").strip()
        proactive_subject = str(metrics.get("proactiveSubject") or "").strip()
        if origin:
            turn["origin"] = origin[:40]
        if origin == "touch":
            turn["interactionEvent"] = {
                "kind": "touch", "zone": str(result.get("touchZone") or ""),
                "action": str(result.get("touchAction") or ""),
                "description": str(metrics.get("interactionDescription") or "")[:600],
            }
        if proactive_event_id:
            turn["proactiveEventId"] = proactive_event_id[:100]
        if proactive_kind:
            turn["proactiveKind"] = proactive_kind[:40]
        if proactive_subject:
            turn["proactiveSubject"] = proactive_subject[:240]
        if result.get("toolUsed"):
            turn["toolUsed"] = str(result.get("toolUsed"))[:160]
            turn["toolStatus"] = str(result.get("toolStatus") or "unknown")[:40]
        if isinstance(result.get("visualGroundingAudit"), dict):
            visual_audit = result["visualGroundingAudit"]
            turn["visualGroundingAudit"] = {
                "passes": bool(visual_audit.get("passes")),
                "confidence": clamp_float(visual_audit.get("confidence"), 0.0, 1.0, 0.0),
                "claims": [
                    {
                        "claimId": str(item.get("claimId") or "")[:100],
                        "text": str(item.get("text") or "")[:600],
                        "kind": str(item.get("kind") or "")[:40],
                        "status": str(item.get("status") or "")[:40],
                        "evidenceIds": [
                            str(ref)[:180]
                            for ref in (
                                item.get("evidenceIds")
                                if isinstance(item.get("evidenceIds"), list) else []
                            )[:12]
                            if str(ref)
                        ],
                    }
                    for item in (
                        visual_audit.get("claims")
                        if isinstance(visual_audit.get("claims"), list) else []
                    )[:24]
                    if isinstance(item, Mapping)
                ],
            }
        if isinstance(result.get("attachmentAction"), Mapping):
            turn["attachmentAction"] = deepcopy(dict(result["attachmentAction"]))
        dialogue_policy = result.get("dialoguePolicy")
        if isinstance(dialogue_policy, dict):
            turn["dialoguePolicy"] = {
                "reply_mode": str(dialogue_policy.get("reply_mode") or "")[:20],
                "action_budget": clamp_int(dialogue_policy.get("action_budget"), 0, 8, 0),
                "reason": str(dialogue_policy.get("reason") or "")[:240],
                "max_sentences": clamp_int(dialogue_policy.get("max_sentences"), 0, 100, 0),
            }
        dialogue_validation = result.get("dialogueValidation")
        if isinstance(dialogue_validation, dict):
            turn["dialogueValidation"] = {
                "sentence_count": clamp_int(dialogue_validation.get("sentence_count"), 0, 1000, 0),
                "character_count": clamp_int(dialogue_validation.get("character_count"), 0, 100000, 0),
                "issues": [
                    str(item)[:80]
                    for item in dialogue_validation.get("issues", [])[:12]
                    if str(item).strip()
                ],
                "needs_rewrite": bool(dialogue_validation.get("needs_rewrite")),
            }
        if isinstance(result.get("turnContract"), dict):
            turn["turnContract"] = {
                "contract_id": str(result["turnContract"].get("contract_id") or "")[:80],
                "primary_act": str(result["turnContract"].get("primary_act") or "")[:80],
                "required_acts": [
                    str(item)[:80]
                    for item in result["turnContract"].get("required_acts", [])[:12]
                ],
                "reply_mode": str(result["turnContract"].get("reply_mode") or "")[:20],
            }
        if isinstance(result.get("finalTurnEnvelope"), dict):
            turn["finalTurnEnvelope"] = {
                key: result["finalTurnEnvelope"].get(key)
                for key in (
                    "turn_id", "contract_id", "required_acts", "completed_acts",
                    "depends_on_undelivered_prefix", "standalone", "turn_complete",
                    "upstream_finish_reason", "delivery_path",
                )
            }
        context_resolution = metrics.get("contextResolution") if isinstance(metrics.get("contextResolution"), dict) else {}
        if context_resolution.get("active"):
            turn["contextResolution"] = {
                "kind": str(context_resolution.get("kind") or "none")[:40],
                "subject": str(context_resolution.get("subject") or "")[:240],
                "intent": str(context_resolution.get("intent") or "")[:80],
                "requestedAction": str(context_resolution.get("requestedAction") or "")[:80],
                "entities": [str(item)[:100] for item in context_resolution.get("entities", [])[:8]],
                "analysisFocus": [str(item)[:100] for item in context_resolution.get("analysisFocus", [])[:8]],
                "userPosition": str(context_resolution.get("userPosition") or "")[:240],
            }
        if isinstance(result.get("webEvidenceStatus"), dict):
            turn["webEvidenceStatus"] = result["webEvidenceStatus"]
        if isinstance(result.get("sources"), list):
            source_batch = (
                dict(result.get("sourceBatch"))
                if isinstance(result.get("sourceBatch"), dict)
                else self.ports.freeze_source_batch(
                    [item for item in result["sources"] if isinstance(item, dict)],
                    request_id=str(metrics.get("requestId") or ""),
                    topic_key=str(
                        (metrics.get("contextResolution") or {}).get("resolvedQuestion")
                        if isinstance(metrics.get("contextResolution"), dict)
                        else user_message
                    ),
                )
            )
            if source_batch.get("sources"):
                turn["sourceBatchId"] = str(source_batch.get("batchId") or "")[:100]
                turn["sources"] = [
                    {
                        "id": str(item.get("displayId") or "")[:20],
                        "sourceUid": str(item.get("sourceUid") or "")[:80],
                        "batchId": str(item.get("batchId") or source_batch.get("batchId") or "")[:100],
                        "title": str(item.get("title") or "")[:500],
                        "url": str(item.get("url") or "")[:2000],
                        "domain": str(item.get("domain") or "")[:200],
                        "sourceTier": str(item.get("sourceTier") or "")[:8],
                        "sourceType": str(item.get("sourceType") or "")[:60],
                    }
                    for item in source_batch["sources"][:8]
                    if isinstance(item, dict)
                ]
            else:
                source_batch = {}
        else:
            source_batch = {}
        if isinstance(result.get("actionReceipt"), dict):
            turn["actionReceipt"] = {
                key: result["actionReceipt"].get(key)
                for key in (
                    "actionId", "tool", "status", "requestedTarget",
                    "resolvedTarget", "adapter", "error", "timestamp", "action",
                    "taskId", "taskVersionBefore", "taskVersionAfter",
                    "bookmarkId", "name", "versionBefore", "versionAfter", "requestId", "timeFacts", "timeSpec",
                )
                if result["actionReceipt"].get(key) not in (None, "")
            }
        if isinstance(result.get("actionReceipts"), list):
            turn["actionReceipts"] = [
                {
                    key: receipt.get(key)
                    for key in (
                        "actionId", "tool", "status", "requestedTarget",
                        "resolvedTarget", "adapter", "error", "timestamp", "action",
                        "taskId", "taskVersionBefore", "taskVersionAfter",
                        "bookmarkId", "name", "versionBefore", "versionAfter", "requestId", "timeFacts", "timeSpec",
                    )
                    if receipt.get(key) not in (None, "")
                }
                for receipt in result["actionReceipts"][:12]
                if isinstance(receipt, Mapping)
            ]
        if isinstance(result.get("todoContext"), dict):
            turn["todoContext"] = {
                "recentTaskIds": [
                    str(item)[:100]
                    for item in result["todoContext"].get("recentTaskIds", [])[:12]
                    if str(item).strip()
                ],
                "candidateTasks": [
                    public_todo_candidate(item)
                    for item in result["todoContext"].get("candidateTasks", [])[:12]
                    if isinstance(item, Mapping)
                ],
                "observedAt": str(result["todoContext"].get("observedAt") or "")[:80],
            }
        turn["proactiveDirective"] = normalize_proactive_directive(
            result.get("proactiveDirective")
        )
        turn["proactiveDirectiveSource"] = str(
            result.get("proactiveDirectiveSource") or "model_directive"
        )[:60]
        if isinstance(result.get("attachments"), list):
            turn["attachments"] = [
                    public_attachment_metadata(item)
                for item in result["attachments"][:self.ports.max_chat_attachments]
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ]
        screen_captures = self.ports.screen_capture_memories_from_result(result)
        if screen_captures:
            turn["screenCaptures"] = screen_captures
        visual_artifacts = visual_artifacts_from_result(result)
        if visual_artifacts:
            turn["visualArtifacts"] = [visual_artifact_descriptor(item) for item in visual_artifacts]
        receipt = research_receipt_from_tool_results(
            request_id or str(turn.get("proactiveEventId") or ""),
            (
                result.get("_conversationToolResults")
                if isinstance(result.get("_conversationToolResults"), list)
                else []
            ),
            completed_at=turn["at"],
        )
        if receipt:
            turn["researchReceipt"] = receipt.to_dict()
        epistemic_state = (
            dict(result.get("epistemicState"))
            if isinstance(result.get("epistemicState"), dict)
            else build_epistemic_turn_state(user_message).to_dict()
        )
        turn["epistemicState"] = epistemic_state
        claim_records = claim_records_from_turn(turn, receipt=receipt)
        turn["claimRecords"] = [item.to_dict() for item in claim_records]
        reference_ledger_after_turn: DiscourseLedger | None = None
        if isinstance(result.get("_referenceLedger"), Mapping):
            reference_ledger_after_turn = append_turn_to_ledger(
                result["_referenceLedger"],
                turn_id=turn["turnId"],
                user_text=turn["user"],
                assistant_text=turn["assistant"],
                created_at=turn["at"],
                user_message_id=turn["userMessageId"],
                assistant_message_id=turn["assistantMessageId"],
            )
            turn_message_ids = {
                item.message_id
                for item in reference_ledger_after_turn.messages
                if item.turn_id == turn["turnId"]
            }
            turn_mention_ids = {
                item.mention_id
                for item in reference_ledger_after_turn.mentions
                if item.message_id in turn_message_ids
            }
            turn["referenceLedgerDelta"] = {
                "schema": reference_ledger_after_turn.schema,
                "messages": [
                    item.to_dict()
                    for item in reference_ledger_after_turn.messages
                    if item.message_id in turn_message_ids
                ],
                "quotes": [
                    item.to_dict()
                    for item in reference_ledger_after_turn.quotes
                    if item.message_id in turn_message_ids
                ],
                "mentions": [
                    item.to_dict()
                    for item in reference_ledger_after_turn.mentions
                    if item.message_id in turn_message_ids
                ],
                "links": [
                    item.to_dict()
                    for item in reference_ledger_after_turn.links
                    if item.source_mention_id in turn_mention_ids
                ],
            }
        return TurnAssembly(
            turn=turn,
            source_batch=source_batch,
            screen_captures=screen_captures,
            visual_artifacts=visual_artifacts,
            receipt=receipt,
            claim_records=claim_records,
            context_resolution=context_resolution,
            reference_ledger=reference_ledger_after_turn,
            context_protection=dict(context_protection),
            origin=origin,
            proactive_event_id=proactive_event_id,
        )
