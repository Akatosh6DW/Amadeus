"""回复内容、自然对话策略与完整性的已有有界修复。

规则检查与模型重写在此共享同一个入口，保留冻结正文、附件交付和既有调用次数。
提示、关系读取和模型请求由明确端口接入；本服务不修改任务或持久会话状态。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import json
from agent.persona_context import PersonaContextSnapshot
from agent.dialogue_architecture import DialogueValidation, TurnPolicy, policy_prompt, validate_character_reply
from backend.chat.completion import reply_integrity_issues
from backend.attachments.delivery import preserve_attachment_delivery
from backend.context.reference import contextual_reference_prompt
from backend.context.tool_evidence import tool_context_prompt
from backend.providers.errors import UpstreamProtocolError
from .response_rules import local_empty_reply

@dataclass(frozen=True)
class ReplyRepairPorts:
    """只注入带运行状态的来源；纯规则直接引用实际模块。"""
    send_chat: Callable[..., Any]
    normalize_reply: Callable[..., dict[str, Any]]
    persona_prefix: Callable[..., Any]
    system_prompt: Callable[[], str]
    relationship_prompt: Callable[[dict[str, Any]], str]
    read_relationship: Callable[[], dict[str, Any]]
    should_align: Callable[..., bool]
    log: Callable[..., Any]
    reply_rule: Callable[[], str]
    natural_style: Callable[[], str]
    emotion_rule: Callable[[], str]
    forbidden_phrases: Callable[[], Any]

class ReplyRepairs:
    def __init__(self, ports: ReplyRepairPorts) -> None:
        self.ports = ports

    def dialogue_validation_for_result(
        self,
        result: dict[str, Any],
        turn_policy: TurnPolicy,
        messages: list[dict[str, Any]] | None = None,
    ) -> DialogueValidation:
        previous_replies = [
            str(item.get("content") or "")
            for item in (messages or [])[-6:]
            if isinstance(item, dict) and item.get("role") == "assistant"
        ]
        return validate_character_reply(
            str(result.get("reply") or ""),
            turn_policy,
            forbidden_phrases=self.ports.forbidden_phrases(),
            previous_replies=previous_replies,
        )


    def dialogue_output_validation(
        self,
        settings: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Separate unusable dialogue content from repairable runtime fields."""

        reply = str(result.get("reply") or "").strip()
        spoken = str(result.get("spokenTextJa") or "").strip()
        missing_fields: list[str] = []
        invalid_fields: list[str] = []
        if not spoken or result.get("spokenTextJaMissingFromModel"):
            missing_fields.append("spokenTextJa")
        else:
            spoken_probe = dict(result)
            spoken_probe["speechSegmentCountMissingFromModel"] = False
            if self.ports.should_align(settings, spoken_probe):
                invalid_fields.append("spokenTextJa")
        if result.get("emotionMissingFromModel"):
            missing_fields.append("emotion")
        model_units = result.get("_modelSpeechUnits")
        semantic_segmentation_missing = not isinstance(model_units, list) or not model_units
        content_valid = bool(reply) and not bool(result.get("emptyReply"))
        return {
            "contentValid": content_valid,
            "structureValid": not bool(result.get("structuredOutputInvalid")),
            "missingFields": list(dict.fromkeys(missing_fields)),
            "invalidFields": list(dict.fromkeys(invalid_fields)),
            "requiresFullRetry": not content_valid,
            "requiresFieldRepair": bool(
                content_valid
                and bool({*missing_fields, *invalid_fields} & {"spokenTextJa"})
            ),
            "requiresSemanticAudit": False,
            # Missing model boundaries are not worth a second provider request.
            # The delivery contract will preserve the whole reply as one atomic
            # unit.  A repair already required for another field may opportunistically
            # add speechUnits below without adding another round trip.
            "semanticSegmentationMissing": semantic_segmentation_missing,
        }


    def repair_incomplete_model_reply(
        self,
        settings: dict[str, Any],
        user_message: str,
        bad_output: Any,
        resolution: dict[str, Any],
        tool_results: list[dict[str, Any]],
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> str:
        """Regenerate a structurally truncated answer without inventing local prose."""

        issues = reply_integrity_issues(bad_output)
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.38,
            "max_tokens": 1000,
            "messages": [
                *self.ports.persona_prefix(persona_snapshot, legacy_system_prompt=True),
                {"role": "system", "content": self.ports.reply_rule()},
                *([{"role": "system", "content": contextual_reference_prompt(resolution)}]
                  if contextual_reference_prompt(resolution) else []),
                *([{"role": "system", "content": tool_context_prompt(tool_results, resolution)}]
                  if tool_results else []),
                {"role": "system", "content": (
                    "上一份回答的可见文本结构不完整，开头像是从句中间或引用编号处断开。"
                    "请根据当前用户消息重新生成一份从第一句话开始、语义连续的完整最终回答；"
                    "不要接着残片续写，也不要解释系统错误。保留仍然有效的判断，使用来源时让引用跟在对应事实之后。"
                )},
                {"role": "user", "content": json.dumps({
                    "currentUserMessage": user_message,
                    "resolvedQuestion": resolution.get("resolvedQuestion"),
                    "integrityIssues": issues,
                    "incompleteReply": bad_output,
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        return self.ports.send_chat(settings, payload, timeout=70, purpose="reply_integrity_repair")


    def repair_dialogue_style_reply(
        self,
        settings: dict[str, Any],
        user_message: str,
        result: dict[str, Any],
        turn_policy: TurnPolicy,
        tool_results: list[dict[str, Any]],
        resolution: dict[str, Any],
        relationship_state: dict[str, Any],
        deep_persona_prompt: str,
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> dict[str, Any]:
        """Ask the same character model for one fact-preserving style rewrite."""

        validation = self.dialogue_validation_for_result(result, turn_policy)
        rewrite_source = dict(result)
        preserved_file_outputs = rewrite_source.pop("_fileOutputs", None)
        if isinstance(preserved_file_outputs, list):
            rewrite_source["fileOutputSummary"] = [
                {
                    "name": str(item.get("name") or "")[:240],
                    "extension": str(item.get("extension") or "")[:40],
                }
                for item in preserved_file_outputs[:4]
                if isinstance(item, dict)
            ]
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.42,
            "max_tokens": min(3_200, turn_policy.max_output_tokens + 240),
            "messages": [
                {"role": "system", "content": (
                    persona_snapshot.system_prompt
                    if persona_snapshot is not None and persona_snapshot.uses_hybrid_provider_context
                    else self.ports.system_prompt()
                )},
                {"role": "system", "content": self.ports.natural_style()},
                {"role": "system", "content": self.ports.reply_rule()},
                {"role": "system", "content": (
                    persona_snapshot.prompt_text
                    if persona_snapshot is not None and persona_snapshot.uses_hybrid_provider_context
                    else deep_persona_prompt
                )},
                {"role": "system", "content": policy_prompt(turn_policy)},
                {"role": "system", "content": self.ports.relationship_prompt(relationship_state)},
                *([{
                    "role": "system",
                    "content": tool_context_prompt(
                        tool_results,
                        resolution,
                        max_chars=turn_policy.tool_evidence_max_chars,
                    ),
                }] if tool_results else []),
                {"role": "system", "content": (
                    "上一份回答已经完成事实推理，但表达违反了本轮自然对话策略。"
                    "只做一次语义完整的角色化重写：保留原结论、必要依据、限定条件、来源编号、"
                    "用户要求的每个子任务以及原有情绪方向；删除空洞铺垫、重复总结、默认追问、"
                    "客服套话和内部流程说明。不能增加新事实，不能硬截断。"
                    "若原结果含 fileOutputs，必须原样保留，reply 只压缩对话说明。"
                )},
                {"role": "user", "content": json.dumps({
                    "currentUserMessage": user_message,
                    "validation": validation.to_dict(),
                    "originalResult": rewrite_source,
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        content = self.ports.send_chat(
            settings,
            payload,
            timeout=70,
            purpose="dialogue_style_repair",
        )
        repaired = self.ports.normalize_reply(content, settings=settings)
        if isinstance(preserved_file_outputs, list):
            repaired["_fileOutputs"] = preserved_file_outputs
        preserve_attachment_delivery(result, repaired)
        return repaired


    def enforce_fast_dialogue_policy_once(
        self,
        settings: dict[str, Any],
        user_message: str,
        result: dict[str, Any],
        turn_policy: TurnPolicy,
        *,
        messages: list[dict[str, Any]] | None = None,
        request_id: str = "",
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> dict[str, Any]:
        """Block an over-budget fast reply before it can enter TTS.

        The normal route already owns a model-authored, fact-preserving style
        rewrite.  Fast streaming previously skipped that gate because accepted
        text was frozen, so ``micro`` overflow was merely logged and delivered.
        Reuse the same owner once, validate the replacement, and fail closed to the
        bounded full-response recovery if it still violates policy.
        """

        validation = self.dialogue_validation_for_result(result, turn_policy, messages)
        if not validation.needs_rewrite:
            return dict(result)
        self.ports.log(
            "fast_dialogue_policy_repair_started",
            requestId=request_id,
            replyMode=turn_policy.reply_mode,
            issues=list(validation.issues),
            sentenceCount=validation.sentence_count,
            characterCount=validation.character_count,
        )
        repaired = self.repair_dialogue_style_reply(
            settings,
            user_message,
            result,
            turn_policy,
            [],
            {},
            self.ports.read_relationship(),
            "",
            persona_snapshot,
        )
        repaired_validation = self.dialogue_validation_for_result(
            repaired,
            turn_policy,
            messages,
        )
        rejection_reasons = [
            *repaired_validation.issues,
            *reply_integrity_issues(repaired),
        ]
        if repaired_validation.needs_rewrite or rejection_reasons:
            self.ports.log(
                "fast_dialogue_policy_repair_rejected",
                requestId=request_id,
                replyMode=turn_policy.reply_mode,
                issues=list(dict.fromkeys(str(item) for item in rejection_reasons)),
            )
            raise UpstreamProtocolError(
                "fast dialogue policy rewrite did not satisfy the delivery contract"
            )
        for key in (
            "_upstreamFinishReason", "_chatMetrics", "_conversationToolResults",
            "epistemicPlan", "groundingGate",
        ):
            if key in result and key not in repaired:
                repaired[key] = result[key]
        repaired["dialoguePolicy"] = turn_policy.to_dict()
        repaired["dialogueValidation"] = repaired_validation.to_dict()
        repaired["dialogueStyleRepaired"] = True
        repaired["fastDialoguePolicyRepaired"] = True
        self.ports.log(
            "fast_dialogue_policy_repair_completed",
            requestId=request_id,
            replyMode=turn_policy.reply_mode,
            sentenceCount=repaired_validation.sentence_count,
            characterCount=repaired_validation.character_count,
        )
        return repaired


    def repair_empty_reply(
        self,
        settings: dict[str, Any],
        memories: list[str],
        user_message: str,
        bad_output: str,
        *,
        request_id: str = "",
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> dict[str, Any]:
        repair_prompt = (
            "上一次模型输出没有可见中文内容，这是无效输出。"
            "现在重新回答用户，必须按 semantic-v3 输出 kind='turn' 和非空 speechUnits；"
            "每项 zh 是完整中文台词，ja 是语义对应的自然日语，不要重复输出 reply/spokenTextJa。"
            "不要提到“空白”“返回空白”“系统提示”或“JSON”。"
        )
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.55,
            "messages": [
                *self.ports.persona_prefix(persona_snapshot, legacy_system_prompt=True),
                {"role": "system", "content": self.ports.reply_rule()},
                {"role": "system", "content": self.ports.emotion_rule()},
                {"role": "system", "content": "当前长期记忆：\n" + "\n".join(f"- {item}" for item in memories)},
                {"role": "system", "content": repair_prompt},
                {"role": "user", "content": json.dumps({
                    "user_message": user_message,
                    "invalid_previous_output": bad_output,
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
                purpose="empty_reply_repair",
            )
            repaired = self.ports.normalize_reply(content, settings=settings)
            validation = self.dialogue_output_validation(settings, repaired)
            if not repaired.get("emptyReply") and not validation["requiresFieldRepair"]:
                repaired["repairedEmptyReply"] = True
                repaired["fullReplyRepairAttempted"] = True
                if repaired.get("emotionMissingFromModel"):
                    repaired["emotionResolvedLocally"] = True
                return repaired
        except Exception:
            pass
        fallback = local_empty_reply(user_message, settings=settings)
        fallback["fullReplyRepairAttempted"] = True
        fallback["fullReplyRepairSucceeded"] = False
        return fallback
