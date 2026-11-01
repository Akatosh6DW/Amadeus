"""上下文、工具与任务完成 grounding 的诊断规则。

校验规则只消费候选回复和已解析的上下文；不生成固定回复、不执行工具、
不写状态，也不反向导入 server。
"""
from __future__ import annotations

import re
from typing import Any
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
