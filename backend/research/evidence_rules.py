"""证据不足时的草稿去重与来源精度约束，不生成答案或请求模型。"""
from __future__ import annotations
from typing import Any
import re
from backend.chat.parsing import parse_json_object
from backend.proactive.rules import reply_dedupe_units, proactive_reply_similarity

def model_reply_text(value: Any) -> str:
    """Extract visible model prose without making a domain-specific judgment."""

    if isinstance(value, dict):
        return str(value.get("reply") or "").strip()
    if isinstance(value, str):
        parsed = parse_json_object(value)
        if isinstance(parsed, dict) and str(parsed.get("reply") or "").strip():
            return str(parsed.get("reply") or "").strip()
        return value.strip()
    return ""


def evidence_synthesis_appends_prior_answer(
    candidate: Any,
    prior_answer: Any,
) -> bool:
    """Detect a rewrite that copied the old answer and then answered again.

    Repetition inside a requested utterance can be intentional (for example,
    asking the character to say one phrase six times), so this validator does
    not count repeated words.  It checks the rewrite boundary instead: whether
    a materially longer candidate contains the previous complete answer as an
    answer block.
    """

    candidate_text = model_reply_text(candidate)
    prior_text = model_reply_text(prior_answer)

    def compact(value: str) -> str:
        return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)

    candidate_key = compact(candidate_text)
    prior_key = compact(prior_text)
    if len(prior_key) < 16 or len(candidate_key) < len(prior_key) + 12:
        return False
    if prior_key in candidate_key:
        return True
    if len(candidate_key) < round(len(prior_key) * 1.22):
        return False
    for unit in reply_dedupe_units(candidate_text):
        unit_key = compact(unit)
        if len(unit_key) < max(16, round(len(prior_key) * 0.62)):
            continue
        if proactive_reply_similarity(unit, prior_text) >= 0.86:
            return True
    return False


def sanitize_secondary_only_precision(
    result: dict[str, Any],
    results: list[dict[str, Any]],
    user_message: str,
) -> dict[str, Any]:
    """Delete high-precision clauses when only secondary web evidence exists."""

    statuses = [
        item.get("evidenceStatus")
        for item in results
        if isinstance(item, dict)
        and item.get("name") in {"web_search", "read_web_page"}
        and isinstance(item.get("evidenceStatus"), dict)
    ]
    secondary_only = any(status.get("secondarySourceIds") for status in statuses) and not any(
        status.get("relevantSubstantiveSourceIds") for status in statuses
    )
    if not secondary_only:
        return result
    reply = str(result.get("reply") or "").strip()
    if not reply:
        return result
    high_precision = re.compile(
        r"(?:\d+(?:\.\d+)?\s*(?:%|％|年|月|日|名|位|次|场|局|分|秒|万|亿|倍|级|章|话|集))|"
        r"(?:“[^”]{1,80}”|「[^」]{1,80}」|『[^』]{1,80}』)|"
        r"(?:曾经?|一度|当时|随后|最终|具体来说|例如|比如|"
        r"单杀|单刷|秒杀|击败|打败|战胜|压制|斩杀|杀死|徒手杀|干掉|淘汰|"
        r"对阵|交手|夺冠|获奖|创下|刷新纪录|发表|宣布|被称为|号称|"
        r"导致|造成|因此让|评价为|给出.{0,12}评价)",
        re.I,
    )
    user_compact = re.sub(r"\s+", "", user_message)
    parts = re.split(r"([，,；;。！？!?]+)", reply)
    kept: list[str] = []
    removed = 0
    index = 0
    while index < len(parts):
        clause = parts[index]
        punctuation = parts[index + 1] if index + 1 < len(parts) else ""
        compact_clause = re.sub(r"\s+", "", clause)
        user_supplied = bool(compact_clause) and compact_clause in user_compact
        if compact_clause and high_precision.search(clause) and not user_supplied:
            removed += 1
        else:
            if clause:
                kept.append(clause)
            if punctuation:
                kept.append(punctuation)
        index += 2
    cleaned = "".join(kept).strip()
    cleaned = re.sub(r"^[，,；;\s]+", "", cleaned)
    cleaned = re.sub(r"([，,；;])([。！？!?])", r"\2", cleaned)
    cleaned = re.sub(r"\s+([，,；;。！？!?])", r"\1", cleaned)
    if not removed or len(re.sub(r"\s+", "", cleaned)) < 12:
        return result
    sanitized = dict(result)
    sanitized["reply"] = cleaned
    sanitized["evidenceLimitedPrecisionSanitized"] = True
    sanitized["evidenceLimitedPrecisionRemovedClauses"] = removed
    return sanitized
