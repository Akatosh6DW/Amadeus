"""交付前核对话语账本，只修复有明确来源证据的我/你归属颠倒。

模型最多按既有预算修订一次；失败时仅更正已证明错误的说话人标签，并清除过期发声单元。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping
import json
from agent.persona_context import PersonaContextSnapshot
from agent.discourse_reference import DiscourseLedger, audit_reply_references, render_discourse_context, resolve_discourse_references
from backend.chat.reply_fields import SPOKEN_JA_NO_EMOJI_RULE

@dataclass(frozen=True)
class AttributionRepairPorts:
    configured: Callable[..., bool]
    send_chat: Callable[..., Any]
    parse_json: Callable[..., Any]
    persona_prefix: Callable[..., Any]
    log: Callable[..., Any]

class AttributionRepair:
    def __init__(self, ports: AttributionRepairPorts) -> None:
        self.ports = ports

    def repair_reference_attribution_once(
        self,
        settings: dict[str, Any],
        result: dict[str, Any],
        ledger_payload: Mapping[str, Any],
        *,
        request_id: str = "",
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> dict[str, Any]:
        """Repair only a ledger-proven speaker reversal before visible delivery."""

        ledger = DiscourseLedger.from_dict(ledger_payload)
        audit = audit_reply_references(str(result.get("reply") or ""), ledger)
        if audit.valid:
            return result
        self.ports.log(
            "reference_attribution_audit_failed",
            requestId=request_id,
            audit=audit.to_dict(),
        )
        repaired_reply = ""
        repaired_spoken = ""
        if self.ports.configured(settings):
            payload: dict[str, Any] = {
                "model": settings["apiModel"],
                "temperature": 0.15,
                "max_tokens": 1400,
                "messages": [
                    *self.ports.persona_prefix(
                        persona_snapshot,
                        legacy_system_prompt=False,
                    ),
                    {"role": "system", "content": (
                        "你是同一轮回答的说话人归因修订器。只修正 auditIssues 指出的我/你或说话人归属，"
                        "完整保留回答其余事实、观点、语气和顺序。不得添加道歉模板、额外事实或新话题。"
                        "participant:user 是用户，participant:assistant 是 Amadeus；引用和 OCR 作者按账本处理。"
                        + "\n" + SPOKEN_JA_NO_EMOJI_RULE
                        + "\n输出 JSON：{\"reply\":\"修正后的完整中文\",\"spokenTextJa\":\"语义完全对应的日语\"}。"
                    )},
                    {"role": "user", "content": json.dumps({
                        "originalReply": str(result.get("reply") or ""),
                        "originalSpokenTextJa": str(result.get("spokenTextJa") or ""),
                        "auditIssues": [dict(item) for item in audit.issues],
                        "referenceLedger": render_discourse_context(
                            ledger,
                            resolve_discourse_references(
                                ledger,
                                message_id=ledger.current_message_id,
                            ),
                            max_chars=3200,
                        ),
                    }, ensure_ascii=False)},
                ],
            }
            if settings.get("useResponseFormat"):
                payload["response_format"] = {"type": "json_object"}
            if "deepseek.com" in settings["apiEndpoint"]:
                payload["thinking"] = {"type": "disabled"}
            try:
                content = self.ports.send_chat(
                    settings,
                    payload,
                    timeout=90,
                    request_id=request_id,
                    purpose="reference_attribution_repair",
                )
                parsed = self.ports.parse_json(content) or {}
                candidate_reply = str(parsed.get("reply") or "").strip()
                candidate_spoken = str(parsed.get("spokenTextJa") or "").strip()
                candidate_audit = audit_reply_references(candidate_reply, ledger)
                if candidate_reply and candidate_audit.valid:
                    repaired_reply = candidate_reply
                    repaired_spoken = candidate_spoken
            except Exception as error:
                self.ports.log(
                    "reference_attribution_model_repair_failed",
                    requestId=request_id,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                )
        if not repaired_reply:
            # Fail closed with a structural, topic-neutral attribution correction.
            # This changes no proposition; it only replaces the proven-wrong
            # participant label with the ledger's actual speaker.
            repaired_reply = str(result.get("reply") or "")
            for issue in audit.issues:
                marker = str(issue.get("marker") or "")
                actual = str(issue.get("actualSpeaker") or "")
                replacement = (
                    "我之前提到过"
                    if actual == "assistant" else
                    "你之前提到过"
                    if actual == "user" else
                    "上一轮对话中提到过"
                )
                if marker:
                    repaired_reply = repaired_reply.replace(marker, replacement, 1)
            repaired_spoken = ""
        repaired = dict(result)
        repaired["reply"] = repaired_reply
        repaired["spokenTextJa"] = repaired_spoken
        repaired["referenceAttributionAudit"] = audit.to_dict()
        repaired["referenceAttributionRepaired"] = True
        repaired.pop("_modelSpeechUnits", None)
        repaired.pop("speechUnits", None)
        repaired.pop("speechUnitContract", None)
        self.ports.log(
            "reference_attribution_repaired",
            requestId=request_id,
            repairMode="model" if repaired_spoken else "local_structural",
            issueCount=len(audit.issues),
        )
        return repaired
