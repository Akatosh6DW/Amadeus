"""回答合同的模型审计与有界重写。

本模块只发起既有的完成性审计或一次合同重写。动作回执和附件交付仍由实际 owner
解释；人格前缀、回复投影及字段修复通过分域端口接入，避免复制请求状态。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol, Sequence

from agent.dialogue_architecture import TurnContract, UtteranceInterpretation, choose_turn_policy
from agent.persona_context import PersonaContextSnapshot
from backend.attachments import delivery as attachment_delivery
from backend.chat import completion as chat_completion
from backend.chat import fast_messages
from backend.chat import presentation
from backend.chat.parsing import parse_json_object
from backend.context import prompts as context_prompts
from backend.context import tool_evidence
from backend.providers.errors import UpstreamProtocolError


class ContractModelGateway(Protocol):
    def send_chat(self, *args: Any, **kwargs: Any) -> Any: ...


class ContractPersonaOperations(Protocol):
    def repair_prefix(self, *args: Any, **kwargs: Any) -> Any: ...
    def nickname_reaction_prompt(self, *args: Any, **kwargs: Any) -> Any: ...
    def enforce_nickname_grounding(self, *args: Any, **kwargs: Any) -> Any: ...


class ContractProjectionOperations(Protocol):
    def normalize_reply(self, *args: Any, **kwargs: Any) -> Any: ...
    def repair_dialogue_fields_once(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class ContractModelServices:
    model: ContractModelGateway
    persona: ContractPersonaOperations
    projection: ContractProjectionOperations


@dataclass(frozen=True)
class ContractModelPolicy:
    utterance_stream_reply_rule: str
    attachment_reply_rule: str
    max_generated_files: int


def _authoritative_outcome_for_result(
    policy: ContractModelPolicy,
    result: Mapping[str, Any],
) -> Any:
    return chat_completion.authoritative_outcome_for_result(
        result,
        max_generated_files=policy.max_generated_files,
    )


def audit_turn_contract_with_model(
    services: ContractModelServices,
    policy: ContractModelPolicy,
    settings: dict[str, Any],
    interpretation: UtteranceInterpretation,
    contract: TurnContract,
    result: dict[str, Any],
    *,
    request_id: str,
    clarification_context: Mapping[str, Any] | None = None,
    task_dialogue: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """只为存在完成性风险的候选调用领域无关语义审计器。"""

    action_outcome = _authoritative_outcome_for_result(policy, result)
    payload: dict[str, Any] = {
        "model": settings["apiModel"],
        "temperature": 0.0,
        "max_tokens": 320,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是回答完成性审计器，不扮演角色，也不改写答案。"
                    "只输出 JSON："
                    '{"answersCurrentIntent":true,"standalone":true,'
                    '"dependsOnUndeliveredPrefix":false,"zhJaCoreAligned":true,'
                    '"actionOutcomeAligned":true,'
                    '"allRequiredActsCompleted":true,'
                    '"clarificationNecessary":false,'
                    '"clarificationAddressesMissingInput":false,'
                    '"availableOrAuthorizedToolCouldResolve":false,'
                    '"missingInput":"","reason":"",'
                    '"completedActs":["..."],"missingRequiredActs":[],'
                    '"confidence":0.0}。'
                    "根据当前用户语义、requiredActs、中文回答和日语发声稿判断；"
                    "不能因为语言流畅就判定完成。若提供 authoritativeActionOutcome，"
                    "其中可能包含后端动作回执、待持久化的记忆决定或已生成产物，"
                    "必须同时判断这些结构化结果是否符合当前用户意图，以及中日文回答"
                    "是否与结果一致；成功后继续询问同一目标、"
                    "失败后声称成功都不一致。若 clarificationCandidate=true，只有当前"
                    "用户消息和已有动作回执确实缺少完成任务所必需的输入，并且回答只索取"
                    "那个缺失输入时，clarificationNecessary 和"
                    "clarificationAddressesMissingInput 才能为 true；可直接完成的任务、"
                    "可选追问、泛泛邀请补充或逃避回答必须判为 false。"
                    "availableInputContext 中已有附件、成功工具结果或已授权工具能够取得"
                    "所需内容时，availableOrAuthorizedToolCouldResolve 必须为 true，且不得"
                    "通过通用澄清。missingInput 写具体缺失项，reason 简述依据。"
                    "澄清需要用户下一轮回答是预期行为，不等于依赖未显示的助手前缀；"
                    "standalone 和 dependsOnUndeliveredPrefix 只判断当前输出是否缺少一段"
                    "本应先展示的助手文本。requiredActs 是内部结果标签，不是回答必须"
                    "逐字出现的口令：按回答实际内容判断是否完成。尤其当用户要求检查、"
                    "评审或评价已经给出的材料时，只要回答给出具体判断、风险或改进建议，"
                    "就属于实质完成 complete_requested_task，不得因为没有自称“检查完成”"
                    "而否定。completedActs 和 missingRequiredActs 只能使用 requiredActs 中"
                    "的原标签；子步骤和解释写进 reason。"
                    "recentDialogue 是实际发生的对话，用于理解省略指代、被处理的内容和用户纠正，不是新任务。"
                    "originalUserMessage 及用户在对话中指定的范围优先于 semanticText 摘要。"
                    "对改写、总结或格式转换任务，直接返回处理后的内容即可，不要求额外宣告完成、标题或过程说明。"
                    "若 requiredActs 只是 respond_to_current_utterance，针对所问对象明确说明不能判断或拒绝并解释理由，也可以完成回应；"
                    "完成回应不等于赞同用户、给出用户强求的结论或证明回答内容质量优秀，不能仅因用户坚持就要求反复重写。"
                    "这不适用于伪称外部操作成功：动作任务仍必须核对真实回执，不能把拒绝算成操作已完成。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "originalUserMessage": interpretation.original_text,
                        "recentDialogue": list(task_dialogue),
                        "semanticText": interpretation.semantic_text,
                        "primarySpeechAct": interpretation.primary_speech_act,
                        "requiredActs": list(contract.required_acts),
                        "replyZh": str(result.get("reply") or ""),
                        "spokenTextJa": str(result.get("spokenTextJa") or ""),
                        "clarificationCandidate": chat_completion.generic_missing_input_clarification_candidate(
                            result,
                            contract,
                        ),
                        "modelDeclaredKind": str(result.get("semanticKind") or ""),
                        "expectedResponseType": str(result.get("expectedResponseType") or ""),
                        "availableInputContext": dict(clarification_context or {}),
                        "authoritativeActionOutcome": action_outcome,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    if settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in settings["apiEndpoint"]:
        payload["thinking"] = {"type": "disabled"}
    content = services.model.send_chat(
        settings,
        payload,
        timeout=45,
        request_id=request_id,
        purpose="turn_contract_audit",
    )
    audit = parse_json_object(content)
    if not isinstance(audit, dict):
        raise UpstreamProtocolError("Turn Contract audit returned no JSON object")
    completed = audit.get("completedActs")
    completed_set = (
        {str(item) for item in completed if str(item)} if isinstance(completed, list) else set()
    )
    declared_missing = audit.get("missingRequiredActs")
    declared_missing_items = (
        [str(item) for item in declared_missing if str(item)]
        if isinstance(declared_missing, list)
        else []
    )
    declared_missing_set = {
        item for item in declared_missing_items if item in contract.required_acts
    }
    all_required_completed = audit.get("allRequiredActsCompleted")
    if all_required_completed is True and not declared_missing_items:
        missing: list[str] = []
        audit["completedActs"] = list(contract.required_acts)
    elif all_required_completed is True:
        # An aggregate success cannot coexist with any declared missing act.
        # Unknown labels are protocol inconsistency, not evidence of success.
        missing = list(contract.required_acts)
        audit["completionVerdictInconsistent"] = True
    elif all_required_completed is False:
        missing = [item for item in contract.required_acts if item in declared_missing_set] or list(
            contract.required_acts
        )
    else:
        # Backward compatibility for providers that have not yet emitted the
        # aggregate semantic verdict. Exact labels are only a legacy fallback,
        # not a second local decision after the model has judged completion.
        missing = [item for item in contract.required_acts if item not in completed_set]
    reply_present = bool(str(result.get("reply") or "").strip())
    spoken_present = bool(str(result.get("spokenTextJa") or "").strip())
    text_only_delivery = bool(result.get("textOnlyDelivery"))
    clarification_candidate = chat_completion.generic_missing_input_clarification_candidate(
        result,
        contract,
    )
    delivery_structure_valid = bool(
        reply_present
        and (
            (audit.get("zhJaCoreAligned") is True and spoken_present)
            or (text_only_delivery and not spoken_present)
        )
        and audit.get("standalone") is True
        and not bool(audit.get("dependsOnUndeliveredPrefix"))
    )
    clarification_delivery_valid = bool(
        reply_present
        and (
            (audit.get("zhJaCoreAligned") is True and spoken_present)
            or (text_only_delivery and not spoken_present)
        )
    )
    audit["clarificationPasses"] = bool(
        clarification_candidate
        and clarification_delivery_valid
        and audit.get("clarificationNecessary") is True
        and audit.get("clarificationAddressesMissingInput") is True
        and audit.get("availableOrAuthorizedToolCouldResolve") is False
    )
    audit["passes"] = bool(
        audit.get("answersCurrentIntent") is True
        and audit.get("standalone") is True
        and not bool(audit.get("dependsOnUndeliveredPrefix"))
        and reply_present
        and (
            (audit.get("zhJaCoreAligned") is True and spoken_present)
            or (text_only_delivery and not spoken_present)
        )
        and not missing
        and (not action_outcome or audit.get("actionOutcomeAligned") is True)
    )
    audit["deliveryStructureValid"] = delivery_structure_valid
    audit["missingRequiredActs"] = missing
    return audit


def rewrite_reply_to_turn_contract(
    services: ContractModelServices,
    policy: ContractModelPolicy,
    settings: dict[str, Any],
    interpretation: UtteranceInterpretation,
    contract: TurnContract,
    result: dict[str, Any],
    audit: dict[str, Any],
    *,
    request_id: str,
    persona_snapshot: PersonaContextSnapshot | None = None,
    task_dialogue: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """让角色模型按合同重写一次；后端不自行编写答案正文。"""

    nickname_contract = services.persona.nickname_reaction_prompt(
        interpretation.original_text,
        request_id,
    )
    payload: dict[str, Any] = {
        "model": settings["apiModel"],
        "temperature": 0.48,
        "max_tokens": (
            6000
            if result.get("_fileOutputExpected")
            else fast_messages.fast_stream_token_budget(
                choose_turn_policy(
                    interpretation.original_text,
                    interpretation=interpretation,
                )
            )
        ),
        "messages": [
            *services.persona.repair_prefix(persona_snapshot, legacy_system_prompt=True),
            *task_dialogue,
            {"role": "system", "content": policy.utterance_stream_reply_rule},
            {
                "role": "system",
                "content": context_prompts.core_dialogue_turn_contract_prompt(
                    interpretation,
                    contract,
                ),
            },
            *(
                [
                    {
                        "role": "system",
                        "content": tool_evidence.tool_context_prompt(
                            [
                                item
                                for item in result.get("_conversationToolResults", [])
                                if isinstance(item, Mapping)
                            ],
                            {},
                            max_chars=52_000,
                        ),
                    }
                ]
                if isinstance(result.get("_conversationToolResults"), list)
                and result.get("_conversationToolResults")
                else []
            ),
            *(
                [{"role": "system", "content": policy.attachment_reply_rule}]
                if result.get("_fileOutputExpected")
                else []
            ),
            *([{"role": "system", "content": nickname_contract}] if nickname_contract else []),
            {
                "role": "system",
                "content": (
                    "上一版没有完成本轮合同。重新生成一份完整、独立的最终回答；"
                    "直接回答当前用户，不要提审计、合同、修复或上一版。"
                    "authoritativeActionOutcome 是后端最终回执：必须据此报告成功、"
                    "待确认或失败，不得继续沿用与它冲突的旧尝试结果。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "currentUserMessage": interpretation.original_text,
                        "previousCandidate": {
                            "reply": str(result.get("reply") or ""),
                            "spokenTextJa": str(result.get("spokenTextJa") or ""),
                        },
                        "audit": audit,
                        "authoritativeActionOutcome": _authoritative_outcome_for_result(
                            policy, result
                        ),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    if settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in settings["apiEndpoint"]:
        payload["thinking"] = {"type": "disabled"}
    content = services.model.send_chat(
        settings,
        payload,
        timeout=75,
        request_id=request_id,
        purpose="turn_contract_rewrite",
    )
    rewritten = services.projection.normalize_reply(content, settings=settings)
    if (
        presentation.normalize_speech_language(rewritten.get("speechLanguage")) == "ja-JP"
        and str(rewritten.get("reply") or "").strip()
        and not str(rewritten.get("spokenTextJa") or "").strip()
    ):
        # 合同重写发生在常规语音对齐之后；复核前只补缺失译文，不能再重写答案。
        rewritten = services.projection.repair_dialogue_fields_once(
            settings,
            rewritten,
            {"missingFields": ["spokenTextJa"]},
            user_message=interpretation.original_text,
            recent_messages=[],
            relationship_state={},
            request_id=request_id,
            persona_snapshot=persona_snapshot,
        )
    rewritten = services.persona.enforce_nickname_grounding(
        interpretation.original_text,
        rewritten,
        reaction_key=request_id,
        persona_snapshot=persona_snapshot,
    )
    for key in (
        "_conversationToolResults",
        "_fileOutputs",
        "attachments",
        "files",
        "sources",
        "sourceBatch",
        "toolUsed",
        "toolStatus",
        "toolErrors",
        "toolResults",
        "actionReceipt",
        "actionReceipts",
        "todoContext",
        "webEvidenceStatus",
        "memoryAction",
        "memoryActionProvided",
        "memoryUpdates",
        "memoryUpdatesProvided",
        "_memoryCandidates",
        "_memoryUpdateTypes",
        "_fileOutputExpected",
    ):
        if key in result and (
            key not in rewritten or key == "_fileOutputs" and not rewritten.get(key)
        ):
            rewritten[key] = result[key]
    attachment_delivery.preserve_attachment_delivery(result, rewritten)
    rewritten["turnContractRewrittenByModel"] = True
    if persona_snapshot is not None:
        rewritten["personaContextSnapshotId"] = persona_snapshot.snapshot_id
    return rewritten
