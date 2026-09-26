"""日语发声的本地风格/内容检查与字段修复预算，均为纯规则。"""
from __future__ import annotations
import re
from typing import Any

def _spoken_ja_alias_present(text: str, alias: str) -> bool:
    if re.search(r"[A-Za-z]", alias):
        return bool(re.search(re.escape(alias), text, re.IGNORECASE))
    return alias in text


def spoken_japanese_style_needs_repair(text: str) -> bool:
    return bool(re.search(
        r"(?:君|お前|だぜ|だぞ|かよ|からだよ|はずだ(?:[。！？!?]|$)|(?:選べ|しろ|黙れ|やれ)(?:[。！？!?]|$))",
        str(text or ""),
    ))


def spoken_japanese_content_needs_repair(text: str) -> bool:
    """Reject non-Japanese or phonetic-looking CJK strings before local TTS."""
    value = str(text or "").strip()
    if not value:
        return True
    kana = re.findall(r"[\u3040-\u30ffー]", value)
    han = re.findall(r"[\u3400-\u9fff]", value)
    if re.search(r"[你妳吗嗎呢吧嘛哇这這们們没沒]", value):
        return True
    if not kana:
        # Short all-kanji Japanese utterances such as "準備完了" are valid.
        # Latin-only or symbol-only output is not usable Japanese speech text.
        return not bool(han)
    if len(han) >= 3 and len(kana) / max(1, len(kana) + len(han)) < 0.12:
        return True
    return False


def soften_spoken_japanese_style(text: str) -> str:
    value = str(text or "")
    replacements = (
        (r"君", "あなた"),
        (r"お前", "あなた"),
        (r"だぜ", "よ"),
        (r"だぞ", "よ"),
        (r"かよ", "なの"),
        (r"からだよ", "からよ"),
        (r"はずだ(?=[。！？!?]|$)", "はずよ"),
        (r"選べ(?=[。！？!?]|$)", "選んで"),
        (r"しろ(?=[。！？!?]|$)", "して"),
        (r"黙れ(?=[。！？!?]|$)", "黙って"),
        (r"やれ(?=[。！？!?]|$)", "やって"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    return value


def dialogue_field_repair_token_budget(
    frozen_reply: str,
    requested_fields: list[str],
) -> int:
    """Reserve output for exactly the fields being repaired.

    A Japanese speech track can approach the length of the accepted Chinese
    reply.  The old fixed 520-token ceiling truncated longer tracks, while a
    large fixed ceiling would waste reservation on micro replies.  Speech-unit
    segmentation and emotion are deliberately not bundled into this request.
    """

    compact_length = len(re.sub(r"\s+", "", str(frozen_reply or "")))
    if "spokenTextJa" in requested_fields:
        return max(320, min(1600, 192 + compact_length * 3))
    return 256
