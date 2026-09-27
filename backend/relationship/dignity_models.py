"""冷处理期间的独立分类与严重侮辱回复修复，不负责写关系状态。

只在原条件下调用模型，保持原有协议、超时、模型与思考开关；人格取自本轮快照。
"""
from __future__ import annotations
import json
from typing import Any, Callable
from agent.persona_context import PersonaContextSnapshot
from backend.common.values import clamp_int
from backend.chat.reply_fields import normalize_boundary_assessment
from backend.chat.parsing import parse_json_object
from backend.relationship.dignity_rules import DIGNITY_BOUNDARY_RULE, dignity_reply_contract_issues

class DignityModelService:
    def __init__(self, *, send: Callable[..., Any], normalize_reply: Callable[..., Any],
                 persona_prefix: Callable[..., Any], reply_rule: Callable[[], str]) -> None:
        self.send = send
        self.normalize_reply = normalize_reply
        self.persona_prefix = persona_prefix
        self.reply_rule = reply_rule

    def classify_dignity_message_with_model(
        self,
        settings: dict[str, Any],
        user_message: str,
        recent_messages: list[dict[str, Any]],
        *,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Classify boundary semantics; never infer a violation from a keyword."""

        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.0,
            "max_tokens": 180,
            "messages": [
                {"role": "system", "content": (
                    "你是对话边界分类器，只判断当前用户消息，不生成台词。"
                    "severe_personal_insult 仅指直接贬低 Amadeus 的人格、尊严、存在价值或以羞辱主体为目的的人身攻击；"
                    "普通争论、粗鲁抱怨、对回答能力的具体批评、熟人吐槽和带脏字但不贬低人格的冲突都算 ordinary_conflict。"
                    "sincere_apology 必须明确承担责任、表达停止伤害的意愿，且不能是讽刺、敷衍、条件式道歉或同时继续辱骂。"
                    "理解否定、引用、假设和上下文，任何单个词都不能决定结果。只输出 JSON："
                    "{\"kind\":\"none|ordinary_conflict|severe_personal_insult|sincere_apology\","
                    "\"severity\":0,\"confidence\":0.0,\"reason\":\"简短依据\"}。"
                )},
                *[
                    {
                        "role": "assistant" if item.get("role") == "assistant" else "user",
                        "content": str(item.get("content") or "")[:800],
                    }
                    for item in recent_messages[-4:]
                    if isinstance(item, dict) and str(item.get("content") or "").strip()
                ],
                {"role": "user", "content": str(user_message or "")[:2400]},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        content = self.send(
            settings,
            payload,
            timeout=25,
            request_id=request_id,
            purpose="dignity_boundary_classifier",
        )
        return normalize_boundary_assessment(parse_json_object(content))


    def repair_dignity_reply_with_model(
        self,
        settings: dict[str, Any],
        user_message: str,
        result: dict[str, Any],
        violation_count: int,
        *,
        request_id: str = "",
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> dict[str, Any]:
        issues = dignity_reply_contract_issues(result)
        if not issues:
            return result
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.45,
            "max_tokens": 420,
            "messages": [
                *self.persona_prefix(persona_snapshot, legacy_system_prompt=True),
                {"role": "system", "content": self.reply_rule()},
                {"role": "system", "content": DIGNITY_BOUNDARY_RULE},
                {"role": "system", "content": (
                    f"这是第 {clamp_int(violation_count, 1, 4, 1)} 次尚未锁定的严重人格侮辱。"
                    "重写为直接、符合角色的反驳：明确她虽是 AI 也有尊严，指出人格羞辱不可接受并发出停止警告。"
                    "次数越高可以越冷、越犀利，但不威胁现实伤害。保留自然中文和对应日语。"
                    "只修复这个边界合同，不讨论内部计数器。"
                )},
                {"role": "user", "content": json.dumps({
                    "currentUserMessage": user_message,
                    "previousModelReply": result.get("reply"),
                    "missingRequirements": issues,
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        content = self.send(
            settings,
            payload,
            timeout=35,
            request_id=request_id,
            purpose="dignity_reply_repair",
        )
        repaired = self.normalize_reply(content, settings=settings)
        if dignity_reply_contract_issues({
            **repaired,
            "boundaryAssessment": result.get("boundaryAssessment"),
        }):
            return result
        preserved = {
            key: result[key]
            for key in (
                "boundaryAssessment", "dignityViolationCount", "dignityLocked",
                "coldShoulder", "deliveryMode",
            )
            if key in result
        }
        return {**repaired, **preserved, "dignityReplyRepaired": True}
