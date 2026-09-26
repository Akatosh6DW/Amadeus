"""主动交流的重复、隐私、来源与屏幕抽样规则。

这里不读会话或配置，不调用模型，也不写入状态；调用者提供本轮实际输入。
屏幕抽样保留原随机源，可通过 random_value 显式验证阈值。
"""
from __future__ import annotations
from difflib import SequenceMatcher
import hashlib
import random
import re
from typing import Any, Mapping
from agent.epistemic_architecture import user_claim_provenance
from backend.common.text import unique_strings
from backend.common.values import clamp_float
from backend.config.normalization import PROACTIVE_TOPIC_CATEGORY_LABELS
from backend.context.recall import memory_keywords, retrieval_overlap_score

def reply_dedupe_units(value: str) -> list[str]:
    """Return whole, sentence, and adjacent-sentence views of one reply."""

    text = re.sub(r"[ \t]+", " ", str(value or "")).strip()
    if not text:
        return []
    parts = [
        item.strip()
        for item in re.split(r"(?:\n{2,}|[。！？!?；;]+)", text)
        if item.strip()
    ]
    units = [text]
    units.extend(item for item in parts if len(re.sub(r"\W+", "", item)) >= 6)
    units.extend(
        f"{parts[index]}。{parts[index + 1]}"
        for index in range(max(0, len(parts) - 1))
    )
    return unique_strings(units)


def proactive_reply_similarity(left: str, right: str) -> float:
    def normalized(value: str) -> str:
        return re.sub(
            r"[\W_]+",
            "",
            str(value or "").lower(),
            flags=re.UNICODE,
        )

    a = normalized(left)
    b = normalized(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 10 and shorter in longer:
        return 0.94
    sequence_score = SequenceMatcher(None, a, b).ratio()
    left_chars = set(a)
    right_chars = set(b)
    character_dice = (
        (2 * len(left_chars & right_chars)) / (len(left_chars) + len(right_chars))
        if left_chars and right_chars
        else 0.0
    )

    def ngram_containment(value_a: str, value_b: str, size: int = 2) -> float:
        grams_a = {
            value_a[index:index + size]
            for index in range(max(0, len(value_a) - size + 1))
        }
        grams_b = {
            value_b[index:index + size]
            for index in range(max(0, len(value_b) - size + 1))
        }
        if not grams_a or not grams_b:
            return 0.0
        return len(grams_a & grams_b) / min(len(grams_a), len(grams_b))

    return max(
        sequence_score,
        character_dice * 0.88,
        ngram_containment(a, b) * 0.9,
    )


def proactive_reply_duplicate(
    reply: str,
    recent_texts: list[str],
    previous_prompt: str = "",
) -> tuple[bool, str]:
    for candidate in recent_texts:
        threshold = 0.68 if previous_prompt and candidate == previous_prompt else 0.64
        for reply_unit in reply_dedupe_units(reply):
            for candidate_unit in reply_dedupe_units(candidate):
                if min(
                    len(re.sub(r"[\W_]+", "", reply_unit)),
                    len(re.sub(r"[\W_]+", "", candidate_unit)),
                ) < 8:
                    continue
                score = proactive_reply_similarity(reply_unit, candidate_unit)
                if score >= threshold:
                    return True, candidate
    return False, ""


def proactive_semantic_private_match(
    text: str,
    policy: dict[str, Any],
) -> str:
    candidate_terms = memory_keywords(text)
    if not candidate_terms:
        return ""
    for index, evidence in enumerate(policy.get("privateSemanticEvidence", [])):
        evidence_text = str(evidence or "").strip()
        if not evidence_text:
            continue
        if retrieval_overlap_score(
            candidate_terms,
            memory_keywords(evidence_text),
        ) >= 2.0:
            digest = hashlib.sha1(
                evidence_text.encode("utf-8"),
            ).hexdigest()[:10]
            return f"persona_semantic:{index + 1}:{digest}"
    return ""


def proactive_pattern_matches(text: str, patterns: Any) -> bool:
    value = str(text or "")
    if not value:
        return False
    for pattern in patterns if isinstance(patterns, list) else []:
        try:
            if re.search(str(pattern), value, flags=re.IGNORECASE):
                return True
        except re.error:
            if str(pattern).lower() in value.lower():
                return True
    return False


def proactive_private_topic_ids(
    text: str,
    policy: dict[str, Any],
    *pattern_fields: str,
) -> list[str]:
    matched: list[str] = []
    for rule in policy.get("privateSelfTopics", []):
        if not isinstance(rule, dict):
            continue
        if any(
            proactive_pattern_matches(text, rule.get(field))
            for field in pattern_fields
        ):
            rule_id = str(rule.get("id") or "").strip()
            if rule_id:
                matched.append(rule_id)
    semantic_match = proactive_semantic_private_match(text, policy)
    if semantic_match:
        matched.append(semantic_match)
    return unique_strings(matched)


def redact_private_proactive_context(
    text: str,
    policy: dict[str, Any],
) -> str:
    """Hide private-persona topic details from the proactive generator only."""

    matched = proactive_private_topic_ids(
        text,
        policy,
        "contextPatterns",
        "directReplyPatterns",
    )
    if not matched:
        return str(text or "")
    return (
        "[上一轮包含用户主动提起的私密人格话题；正常对话已经可以按人格回应，"
        "但主动聊天不得继续延伸或把它当成新话题。]"
    )


def proactive_private_topic_violation(
    reply: str,
    recent_user_texts: list[str],
    policy: dict[str, Any],
) -> tuple[bool, str]:
    """Independently reject proactive self-disclosure and private-topic carryover."""

    for rule in policy.get("privateSelfTopics", []):
        if not isinstance(rule, dict):
            continue
        rule_id = str(rule.get("id") or "").strip()
        if proactive_pattern_matches(reply, rule.get("directReplyPatterns")):
            return True, rule_id
        user_opened_private_topic = any(
            proactive_pattern_matches(text, rule.get("contextPatterns"))
            for text in recent_user_texts
        )
        if user_opened_private_topic and proactive_pattern_matches(
            reply,
            rule.get("continuationReplyPatterns"),
        ):
            return True, rule_id
    semantic_match = proactive_semantic_private_match(reply, policy)
    if semantic_match:
        return True, semantic_match
    return False, ""


def proactive_topic_seeds(
    messages: list[dict[str, Any]],
    selected_categories: list[str],
    memories: list[str],
    policy: dict[str, Any],
) -> list[dict[str, str]]:
    """Build an allowlist of topic sources without trusting assistant-authored lore."""

    seeds: list[dict[str, str]] = []
    user_index = 0
    for item in messages[-10:]:
        if item.get("role") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        user_index += 1
        if proactive_private_topic_ids(
            content,
            policy,
            "contextPatterns",
            "directReplyPatterns",
        ):
            continue
        seeds.append({
            "id": f"user:{user_index}",
            "kind": "recent_user_topic",
            "topic": content[:420],
            "provenance": user_claim_provenance(content),
        })
    for category in selected_categories:
        label = str(PROACTIVE_TOPIC_CATEGORY_LABELS.get(category) or "").strip()
        if label:
            seeds.append({
                "id": f"interest:{category}",
                "kind": "configured_interest",
                "topic": label,
                "provenance": "topic_direction",
            })
    for topic in policy.get("publicSelfTopics", []):
        if not isinstance(topic, dict):
            continue
        topic_id = str(topic.get("id") or "").strip()
        label = str(topic.get("label") or "").strip()
        if topic_id and label:
            seeds.append({
                "id": f"persona:{topic_id}",
                "kind": "public_persona_preference",
                "topic": label,
                "provenance": "persona_public_fact",
            })
    memory_index = 0
    for memory in memories:
        value = str(memory or "").strip()
        if not value:
            continue
        memory_index += 1
        if proactive_private_topic_ids(
            value,
            policy,
            "contextPatterns",
            "directReplyPatterns",
        ):
            continue
        seeds.append({
            "id": f"memory:{memory_index}",
            "kind": "reliable_user_memory",
            "topic": value[:420],
            "provenance": user_claim_provenance(value),
        })
    deduped: dict[str, dict[str, str]] = {}
    for seed in seeds:
        deduped.setdefault(seed["id"], seed)
    return list(deduped.values())


def should_proactive_observe_screen(
    settings: dict[str, Any],
    body: dict[str, Any],
    *,
    random_value: float | None = None,
) -> bool:
    """Decide whether an idle *new* outreach may use a fresh screen sample."""

    if str(body.get("kind") or "new").strip().lower() == "follow_up":
        return False
    if not settings.get("proactiveInteractionsEnabled"):
        return False
    if not settings.get("proactiveScreenObservationEnabled"):
        return False
    if not settings.get("permissionReadScreen") or not settings.get("visionEnabled"):
        return False
    chance = clamp_float(
        settings.get("proactiveScreenObservationChance"), 0.0, 0.60, 0.20
    )
    sample = random.random() if random_value is None else clamp_float(
        random_value, 0.0, 1.0, 1.0
    )
    return chance > 0 and sample < chance


def proactive_outreach_context_key(context: Mapping[str, Any]) -> tuple[Any, ...]:
    """A background decision cannot outlive a newer user turn or conversation."""
    latest_user = next((turn for turn in reversed(context.get("recentTurns") or [])
                        if isinstance(turn, Mapping) and turn.get("user")), {})
    return (
        context.get("conversationId"), context.get("conversationEpoch"),
        latest_user.get("turnId"), latest_user.get("at"), latest_user.get("user"),
    )
