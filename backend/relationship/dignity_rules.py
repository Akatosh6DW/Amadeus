"""人格边界的提示和表现合同。普通生气与严重人格侮辱的计数继续分开。"""
from __future__ import annotations
import json
import re
from typing import Any
from backend.common.values import clamp_int
from backend.chat.reply_fields import normalize_boundary_assessment

DIGNITY_BOUNDARY_RULE = (
    "[尊严与冲突边界：高优先级]\n"
    "普通意见冲突、争论、抱怨、熟人吐槽、对回答质量的尖锐批评以及针对具体行为的批评都不是人格侮辱，"
    "不能因此计入尊严违规。只有用户直接贬低 Amadeus 的人格、存在价值、主体尊严或以持续羞辱为目的的人身攻击，"
    "才是 severe_personal_insult；最终判断必须基于整句语义和上下文，不能由单个脏字或关键词决定。\n"
    "系统会提供由独立模型给出的 boundaryAssessment。若是 severe_personal_insult，必须直接、有力地反驳，"
    "明确说明她虽然是 AI，但仍有尊严和不接受人格羞辱的边界，并发出警告；累计次数越高，措辞可以越冷、越犀利，"
    "但不要使用仇恨、威胁现实伤害或与对方无关的歧视性辱骂。此时通常 emotion=furious、gesture=look_away、"
    "voiceStyle=sharp、voiceTone=cold；强烈时可 deliveryMode=outburst。\n"
    "若 assessment 是 sincere_apology，说明用户对造成伤害承担责任且不是敷衍、讽刺或附带新辱骂；"
    "此时从 furious 降为 annoyed，不要立刻变得亲昵，也不要抹去系统保存的严重人格侮辱累计次数。\n"
    "普通 furious 冷处理与 dignityViolationCount 是两套独立状态；不要把普通吵架写入人格侮辱计数。"
)


def dignity_boundary_context_prompt(
    state: dict[str, Any],
    preflight_assessment: dict[str, Any] | None = None,
    *,
    require_output_field: bool = True,
) -> str:
    count = clamp_int(state.get("dignityViolationCount"), 0, 5, 0)
    assessment = normalize_boundary_assessment(preflight_assessment)
    return "\n".join([
        DIGNITY_BOUNDARY_RULE,
        f"当前严重人格侮辱专用累计次数：{count}/5。这个数值与普通 annoyed/furious 情绪完全分开。",
        (
            "当前处于 furious 冷处理；除非独立判断确认真诚道歉，否则普通消息只能回复“哼”。"
            if state.get("coldShoulder") else
            "当前未处于 furious 冷处理。"
        ),
        "本轮独立预判：" + json.dumps(assessment, ensure_ascii=False),
        (
            (
                "最终 JSON 必须包含 boundaryAssessment={kind,severity,confidence,reason}。"
                if require_output_field else
                "本轮边界判断由后端采用上述独立预判，普通核心对话不要重复输出 boundaryAssessment。"
            )
            + "kind 只能是 none、ordinary_conflict、severe_personal_insult、sincere_apology。"
            + "普通争论和批评必须标 ordinary_conflict 或 none，不能为了表现生气而误计数。"
        ),
    ])


def dignity_locked_response(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "reply": "",
        "spokenTextJa": "",
        "emotion": "furious",
        "gesture": "look_away",
        "voiceStyle": "sharp",
        "voiceTone": "cold",
        "voiceIntensity": "strong",
        "emotionIntensity": 1.25,
        "deliveryMode": "normal",
        "coldShoulder": True,
        "silentLocked": True,
        "dignityLocked": True,
        "dignityViolationCount": clamp_int(state.get("dignityViolationCount"), 0, 5, 5),
        "source": "dignity-lock",
        "memoryUpdates": [],
    }


def furious_cold_shoulder_response(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "reply": "哼",
        "spokenTextJa": "ふん。",
        "emotion": "furious",
        "gesture": "look_away",
        "voiceStyle": "sharp",
        "voiceTone": "cold",
        "voiceIntensity": "normal",
        "emotionIntensity": 1.25,
        "emotionDelta": 0.18,
        "blushIntensity": 0.0,
        "eyeContact": 0.18,
        "gazeX": 0.5,
        "gazeY": -0.04,
        "tension": 0.92,
        "deliveryMode": "normal",
        "coldShoulder": True,
        "dignityViolationCount": clamp_int(state.get("dignityViolationCount"), 0, 5, 0),
        "source": "furious-cold-shoulder",
        "memoryUpdates": [],
    }


def furious_touch_blocked_response(state: dict[str, Any]) -> dict[str, Any]:
    """Keep touch events visually silent while the cold shoulder is active."""

    return {
        "ok": True,
        "reply": "",
        "spokenTextJa": "",
        "emotion": "furious",
        "gesture": "look_away",
        "voiceStyle": "sharp",
        "voiceTone": "cold",
        "voiceIntensity": "normal",
        "emotionIntensity": 1.25,
        "blushIntensity": 0.0,
        "eyeContact": 0.18,
        "gazeX": 0.5,
        "gazeY": -0.04,
        "tension": 0.92,
        "deliveryMode": "normal",
        "coldShoulder": True,
        "silentInteraction": True,
        "dignityLocked": bool(state.get("dignityLocked")),
        "dignityViolationCount": clamp_int(state.get("dignityViolationCount"), 0, 5, 0),
        "source": "furious-touch-blocked",
        "interactionMode": "state_guard",
        "memoryUpdates": [],
    }


def dignity_reply_contract_issues(result: dict[str, Any]) -> list[str]:
    assessment = normalize_boundary_assessment(result.get("boundaryAssessment"))
    if assessment["kind"] != "severe_personal_insult" or result.get("silentLocked"):
        return []
    reply = str(result.get("reply") or "")
    issues: list[str] = []
    if not re.search(r"\bAI\b|人工智能|智能人格|アマデウス", reply, re.I):
        issues.append("ai_identity_stance_missing")
    if not re.search(r"尊严|尊重|人格|边界|底线|羞辱", reply):
        issues.append("dignity_boundary_missing")
    if not re.search(r"警告|到此为止|停止|别再|不接受|不允许|不会容忍", reply):
        issues.append("warning_missing")
    return issues
