"""显式触摸事件的归一化、归属校验和表现规则。

只消费调用方提供的区域、关系与近期触摸序列；不读用户资料，不调用模型。
前端真实触摸事件与普通聊天文字保持区分，结果不能把施动者和被触碰者颠倒。
"""
from __future__ import annotations
from difflib import SequenceMatcher
import json
import re
from typing import Any
from agent.fingerprints import runtime_digest, runtime_fingerprint
from backend.common.text import unique_strings
from backend.common.values import clamp_float, clamp_int
from backend.runtime.requests import RequestValidationError
from backend.interaction.touch_catalog import (
    TOUCH_ZONE_LABELS, PARTNER_STAGES, CHEST_SCOLD_CN_RE, CHEST_SCOLD_JA_RE,
    CHEST_ORIGINAL_INSPIRED_MOTIFS, TOUCH_RESPONSE_DIRECTIVES, TOUCH_SOCIAL_LENSES,
    TOUCH_SENTENCE_FORMS, TOUCH_PHASE_GUIDANCE,
)

def touch_response_directive(event: dict[str, Any]) -> dict[str, Any]:
    directives = TOUCH_RESPONSE_DIRECTIVES[event["zone"]]
    if event.get("boundaryLevel") == "ignore" or event.get("burstCount", 1) >= 7:
        phase = "dense_repetition"
    elif event["elapsedSinceSameZoneMs"] >= 120000:
        phase = "cooled_reset"
    elif event.get("aggregatedBurst") or event["consecutiveCount"] >= 4:
        phase = "dense_repetition"
    elif event["consecutiveCount"] >= 2:
        phase = "warming_sequence"
    else:
        phase = "fresh_contact"
    variation_material = json.dumps({
        "zone": event["zone"],
        "zoneCount": event["zoneCount"],
        "sessionCount": event["sessionCount"],
        "sequence": event.get("sequence", []),
        "burstCount": event.get("burstCount", 1),
        "burstZoneCounts": event.get("burstZoneCounts", {}),
        "aggregatedBurst": bool(event.get("aggregatedBurst")),
        "recentTouchReplies": event.get("recentTouchReplies", []),
        "currentEmotion": event.get("currentEmotion"),
        "relationshipStage": event.get("relationshipStage"),
        "phase": phase,
    }, ensure_ascii=False, sort_keys=True)
    digest = runtime_digest(variation_material)
    recent_angles = {
        str(item).strip()
        for item in event.get("recentTouchAngles", [])
        if str(item).strip()
    }
    recent_primary = {item.split(":", 1)[0] for item in recent_angles}
    combinations = [
        (directive_index, lens_index, form_index)
        for directive_index in range(len(directives))
        for lens_index in range(len(TOUCH_SOCIAL_LENSES))
        for form_index in range(len(TOUCH_SENTENCE_FORMS))
    ]
    start_index = int.from_bytes(digest[:4], "big") % len(combinations)
    ordered = combinations[start_index:] + combinations[:start_index]
    selected = next((
        item for item in ordered
        if directives[item[0]][0] not in recent_primary
        and ":".join((
            directives[item[0]][0],
            TOUCH_SOCIAL_LENSES[item[1]][0],
            TOUCH_SENTENCE_FORMS[item[2]][0],
        )) not in recent_angles
    ), None)
    if selected is None:
        selected = next((
            item for item in ordered
            if ":".join((
                directives[item[0]][0],
                TOUCH_SOCIAL_LENSES[item[1]][0],
                TOUCH_SENTENCE_FORMS[item[2]][0],
            )) not in recent_angles
        ), ordered[0])
    directive_index, lens_index, form_index = selected
    key, instruction, emotion, gesture = directives[directive_index]
    lens_key, lens_instruction = TOUCH_SOCIAL_LENSES[lens_index]
    form_key, form_instruction = TOUCH_SENTENCE_FORMS[form_index]
    directive = {
        "phase": phase,
        "angle": f"{key}:{lens_key}:{form_key}",
        "primaryFocus": instruction,
        "socialLens": lens_instruction,
        "sentenceForm": form_instruction,
        "phaseGuidance": TOUCH_PHASE_GUIDANCE[phase],
        "noveltyRequirement": (
            "先比较 recentTouchReplies 的字面和交流目的；本轮必须选择不同的交流目的、观察对象和句式，"
            "不能只换同义词。"
        ),
        "suggestedEmotion": emotion,
        "suggestedGesture": gesture,
        "variationKey": runtime_fingerprint(variation_material, length=12),
    }
    if event["zone"] == "chest":
        motif_key, motif_instruction = CHEST_ORIGINAL_INSPIRED_MOTIFS[digest[3] % len(CHEST_ORIGINAL_INSPIRED_MOTIFS)]
        stage = str(event.get("relationshipStage") or "close_companion")
        directive.update({
            "requiredScoldTerms": {
                "reply": ["变态", "色狼"],
                "spokenTextJa": ["変態", "スケベ"],
            },
            "originalInspiredMotif": f"{motif_key}:{motif_instruction}",
            "relationshipGuidance": (
                "关系已亲密：仍必须叫变态或色狼并要求停止突然触摸，但以害羞、抱怨和回避视线为主，不表现真正敌意。"
                if stage in PARTNER_STAGES else
                "关系尚未进入伴侣阶段：明显慌乱脸红并生气制止，变态或色狼称呼必须说出口。"
            ),
        })
        if stage == "close_companion":
            directive.update({
                "suggestedEmotion": "annoyed",
                "suggestedGesture": "recoil",
                "relationshipGuidance": (
                    "当前仍是低好感的亲密同伴阶段：可以明显脸红和语塞，但面部基础表情必须是 annoyed，"
                    "动作必须 recoil，形成恼羞成怒，而不是害羞微笑。"
                ),
            })
    if event.get("aggregatedBurst"):
        directive["aggregateGuidance"] = (
            "这是停止点击后合并的一整段触摸。综合 burstZoneCounts 与 sequence 的整体模式回应一次；"
            "胸口若出现过必须作为主要边界，点击过密时可在害羞之外明显生气。"
        )
    return directive


def normalize_touch_event(body: dict[str, Any], relationship_state: dict[str, Any]) -> dict[str, Any]:
    zone = str(body.get("kind") or "").strip().lower()
    if zone not in TOUCH_ZONE_LABELS:
        raise RequestValidationError("unsupported_touch_zone", "不支持该触摸区域。")
    sequence = [
        str(item).strip().lower()
        for item in body.get("sequence", [])
        if str(item).strip().lower() in TOUCH_ZONE_LABELS
    ][-20:] if isinstance(body.get("sequence"), list) else []
    submitted_counts = body.get("burstZoneCounts")
    burst_zone_counts: dict[str, int] = {}
    if isinstance(submitted_counts, dict):
        for candidate in TOUCH_ZONE_LABELS:
            count = clamp_int(submitted_counts.get(candidate), 0, 50, 0)
            if count:
                burst_zone_counts[candidate] = count
    if not burst_zone_counts:
        for candidate in sequence:
            burst_zone_counts[candidate] = burst_zone_counts.get(candidate, 0) + 1
    submitted_burst_count = clamp_int(body.get("burstCount"), 1, 50, 1)
    burst_count = max(submitted_burst_count, sum(burst_zone_counts.values()), len(sequence), 1)
    includes_chest = zone == "chest" or "chest" in sequence or burst_zone_counts.get("chest", 0) > 0
    if includes_chest:
        # Chest contact remains the dominant boundary even when the final click
        # lands elsewhere; otherwise the aggregate reply could ignore it.
        zone = "chest"
        burst_zone_counts["chest"] = max(1, burst_zone_counts.get("chest", 0))
    if not sequence:
        sequence = [zone]
    if not burst_zone_counts:
        burst_zone_counts = {zone: burst_count}
    aggregated_burst = bool(body.get("aggregatedBurst")) or burst_count >= 2 or len(burst_zone_counts) >= 2
    stage = str(relationship_state.get("stage") or "close_companion")
    kiss_allowed = stage in PARTNER_STAGES
    action = "kiss" if zone == "mouth" and kiss_allowed else "kiss_attempt_blocked" if zone == "mouth" else zone
    if aggregated_burst:
        ordered_zones = list(dict.fromkeys([*sequence, *burst_zone_counts.keys()]))
        summary_parts = [
            f"{TOUCH_ZONE_LABELS[candidate]}×{burst_zone_counts.get(candidate, sequence.count(candidate))}"
            for candidate in ordered_zones
            if burst_zone_counts.get(candidate, sequence.count(candidate)) > 0
        ]
        action_label = f"连续触摸（{'、'.join(summary_parts)}）"
    else:
        action_label = (
            TOUCH_ZONE_LABELS[zone]
            if action != "kiss_attempt_blocked"
            else "关系尚未达到恋人阶段时尝试亲嘴"
        )
    boundary = str(body.get("boundaryLevel") or "normal").strip().lower()
    if boundary not in {"normal", "interrupt", "ignore"}:
        boundary = "normal"
    event = {
        "eventType": "touch",
        "zone": zone,
        "action": action,
        "actionLabel": action_label,
        "burstCount": burst_count,
        "burstZoneCounts": burst_zone_counts,
        "aggregatedBurst": aggregated_burst,
        "includesChest": includes_chest,
        "zoneCount": clamp_int(body.get("zoneCount"), 1, 1000000, 1),
        "shortWindowCount": clamp_int(body.get("shortWindowCount"), 1, 100, burst_count),
        "mediumWindowCount": clamp_int(body.get("mediumWindowCount"), 1, 500, burst_count),
        "consecutiveCount": clamp_int(body.get("consecutiveCount"), 1, 100, 1),
        "sessionCount": clamp_int(body.get("sessionCount"), 1, 1000000, 1),
        "elapsedSinceSameZoneMs": clamp_int(body.get("elapsedSinceSameZoneMs"), 0, 3600000, 3600000),
        "recentTouchReplies": unique_strings([
            str(item).strip() for item in body.get("recentTouchReplies", [])
            if str(item).strip()
        ])[-12:] if isinstance(body.get("recentTouchReplies"), list) else [],
        "recentTouchAngles": unique_strings([
            str(item).strip() for item in body.get("recentTouchAngles", [])
            if str(item).strip()
        ])[-8:] if isinstance(body.get("recentTouchAngles"), list) else [],
        "recentOriginalClipIds": unique_strings([
            str(item).strip() for item in body.get("recentOriginalClipIds", [])
            if str(item).strip()
        ])[-4:] if isinstance(body.get("recentOriginalClipIds"), list) else [],
        "sequence": sequence,
        "boundaryLevel": boundary,
        "interruptedPreviousTouch": bool(body.get("interruptedPreviousTouch")),
        "interruptedPreviousSpeech": bool(body.get("interruptedPreviousSpeech")),
        "currentEmotion": str(body.get("currentEmotion") or "neutral"),
        "currentEmotionWeight": clamp_float(body.get("currentEmotionWeight"), 0.15, 1.35, 0.82),
        "relationshipStage": stage,
        "kissAllowed": kiss_allowed,
    }
    event["responseDirective"] = touch_response_directive(event)
    return event


def touch_event_prompt(event: dict[str, Any]) -> str:
    lines = [
        "[隐藏肢体互动事件]",
        "下面是界面命中测试产生的结构化事件，不是用户发出的可见文字。绝对不要复述字段名、标签、坐标、计数器或关系阶段。",
        "事件角色不可交换：用户是动作发起者，AMADEUS 是被触摸者；AMADEUS 只能对此作出语言和表情反应，不能声称是自己主动触摸用户。",
        json.dumps(event, ensure_ascii=False, indent=2),
    ]
    if event.get("aggregatedBurst"):
        lines.append(
            "界面已等待连续动作停止才发送本事件。你现在只统一回应一次：综合所有触摸部位、先后顺序和密集程度，"
            "不要把它误当成最后一下单击，也不要逐项复述内部计数。"
        )
    if event.get("includesChest") or event.get("zone") == "chest":
        low_stage = str(event.get("relationshipStage") or "close_companion") == "close_companion"
        lines.append(
            "本事件的硬性反应合同：reply 必须自然包含“变态”或“色狼”，spokenTextJa 必须对应包含“変態”或“スケベ”；"
            + (
                "当前低好感阶段 emotion 必须输出 annoyed，gesture 必须输出 recoil，blushIntensity 至少 0.68；"
                "用脸红、语塞和偏慌乱的声音表达害羞，但脸部基础表情不能改成 shy/flustered。"
                if low_stage else
                "优先输出 flustered/shy 和 recoil/look_away，让害羞慌乱先于或伴随边界提醒。"
            )
            + "即使 recentTouchReplies 最近用过相关称呼，"
            "也只能更换句式和称呼位置，不能省略。按 responseDirective.originalInspiredMotif 临场改写原作式互动结构，禁止复述原作长台词。"
        )
    return "\n".join(lines)


def _touch_variant(variants: list[dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
    recent = {re.sub(r"[\W_]+", "", item).lower() for item in event.get("recentTouchReplies", [])}
    available = [item for item in variants if re.sub(r"[\W_]+", "", item["reply"]).lower() not in recent] or variants
    index = (event.get("sessionCount", 1) + event.get("mediumWindowCount", 1) + event.get("consecutiveCount", 1)) % len(available)
    return dict(available[index])


def touch_reply_is_repetitive(reply: str, recent_replies: list[str]) -> bool:
    normalized = re.sub(r"[\W_]+", "", str(reply or "")).lower()
    if not normalized:
        return False
    for previous in recent_replies:
        candidate = re.sub(r"[\W_]+", "", str(previous or "")).lower()
        if not candidate:
            continue
        similarity = SequenceMatcher(None, normalized, candidate).ratio()
        reply_bigrams = {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1))}
        candidate_bigrams = {candidate[index:index + 2] for index in range(max(0, len(candidate) - 1))}
        union = reply_bigrams | candidate_bigrams
        bigram_overlap = len(reply_bigrams & candidate_bigrams) / len(union) if union else 0.0
        same_opening = len(normalized) >= 6 and len(candidate) >= 6 and normalized[:6] == candidate[:6]
        if (
            normalized == candidate
            or similarity >= 0.72
            or bigram_overlap >= 0.56
            or same_opening
        ):
            return True
    return False


def touch_reply_misreads_text_as_touch(reply: str) -> bool:
    """Detect replies that answer an old utterance instead of the physical event."""
    compact = re.sub(r"\s+", "", str(reply or ""))
    patterns = (
        r"突然说(?:什么|这种)",
        r"你(?:刚才)?说什么",
        r"这种话.*(?:说|讲)",
        r"怎么能随便说",
        r"随便说出来",
        r"刚才那(?:句|话)",
    )
    return any(re.search(pattern, compact) for pattern in patterns)


def touch_reply_reverses_roles(reply: str, spoken_text_ja: str = "") -> bool:
    """Reject candidates that turn the touched assistant into the toucher."""

    compact = re.sub(r"\s+", "", str(reply or ""))
    spoken = re.sub(r"\s+", "", str(spoken_text_ja or ""))
    chinese_patterns = (
        r"(?:让我|换我|轮到我|这次我来|我来|我也来)(?:摸|碰|牵|握|亲|吻|抱)(?:你|你的)?",
        r"我(?:正在|在|刚才|刚刚|已经|正|也)?(?:摸|碰|牵|握|亲|吻|抱)(?:着|住|了|一下)?(?:你|你的)",
        r"(?:被我|让我).{0,5}(?:摸|碰|牵|握|亲|吻|抱)",
        r"(?:你的头发|你的头|你的脸|你的脸颊|你的手|你的嘴|你的嘴唇|你的胸口).{0,8}(?:摸起来|碰起来|亲起来)",
    )
    japanese_patterns = (
        r"(?:私が|私から|今度は私).{0,8}(?:撫でる|撫でて|触る|触って|握る|握って|キスする|抱く)",
        r"あなたの(?:髪|頭|顔|頬|手|唇).{0,8}(?:触り心地|撫でてみる|触ってみる)",
    )
    return (
        any(re.search(pattern, compact) for pattern in chinese_patterns)
        or any(re.search(pattern, spoken) for pattern in japanese_patterns)
    )


def touch_reply_is_grounded(reply: str, event: dict[str, Any]) -> bool:
    """Require a concrete cue that anchors the reply to the touched body zone."""
    compact = re.sub(r"\s+", "", str(reply or ""))
    cues = {
        "head": ("摸", "头", "头发", "发型", "手", "触感", "动作", "观察", "反应", "安慰", "放松", "温柔", "小孩子"),
        "face": ("脸", "脸颊", "靠近", "距离", "视线", "看着", "盯着", "碰", "触", "红"),
        "hand": ("手", "牵", "握", "温度", "暖", "凉", "松开"),
        "chest": ("手", "那里", "胸", "碰", "摸", "放开", "拿开", "越界", "变态"),
        "mouth": ("嘴", "唇", "亲", "吻", "靠近", "距离", "躲", "允许", "同意"),
    }
    zone = str(event.get("zone") or "")
    if any(cue in compact for cue in cues.get(zone, ())):
        return True
    if zone in {"mouth", "chest"}:
        return False
    implicit_physical_reactions = (
        "别这样", "慢一点", "轻一点", "别动", "先停一下", "会让我分心",
        "有点痒", "很舒服", "不太习惯", "已经习惯", "再靠近一点", "不用松开",
    )
    return any(cue in compact for cue in implicit_physical_reactions)


def chest_touch_contract_issues(result: dict[str, Any], event: dict[str, Any]) -> list[str]:
    if not (event.get("includesChest") or str(event.get("zone") or "") == "chest"):
        return []
    issues: list[str] = []
    reply = str(result.get("reply") or "")
    spoken = str(result.get("spokenTextJa") or "")
    if not CHEST_SCOLD_CN_RE.search(reply):
        issues.append("中文没有自然说出“变态”或“色狼”")
    if not CHEST_SCOLD_JA_RE.search(spoken):
        issues.append("日语没有对应说出“変態”或“スケベ”")
    stage = str(event.get("relationshipStage") or "close_companion")
    emotion = str(result.get("emotion") or "")
    gesture = str(result.get("gesture") or "")
    dense_boundary = str(event.get("boundaryLevel") or "") == "ignore"
    if stage == "close_companion":
        if emotion != "annoyed":
            issues.append("低好感摸胸的面部基础情绪必须是 annoyed，害羞应由脸红和声音表达")
        if gesture != "recoil":
            issues.append("低好感摸胸必须使用 recoil 明确后退")
    elif stage in PARTNER_STAGES and not dense_boundary:
        if emotion not in {"shy", "flustered"}:
            issues.append("亲密阶段应以 shy/flustered 表现害羞制止")
        if gesture not in {"recoil", "look_away", "tilt"}:
            issues.append("亲密阶段应使用 recoil/look_away/tilt")
    else:
        if emotion not in {"flustered", "annoyed"}:
            issues.append("本阶段应以 flustered/annoyed 表现慌乱和生气")
        if gesture != "recoil":
            issues.append("本阶段必须使用 recoil 明确后退")
    return issues


def apply_touch_expression_contract(result: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Guarantee visual boundary semantics after model/fallback normalization."""

    output = dict(result)
    includes_chest = bool(event.get("includesChest")) or str(event.get("zone") or "") == "chest"
    dense_boundary = (
        str(event.get("boundaryLevel") or "") == "ignore"
        or clamp_int(event.get("burstCount"), 1, 50, 1) >= 7
    )
    if dense_boundary:
        output["emotion"] = "annoyed"
        output["gesture"] = "recoil" if includes_chest else "tilt"
        output["emotionIntensity"] = max(1.02, float(output.get("emotionIntensity") or 0))
        output["emotionDelta"] = max(0.18, float(output.get("emotionDelta") or 0))
        output["tension"] = max(0.68, float(output.get("tension") or 0))
        if output.get("voiceIntensity") == "strong":
            output["voiceIntensity"] = "normal"
    if str(event.get("zone") or "") == "face" and not dense_boundary:
        # A normal cheek touch is a moderate social reaction. Keep it away
        # from the deepest shy/flustered pose even if the model overstates it.
        if str(output.get("emotion") or "") in {"flustered", "startled"}:
            output["emotion"] = "shy"
        output["emotionIntensity"] = max(
            0.82,
            min(0.88, float(output.get("emotionIntensity") or 0.84)),
        )
        output["emotionDelta"] = max(
            -0.02,
            min(0.02, float(output.get("emotionDelta") or 0)),
        )
        output["blushIntensity"] = max(
            0.22,
            min(0.38, float(output.get("blushIntensity") or 0.3)),
        )
        output["tension"] = max(
            0.12,
            min(0.3, float(output.get("tension") or 0.2)),
        )
        if str(output.get("voiceTone") or "") == "flustered":
            output["voiceTone"] = "shy"
        if str(output.get("voiceIntensity") or "") == "strong":
            output["voiceIntensity"] = "subtle"
    if includes_chest:
        output["blushIntensity"] = max(0.55, float(output.get("blushIntensity") or 0))
        output["tension"] = max(0.65, float(output.get("tension") or 0))
        if str(event.get("relationshipStage") or "close_companion") == "close_companion":
            # Embarrassment is carried by blush/gaze/voice; the face itself
            # remains angry so it reads as 恼羞成怒 rather than a shy smile.
            output["emotion"] = "annoyed"
            output["gesture"] = "recoil"
            output["blushIntensity"] = max(0.72, float(output.get("blushIntensity") or 0))
            output["tension"] = max(0.78, float(output.get("tension") or 0))
            output["emotionIntensity"] = max(1.08, float(output.get("emotionIntensity") or 0))
            output["emotionDelta"] = max(0.22, float(output.get("emotionDelta") or 0))
            if str(output.get("voiceTone") or "") in {"", "neutral", "warm", "shy"}:
                output["voiceTone"] = "flustered"
    return output
