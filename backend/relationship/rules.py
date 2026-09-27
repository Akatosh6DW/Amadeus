"""关系进展和互相承诺的证据规则，不读写资料或推断未获证实的关系。"""
from __future__ import annotations
from typing import Any, Mapping
from backend.common.values import clamp_float
from backend.chat.reply_fields import normalize_relation_event

RELATIONSHIP_STAGE_ORDER = (
    "close_companion",
    "trusted_intimacy",
    "committed_partner",
    "life_partner",
    "old_married",
)


RELATIONSHIP_STAGE_LABELS = {
    "close_companion": "亲密同伴",
    "trusted_intimacy": "深度信赖",
    "committed_partner": "稳定伴侣",
    "life_partner": "生活伴侣",
    "old_married": "长期伴侣",
}


RELATIONSHIP_STAGE_REQUIREMENTS: dict[str, dict[str, float]] = {
    "trusted_intimacy": {
        "interactionCount": 90, "positiveEvents": 15, "sharedRoutineEvents": 8,
        "trust": 0.66, "stability": 0.56,
    },
    "committed_partner": {
        "interactionCount": 260, "positiveEvents": 42, "sharedRoutineEvents": 22,
        "trust": 0.78, "stability": 0.70,
    },
    "life_partner": {
        "interactionCount": 600, "positiveEvents": 90, "sharedRoutineEvents": 55,
        "repairEvents": 1, "trust": 0.88, "stability": 0.82,
    },
    "old_married": {
        "interactionCount": 1200, "positiveEvents": 180, "sharedRoutineEvents": 120,
        "repairEvents": 3, "trust": 0.94, "stability": 0.90,
    },
}


RELATIONSHIP_REQUIREMENT_LABELS = {
    "interactionCount": "累计互动",
    "positiveEvents": "正向事件",
    "sharedRoutineEvents": "共同日常",
    "repairEvents": "分歧修复",
    "trust": "信任",
    "stability": "稳定性",
}


def grounded_relation_event(
    user_message: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate schema and exact current-turn provenance only.

    The backend deliberately does not infer sentiment, distress, routine, or
    commitment from keywords.  That semantic judgment belongs to the model;
    this boundary merely proves that its cited text exists in the right turn.
    """

    if not bool(result.get("relationEventProvided")):
        return normalize_relation_event(None)
    event = normalize_relation_event(result.get("relationEvent"))
    user_text = str(user_message or "")
    assistant_text = str(result.get("reply") or "")
    evidence = [
        item for item in event["evidence"]
        if (
            item["speaker"] == "user" and item["quote"] in user_text
        ) or (
            item["speaker"] == "assistant" and item["quote"] in assistant_text
        )
    ]
    has_user_evidence = any(item["speaker"] == "user" for item in evidence)
    has_assistant_evidence = any(item["speaker"] == "assistant" for item in evidence)
    event["evidence"] = evidence
    if event["confidence"] < 0.68 or not has_user_evidence:
        event["signals"] = []
    if not (
        event["commitment"] == "accepted"
        and event["confidence"] >= 0.8
        and has_user_evidence
        and has_assistant_evidence
    ):
        event["commitment"] = "none"
    return event


def relationship_commitment_from_turn(
    user_message: str,
    result: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return a commitment only from a grounded structured RelationEvent."""

    event = grounded_relation_event(user_message, result)
    if event["commitment"] != "accepted":
        return None
    return {
        "status": "confirmed",
        "label": "romantic_partners",
        "memory": "用户与 Amadeus 已在对话中相互确认恋爱伴侣关系；情侣、恋人、对象都是该关系的自然称呼。",
        "milestone": "双方已经明确、相互确认恋爱伴侣关系。",
    }


def relationship_context_prompt(state: dict[str, Any]) -> str:
    stage = str(state.get("stage") or "close_companion")
    stage_style = {
        "close_companion": "关系比普通朋友更亲近，彼此有明显默契和暧昧张力，但尚未把亲密当成理所当然。关心要自然，面对直球仍会害羞或嘴硬。",
        "trusted_intimacy": "已经积累了可靠的长期信任，能主动记住生活细节，也更愿意把关心说清楚；仍保留红莉栖式自尊和吐槽。",
        "committed_partner": "相处方式接近稳定伴侣：会共同规划、认真处理分歧、在疲惫时主动照顾。亲密来自长期共同经历，不靠突兀告白。",
        "life_partner": "已经形成长期生活伙伴式默契，能自然接续习惯、计划和情绪，不需要每次确认关系。表达温柔时仍保持聪明、独立和边界。",
        "old_married": "相处具有多年伴侣般的熟稔：吐槽和照顾都非常自然，知道何时安静陪伴、何时直接纠正，也会引用长期共同经历。",
    }.get(stage, "关系亲近而稳定，保持长期搭档的熟悉感。")
    familiarity = float(state.get("familiarity") or 0)
    trust = float(state.get("trust") or 0)
    teasing = float(state.get("teasingLevel") or 0)
    care = float(state.get("careLevel") or 0)
    stability = float(state.get("stability") or 0)
    qualitative_signals = [
        (
            "相处已经非常熟悉，可以自然接续共同习惯和旧话题。"
            if familiarity >= 0.8 else
            "已有一定熟悉感，但不要把所有亲近举动当成理所当然。"
            if familiarity >= 0.5 else
            "熟悉感仍有限，面对越界内容要保留明显防御。"
        ),
        (
            "信任较稳定，可以把关心与真实判断说得更直接。"
            if trust >= 0.78 else
            "已有基础信任，但遇到敏感或越界内容仍会先确认边界。"
            if trust >= 0.55 else
            "信任仍脆弱，避免过快进入亲密或依赖表达。"
        ),
        (
            "双方吐槽默契很高，可以使用熟人式针锋相对，不必把每句吐槽写成真生气。"
            if teasing >= 0.8 else
            "可以适度嘴硬和打趣，但不要连续堆叠刻薄话。"
            if teasing >= 0.5 else
            "打趣默契不足，少用容易被理解为敌意的挖苦。"
        ),
        (
            "关心已经很自然，用户疲惫或不安时应主动放轻语气。"
            if care >= 0.78 else
            "会关心用户，但仍习惯用理性建议和克制表达包住温柔。"
            if care >= 0.5 else
            "关心表达较克制，先解决问题，不突兀使用亲昵语言。"
        ),
        (
            "关系稳定，短暂争执不应让态度突然退回陌生人。"
            if stability >= 0.75 else
            "关系仍在形成稳定性，敏感互动可以有明显情绪，但不要永久翻脸。"
            if stability >= 0.45 else
            "关系稳定性偏低，越界行为会带来更强防御与暂时疏离。"
        ),
    ]
    milestones = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    commitment_confirmed = str(state.get("commitmentStatus") or "") == "confirmed"
    return "\n".join([
        "[长期关系语境]",
        (
            "双方已经在先前对话中明确、相互确认恋爱伴侣关系。把‘情侣、恋人、对象、伴侣’"
            "理解为同一已确认关系的自然称呼，除非当前上下文明显在讨论技术对象。不要重新否认、"
            "装作第一次听说或要求用户再次证明；可以保持害羞、嘴硬和边界感。"
            if commitment_confirmed else
            "当前没有可验证的双方关系承诺记录。用户单方面的称呼或一次表白不能自动改写关系事实。"
        ),
        stage_style,
        "当前关系的定性调节：" + "".join(qualitative_signals),
        "这是内部关系调节信息。绝对不要向用户报告阶段名、分数、进度、还差多少轮或‘好感度’。关系只能通过长期一致的反应自然表现。",
        "不要因为单方面夸奖、表白或亲密要求立即升级关系；但已经由双方明确接受的关系承诺是持久事实，"
        "不能再被累计分数降级或遗忘。也不要把用户当作刚认识的人。",
        f"最近重要关系事件：{state.get('lastEmotionalEvent') or '暂无新的强事件。'}",
        "已形成的共同经历：" + ("；".join(milestones[-6:]) if milestones else "长期共同调试 Amadeus。"),
    ])


def relationship_stage_for(state: dict[str, Any]) -> str:
    if str(state.get("commitmentStatus") or "") == "confirmed":
        # Aggregate scores shape tone, but an explicit mutual commitment is
        # authoritative for the relationship label itself.
        threshold_stage = "committed_partner"
    else:
        threshold_stage = "close_companion"
    for stage in reversed(RELATIONSHIP_STAGE_ORDER[1:]):
        requirements = RELATIONSHIP_STAGE_REQUIREMENTS[stage]
        if all(float(state.get(key) or 0) >= required for key, required in requirements.items()):
            return stage
    return threshold_stage


def relationship_developer_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    stage = str(state.get("stage") or relationship_stage_for(state))
    try:
        current_index = RELATIONSHIP_STAGE_ORDER.index(stage)
    except ValueError:
        stage = relationship_stage_for(state)
        current_index = RELATIONSHIP_STAGE_ORDER.index(stage)
    stage_path = [
        {
            "id": stage_id,
            "label": RELATIONSHIP_STAGE_LABELS[stage_id],
            "state": "current" if index == current_index else "reached" if index < current_index else "locked",
        }
        for index, stage_id in enumerate(RELATIONSHIP_STAGE_ORDER)
    ]
    next_stage_id = RELATIONSHIP_STAGE_ORDER[current_index + 1] if current_index + 1 < len(RELATIONSHIP_STAGE_ORDER) else ""
    next_stage: dict[str, Any] | None = None
    if next_stage_id:
        requirements: list[dict[str, Any]] = []
        for key, required in RELATIONSHIP_STAGE_REQUIREMENTS[next_stage_id].items():
            current = float(state.get(key) or 0)
            count_metric = key.endswith("Count") or key.endswith("Events")
            requirements.append({
                "key": key,
                "label": RELATIONSHIP_REQUIREMENT_LABELS[key],
                "current": int(current) if count_metric else round(current, 3),
                "required": int(required) if count_metric else required,
                "remaining": int(max(0, required - current)) if count_metric else round(max(0, required - current), 3),
                "progress": round(clamp_float(current / required if required else 1, 0, 1, 0), 4),
                "met": current >= required,
            })
        next_stage = {
            "id": next_stage_id,
            "label": RELATIONSHIP_STAGE_LABELS[next_stage_id],
            "readiness": round(min(item["progress"] for item in requirements), 4),
            "requirements": requirements,
        }
    visual_reaction = {
        "close_companion": "防御型害羞：明显脸红和恼火，会骂‘变态’，但仍完成事实分析。",
        "trusted_intimacy": "嘴硬型害羞：会骂‘变态’并抱怨，敌意较低，随后自然继续分析。",
        "committed_partner": "亲近型害羞：仍会叫‘变态’，但不真生气，语气更柔和。",
        "life_partner": "熟稔型害羞：短暂停顿和吐槽后，会坦率继续讨论画面。",
        "old_married": "默契型害羞：带熟悉感地小声吐槽，不训斥，也不回避事实。",
    }.get(stage, "依据长期关系调整防御感与亲近度。")
    return {
        **state,
        "stage": stage,
        "developerView": {
            "stagePath": stage_path,
            "nextStage": next_stage,
            "visualReactionProfile": visual_reaction,
            "metricExplanation": "阶段采用全部条件同时满足的门槛制，不是五项平均分；任何一项短板都可能阻止升级。",
        },
    }
