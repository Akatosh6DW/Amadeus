"""上下文、工具与任务完成 grounding 的回复修订边界。

校验规则只消费候选回复和已解析的上下文；模型修订通过窄端口调用，
不执行工具、不写状态，也不反向导入 server。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable

from agent.persona_context import PersonaContextSnapshot
from backend.chat.parsing import parse_json_object
from backend.context.reference import SPORT_CONTEXT_PATTERN


def context_grounding_violation(value: Any, resolution: dict[str, Any]) -> bool:
    if not resolution.get("active"):
        return False
    parsed = parse_json_object(value) if isinstance(value, str) else value if isinstance(value, dict) else {}
    reply = str((parsed or {}).get("reply") or value or "").strip()
    if not reply:
        return True
    clarification = re.search(
        r"(?:你是指|你指的是|具体是|到底是).{0,12}(?:什么|哪个|哪场)|"
        r"(?:哪场|什么比赛|什么比分|什么对象|哪句话)[？?!！。]*$|"
        r"(?:逆转|翻盘|评价|查)(?:什么|哪一个)|"
        r"(?:说清楚|讲清楚|给我上下文|总得给我上下文)",
        reply,
    )
    # 普通对话允许在一段已经实质作答的内容末尾自然反问。此前只要长回复
    # 任意位置出现“你是指什么”之类短语，就会把整段回答判废并最终换成
    # 固定总结。只有短回复基本停留在澄清阶段时才触发普通上下文修复；
    # 体育事实链继续保持严格检查。
    if clarification and (
        resolution.get("kind") == "sports"
        or len(re.sub(r"\s+", "", reply)) <= 100
    ):
        return True
    if resolution.get("kind") == "sports":
        subject = str(resolution.get("subject") or "")
        subject_terms = {
            item.lower()
            for item in re.findall(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,8}", subject)
            if item not in {"最近讨论", "这场比赛", "最新比分", "赛后评价", "最新赛况"}
        }
        reply_lower = reply.lower()
        grounded = bool(SPORT_CONTEXT_PATTERN.search(reply)) or any(term in reply_lower for term in subject_terms)
        strong_grounding = bool(re.search(r"足球|篮球|世界杯|欧洲杯|比赛|比分|赛果|进球|球队|球员", reply)) or any(
            term in reply_lower for term in subject_terms
        )
        unrelated = bool(re.search(r"实验室|课题组|问题清单|传感器|论文|研究计划|长期记忆", reply))
        trusted_tool_evidence = resolution.get("trustedToolEvidence") or []
        stale_tool_denial = bool(trusted_tool_evidence) and bool(re.search(
            r"没(?:有)?联网|无法联网|没有实时联网|没有联网权限|不能(?:查|联网)|离线运行",
            reply,
        ))
        if not grounded or stale_tool_denial or (unrelated and not strong_grounding):
            return True
        if resolution.get("intent") == "赛后评价":
            has_analysis = bool(re.search(
                r"我的看法|我的评价|我认为|我觉得|在我看来|表现(?:得|上)|踢得|打得|"
                r"关键|优势|问题|防守|进攻|控球|机会|战术|节奏|发挥|胜负手|"
                r"出色|糟糕|被动|主动|实至名归|完胜|险胜",
                reply,
            ))
            honest_insufficiency = bool(re.search(
                r"尚未找到|没有找到|没找到|无法确认|不能确认|证据不足|资料不足|"
                r"只有.{0,8}(?:预告|赛程)|尚无赛后|不能据此评价",
                reply,
            ))
            if not has_analysis and not honest_insufficiency:
                return True
            if "individual_responsibility" in (resolution.get("analysisFocus") or []):
                addresses_responsibility = bool(re.search(
                    r"战犯|责任|最差|失误|红牌|送点|主教练|换人|不能.{0,10}(?:点名|归责)|"
                    r"无法.{0,10}(?:点名|归责)|不该.{0,10}(?:点名|归责)",
                    reply,
                ))
                if not addresses_responsibility and not honest_insufficiency:
                    return True
                if "overall_performance" in (resolution.get("analysisFocus") or []) and len(reply) < 90:
                    return True
            if "discipline_fair_play" in (resolution.get("analysisFocus") or []):
                evidence_status = (
                    resolution.get("webEvidenceStatus")
                    if isinstance(resolution.get("webEvidenceStatus"), dict)
                    else {}
                )
                addresses_discipline = bool(re.search(
                    r"踢得.{0,6}脏|打得.{0,6}脏|不算.{0,6}脏|不能.{0,10}(?:说|定性).{0,6}脏|"
                    r"犯规|黄牌|红牌|吃牌|粗野|恶意动作|危险动作|身体对抗|比赛纪律|公平竞赛|"
                    r"证据不足|资料不足",
                    reply,
                ))
                if not addresses_discipline and not honest_insufficiency:
                    return True
                if evidence_status and not evidence_status.get("disciplineFound"):
                    admits_missing_discipline_evidence = bool(re.search(
                        r"证据不足|资料不足|无法.{0,8}(?:判断|确认|定性)|不能.{0,8}(?:判断|确认|定性)|"
                        r"不足以.{0,10}(?:判断|说明|证明|定性)",
                        reply,
                    ))
                    claims_clean_play = bool(re.search(
                        r"(?:一场|比赛|逆转|表现).{0,10}(?:很|相当|比较)?干净|"
                        r"(?:阿根廷|对手).{0,10}(?:不脏|很干净|没有.{0,5}(?:恶意|粗野))|"
                        r"没有.{0,10}(?:恶意|粗野|危险)(?:犯规|动作)",
                        reply,
                    ))
                    if not admits_missing_discipline_evidence or claims_clean_play:
                        return True
    return False

def context_repair_needs_web_research(
    user_message: str,
    resolution: dict[str, Any],
    tool_results: list[dict[str, Any]],
) -> bool:
    """Do not manufacture research after a context-quality failure.

    QueryLoop may repair the answer or the main model may explicitly request a
    Web tool.  A backend regex is not allowed to turn a weak answer into a new
    network action.
    """

    return False

def task_completion_violation(value: Any, resolution: dict[str, Any]) -> bool:
    if not resolution.get("active") or resolution.get("requestedAction") != "choose_winner":
        return False
    parsed = parse_json_object(value) if isinstance(value, str) else value if isinstance(value, dict) else {}
    reply = str((parsed or {}).get("reply") or value or "").strip()
    if not reply:
        return True
    entities = [str(item).strip() for item in resolution.get("entities", []) if str(item).strip()]
    mentions_entity = any(entity.lower() in reply.lower() for entity in entities)
    makes_choice = bool(re.search(r"我(?:会)?(?:猜|选|押|看好)|更可能|胜算更高|更看好|倾向|能赢|会赢|获胜", reply))
    evades = bool(re.search(
        r"你先告诉我|什么比赛|哪场比赛|"
        r"D-?mail|电话微波炉|实验变量",
        reply,
        re.I,
    ))
    return not (mentions_entity and makes_choice) or evades

@dataclass(frozen=True)
class GroundingRepairPorts:
    """回复修订所需的模型、上下文提示和视觉反应窄端口。"""

    send_request: Callable[..., str]
    persona_repair_prefix: Callable[..., list[dict[str, str]]]
    contextual_reference_prompt: Callable[[dict[str, Any]], str]
    tool_context_prompt: Callable[[list[dict[str, Any]], dict[str, Any]], str]
    tool_grounding_instruction: Callable[[list[dict[str, Any]]], str]
    visual_relationship_reaction_instruction: Callable[[list[dict[str, Any]], dict[str, Any]], str]
    default_relationship_state: Callable[[], dict[str, Any]]
    web_answer_diagnostics: Callable[[Any, list[dict[str, Any]]], list[str]]
    utterance_stream_reply_rule: str


class GroundingRepairService:
    """Perform one bounded model rewrite for an already-detected contract failure."""

    def __init__(self, ports: GroundingRepairPorts) -> None:
        self.ports = ports

    def repair_context_grounded_reply(
        self,
        settings: dict[str, Any],
        user_message: str,
        bad_output: str,
        resolution: dict[str, Any],
        tool_results: list[dict[str, Any]],
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.35,
            "max_tokens": 800,
            "messages": [
                *self.ports.persona_repair_prefix(persona_snapshot, legacy_system_prompt=True),
                {"role": "system", "content": self.ports.utterance_stream_reply_rule},
                {"role": "system", "content": self.ports.contextual_reference_prompt(resolution)},
                *([{"role": "system", "content": self.ports.tool_context_prompt(tool_results, resolution)}] if tool_results else []),
                *([{"role": "system", "content": self.ports.tool_grounding_instruction(tool_results)}]
                  if self.ports.tool_grounding_instruction(tool_results) else []),
                {"role": "system", "content": (
                    "上一份回答没有承接最近用户原文，或错误切换到了旧话题。重新输出最终 JSON。"
                    "下面的 resolvedContext 是后端根据最近对话解析出的本轮上下文，不要重新猜指代。"
                    "直接回答 resolvedQuestion 并逐项满足 constraints；不要反问对象是什么，"
                    "不要为上一份错误回答辩护。若提供了联网或工具证据，必须以证据为准。"
                )},
                {"role": "user", "content": json.dumps({
                    "currentUserMessage": user_message,
                    "failureReason": "context_or_task_contract_not_satisfied",
                    "resolvedContext": {
                        "kind": resolution.get("kind"),
                        "subject": resolution.get("subject"),
                        "requestedAction": resolution.get("requestedAction"),
                        "resolvedQuestion": resolution.get("resolvedQuestion"),
                        "constraints": resolution.get("constraints"),
                        "recentUserEvidence": resolution.get("evidence"),
                        "trustedToolEvidence": resolution.get("trustedToolEvidence"),
                    },
                    "invalidReply": bad_output,
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        return self.ports.send_request(settings, payload, timeout=60)

    def repair_task_completion_reply(
        self,
        settings: dict[str, Any],
        user_message: str,
        bad_output: Any,
        resolution: dict[str, Any],
        tool_results: list[dict[str, Any]],
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.38,
            "max_tokens": 700,
            "messages": [
                *self.ports.persona_repair_prefix(persona_snapshot, legacy_system_prompt=True),
                {"role": "system", "content": self.ports.utterance_stream_reply_rule},
                {"role": "system", "content": self.ports.contextual_reference_prompt(resolution)},
                *([{"role": "system", "content": self.ports.tool_context_prompt(tool_results, resolution)}] if tool_results else []),
                {"role": "system", "content": (
                    "上一份回答没有完成用户要求的明确选择。重新输出最终 JSON。"
                    "reply 第一句必须在候选对象中明确选一个或明确倾向一方，然后简要说明依据；"
                    "可以声明预测有不确定性，但不得拒绝、反问、改谈实验或用角色梗代替结论。"
                )},
                {"role": "user", "content": json.dumps({
                    "currentUserMessage": user_message,
                    "task": {
                        "subject": resolution.get("subject"),
                        "requestedAction": resolution.get("requestedAction"),
                        "entities": resolution.get("entities"),
                        "constraints": resolution.get("constraints"),
                    },
                    "invalidReply": bad_output,
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        return self.ports.send_request(settings, payload, timeout=60)

    def repair_tool_grounded_reply(
        self,
        settings: dict[str, Any],
        user_message: str,
        bad_output: str,
        results: list[dict[str, Any]],
        resolution: dict[str, Any] | None = None,
        relationship_state: dict[str, Any] | None = None,
        persona_snapshot: PersonaContextSnapshot | None = None,
    ) -> str:
        resolution = resolution or {}
        relationship_state = relationship_state or self.ports.default_relationship_state()
        research_diagnostics = self.ports.web_answer_diagnostics(bad_output, results)
        has_web = any(
            item.get("ok") and item.get("name") in {"web_search", "read_web_page"}
            for item in results
        )
        repair_instruction = (
            "上一份回答没有形成合格的联网研究结论。不要原样重问，也不要丢掉上一份回答里仍然有效的推理。"
            "结合当前问题、上一份回答和来源正文，重新输出最终 JSON：第一段直接回答用户，之后综合证据并在对应事实后标注来源。"
            "不要汇报搜索是否成功，不要展示搜索结果清单，不要拿来源标题或摘要代替自己的分析。"
            "如果证据只覆盖部分主张，只限定未覆盖的部分，其余问题仍要正常回答。"
            if has_web else
            "上一份回答违背了已经成功执行的工具结果。重新输出最终 JSON，直接自然描述观测内容；"
            "不要解释工具调用过程，不要重复错误回答，也不要质疑工具是否存在。"
            "若这是评价问题，必须逐项完成用户的问题，给出观点、具体证据和必要的不确定性。"
        )
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.35,
            "max_tokens": 700,
            "messages": [
                *self.ports.persona_repair_prefix(persona_snapshot, legacy_system_prompt=True),
                {"role": "system", "content": self.ports.utterance_stream_reply_rule},
                {"role": "system", "content": self.ports.tool_context_prompt(results, resolution)},
                {"role": "system", "content": self.ports.tool_grounding_instruction(results)},
                *([{"role": "system", "content": self.ports.visual_relationship_reaction_instruction(results, relationship_state)}]
                  if self.ports.visual_relationship_reaction_instruction(results, relationship_state) else []),
                *([{"role": "system", "content": self.ports.contextual_reference_prompt(resolution)}]
                  if self.ports.contextual_reference_prompt(resolution) else []),
                {"role": "system", "content": repair_instruction},
                {"role": "user", "content": json.dumps({
                    "userMessage": user_message,
                    "resolvedQuestion": resolution.get("resolvedQuestion"),
                    "resolvedContext": {
                        "subject": resolution.get("subject"),
                        "requestedAction": resolution.get("requestedAction"),
                        "constraints": resolution.get("constraints"),
                    },
                    "validationFailures": research_diagnostics,
                    "invalidReply": bad_output,
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        return self.ports.send_request(settings, payload, timeout=60)
