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
