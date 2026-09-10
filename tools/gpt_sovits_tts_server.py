#!/usr/bin/env python3
"""Minimal local GPT-SoVITS HTTP TTS server for AMADEUS.

POST /tts with JSON:
  {"text": "...", "voiceStyle": "calm", "locale": "ja-JP"}

Returns audio/wav. A style map can provide different reference prompts per
voiceStyle; otherwise one default reference is used for every style.
"""

from __future__ import annotations

import argparse
import base64
import builtins
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import soundfile as sf
except ModuleNotFoundError:
    class _MissingSoundFile:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError(
                "soundfile is required by the GPT-SoVITS runtime for audio IO"
            )

    sf = _MissingSoundFile()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SynthesisCancelled(RuntimeError):
    """Raised when the owning chat request is explicitly stopped."""


def runtime_fingerprint(value: Any, *, length: int = 16) -> str:
    """Compact non-security identity for TTS cache and diagnostic records."""

    payload = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
    digest_size = max(4, min(32, (max(8, length) + 1) // 2))
    return hashlib.blake2s(payload, digest_size=digest_size).hexdigest()[:length]


def runtime_digest(value: Any, *, size: int = 16) -> bytes:
    payload = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
    return hashlib.blake2s(payload, digest_size=max(1, min(size, 32))).digest()

LANGUAGE_BY_LOCALE = {
    "ja": "日文",
    "ja-jp": "日文",
    "zh": "中文",
    "zh-cn": "中文",
    "zh-tw": "中文",
    "en": "英文",
    "en-us": "英文",
}

JAPANESE_LATIN_LETTER_NAMES = {
    "A": "エー", "B": "ビー", "C": "シー", "D": "ディー", "E": "イー",
    "F": "エフ", "G": "ジー", "H": "エイチ", "I": "アイ", "J": "ジェー",
    "K": "ケー", "L": "エル", "M": "エム", "N": "エヌ", "O": "オー",
    "P": "ピー", "Q": "キュー", "R": "アール", "S": "エス", "T": "ティー",
    "U": "ユー", "V": "ブイ", "W": "ダブリュー", "X": "エックス",
    "Y": "ワイ", "Z": "ゼット",
}

# Unicode blocks whose characters use emoji presentation by default. Keep
# this independent of the Japanese frontend: display text may contain emoji,
# but no voice backend should receive them or budget speech time for them.
# Text symbols such as plain ©, ® and ™ are intentionally not included;
# they are removed only when U+FE0F explicitly requests emoji presentation.
DEFAULT_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x231A, 0x231B),
    (0x23E9, 0x23EC),
    (0x23F0, 0x23F0),
    (0x23F3, 0x23F3),
    (0x25FD, 0x25FE),
    (0x2614, 0x2615),
    (0x2648, 0x2653),
    (0x267F, 0x267F),
    (0x2693, 0x2693),
    (0x26A1, 0x26A1),
    (0x26AA, 0x26AB),
    (0x26BD, 0x26BE),
    (0x26C4, 0x26C5),
    (0x26CE, 0x26CE),
    (0x26D4, 0x26D4),
    (0x26EA, 0x26EA),
    (0x26F2, 0x26F3),
    (0x26F5, 0x26F5),
    (0x26FA, 0x26FA),
    (0x26FD, 0x26FD),
    (0x2705, 0x2705),
    (0x270A, 0x270B),
    (0x2728, 0x2728),
    (0x274C, 0x274C),
    (0x274E, 0x274E),
    (0x2753, 0x2755),
    (0x2757, 0x2757),
    (0x2795, 0x2797),
    (0x27B0, 0x27B0),
    (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
)
EMOJI_VARIATION_SELECTORS = frozenset((0xFE0E, 0xFE0F))
EMOJI_SKIN_TONE_RANGE = (0x1F3FB, 0x1F3FF)
EMOJI_TAG_RANGE = (0xE0020, 0xE007F)
EMOJI_ZWJ = 0x200D
EMOJI_KEYCAP = 0x20E3
JAPANESE_TECHNICAL_WORDS = {
    "amadeus": "アマデウス",
    "api": "エーピーアイ",
    "codex": "コーデックス",
    "css": "シーエスエス",
    "deepseek": "ディープシーク",
    "exe": "実行ファイル",
    "gpt": "ジーピーティー",
    "gpt-sovits": "ジーピーティーソヴィッツ",
    "html": "エイチティーエムエル",
    "javascript": "ジャバスクリプト",
    "js": "ジェーエス",
    "json": "ジェイソン",
    "live2d": "ライブツーディー",
    "markdown": "マークダウン",
    "mages": "メージス",
    "nintendo": "ニンテンドー",
    "passed": "合格",
    "playstation": "プレイステーション",
    "powershell": "パワーシェル",
    "python": "パイソン",
    "reboot": "リブート",
    "release": "リリース",
    "run": "ラン",
    "server": "サーバー",
    "sovits": "ソヴィッツ",
    "steam": "スチーム",
    "switch": "スイッチ",
    "series": "シリーズ",
    "test": "テスト",
    "tests": "テスト",
    "tts": "ティーティーエス",
    "typescript": "タイプスクリプト",
    "ts": "ティーエス",
    "wav": "音声ファイル",
    "xbox": "エックスボックス",
}
JAPANESE_FILE_EXTENSION_NAMES = {
    "ps1": "パワーシェルスクリプト",
    "py": "パイソンスクリプト",
    "js": "ジャバスクリプトファイル",
    "ts": "タイプスクリプトファイル",
    "md": "マークダウン文書",
    "json": "ジェイソンファイル",
    "yaml": "ヤムルファイル",
    "yml": "ヤムルファイル",
    "txt": "テキストファイル",
    "html": "エイチティーエムエルファイル",
    "css": "スタイルシート",
    "exe": "実行ファイル",
    "wav": "音声ファイル",
    "png": "画像ファイル",
    "jpg": "画像ファイル",
    "jpeg": "画像ファイル",
    "zip": "圧縮ファイル",
}
_ENGLISH_G2P: Any = None
_ENGLISH_G2P_UNAVAILABLE = False

ARPABET_VOWELS = {
    "AA": ("a", ""),
    "AE": ("a", ""),
    "AH": ("a", ""),
    "AO": ("o", "ー"),
    "AW": ("a", "ウ"),
    "AY": ("a", "イ"),
    "EH": ("e", ""),
    "ER": ("a", "ー"),
    "EY": ("e", "イ"),
    "IH": ("i", ""),
    "IY": ("i", "ー"),
    "OW": ("o", "ウ"),
    "OY": ("o", "イ"),
    "UH": ("u", ""),
    "UW": ("u", "ー"),
}
KATAKANA_ONSET_ROWS = {
    "": {"a": "ア", "e": "エ", "i": "イ", "o": "オ", "u": "ウ"},
    "B": {"a": "バ", "e": "ベ", "i": "ビ", "o": "ボ", "u": "ブ"},
    "P": {"a": "パ", "e": "ペ", "i": "ピ", "o": "ポ", "u": "プ"},
    "T": {"a": "タ", "e": "テ", "i": "ティ", "o": "ト", "u": "トゥ"},
    "D": {"a": "ダ", "e": "デ", "i": "ディ", "o": "ド", "u": "ドゥ"},
    "K": {"a": "カ", "e": "ケ", "i": "キ", "o": "コ", "u": "ク"},
    "G": {"a": "ガ", "e": "ゲ", "i": "ギ", "o": "ゴ", "u": "グ"},
    "S": {"a": "サ", "e": "セ", "i": "シ", "o": "ソ", "u": "ス"},
    "Z": {"a": "ザ", "e": "ゼ", "i": "ジ", "o": "ゾ", "u": "ズ"},
    "SH": {"a": "シャ", "e": "シェ", "i": "シ", "o": "ショ", "u": "シュ"},
    "CH": {"a": "チャ", "e": "チェ", "i": "チ", "o": "チョ", "u": "チュ"},
    "JH": {"a": "ジャ", "e": "ジェ", "i": "ジ", "o": "ジョ", "u": "ジュ"},
    "M": {"a": "マ", "e": "メ", "i": "ミ", "o": "モ", "u": "ム"},
    "N": {"a": "ナ", "e": "ネ", "i": "ニ", "o": "ノ", "u": "ヌ"},
    "R": {"a": "ラ", "e": "レ", "i": "リ", "o": "ロ", "u": "ル"},
    "L": {"a": "ラ", "e": "レ", "i": "リ", "o": "ロ", "u": "ル"},
    "H": {"a": "ハ", "e": "ヘ", "i": "ヒ", "o": "ホ", "u": "フ"},
    "F": {"a": "ファ", "e": "フェ", "i": "フィ", "o": "フォ", "u": "フ"},
    "V": {"a": "ヴァ", "e": "ヴェ", "i": "ヴィ", "o": "ヴォ", "u": "ヴ"},
    "W": {"a": "ワ", "e": "ウェ", "i": "ウィ", "o": "ウォ", "u": "ウ"},
    "Y": {"a": "ヤ", "e": "イェ", "i": "イ", "o": "ヨ", "u": "ユ"},
    "TH": {"a": "サ", "e": "セ", "i": "シ", "o": "ソ", "u": "ス"},
    "DH": {"a": "ザ", "e": "ゼ", "i": "ジ", "o": "ゾ", "u": "ズ"},
}
KATAKANA_CODAS = {
    "B": "ブ", "CH": "チ", "D": "ド", "DH": "ズ", "F": "フ",
    "G": "グ", "H": "フ", "JH": "ジ", "K": "ク", "L": "ル",
    "M": "ム", "N": "ン", "NG": "ング", "P": "プ", "R": "ル",
    "S": "ス", "SH": "シュ", "T": "ト", "TH": "ス", "V": "ヴ",
    "W": "ウ", "Y": "イ", "Z": "ズ",
}
ENGLISH_ONSET_CLUSTERS = {
    ("B", "L"), ("B", "R"), ("D", "R"), ("F", "L"), ("F", "R"),
    ("G", "L"), ("G", "R"), ("K", "L"), ("K", "R"), ("P", "L"),
    ("P", "R"), ("S", "K"), ("S", "L"), ("S", "M"), ("S", "N"),
    ("S", "P"), ("S", "T"), ("S", "W"), ("T", "R"),
}


def prepare_inference_weight_state_redirect(
    state_path: Path,
    source_root: Path,
) -> tuple[Path, Any]:
    """Return a narrowly scoped opener for GPT-SoVITS' mutable weight state."""
    target = Path(state_path).resolve()
    expected_root = Path(source_root).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text('{"GPT":{},"SoVITS":{}}', encoding="utf-8")
    original_open = builtins.open

    def redirected_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            relative_name = os.fspath(file).replace("\\", "/")
        except TypeError:
            relative_name = ""
        if relative_name in {"weight.json", "./weight.json"} and Path.cwd().resolve() == expected_root:
            file = target
        return original_open(file, *args, **kwargs)

    return target, redirected_open


def language_from_locale(locale: str, fallback: str = "日文") -> str:
    key = str(locale or "").strip().lower()
    return LANGUAGE_BY_LOCALE.get(key, LANGUAGE_BY_LOCALE.get(key.split("-")[0], fallback))


def is_chinese_target_language(language: str) -> bool:
    value = str(language or "").strip().lower().replace("_", "-")
    return value in {"中文", "中英混合", "zh", "zh-cn", "zh-hans", "chinese"}


def target_language_for_text(configured_language: str, text: str) -> str:
    """Use mixed Chinese/English G2P only when Latin tokens are present."""
    language = language_from_locale(configured_language, str(configured_language or "日文"))
    if language == "中文" and re.search(r"[A-Za-z]", str(text or "")):
        return "中英混合"
    return language


def normalize_voice_intensity(value: Any) -> str:
    raw = str(value or "normal").strip().lower()
    aliases = {"low": "subtle", "mild": "subtle", "medium": "normal", "moderate": "normal", "high": "strong", "intense": "strong"}
    normalized = aliases.get(raw, raw)
    return normalized if normalized in {"subtle", "normal", "strong"} else "normal"


def japanese_integer_text(value: str) -> str:
    """Render a bounded non-negative integer without leaving Latin digits."""

    raw = re.sub(r"[,，_]", "", str(value or ""))
    if not raw.isdigit():
        return str(value or "")
    number = int(raw)
    if number == 0:
        return "零"
    if number > 999_999_999_999:
        return "".join("零一二三四五六七八九"[int(digit)] for digit in raw)

    digits = "零一二三四五六七八九"

    def four_digit_group(group: int) -> str:
        result: list[str] = []
        for divisor, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
            digit = group // divisor
            group %= divisor
            if not digit:
                continue
            if divisor > 1 and digit == 1:
                result.append(unit)
            else:
                result.append(digits[digit] + unit)
        return "".join(result)

    parts: list[str] = []
    for divisor, unit in ((100_000_000, "億"), (10_000, "万"), (1, "")):
        group = number // divisor
        number %= divisor
        if group:
            parts.append(four_digit_group(group) + unit)
    return "".join(parts)


def english_g2p_phonemes(word: str) -> list[str]:
    """Return stress-free ARPABET phones when the bundled runtime supports it."""

    global _ENGLISH_G2P, _ENGLISH_G2P_UNAVAILABLE
    if _ENGLISH_G2P_UNAVAILABLE:
        return []
    try:
        if _ENGLISH_G2P is None:
            from g2p_en import G2p
            _ENGLISH_G2P = G2p()
        return [
            re.sub(r"\d", "", str(item)).upper()
            for item in _ENGLISH_G2P(str(word or ""))
            if re.fullmatch(r"[A-Za-z]+\d*", str(item))
        ]
    except Exception:
        _ENGLISH_G2P_UNAVAILABLE = True
        return []


def katakana_coda(phones: list[str]) -> str:
    return "".join(KATAKANA_CODAS.get(phone, "") for phone in phones)


def katakana_syllable(onset: list[str], vowel: str) -> str:
    category, suffix = ARPABET_VOWELS[vowel]
    if not onset:
        return KATAKANA_ONSET_ROWS[""][category] + suffix
    cluster = tuple(onset)
    if cluster in ENGLISH_ONSET_CLUSTERS:
        first = katakana_coda([onset[0]])
        last = KATAKANA_ONSET_ROWS.get(onset[-1], KATAKANA_ONSET_ROWS[""])[category]
        return first + last + suffix
    prefix = katakana_coda(onset[:-1])
    row = KATAKANA_ONSET_ROWS.get(onset[-1], KATAKANA_ONSET_ROWS[""])
    return prefix + row[category] + suffix


def english_word_to_katakana(word: str) -> str:
    """Approximate an unknown English word as one spoken Japanese loanword.

    Known conventional readings stay in ``JAPANESE_TECHNICAL_WORDS``. This
    phoneme path is the generic fallback for new product names, so a word such
    as ``Steam`` is not expanded into five unrelated letter names.
    """

    raw = re.sub(r"[^A-Za-z]", "", str(word or ""))
    if not raw:
        return ""
    known = JAPANESE_TECHNICAL_WORDS.get(raw.casefold())
    if known:
        return known
    # Initial X before another consonant is normally the named letter in
    # product words (Xcode, Xbox), not the /z/ pronunciation returned by an
    # English dictionary.
    if len(raw) > 1 and raw[0].casefold() == "x" and raw[1].casefold() not in "aeiou":
        tail = english_word_to_katakana(raw[1:])
        return "エックス" + tail if tail else "エックス"
    phones = english_g2p_phonemes(raw)
    if not phones:
        return "".join(JAPANESE_LATIN_LETTER_NAMES[letter] for letter in raw.upper())

    output: list[str] = []
    pending: list[str] = []
    seen_vowel = False
    for phone in phones:
        if phone not in ARPABET_VOWELS:
            pending.append(phone)
            continue
        if seen_vowel and pending:
            if tuple(pending) in ENGLISH_ONSET_CLUSTERS or len(pending) == 1:
                onset = pending
            else:
                output.append(katakana_coda(pending[:-1]))
                onset = pending[-1:]
        else:
            onset = pending
        output.append(katakana_syllable(onset, phone))
        pending = []
        seen_vowel = True
    output.append(katakana_coda(pending))
    return "".join(output) or "".join(
        JAPANESE_LATIN_LETTER_NAMES[letter] for letter in raw.upper()
    )


def japanese_technical_token(token: str) -> str:
    """Turn an ASCII technical token into stable Japanese speech text."""

    raw = str(token or "")
    if not raw:
        return ""
    key = raw.casefold()
    if key in JAPANESE_TECHNICAL_WORDS:
        return JAPANESE_TECHNICAL_WORDS[key]
    if raw.isdigit() or re.fullmatch(r"\d[\d,，_]*", raw):
        return japanese_integer_text(raw)
    if re.fullmatch(r"v\d+", raw, flags=re.I):
        return "バージョン" + japanese_integer_text(raw[1:])
    pieces: list[str] = []
    for part in re.findall(r"[A-Za-z]+|\d+|[^A-Za-z\d]+", raw):
        if not part:
            continue
        part_key = part.casefold()
        if part_key in JAPANESE_TECHNICAL_WORDS:
            pieces.append(JAPANESE_TECHNICAL_WORDS[part_key])
        elif part.isdigit():
            pieces.append(japanese_integer_text(part))
        elif part.isalpha():
            word_like = (
                len(part) >= 5
                or not part.isupper()
                or bool(re.search(r"[aeiouy]", part, flags=re.I)) and len(part) >= 4
            )
            pieces.append(
                english_word_to_katakana(part)
                if word_like
                else "".join(JAPANESE_LATIN_LETTER_NAMES[letter] for letter in part.upper())
            )
        else:
            pieces.append(" ")
    return " ".join(item for item in pieces if item.strip())


def codepoint_in_ranges(codepoint: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= codepoint <= end for start, end in ranges)


def default_emoji_codepoint(codepoint: int) -> bool:
    return codepoint_in_ranges(codepoint, DEFAULT_EMOJI_RANGES)


def emoji_cluster_end(text: str, start: int) -> int:
    """Return the end of one emoji grapheme-like sequence, or ``start``.

    Python's standard library does not expose the Unicode Extended_Pictographic
    property or grapheme-cluster matcher. This scanner covers the structural
    emoji forms used by voice input: default emoji code points, presentation
    selector sequences, skin tones, keycaps, flags, tag flags and ZWJ chains.
    It deliberately preserves text-default symbols unless U+FE0F turns them
    into emoji presentation.
    """

    if start < 0 or start >= len(text):
        return start
    codepoint = ord(text[start])
    next_codepoint = ord(text[start + 1]) if start + 1 < len(text) else -1

    if (
        text[start] in "#*0123456789"
        and (
            next_codepoint == EMOJI_KEYCAP
            or (
                next_codepoint == 0xFE0F
                and start + 2 < len(text)
                and ord(text[start + 2]) == EMOJI_KEYCAP
            )
        )
    ):
        return start + (3 if next_codepoint == 0xFE0F else 2)

    is_regional_indicator = 0x1F1E6 <= codepoint <= 0x1F1FF
    emoji_start = bool(
        default_emoji_codepoint(codepoint)
        or next_codepoint == 0xFE0F
        or is_regional_indicator
    )
    if not emoji_start:
        return start

    end = start + 1
    if (
        is_regional_indicator
        and end < len(text)
        and 0x1F1E6 <= ord(text[end]) <= 0x1F1FF
    ):
        end += 1

    def consume_suffix(index: int) -> int:
        while index < len(text):
            current = ord(text[index])
            if (
                current in EMOJI_VARIATION_SELECTORS
                or EMOJI_SKIN_TONE_RANGE[0] <= current <= EMOJI_SKIN_TONE_RANGE[1]
                or EMOJI_TAG_RANGE[0] <= current <= EMOJI_TAG_RANGE[1]
            ):
                index += 1
                continue
            break
        return index

    end = consume_suffix(end)
    while end < len(text) and ord(text[end]) == EMOJI_ZWJ:
        end += 1
        if end >= len(text):
            break
        # Once a valid emoji starts a ZWJ sequence, the joined glyph belongs to
        # that emoji even when its base is normally text presentation (for
        # example the gender and direction symbols).
        end = consume_suffix(end + 1)
    return end


def strip_emoji_for_speech(text: str) -> str:
    """Project display text to speech text without pronouncing emoji.

    A removed cluster becomes a word boundary instead of an empty string so
    ``hello❤️girl`` does not accidentally turn into the unrelated token
    ``hellogirl``. Orphan emoji formatting controls are also removed because
    they have no acoustic meaning.
    """

    source = str(text or "")
    output: list[str] = []
    index = 0
    while index < len(source):
        end = emoji_cluster_end(source, index)
        if end > index:
            if output and not output[-1].isspace():
                output.append(" ")
            index = end
            continue
        codepoint = ord(source[index])
        if (
            codepoint == EMOJI_ZWJ
            or codepoint in EMOJI_VARIATION_SELECTORS
            or EMOJI_SKIN_TONE_RANGE[0] <= codepoint <= EMOJI_SKIN_TONE_RANGE[1]
            or EMOJI_TAG_RANGE[0] <= codepoint <= EMOJI_TAG_RANGE[1]
            or codepoint == EMOJI_KEYCAP
        ):
            index += 1
            continue
        output.append(source[index])
        index += 1
    return re.sub(r"\s+", " ", "".join(output)).strip()


def normalize_technical_speech_text(text: str, locale: str = "ja-JP") -> str:
    """Remove unstable Latin/code switches from Japanese synthesis input.

    The visible reply and semantic Japanese script remain unchanged. This is
    only the text presented to the acoustic model, where identifiers such as
    ``run_amadeus.ps1`` otherwise become many alternating ja/en fragments and
    frequently collapse into a short phrase followed by long silence.
    """

    value = strip_emoji_for_speech(text)
    if not value or not str(locale or "").lower().startswith("ja"):
        return value

    value = re.sub(
        r"(?<![A-Za-z0-9])(\d[\d,，_]*)\s+(?:tests?\s+)?passed(?![A-Za-z])",
        lambda match: japanese_integer_text(match.group(1)) + "件合格",
        value,
        flags=re.I,
    )
    # A colon inside an ASCII product/edition name is typography, not a cue to
    # read both halves as separate acronyms (for example RE:BOOT).
    value = re.sub(
        r"[A-Za-z]+(?::[A-Za-z]+)+",
        lambda match: english_word_to_katakana(match.group(0).replace(":", "")),
        value,
    )
    value = re.sub(
        r"(?<=[A-Za-z0-9])\s*\|\s*(?=[A-Za-z0-9])",
        " または ",
        value,
    )

    def replace_file(match: re.Match[str]) -> str:
        base = match.group("base")
        extension = match.group("extension").casefold()
        spoken_base = re.sub(
            r"[A-Za-z][A-Za-z0-9+-]*|\d+",
            lambda token_match: japanese_technical_token(token_match.group(0)),
            base,
        )
        spoken_base = re.sub(r"[_/\\.-]+", " ", spoken_base)
        spoken_base = re.sub(r"\s+", " ", spoken_base).strip()
        extension_name = JAPANESE_FILE_EXTENSION_NAMES[extension]
        return f"{spoken_base}の{extension_name}" if spoken_base else extension_name

    extension_pattern = "|".join(
        sorted((re.escape(item) for item in JAPANESE_FILE_EXTENSION_NAMES), key=len, reverse=True)
    )
    value = re.sub(
        rf"(?P<base>[^\s、。，！？!?]{{1,100}})\.(?P<extension>{extension_pattern})(?![A-Za-z0-9])",
        replace_file,
        value,
        flags=re.I,
    )
    value = re.sub(
        r"[A-Za-z][A-Za-z0-9_+\-/]*|\d[\d,，_]*",
        lambda match: japanese_technical_token(match.group(0)),
        value,
    )
    value = re.sub(r"(?<=[ァ-ヶー])\.(?=[ぁ-んァ-ヶ一-龯])", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def project_leading_ellipsis_for_synthesis(text: str) -> tuple[str, str]:
    """Project a leading ellipsis into delivery prosody instead of phonemes.

    The caller-owned text and semantic-unit contract stay unchanged. Only the
    text sent to the acoustic frontend loses the initial ellipsis; the removed
    punctuation is returned so the delivery assembler can restore an explicit
    leading pause. Internal ellipses remain lexical input and punctuation-only
    units are left untouched for the normal preflight to reject.
    """

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    match = re.match(r"^(?P<prefix>(?:(?:…+|⋯+|\.{2,})\s*)+)", value)
    if not match:
        return value, ""
    projected = value[match.end():].lstrip()
    if not projected or not re.search(r"[^\W_]", projected, flags=re.UNICODE):
        return value, ""
    return projected, match.group("prefix").strip()


def visible_speech_chars(text: str) -> int:
    acoustic_text = strip_emoji_for_speech(text)
    return len(re.sub(r"[\s、。，．,.!?！？「」『』（）()…—–]", "", acoustic_text))


def inference_segment_seed(
    text: str,
    reference_audio: Path | str,
    style: str,
    intensity: str,
) -> int:
    """Return a deterministic seed owned by one acoustic inference segment.

    A later sentence must not change how an earlier sentence is sampled.  The
    old utterance-wide seed included the complete reply even though inference
    happened chunk by chunk, so appending unrelated text could turn the first
    chunk from valid speech into a reproducible silence/truncation failure.
    """

    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    reference_path = str(Path(reference_audio).resolve())
    seed_material = f"{normalized_text}|{reference_path}|{style}|{intensity}"
    return int.from_bytes(
        runtime_digest(seed_material, size=4),
        "big",
    ) % 900000


DEFAULT_PREFERRED_SYNTHESIS_CHARS = 40
DEFAULT_MAX_SYNTHESIS_CHARS = 40
MAX_RELAXED_JAPANESE_SYNTHESIS_CHARS = 72
DEFAULT_SHORT_ROUTE_MAX_CHARS = 32
SEMANTIC_BUDGET_MIN_REDUNDANCY_SECONDS = 1.5
SEMANTIC_BUDGET_REDUNDANCY_RATIO = 0.25
SEMANTIC_BUDGET_ABSOLUTE_MAX_SECONDS = 22.0


def load_tts_runtime_contract() -> dict[str, int]:
    """Load retry limits shared with the desktop capability handshake."""

    fallback = {
        "maxNormalAttempts": 3,
        "maxConservativeAttempts": 0,
    }
    path = PROJECT_ROOT / "configs" / "tts_runtime_contract.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    if not isinstance(raw, dict):
        return fallback
    result: dict[str, int] = {}
    for key, default in fallback.items():
        try:
            result[key] = max(0, int(raw.get(key, default)))
        except (TypeError, ValueError):
            result[key] = default
    result["maxNormalAttempts"] = max(1, result["maxNormalAttempts"])
    return result


TTS_RUNTIME_CONTRACT = load_tts_runtime_contract()
MAX_NORMAL_ATTEMPTS = TTS_RUNTIME_CONTRACT["maxNormalAttempts"]
MAX_CONSERVATIVE_ATTEMPTS = TTS_RUNTIME_CONTRACT["maxConservativeAttempts"]
SPEECH_PROSODY_PROFILES = {"compact", "natural", "deliberate"}


def normalize_speech_prosody(value: Any) -> str:
    candidate = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "compact": "compact",
        "tight": "compact",
        "紧凑": "compact",
        "緊湊": "compact",
        "natural": "natural",
        "normal": "natural",
        "自然": "natural",
        "deliberate": "deliberate",
        "relaxed": "deliberate",
        "舒缓": "deliberate",
        "舒緩": "deliberate",
    }
    return aliases.get(candidate, "natural")


def is_chinese_speech_locale(locale: str) -> bool:
    candidate = str(locale or "").strip().lower().replace("_", "-")
    return candidate.startswith("zh") or "中文" in candidate or "汉语" in candidate


def is_japanese_speech_locale(locale: str) -> bool:
    candidate = str(locale or "").strip().lower().replace("_", "-")
    return candidate.startswith("ja") or "日本語" in candidate


def chinese_prosody_clauses(text: str, prosody_profile: str = "natural") -> list[str]:
    """Split one semantic sentence at Mandarin phrase boundaries without loss.

    The GPT-SoVITS model receives each returned phrase independently, allowing
    AMADEUS to own the pause duration at punctuation instead of hoping that a
    Japanese-finetuned acoustic model infers Mandarin phrasing.  Enumeration
    commas (``、``) deliberately remain inside a phrase so lists do not become
    robotic word-by-word speech.
    """

    source = str(text or "")
    profile = normalize_speech_prosody(prosody_profile)
    # Mixed Chinese/Latin chunks are substantially less stable when decoded as
    # several independent prompts. Keep them whole and let GPT-SoVITS' mixed
    # frontend preserve cross-token context; correctness wins over an inserted
    # comma pause for technical speech.
    if (
        profile == "compact"
        or re.search(r"[A-Za-z]", source)
        or not re.search(r"[，,；;：:]", source)
    ):
        return [source] if source else []
    parts = [item for item in re.findall(r".*?(?:[，,；;：:]+|$)", source) if item]
    result: list[str] = []
    pending = ""
    for index, part in enumerate(parts):
        pending += part
        if not re.search(r"[，,；;：:]+$", pending):
            continue
        remaining = "".join(parts[index + 1 :])
        boundary_is_strong = bool(re.search(r"[；;：:]+$", pending))
        minimum_before = 1 if boundary_is_strong else 2
        if (
            visible_speech_chars(pending) >= minimum_before
            and visible_speech_chars(remaining) >= 3
        ):
            result.append(pending)
            pending = ""
    if pending:
        result.append(pending)
    if "".join(result) != source:
        return [source]
    return result


def semantic_speech_units(text: str) -> list[str]:
    """Return one atomic unit when the caller supplied no semantic contract.

    Semantic delivery boundaries belong to the model protocol, not to the TTS
    engine.  In particular, a period, question mark, or ellipsis may be an
    internal pause and must never create a new visible delivery unit here.
    Caller-supplied units are checked separately by
    :func:`validate_semantic_units`.
    """

    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned or not re.search(r"[^\W_]", cleaned, flags=re.UNICODE):
        return []
    return [cleaned]


def normalized_contract_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


def validate_semantic_units(text: str, units: Any = None) -> list[str]:
    """Validate caller-supplied units or keep the request atomic.

    The concatenated units must cover the complete requested text.  This stops
    a caller or retry path from silently dropping a difficult tail.
    """

    if units is None:
        result = semantic_speech_units(text)
    else:
        if not isinstance(units, list):
            raise ValueError("semanticUnits must be an array of strings")
        result = [re.sub(r"\s+", " ", str(item or "")).strip() for item in units]
        if not result or any(
            not item or not re.search(r"[^\W_]", item, flags=re.UNICODE)
            for item in result
        ):
            raise ValueError("semanticUnits contains an empty or punctuation-only unit")
    if normalized_contract_text("".join(result)) != normalized_contract_text(text):
        raise ValueError("semanticUnits do not cover the complete TTS text")
    if units is not None:
        incomplete = [
            index
            for index, item in enumerate(result, 1)
            if re.search(r"[、，,；;：:]\s*$", item)
        ]
        if incomplete:
            indexes = ",".join(str(item) for item in incomplete)
            raise ValueError(
                f"semanticUnits contains incomplete weak boundary at unit {indexes}"
            )
    return result


def rebalance_weak_inference_chunks(
    chunks: list[str],
    *,
    max_chars: int = DEFAULT_MAX_SYNTHESIS_CHARS,
) -> list[str]:
    """Move a dangling comma-clause behind the preceding complete sentence.

    This operates only inside one caller-authored semantic unit.  It never
    invents or merges visible speech units, and deliberately does not treat an
    ellipsis as a sentence boundary.  If the tail cannot be moved without
    exceeding the decoder budget, the original lossless plan is retained.
    """

    result = [str(item or "").strip() for item in chunks if str(item or "").strip()]
    index = 0
    while index + 1 < len(result):
        current = result[index]
        if not re.search(r"[、，,；;：:]\s*$", current):
            index += 1
            continue
        strong_ends = [match.end() for match in re.finditer(r"[。！？!?]", current)]
        if not strong_ends:
            index += 1
            continue
        split_at = strong_ends[-1]
        complete_prefix = current[:split_at]
        dangling_tail = current[split_at:]
        following = result[index + 1]
        if not complete_prefix or not dangling_tail:
            index += 1
            continue

        combined = dangling_tail + following
        if len(combined) <= max_chars:
            result[index] = complete_prefix
            result[index + 1] = combined
            index += 1
            continue

        available = max_chars - len(dangling_tail)
        candidate_ends = [
            match.end()
            for match in re.finditer(r"[。！？!?]", following)
            if match.end() <= available and match.end() < len(following)
        ]
        if not candidate_ends:
            index += 1
            continue
        following_split = candidate_ends[-1]
        result[index] = complete_prefix
        result[index + 1] = dangling_tail + following[:following_split]
        result.insert(index + 2, following[following_split:])
        index += 1

    return result


JAPANESE_INFERENCE_PUNCTUATION = "。！？!?：:、，,；;"
JAPANESE_STRONG_INFERENCE_END_RE = re.compile(r"[。！？!?]+\s*$")
JAPANESE_STRONG_INFERENCE_BOUNDARY_RE = re.compile(r"[。！？!?]+")
JAPANESE_WEAK_INFERENCE_BOUNDARY_RE = re.compile(r"[：:、，,；;]+")
JAPANESE_SAFE_TOKEN_BOUNDARY_RE = re.compile(
    r"(?:\s+|[」』）】〕〉》”’]+|[A-Za-z][A-Za-z0-9_+\-/]*|\d[\d,，_.-]*)"
)
MIN_JAPANESE_MULTI_SENTENCE_SPLIT_CHARS = 16
MIN_JAPANESE_ACOUSTIC_SENTENCE_CHARS = 4


def relaxed_japanese_inference_limit(preferred_chars: int) -> int:
    """Allow complete Japanese phrases 20% beyond the preferred target."""

    preferred = max(2, int(preferred_chars))
    return min(
        MAX_RELAXED_JAPANESE_SYNTHESIS_CHARS,
        max(preferred, math.ceil(preferred * 1.2)),
    )


def split_japanese_complete_sentences(text: str) -> list[str]:
    """Split a substantial multi-sentence unit at strong acoustic boundaries.

    This does not change the caller-owned visible or semantic-unit contract: the
    returned strings are internal decoder chunks and are stitched back into one
    WAV. Tiny reactions stay attached to a neighbour so inputs such as
    ``え？ そうね。`` are not turned into unstable one-token generations.
    """

    source = str(text or "")
    raw = [
        item for item in re.findall(r".*?(?:[。！？!?]+|$)", source) if item
    ]
    if (
        len(raw) < 2
        or visible_speech_chars(source) < MIN_JAPANESE_MULTI_SENTENCE_SPLIT_CHARS
    ):
        return [source] if source else []

    result: list[str] = []
    pending = ""
    for sentence in raw:
        pending += sentence
        if visible_speech_chars(pending) >= MIN_JAPANESE_ACOUSTIC_SENTENCE_CHARS:
            result.append(pending)
            pending = ""
    if pending:
        if result:
            result[-1] += pending
        else:
            result.append(pending)
    if (
        len(result) < 2
        or "".join(result) != source
        or any(
            visible_speech_chars(item) < MIN_JAPANESE_ACOUSTIC_SENTENCE_CHARS
            for item in result
        )
    ):
        return [source]
    return result


def _balanced_japanese_hard_split_point(text: str, max_chars: int) -> int:
    """Choose a lossless, balanced acoustic boundary below the hard ceiling."""

    hard_limit = max(2, min(int(max_chars), len(text) - 1))
    required_chunks = max(2, math.ceil(len(text) / max(2, int(max_chars))))
    ideal = max(1, math.ceil(len(text) / required_chunks))
    minimum_chunk = min(8, max(2, ideal // 3))
    minimum_end = minimum_chunk
    maximum_end = min(hard_limit, len(text) - minimum_chunk)
    remaining_capacity = (required_chunks - 1) * max(2, int(max_chars))
    minimum_end = max(minimum_end, len(text) - remaining_capacity)
    if maximum_end < minimum_end:
        return hard_limit

    def candidates(pattern: re.Pattern[str]) -> list[int]:
        return [
            match.end()
            for match in pattern.finditer(text)
            if minimum_end <= match.end() <= maximum_end
            and visible_speech_chars(text[:match.end()]) >= 4
            and visible_speech_chars(text[match.end():]) >= 4
        ]

    # Punctuation is an acoustic boundary even when it is not a visible/model
    # speech-unit boundary.  Prefer it over a grammar hint or a raw character
    # cut, while selecting the punctuation nearest the balanced target.
    for pattern in (
        JAPANESE_STRONG_INFERENCE_BOUNDARY_RE,
        JAPANESE_WEAK_INFERENCE_BOUNDARY_RE,
        JAPANESE_SAFE_TOKEN_BOUNDARY_RE,
    ):
        points = candidates(pattern)
        if points:
            return min(points, key=lambda value: (abs(value - ideal), value))
    return max(minimum_end, min(ideal, maximum_end))


def _split_long_japanese_inference_unit(text: str, max_chars: int) -> list[str]:
    """Split a unit above the relaxed ceiling without leaving a micro-tail."""

    hard_limit = max(2, int(max_chars))
    result: list[str] = []
    remaining = str(text or "")
    while len(remaining) > hard_limit:
        split_at = _balanced_japanese_hard_split_point(remaining, hard_limit)
        result.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        result.append(remaining)
    return result


def split_japanese_inference_phrase(
    text: str,
    max_chars: int,
    preferred_chars: int = DEFAULT_PREFERRED_SYNTHESIS_CHARS,
) -> list[str]:
    """Plan Japanese acoustic chunks with a soft target and relaxed ceiling.

    Complete sentences may exceed the 40-character preferred target up to the
    configured hard ceiling.  Above that ceiling, punctuation, Japanese phrase
    hints, and finally a balanced hard split are tried in that order.
    """

    source = str(text or "")
    hard_limit = max(2, int(max_chars))
    preferred_limit = min(max(2, int(preferred_chars)), hard_limit)
    if len(source) <= preferred_limit:
        return [source] if source else []

    raw_sentences = [
        item for item in re.findall(r".*?(?:[。！？!?]+|$)", source) if item
    ]
    sentences: list[str] = []
    pending_punctuation = ""
    for sentence in raw_sentences:
        if re.search(r"[^\W_]", sentence, flags=re.UNICODE):
            sentences.append(pending_punctuation + sentence)
            pending_punctuation = ""
        else:
            pending_punctuation += sentence
    if pending_punctuation:
        if sentences:
            sentences[-1] += pending_punctuation
        else:
            sentences = [source]

    result: list[str] = []
    pending = ""
    for sentence in sentences:
        if len(sentence) > hard_limit:
            if pending:
                result.append(pending)
                pending = ""
            result.extend(_split_long_japanese_inference_unit(sentence, hard_limit))
            continue
        if pending and len(pending) + len(sentence) > preferred_limit:
            result.append(pending)
            pending = sentence
        else:
            pending += sentence
    if pending:
        result.append(pending)
    if (
        "".join(result) != source
        or any(len(item) > hard_limit for item in result)
        or any(not re.search(r"[^\W_]", item, flags=re.UNICODE) for item in result)
    ):
        return _split_long_japanese_inference_unit(source, hard_limit)
    return result


def split_tts_segments(
    text: str,
    max_chars: int = DEFAULT_MAX_SYNTHESIS_CHARS,
    *,
    locale: str = "",
    prosody_profile: str = "natural",
) -> list[str]:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned or not re.search(r"[^\W_]", cleaned, flags=re.UNICODE):
        return []
    # This returns one unit unless the caller supplied an explicit model
    # contract.  Internal acoustic chunking below may still enforce a decoder
    # length budget, but it does not create visible semantic units.
    sentences = semantic_speech_units(cleaned)
    result: list[str] = []
    for sentence in sentences:
        phrase_candidates = (
            chinese_prosody_clauses(sentence, prosody_profile)
            if is_chinese_speech_locale(locale)
            else [sentence]
        )
        for phrase in phrase_candidates:
            if is_japanese_speech_locale(locale):
                relaxed_limit = relaxed_japanese_inference_limit(max_chars)
                for acoustic_sentence in split_japanese_complete_sentences(phrase):
                    if len(acoustic_sentence) <= max_chars:
                        result.append(acoustic_sentence)
                    else:
                        result.extend(split_japanese_inference_phrase(
                            acoustic_sentence,
                            relaxed_limit,
                            preferred_chars=max_chars,
                        ))
                continue
            if len(phrase) <= max_chars:
                result.append(phrase)
                continue
            phrase_chunks: list[str] = []
            clauses = [item for item in re.split(r"(?<=[、，,；;])", phrase) if item]
            current = ""
            for clause in clauses:
                if current and len(current) + len(clause) > max_chars:
                    phrase_chunks.append(current.strip())
                    current = ""
                while len(clause) > max_chars:
                    if current:
                        phrase_chunks.append(current.strip())
                        current = ""
                    phrase_chunks.append(clause[:max_chars].strip())
                    clause = clause[max_chars:]
                current += clause
            if current.strip():
                phrase_chunks.append(current.strip())
            result.extend(rebalance_weak_inference_chunks(
                phrase_chunks,
                max_chars=max_chars,
            ))

    return result


def synthesis_segments(
    text: str,
    max_chars: int = DEFAULT_MAX_SYNTHESIS_CHARS,
    segment_mode: str = "auto",
    semantic_units: Any = None,
    locale: str = "",
    prosody_profile: str = "natural",
) -> list[str]:
    """Plan inference chunks without changing the model-selected delivery count.

    ``joined`` may use several stable inference chunks, but synthesize() joins
    them into one WAV for the caller. ``whole`` remains available for targeted
    diagnostics where a single semantic decode is explicitly required.
    """

    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned or not re.search(r"[^\W_]", cleaned, flags=re.UNICODE):
        return []
    units = validate_semantic_units(cleaned, semantic_units)
    if str(segment_mode or "").strip().lower() == "whole":
        return units
    chunks: list[str] = []
    for unit in units:
        chunks.extend(split_tts_segments(
            unit,
            max_chars=max_chars,
            locale=locale,
            prosody_profile=prosody_profile,
        ))
    return chunks


def tts_resegmentation_enabled() -> bool:
    value = os.environ.get("AMADEUS_TTS_RESEGMENT_ON_FAILURE", "0")
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def tts_adaptive_retry_shadow_enabled() -> bool:
    value = os.environ.get("AMADEUS_TTS_ADAPTIVE_RETRY_SHADOW", "0")
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def tts_bounded_retry_enabled() -> bool:
    value = os.environ.get("AMADEUS_TTS_BOUNDED_RETRY", "0")
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def tts_high_confidence_resegment_enabled() -> bool:
    value = os.environ.get("AMADEUS_TTS_HIGH_CONFIDENCE_RESEGMENT", "0")
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def tts_runtime_capabilities() -> dict[str, Any]:
    bounded_retry = tts_bounded_retry_enabled()
    return {
        "resegmentOnFailure": tts_resegmentation_enabled(),
        "adaptiveRetryShadow": tts_adaptive_retry_shadow_enabled(),
        "boundedRetry": bounded_retry,
        "highConfidenceResegment": tts_high_confidence_resegment_enabled(),
        "coherentReferenceFallback": True,
        "maxNormalAttempts": MAX_NORMAL_ATTEMPTS,
        "maxConservativeAttempts": MAX_CONSERVATIVE_ATTEMPTS,
    }


def inference_chunk_has_weak_boundary(text: str) -> bool:
    """Detect chunks that end before their local thought is syntactically whole."""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return True
    return bool(re.search(
        r"(?:[、，,；;：:]|"
        r"(?:けど|けれど|ので|のに|から|なら|ても|では|そして|それで|"
        r"でも|また|つまり|だから|一方で))\s*$",
        value,
    ))


def stabilize_inference_segments(
    segments: list[str],
    *,
    max_chars: int = DEFAULT_MAX_SYNTHESIS_CHARS,
) -> list[str]:
    """Preserve planned semantic boundaries before acoustic inference.

    Older revisions merged chunks according to character counts.  That erased
    complete short speech acts and allowed the model to stop after the previous
    sentence.  Long units are already split within their own boundary by
    ``synthesis_segments``; no cross-boundary merge is valid here.
    """

    cleaned = [re.sub(r"\s+", " ", str(item or "")).strip() for item in segments]
    cleaned = [item for item in cleaned if item]
    return cleaned


def segment_pause_seconds(
    segment: str,
    speech_speed: float = 1.0,
    *,
    locale: str = "",
    prosody_profile: str = "natural",
) -> float:
    """Return a natural inter-segment pause while preserving the requested pace."""
    text = str(segment or "").rstrip()
    chinese = is_chinese_speech_locale(locale)
    profile = normalize_speech_prosody(prosody_profile)
    if chinese:
        if re.search(r"…{2,}$|\.{3,}$", text):
            base = 0.48
        elif re.search(r"[！？!?]+$", text):
            base = 0.40
        elif re.search(r"[。．]+$", text):
            base = 0.36
        elif re.search(r"[；;：:]+$", text):
            base = 0.29
        elif re.search(r"[，,]+$", text):
            base = 0.22
        elif re.search(r"[、]+$", text):
            base = 0.14
        else:
            base = 0.14
        base *= {"compact": 0.78, "natural": 1.0, "deliberate": 1.28}[profile]
        maximum = 0.68
    else:
        if re.search(r"…{2,}$|\.{3,}$", text):
            base = 0.48
        elif re.search(r"[！？!?]+$", text):
            base = 0.40
        elif re.search(r"[。．]+$", text):
            base = 0.36
        elif re.search(r"[、，,；;：:]+$", text):
            base = 0.17
        else:
            base = 0.13
        maximum = 0.58
    speed = max(0.62, min(1.45, float(speech_speed or 1.0)))
    return max(0.1, min(maximum, base / (speed ** 0.45)))


def segment_timing_bounds(visible_chars: int, speech_speed: float = 1.0) -> tuple[float, float, float, int]:
    """Return expected/min/max waveform seconds and a padded semantic budget.

    ``maximum`` remains the quality-gate boundary: audio beyond it is suspicious.
    The semantic decoder must have more room than that boundary, otherwise a
    naturally slow but complete sentence can be cut before EOS. Keep at least
    1.5 seconds and roughly 25% of expected delivery as decoder-only headroom,
    with a final absolute guard against an unbounded non-EOS token stream.
    """
    speed = max(0.62, min(1.45, float(speech_speed or 1.0)))
    char_count = max(1, int(visible_chars))
    expected = (char_count * 0.22 + 0.6) / speed
    maximum = min(18.0, max(4.5, expected * 1.9))
    # Very short Japanese reactions such as "論文は？" commonly contain only
    # about 0.5 seconds of active speech after edge silence is compacted. Keep
    # the stricter floor for normal sentences so incomplete long generations
    # are still rejected, without turning a valid short final clause into a
    # failure for the entire utterance.
    minimum_floor = 0.22 if char_count <= 2 else 0.50 if char_count <= 4 else 0.62
    # The estimate deliberately treats written characters conservatively, but
    # mixed Japanese/Latin text is often spoken much faster than that estimate
    # (for example "ITVX" or "World Superbike"). A 35% lower bound still
    # rejects half-rendered normal sentences while accepting complete compact
    # speech after silence removal.
    # Compact Japanese questions around 5-10 visible characters can be fully
    # spoken in roughly 0.7 seconds. Keep the stricter guard for longer prose,
    # where a short waveform is much more likely to be a truncated sentence.
    minimum_ratio = 0.29 if visible_chars <= 10 else 0.35
    minimum = min(maximum - 0.35, max(minimum_floor, expected * minimum_ratio))
    decoder_headroom = max(
        SEMANTIC_BUDGET_MIN_REDUNDANCY_SECONDS,
        expected * SEMANTIC_BUDGET_REDUNDANCY_RATIO,
    )
    semantic_budget = max(
        6,
        int(min(
            SEMANTIC_BUDGET_ABSOLUTE_MAX_SECONDS,
            maximum + decoder_headroom,
        ) + 0.999),
    )
    return expected, minimum, maximum, semantic_budget


def segment_acceptance_minimum(visible_chars: int, calculated_minimum: float) -> float:
    """Allow a narrow duration tolerance for complete short Japanese phrases.

    Silence compaction can leave a valid phrase such as ``こんにちは。`` or
    ``わかった。`` a few frames below the conservative estimate. Waveform
    validity and active-speech completeness are still checked independently;
    normal sentences retain the full truncation guard.
    """

    minimum = max(0.0, float(calculated_minimum))
    characters = int(visible_chars)
    if 1 <= characters <= 2:
        # One- and two-mora acknowledgements (for example ``ん。`` and
        # ``うん。``) can be complete in about 160-230 ms after edge-silence
        # compaction.  Duration alone must not reject them when the independent
        # validity, completeness, artifact and audibility guards all pass.
        return max(0.16, minimum * 0.72)
    if 3 <= characters <= 6:
        return minimum * 0.80
    if 7 <= characters <= 10:
        return minimum * 0.90
    return minimum


def segment_completeness_metrics(quality: dict[str, Any], visible_chars: int) -> dict[str, Any]:
    """Detect clean-looking waveforms that contain only part of the text.

    GPT semantic decoding can emit a short phrase followed by many seconds of
    silence. Silence compaction makes the total duration look plausible while
    the requested sentence is still missing. Require enough active speech for
    the character count. Removed silence remains a diagnostic: once speech is
    complete, a long decoded gap is safe to compact and must not cause a false
    rejection by itself unless it exceeds the explicit decoded-silence bound.
    """

    characters = max(1, int(visible_chars))
    active_ms = max(0, int(quality.get("activeSpeechMs") or 0))
    active_ratio = max(0.0, float(quality.get("activeRatio") or 0.0))
    compacted_ms = max(0, int(quality.get("compactedSilenceMs") or 0))
    if characters <= 2:
        minimum_ms_per_character, minimum_active_ratio = 55.0, 0.18
    elif characters <= 4:
        minimum_ms_per_character, minimum_active_ratio = 52.0, 0.25
    elif characters <= 8:
        minimum_ms_per_character, minimum_active_ratio = 50.0, 0.32
    elif characters <= 12:
        minimum_ms_per_character, minimum_active_ratio = 70.0, 0.45
    else:
        minimum_ms_per_character, minimum_active_ratio = 82.0, 0.55
    calculated_minimum_active_ms = max(160, int(round(characters * minimum_ms_per_character)))
    # Written kana are not fixed-duration acoustic units. Fast, clean product
    # names can finish well below the prose estimate while still containing
    # every mora. Grant the wider tolerance only when speech occupies most of
    # the compacted waveform and there is no hidden decoded silence; partial
    # speech followed by a long gap keeps the strict threshold.
    clean_compact_delivery = bool(
        active_ratio >= max(0.72, minimum_active_ratio)
        and compacted_ms <= max(180, int(round(active_ms * 0.20)))
    )
    active_tolerance = 0.84 if clean_compact_delivery else 0.95
    minimum_active_ms = max(160, int(round(calculated_minimum_active_ms * active_tolerance)))
    maximum_compacted_ms = max(1500, int(round(active_ms * 0.75)))
    active_ms_per_character = active_ms / characters
    complete = active_ms >= minimum_active_ms and active_ratio >= minimum_active_ratio
    return {
        "completeSpeech": bool(complete),
        "excessiveCompactedSilence": bool(compacted_ms > maximum_compacted_ms),
        "activeMsPerCharacter": round(active_ms_per_character, 2),
        "minimumActiveSpeechMs": minimum_active_ms,
        "calculatedMinimumActiveSpeechMs": calculated_minimum_active_ms,
        "cleanCompactDelivery": clean_compact_delivery,
        "activeSpeechTolerance": active_tolerance,
        "minimumActiveRatio": minimum_active_ratio,
        "maximumCompactedSilenceMs": maximum_compacted_ms,
    }


def segment_audibility_metrics(audio: Any, sample_rate: int) -> dict[str, Any]:
    """Reject speech-shaped waveforms that are effectively inaudible.

    Duration, silence and spectral checks can all pass when GPT-SoVITS emits a
    very low-level fragment.  Such a fragment used to be accepted and then the
    conservative +/-3 dB level matcher could not make it audible beside the
    other units.  Measure active speech itself so the normal bounded retry can
    generate a usable unit instead of shipping a nearly silent one.
    """

    import numpy as np

    rms = max(1e-8, float(active_voice_rms(audio, sample_rate)))
    rms_db = float(20.0 * np.log10(rms))
    minimum_db = -32.0
    return {
        "activeVoiceRmsDb": round(rms_db, 2),
        "minimumActiveVoiceRmsDb": minimum_db,
        "audibleSpeech": bool(rms_db >= minimum_db),
    }


def segment_quality_gate(
    quality: dict[str, Any],
    *,
    compacted_duration: float,
    acceptance_minimum: float,
    maximum_duration: float,
    upper_margin: float = 0.2,
) -> dict[str, Any]:
    """Classify a waveform as pass, bounded gray-pass, or fail.

    The confidence value is an observable heuristic, not a calibrated
    probability.  A gray pass is allowed only when every correctness and
    waveform guard passes and the sole violation is a very small decoded-
    silence overrun.  Incomplete speech never enters the gray zone.
    """

    duration = max(0.0, float(compacted_duration or 0.0))
    minimum_duration = max(0.0, float(acceptance_minimum or 0.0))
    upper_bound = max(
        minimum_duration,
        float(maximum_duration or 0.0) - max(0.0, float(upper_margin or 0.0)),
    )
    active_ms = max(0, int(quality.get("activeSpeechMs") or 0))
    minimum_active_ms = max(1, int(quality.get("minimumActiveSpeechMs") or 1))
    active_ratio = max(0.0, float(quality.get("activeRatio") or 0.0))
    minimum_active_ratio = max(0.0, float(quality.get("minimumActiveRatio") or 0.0))
    compacted_silence_ms = max(0, int(quality.get("compactedSilenceMs") or 0))
    maximum_compacted_silence_ms = max(
        1,
        int(quality.get("maximumCompactedSilenceMs") or 1500),
    )
    silence_overrun_ms = max(
        0,
        compacted_silence_ms - maximum_compacted_silence_ms,
    )
    silence_overrun_ratio = silence_overrun_ms / maximum_compacted_silence_ms
    duration_ok = minimum_duration <= duration < upper_bound
    correctness_guards_pass = bool(
        duration_ok
        and quality.get("validSpeech")
        and quality.get("artifactSafe", True)
        and quality.get("completeSpeech")
        and quality.get("audibleSpeech", True)
    )
    common = {
        "qualityGateVersion": "three-tier-v1",
        "qualityConfidenceCalibrated": False,
        "silenceOverrunMs": silence_overrun_ms,
        "silenceOverrunRatio": round(silence_overrun_ratio, 4),
    }
    if correctness_guards_pass and silence_overrun_ms == 0:
        return {
            **common,
            "qualityTier": "pass",
            "qualityConfidence": 0.99,
            "qualityGateReason": "all_guards_passed",
        }

    active_margin_pass = active_ms >= round(minimum_active_ms * 1.05)
    active_ratio_margin_pass = active_ratio >= minimum_active_ratio + 0.05
    gray_silence_pass = bool(
        correctness_guards_pass
        and silence_overrun_ms <= 100
        and silence_overrun_ratio <= 0.05
        and active_margin_pass
        and active_ratio_margin_pass
    )
    if gray_silence_pass:
        boundary_fraction = max(
            silence_overrun_ms / 100.0,
            silence_overrun_ratio / 0.05,
        )
        return {
            **common,
            "qualityTier": "gray_pass",
            "qualityConfidence": round(max(0.80, 0.98 - 0.15 * boundary_fraction), 3),
            "qualityGateReason": "borderline_decoded_silence",
        }

    failed_guards: list[str] = []
    if not duration_ok:
        failed_guards.append("duration")
    if not quality.get("validSpeech"):
        failed_guards.append("valid_speech")
    if not quality.get("artifactSafe", True):
        failed_guards.append("artifact")
    if not quality.get("completeSpeech"):
        failed_guards.append("complete_speech")
    if not quality.get("audibleSpeech", True):
        failed_guards.append("audibility")
    if silence_overrun_ms:
        failed_guards.append("decoded_silence")
    return {
        **common,
        "qualityTier": "fail",
        "qualityConfidence": 0.0,
        "qualityGateReason": "+".join(failed_guards) or "quality_guard",
    }


def classify_tts_attempt_failure(
    quality: dict[str, Any],
    *,
    segment_text: str,
    compacted_duration: float,
    acceptance_minimum: float,
    maximum_duration: float,
) -> tuple[str, bool]:
    """Classify a rejected waveform and identify safe early resegmentation."""

    active_ms = max(0, int(quality.get("activeSpeechMs") or 0))
    minimum_active_ms = max(1, int(quality.get("minimumActiveSpeechMs") or 1))
    active_fraction = active_ms / minimum_active_ms
    excessive_silence = bool(
        quality.get("excessiveCompactedSilence")
        or int(quality.get("compactedSilenceMs") or 0)
        > int(quality.get("maximumCompactedSilenceMs") or 1500)
    )
    incomplete = not bool(quality.get("completeSpeech"))
    weak_boundary = inference_chunk_has_weak_boundary(segment_text)
    if quality.get("semanticLimitReached"):
        return "semantic_runaway", True
    if not quality.get("validSpeech") or not quality.get("artifactSafe", True):
        return "waveform_invalid", False
    if incomplete and excessive_silence and active_fraction < 0.65:
        return "excessive_silence", True
    if incomplete and active_fraction < 0.48:
        return "truncation", True
    if incomplete and weak_boundary:
        return "boundary_failure", True
    if compacted_duration >= maximum_duration - 0.05:
        return "excessive_duration", bool(incomplete)
    if compacted_duration < acceptance_minimum or incomplete:
        return "random_sampling_failure", False
    if not quality.get("audibleSpeech", True):
        return "inaudible_speech", False
    return "quality_gate_rejection", False


def adaptive_retry_recommendation(
    failure_type: str,
    *,
    high_confidence_boundary_failure: bool,
    has_following_chunk: bool,
) -> str:
    if high_confidence_boundary_failure:
        return "coherent_reference_fallback"
    if failure_type in {"boundary_failure", "truncation", "excessive_silence"}:
        return "stable_parameters"
    if failure_type == "random_sampling_failure":
        return "same_boundary_stable_retry"
    return "conservative_retry"


def tts_attempt_config_hash(
    segment: str,
    *,
    boundary_version: int,
    top_p: float,
    temperature: float,
    reference_audio: Path,
) -> str:
    material = json.dumps({
        "segment": str(segment or ""),
        "boundaryVersion": int(boundary_version),
        "topP": round(float(top_p), 4),
        "temperature": round(float(temperature), 4),
        "referenceAudio": str(reference_audio),
    }, ensure_ascii=False, sort_keys=True)
    return runtime_fingerprint(material)


def near_complete_compact_candidate(
    candidate: tuple[Any, ...],
    acceptance_minimum: float,
    maximum_duration: float,
) -> bool:
    """Keep a clean near-threshold decode instead of discarding the whole utterance.

    The strict completeness estimate is deliberately conservative. Long joined
    replies used to lose every already generated chunk when one fast, compact
    list or technical phrase missed that estimate by only a few frames. This
    recovery remains bounded to artifact-safe, speech-dense audio that satisfies
    the duration guard and reaches at least 90% of the conservative active-speech
    threshold. Partial output followed by decoded silence is still rejected.
    """

    duration, _sample_rate, _audio, quality = candidate[:4]
    active_ms = max(0, int(quality.get("activeSpeechMs") or 0))
    minimum_active_ms = int(quality.get("minimumActiveSpeechMs") or 0)
    return bool(
        minimum_active_ms > 0
        and float(acceptance_minimum) <= float(duration) < float(maximum_duration) - 0.05
        and quality.get("validSpeech")
        and quality.get("artifactSafe")
        and quality.get("audibleSpeech", True)
        and quality.get("cleanCompactDelivery")
        and not quality.get("semanticLimitReached")
        and not quality.get("excessiveCompactedSilence")
        and active_ms >= round(minimum_active_ms * 0.90)
    )


def pcm_to_float32(audio: Any) -> Any:
    """Convert GPT-SoVITS integer PCM to normalized float audio."""
    import numpy as np

    source = np.asarray(audio)
    if np.issubdtype(source.dtype, np.integer):
        info = np.iinfo(source.dtype)
        scale = float(max(abs(info.min), info.max))
        normalized = source.astype(np.float32) / scale
    else:
        normalized = source.astype(np.float32)
        peak = float(np.nanmax(np.abs(normalized))) if normalized.size else 0.0
        # Some forks return integer-scale PCM in a float container.
        if peak > 1.25:
            normalized /= 32768.0 if peak <= 32768.0 * 1.1 else peak
    return np.nan_to_num(normalized, copy=False, nan=0.0, posinf=1.0, neginf=-1.0).clip(-1.0, 1.0)


def fade_segment_edges(audio: Any, sample_rate: int, fade_ms: float = 4.0) -> Any:
    import numpy as np

    result = np.asarray(audio, dtype=np.float32).copy()
    fade_samples = min(len(result) // 2, max(0, int(sample_rate * fade_ms / 1000.0)))
    if fade_samples:
        ramp = np.linspace(0.0, 1.0, fade_samples, endpoint=False, dtype=np.float32)
        result[:fade_samples] *= ramp
        result[-fade_samples:] *= ramp[::-1]
    return result


def active_voice_rms(audio: Any, sample_rate: int) -> float:
    """Measure speech energy while ignoring pauses and edge silence."""
    import numpy as np

    source = np.asarray(audio, dtype=np.float32)
    frame_size = max(1, int(sample_rate * 0.02))
    frame_count = len(source) // frame_size
    if frame_count < 1:
        return float(np.sqrt(np.mean(np.square(source)) + 1e-10)) if len(source) else 0.0
    frames = source[:frame_count * frame_size].reshape(frame_count, frame_size)
    rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-10)
    peak_rms = float(np.percentile(rms, 95))
    floor_rms = float(np.percentile(rms, 15))
    active = rms[rms >= max(0.00045, floor_rms * 2.2, peak_rms * 0.08)]
    return float(np.sqrt(np.mean(np.square(active)) + 1e-10)) if len(active) else peak_rms


def audio_artifact_metrics(audio: Any, sample_rate: int) -> dict[str, Any]:
    """Detect obvious synthesis noise without rejecting normal unvoiced consonants."""
    import numpy as np

    source = np.asarray(audio, dtype=np.float32)
    if not len(source) or sample_rate <= 0:
        return {
            "artifactSafe": False,
            "clippingRatio": 0.0,
            "dcOffset": 0.0,
            "highFrequencyRatio": 0.0,
            "spectralFlatness": 1.0,
        }
    dc_offset = float(abs(np.mean(source)))
    centered = source - np.mean(source)
    clipping_ratio = float(np.mean(np.abs(centered) >= 0.995))
    frame_size = max(256, int(sample_rate * 0.032))
    frame_count = len(centered) // frame_size
    if frame_count < 1:
        peak = float(np.max(np.abs(centered)))
        return {
            "artifactSafe": bool(peak >= 0.003 and clipping_ratio <= 0.003 and dc_offset <= 0.02),
            "clippingRatio": round(clipping_ratio, 8),
            "dcOffset": round(dc_offset, 8),
            "highFrequencyRatio": 0.0,
            "spectralFlatness": 0.0,
        }
    frames = centered[:frame_count * frame_size].reshape(frame_count, frame_size)
    frame_rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
    threshold = max(0.00045, float(np.percentile(frame_rms, 15)) * 2.4, float(np.percentile(frame_rms, 95)) * 0.045)
    active_frames = frames[frame_rms >= threshold]
    analysis_frames = active_frames if len(active_frames) else frames
    high_frequency: list[float] = []
    flatness: list[float] = []
    window = np.hanning(frame_size).astype(np.float32)
    frequencies = np.fft.rfftfreq(frame_size, d=1.0 / sample_rate)
    for frame in analysis_frames:
        power = np.square(np.abs(np.fft.rfft(frame * window))) + 1e-12
        if sample_rate > 18000:
            high_frequency.append(float(np.sum(power[frequencies >= 8000]) / np.sum(power)))
        flatness.append(float(np.exp(np.mean(np.log(power))) / np.mean(power)))
    high_frequency_ratio = float(np.median(high_frequency)) if high_frequency else 0.0
    spectral_flatness = float(np.median(flatness)) if flatness else 0.0
    artifact_safe = (
        clipping_ratio <= 0.003
        and dc_offset <= 0.02
        and high_frequency_ratio <= 0.08
        and spectral_flatness <= 0.18
    )
    return {
        "artifactSafe": bool(artifact_safe),
        "clippingRatio": round(clipping_ratio, 8),
        "dcOffset": round(dc_offset, 8),
        "highFrequencyRatio": round(high_frequency_ratio, 6),
        "spectralFlatness": round(spectral_flatness, 6),
    }


def audio_delivery_metrics(
    audio: Any,
    sample_rate: int,
    *,
    boundary_samples: list[int] | tuple[int, ...] = (),
    reference_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure delivery-stage hum, broadband noise, joins, and headroom.

    These diagnostics locate a defect; they do not denoise or notch the signal.
    That distinction matters because a filter can hide evidence and alter the
    trained voice before the earliest noisy stage has been identified.
    """

    import numpy as np

    source = np.asarray(audio, dtype=np.float32)
    if source.ndim > 1:
        source = np.mean(source, axis=1, dtype=np.float32)
    base = audio_artifact_metrics(source, sample_rate)
    if not len(source) or sample_rate <= 0:
        return {
            **base,
            "sampleRate": int(sample_rate or 0),
            "channels": 1,
            "dtype": "float32",
            "durationSec": 0.0,
            "peak": 0.0,
            "rms": 0.0,
            "activeRatio": 0.0,
            "humPeakRatio": 0.0,
            "highBandNoiseRatio": 0.0,
            "silenceSpectralFlatness": 0.0,
            "boundaryJumpScore": 0.0,
            "referenceOutputNoiseDelta": {},
        }

    centered = source - float(np.mean(source))
    peak = float(np.max(np.abs(source)))
    rms = float(np.sqrt(np.mean(np.square(source)) + 1e-12))
    frame_size = max(256, int(sample_rate * 0.032))
    frame_count = len(centered) // frame_size
    active_ratio = 1.0
    silence_flatness = 0.0
    high_band_noise = 0.0
    if frame_count:
        frames = centered[:frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-12)
        low = float(np.percentile(frame_rms, 15))
        high = float(np.percentile(frame_rms, 95))
        active_threshold = max(0.00045, low * 2.2, high * 0.06)
        active_mask = frame_rms >= active_threshold
        active_ratio = float(np.mean(active_mask))
        silence_frames = frames[~active_mask]
        if len(silence_frames):
            window = np.hanning(frame_size).astype(np.float32)
            frequencies = np.fft.rfftfreq(frame_size, d=1.0 / sample_rate)
            flatness_values: list[float] = []
            high_values: list[float] = []
            high_start = min(8000.0, sample_rate * 0.36)
            for frame in silence_frames:
                power = np.square(np.abs(np.fft.rfft(frame * window))) + 1e-12
                total = float(np.sum(power))
                flatness_values.append(float(np.exp(np.mean(np.log(power))) / np.mean(power)))
                if total > 0 and sample_rate >= 12000:
                    high_values.append(float(np.sum(power[frequencies >= high_start]) / total))
            silence_flatness = float(np.median(flatness_values)) if flatness_values else 0.0
            high_band_noise = float(np.median(high_values)) if high_values else 0.0

    analysis_length = min(len(centered), max(1024, int(sample_rate * 4.0)))
    analysis = centered[:analysis_length]
    fft_size = 1
    while fft_size < analysis_length:
        fft_size *= 2
    spectrum = np.square(np.abs(np.fft.rfft(
        analysis * np.hanning(analysis_length),
        n=fft_size,
    ))) + 1e-12
    frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
    audible = frequencies >= 20.0
    total_power = float(np.sum(spectrum[audible])) or 1e-12
    hum_families: list[float] = []
    for fundamental in (50.0, 60.0):
        family_power = 0.0
        for harmonic in range(1, 7):
            center = fundamental * harmonic
            if center >= sample_rate / 2:
                break
            band = np.abs(frequencies - center) <= max(1.5, sample_rate / fft_size * 1.5)
            family_power += float(np.sum(spectrum[band]))
        hum_families.append(family_power / total_power)
    hum_peak_ratio = max(hum_families, default=0.0)

    active_level = max(1e-5, active_voice_rms(source, sample_rate))
    jumps = [
        abs(float(source[index]) - float(source[index - 1])) / active_level
        for index in boundary_samples
        if 0 < int(index) < len(source)
    ]
    boundary_jump = max(jumps, default=0.0)
    delta: dict[str, float] = {}
    if isinstance(reference_metrics, dict):
        for key in (
            "humPeakRatio",
            "highBandNoiseRatio",
            "silenceSpectralFlatness",
            "dcOffset",
            "clippingRatio",
        ):
            if key in reference_metrics:
                delta[key] = round(float({
                    "humPeakRatio": hum_peak_ratio,
                    "highBandNoiseRatio": high_band_noise,
                    "silenceSpectralFlatness": silence_flatness,
                    "dcOffset": base.get("dcOffset", 0.0),
                    "clippingRatio": base.get("clippingRatio", 0.0),
                }[key]) - float(reference_metrics.get(key) or 0.0), 8)
    return {
        **base,
        "sampleRate": int(sample_rate),
        "channels": 1,
        "dtype": "float32",
        "durationSec": round(len(source) / sample_rate, 6),
        "peak": round(peak, 8),
        "rms": round(rms, 8),
        "activeRatio": round(active_ratio, 6),
        "humPeakRatio": round(hum_peak_ratio, 8),
        "highBandNoiseRatio": round(high_band_noise, 8),
        "silenceSpectralFlatness": round(silence_flatness, 8),
        "boundaryJumpScore": round(boundary_jump, 6),
        "referenceOutputNoiseDelta": delta,
    }


def audio_noise_flags(metrics: dict[str, Any]) -> tuple[str, ...]:
    """Conservative diagnostic flags; a flag is not an automatic filter order."""

    checks = (
        ("clipping", float(metrics.get("clippingRatio") or 0.0) > 0.003),
        ("dc_offset", abs(float(metrics.get("dcOffset") or 0.0)) > 0.02),
        ("hum", float(metrics.get("humPeakRatio") or 0.0) > 0.16),
        ("high_band_noise", float(metrics.get("highBandNoiseRatio") or 0.0) > 0.14),
        ("boundary_jump", float(metrics.get("boundaryJumpScore") or 0.0) > 2.5),
    )
    return tuple(name for name, failed in checks if failed)


def stitch_audio_segments(
    segments: list[tuple[int, Any]],
    pauses_seconds: list[float],
    *,
    crossfade_ms: float = 10.0,
) -> tuple[Any, list[int], list[int]]:
    """Stitch float32 chunks and expose every join for click diagnostics."""

    import numpy as np

    if not segments:
        return np.zeros(0, dtype=np.float32), [], []
    sample_rate = segments[0][0]
    output = np.asarray(segments[0][1], dtype=np.float32).copy()
    boundaries: list[int] = []
    pause_ms: list[int] = []
    for index, (rate, audio) in enumerate(segments[1:], 1):
        if rate != sample_rate:
            raise ValueError("cannot stitch inconsistent sample rates")
        next_audio = np.asarray(audio, dtype=np.float32)
        pause = max(0.0, float(pauses_seconds[index - 1] if index - 1 < len(pauses_seconds) else 0.0))
        if pause > 0:
            fade_samples = min(
                len(output),
                len(next_audio),
                max(0, int(sample_rate * max(0.0, crossfade_ms) / 1000.0)),
            )
            if fade_samples:
                output[-fade_samples:] *= np.linspace(
                    1.0, 0.0, fade_samples, endpoint=True, dtype=np.float32
                )
                next_audio = next_audio.copy()
                next_audio[:fade_samples] *= np.linspace(
                    0.0, 1.0, fade_samples, endpoint=True, dtype=np.float32
                )
            pause_samples = max(1, int(sample_rate * pause))
            boundaries.extend((len(output), len(output) + pause_samples))
            output = np.concatenate((output, np.zeros(pause_samples, dtype=np.float32), next_audio))
            pause_ms.append(round(pause * 1000))
            continue
        overlap = min(
            len(output),
            len(next_audio),
            max(0, int(sample_rate * max(0.0, crossfade_ms) / 1000.0)),
        )
        boundary = len(output)
        if overlap:
            fade_out = np.linspace(1.0, 0.0, overlap, endpoint=False, dtype=np.float32)
            fade_in = 1.0 - fade_out
            mixed = output[-overlap:] * fade_out + next_audio[:overlap] * fade_in
            output = np.concatenate((output[:-overlap], mixed, next_audio[overlap:]))
            boundary -= overlap // 2
        else:
            output = np.concatenate((output, next_audio))
        boundaries.append(boundary)
        pause_ms.append(0)
    return output, boundaries, pause_ms


def join_audio_segments_legacy(
    segments: list[tuple[int, Any]],
    pauses_seconds: list[float],
) -> tuple[Any, list[int], list[int]]:
    """Reproduce the pre-governance concatenation path for A/B listening."""

    import numpy as np

    if not segments:
        return np.zeros(0, dtype=np.float32), [], []
    sample_rate = segments[0][0]
    joined: list[Any] = []
    boundaries: list[int] = []
    pause_ms: list[int] = []
    length = 0
    for index, (rate, segment_audio) in enumerate(segments):
        if rate != sample_rate:
            raise ValueError("cannot join inconsistent sample rates")
        if index:
            pause = max(0.0, float(
                pauses_seconds[index - 1]
                if index - 1 < len(pauses_seconds)
                else 0.0
            ))
            pause_samples = max(1, int(sample_rate * pause)) if pause > 0 else 0
            boundaries.append(length)
            if pause_samples:
                joined.append(np.zeros(pause_samples, dtype=np.float32))
                length += pause_samples
                boundaries.append(length)
            pause_ms.append(round(pause * 1000))
        audio = np.asarray(segment_audio, dtype=np.float32)
        joined.append(audio)
        length += len(audio)
    return np.concatenate(joined), boundaries, pause_ms


def assemble_delivery_audio(
    segments: list[tuple[int, Any]],
    pauses_seconds: list[float],
    *,
    post_processing_enabled: bool,
) -> tuple[Any, Any, list[int], list[int], int, float, str]:
    """Choose between the prior A/B path and the protected delivery path."""

    if not post_processing_enabled:
        audio, boundaries, pauses_ms = join_audio_segments_legacy(segments, pauses_seconds)
        return audio, audio, boundaries, pauses_ms, 0, 0.0, "legacy_concat"
    stitched, boundaries, pauses_ms = stitch_audio_segments(segments, pauses_seconds)
    sample_rate = segments[0][0] if segments else 0
    audio = add_protected_post_roll(stitched, sample_rate, post_roll_ms=140)
    audio, peak_protection_db = peak_protect(audio)
    return audio, stitched, boundaries, pauses_ms, 140, peak_protection_db, "protected_crossfade"


def add_protected_post_roll(audio: Any, sample_rate: int, post_roll_ms: int = 140) -> Any:
    """Append delivery silence after all speech processing and fades."""

    import numpy as np

    source = np.asarray(audio, dtype=np.float32)
    samples = max(0, int(sample_rate * max(0, int(post_roll_ms)) / 1000.0))
    if not samples:
        return source
    return np.concatenate((source, np.zeros(samples, dtype=np.float32)))


def peak_protect(audio: Any, ceiling: float = 0.985) -> tuple[Any, float]:
    import numpy as np

    source = np.asarray(audio, dtype=np.float32)
    peak = float(np.max(np.abs(source))) if len(source) else 0.0
    if peak <= max(0.1, float(ceiling)):
        return source, 0.0
    gain = max(0.1, float(ceiling)) / peak
    return source * gain, round(20.0 * np.log10(gain), 4)


def write_tts_diagnostic_stage(
    root: Path | None,
    trace_id: str,
    filename: str,
    audio: Any,
    sample_rate: int,
    *,
    final_delivery: bool = False,
) -> str:
    """Write an opt-in stage capture; normal synthesis performs no extra IO."""

    if root is None:
        return ""
    safe_trace = re.sub(r"[^A-Za-z0-9._-]", "_", str(trace_id or "trace"))[:100]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(filename or "stage.wav"))[:120]
    trace_dir = root.resolve() / safe_trace
    trace_dir.mkdir(parents=True, exist_ok=True)
    target = trace_dir / safe_name
    sf.write(
        target,
        audio,
        sample_rate,
        format="WAV",
        subtype="PCM_16" if final_delivery else "FLOAT",
    )
    return str(target)


def write_delivery_wav(output: Any, audio: Any, sample_rate: int) -> None:
    """Quantize once while remaining compatible with older soundfile shims."""

    try:
        sf.write(output, audio, sample_rate, format="WAV", subtype="PCM_16")
    except TypeError as error:
        # Older embedded soundfile builds and narrow unit-test adapters expose
        # the historical four-argument signature. WAV defaults to PCM_16 there.
        if "subtype" not in str(error):
            raise
        sf.write(output, audio, sample_rate, format="WAV")


def normalize_segment_levels(generated_segments: list[tuple[int, Any]]) -> tuple[list[tuple[int, Any]], list[dict[str, float]]]:
    """Match active-speech loudness across one utterance without flattening expression."""
    import numpy as np

    if len(generated_segments) < 2:
        return generated_segments, [{"gainDb": 0.0} for _item in generated_segments]
    levels = [max(1e-6, active_voice_rms(audio, rate)) for rate, audio in generated_segments]
    target_db = float(np.median([20.0 * np.log10(level) for level in levels]))
    normalized: list[tuple[int, Any]] = []
    diagnostics: list[dict[str, float]] = []
    for (rate, audio), level in zip(generated_segments, levels):
        before_db = float(20.0 * np.log10(level))
        gain_db = float(np.clip(target_db - before_db, -3.0, 3.0))
        gain = float(10.0 ** (gain_db / 20.0))
        adjusted = np.asarray(audio, dtype=np.float32) * gain
        peak = float(np.max(np.abs(adjusted))) if len(adjusted) else 0.0
        if peak > 0.97:
            limiter = 0.97 / peak
            adjusted *= limiter
            gain *= limiter
            gain_db = float(20.0 * np.log10(max(gain, 1e-6)))
        after_level = max(1e-6, active_voice_rms(adjusted, rate))
        diagnostics.append({
            "beforeDb": round(before_db, 2),
            "targetDb": round(target_db, 2),
            "gainDb": round(gain_db, 2),
            "afterDb": round(20.0 * np.log10(after_level), 2),
        })
        normalized.append((rate, adjusted))
    return normalized, diagnostics


def compact_segment_silence(audio: Any, sample_rate: int) -> tuple[Any, dict[str, Any]]:
    """Trim edge silence and shorten unnatural internal gaps without touching speech."""
    import numpy as np

    source = np.asarray(audio, dtype=np.float32)
    if not len(source) or sample_rate <= 0:
        return source, {"validSpeech": False, "activeRatio": 0.0, "compactedSilenceMs": 0}
    frame_size = max(1, int(sample_rate * 0.01))
    frame_count = len(source) // frame_size
    if frame_count < 2:
        peak = float(np.max(np.abs(source))) if len(source) else 0.0
        return source, {"validSpeech": peak >= 0.008, "activeRatio": 1.0 if peak >= 0.008 else 0.0, "compactedSilenceMs": 0}
    framed = source[:frame_count * frame_size].reshape(frame_count, frame_size)
    rms = np.sqrt(np.mean(np.square(framed), axis=1) + 1e-10)
    peak_rms = float(np.percentile(rms, 95))
    noise_floor = float(np.percentile(rms, 15))
    threshold = max(0.00045, noise_floor * 2.4, peak_rms * 0.045)
    active = rms >= threshold
    indices = np.flatnonzero(active)
    if not len(indices):
        return source, {"validSpeech": False, "activeRatio": 0.0, "compactedSilenceMs": 0}

    edge_pad = int(sample_rate * 0.035)
    start = max(0, int(indices[0] * frame_size) - edge_pad)
    end = min(len(source), int((indices[-1] + 1) * frame_size) + edge_pad)
    trimmed = source[start:end]

    local_start_frame = max(0, start // frame_size)
    local_end_frame = min(frame_count, int(np.ceil(end / frame_size)))
    local_active = active[local_start_frame:local_end_frame]
    max_gap_frames = max(1, int(0.62 / 0.01))
    keep_gap_frames = max(1, int(0.16 / 0.01))
    chunks: list[Any] = []
    cursor = 0
    compacted_samples = 0
    run_start = None
    for index, is_active in enumerate(local_active):
        if not is_active and run_start is None:
            run_start = index
        if (is_active or index == len(local_active) - 1) and run_start is not None:
            run_end = index if is_active else index + 1
            run_frames = run_end - run_start
            if run_frames > max_gap_frames and run_start > 0 and run_end < len(local_active):
                gap_start = min(len(trimmed), run_start * frame_size)
                gap_end = min(len(trimmed), run_end * frame_size)
                keep_samples = keep_gap_frames * frame_size
                left_keep = keep_samples // 2
                right_keep = keep_samples - left_keep
                chunks.append(trimmed[cursor:gap_start + left_keep])
                cursor = max(gap_start + left_keep, gap_end - right_keep)
                compacted_samples += max(0, cursor - (gap_start + left_keep))
            run_start = None
    chunks.append(trimmed[cursor:])
    compacted = np.concatenate(chunks) if len(chunks) > 1 else trimmed
    peak = float(np.max(np.abs(compacted))) if len(compacted) else 0.0
    compacted_frames = len(compacted) // frame_size
    if compacted_frames:
        compacted_rms = np.sqrt(np.mean(np.square(compacted[:compacted_frames * frame_size].reshape(compacted_frames, frame_size)), axis=1) + 1e-10)
        compacted_active = compacted_rms >= threshold
        active_ratio = float(np.mean(compacted_active))
        active_ms = int(np.sum(compacted_active) * 10)
    else:
        active_ratio = 0.0
        active_ms = 0
    artifact_metrics = audio_artifact_metrics(compacted, sample_rate)
    return compacted, {
        "validSpeech": bool(peak >= 0.003 and active_ms >= 160 and artifact_metrics["artifactSafe"]),
        "activeRatio": round(active_ratio, 4),
        "activeSpeechMs": active_ms,
        "compactedSilenceMs": round(compacted_samples / sample_rate * 1000),
        "trimmedEdgeMs": round((len(source) - len(trimmed)) / sample_rate * 1000),
        **artifact_metrics,
    }


def load_style_map(path: Path | None) -> dict[str, dict[str, str]]:
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("style map must be a JSON object")
    result: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            normalized = {str(k): str(v) for k, v in value.items()}
            for field in ("refAudio", "refText"):
                raw_path = Path(normalized.get(field) or "")
                if str(raw_path) and not raw_path.is_absolute():
                    normalized[field] = str((path.parent / raw_path).resolve())
            result[str(key)] = normalized
    return result


def provisional_japanese_mouth_cues(text: str, speech_start: float, speech_end: float) -> list[dict[str, Any]]:
    """Map Japanese mora vowels to Rhubarb-compatible anime viseme groups."""
    units: list[tuple[str, float]] = []
    try:
        import pyopenjtalk

        phones = str(pyopenjtalk.g2p(str(text or ""), kana=False) or "").split()
        pending_lip_close = False
        vowel_visemes = {"a": "D", "i": "C", "u": "F", "e": "G", "o": "E"}
        for raw_phone in phones:
            phone = raw_phone.strip()
            lowered = phone.lower()
            if lowered in {"sil", "pau", "sp"}:
                units.append(("A", 1.8))
                pending_lip_close = False
            elif lowered in {"cl", "q"}:
                units.append(("A", 0.7))
                pending_lip_close = False
            elif lowered in {"n", "ng"} or phone == "N":
                units.append(("B", 0.72))
                pending_lip_close = False
            elif lowered in {"m", "b", "p"}:
                pending_lip_close = True
            elif lowered in vowel_visemes:
                if pending_lip_close:
                    units.append(("A", 0.34))
                units.append((vowel_visemes[lowered], 1.0))
                pending_lip_close = False
    except Exception:
        units = []

    if not units:
        for char in str(text or ""):
            if re.match(r"[\s。！？!?、，,；;：:]", char):
                units.append(("A", 1.5))
            elif re.match(r"[あアぁァかカがガさサざザたタだダなナはハばバぱパまマやヤゃャらラわワ]", char):
                units.append(("D", 1.0))
            elif re.match(r"[いイぃィきキぎギしシじジちチぢヂにニひヒびビぴピみミりリ]", char):
                units.append(("C", 1.0))
            elif re.match(r"[うウぅゥくクぐグすスずズつツっッづヅぬヌふフぶブぷプむムゆユゅュるル]", char):
                units.append(("F", 1.0))
            elif re.match(r"[えエぇェけケげゲせセぜゼてテでデねネへヘべベぺペめメれレ]", char):
                units.append(("G", 1.0))
            elif re.match(r"[おオぉォこコごゴそソぞゾとトどドのノほホぼボぽポもモよヨょョろロをヲ]", char):
                units.append(("E", 1.0))
            elif re.match(r"[んン]", char):
                units.append(("B", 0.72))
            else:
                units.append(("B", 0.9))

    usable_start = max(0.0, float(speech_start or 0.0))
    usable_end = max(usable_start + 0.01, float(speech_end or usable_start + 0.01))
    total_weight = sum(weight for _value, weight in units) or 1.0
    seconds_per_weight = (usable_end - usable_start) / total_weight
    cursor = usable_start
    cues: list[dict[str, Any]] = []
    if usable_start > 0.005:
        cues.append({"start": 0.0, "end": round(usable_start, 4), "value": "A"})
    for value, weight in units:
        end = min(usable_end, cursor + seconds_per_weight * weight)
        cues.append({"start": round(cursor, 4), "end": round(max(cursor + 0.01, end), 4), "value": value})
        cursor = end
    if cursor < usable_end:
        cues.append({"start": round(cursor, 4), "end": round(usable_end, 4), "value": "B"})
    cues.append({"start": round(usable_end, 4), "end": round(usable_end + 0.06, 4), "value": "A"})
    return cues


def provisional_chinese_mouth_cues(text: str, speech_start: float, speech_end: float) -> list[dict[str, Any]]:
    """Map Mandarin finals to the same compact anime viseme vocabulary."""
    units: list[tuple[str, float]] = []
    try:
        from pypinyin import Style, pinyin

        for syllables in pinyin(str(text or ""), style=Style.FINALS, errors=lambda value: list(value)):
            final = str(syllables[0] if syllables else "").lower()
            if not final or re.match(r"[\s。！？!?、，,；;：:]", final):
                units.append(("A", 1.5))
            elif final.startswith(("i", "v", "ü")):
                units.append(("C", 1.0))
            elif final.startswith(("u", "ou")):
                units.append(("F", 1.0))
            elif final.startswith(("e", "ei", "en", "eng", "er")):
                units.append(("G", 1.0))
            elif final.startswith(("o", "ong")):
                units.append(("E", 1.0))
            else:
                units.append(("D", 1.0))
    except Exception:
        units = []
    if not units:
        units = [
            ("A", 1.5)
            if re.match(r"[\s。！？!?、，,；;：:]", char)
            else (("D", 1.0) if re.match(r"[\u3400-\u9fff]", char) else ("B", 0.9))
            for char in str(text or "")
        ]
    return timed_mouth_cues(units, speech_start, speech_end)


def timed_mouth_cues(
    units: list[tuple[str, float]],
    speech_start: float,
    speech_end: float,
) -> list[dict[str, Any]]:
    usable_start = max(0.0, float(speech_start or 0.0))
    usable_end = max(usable_start + 0.01, float(speech_end or usable_start + 0.01))
    total_weight = sum(weight for _value, weight in units) or 1.0
    seconds_per_weight = (usable_end - usable_start) / total_weight
    cursor = usable_start
    cues: list[dict[str, Any]] = []
    if usable_start > 0.005:
        cues.append({"start": 0.0, "end": round(usable_start, 4), "value": "A"})
    for value, weight in units:
        end = min(usable_end, cursor + seconds_per_weight * weight)
        cues.append({"start": round(cursor, 4), "end": round(max(cursor + 0.01, end), 4), "value": value})
        cursor = end
    if cursor < usable_end:
        cues.append({"start": round(cursor, 4), "end": round(usable_end, 4), "value": "B"})
    cues.append({"start": round(usable_end, 4), "end": round(usable_end + 0.06, 4), "value": "A"})
    return cues


def provisional_mouth_cues(
    text: str,
    speech_start: float,
    speech_end: float,
    language: str,
) -> list[dict[str, Any]]:
    if is_chinese_target_language(language):
        return provisional_chinese_mouth_cues(text, speech_start, speech_end)
    return provisional_japanese_mouth_cues(text, speech_start, speech_end)


def semantic_decode_observation(
    *,
    backend: str,
    token_count: int,
    token_budget: int,
    phoneme_count: int,
    route: str,
    gpt_model: str,
    visible_chars: int,
    semantic_frame_rate_hz: float = 25.0,
    decoder_budget_rate_hz: float = 50.0,
    error: str = "",
) -> dict[str, Any]:
    """Describe why one autoregressive semantic decode stopped.

    GPT-SoVITS exposes the generated token count but not whether EOS came from
    argmax or sampling.  For a successful batch-one decode, stopping below both
    configured length ceilings can only be EOS; the source remains explicitly
    unknown so developer diagnostics do not claim false precision.
    """

    tokens = max(0, int(token_count or 0))
    budget = max(0, int(token_budget or 0))
    phonemes = max(0, int(phoneme_count or 0))
    semantic_rate = max(1.0, float(semantic_frame_rate_hz or 25.0))
    budget_rate = max(1.0, float(decoder_budget_rate_hz or 50.0))
    error_text = str(error or "").strip()
    if error_text:
        stop_reason = "decode_error"
    elif budget and tokens >= budget:
        stop_reason = "max_duration"
    elif tokens >= 1499:
        stop_reason = "decoder_limit"
    else:
        stop_reason = "eos"
    ended_by_eos = stop_reason == "eos"
    return {
        "backend": str(backend or "unknown"),
        "gptModelRoute": str(route or "default"),
        "gptModel": str(gpt_model or ""),
        "visibleChars": max(0, int(visible_chars or 0)),
        "conditionedPhonemeCount": phonemes,
        "semanticTokenCount": tokens,
        "semanticTokenBudget": budget,
        "semanticFrameRateHz": round(semantic_rate, 3),
        "semanticDurationEstimateMs": round(tokens / semantic_rate * 1000),
        "decoderBudgetRateHz": round(budget_rate, 3),
        "decoderConfiguredLimitMs": round(budget / budget_rate * 1000) if budget else 0,
        "decoderEffectiveAudioLimitEstimateMs": (
            round(budget / semantic_rate * 1000) if budget else 0
        ),
        "semanticTokensPerConditionedPhoneme": (
            round(tokens / phonemes, 3) if phonemes else None
        ),
        "stopReason": stop_reason,
        "endedByEos": ended_by_eos,
        "eosDecisionSource": "argmax_or_sample" if ended_by_eos else "",
        "error": error_text[:500],
    }


def summarize_semantic_decodes(events: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [dict(item) for item in events if isinstance(item, dict)]
    reasons = list(dict.fromkeys(
        str(item.get("stopReason") or "unknown") for item in normalized
    ))
    return {
        "semanticDecodes": normalized,
        "semanticDecodeCount": len(normalized),
        "semanticTokenCount": sum(
            int(item.get("semanticTokenCount") or 0) for item in normalized
        ),
        "semanticTokenBudget": sum(
            int(item.get("semanticTokenBudget") or 0) for item in normalized
        ),
        "conditionedPhonemeCount": sum(
            int(item.get("conditionedPhonemeCount") or 0) for item in normalized
        ),
        "semanticStopReason": (
            reasons[0] if len(reasons) == 1 else "mixed" if reasons else "unobserved"
        ),
        "semanticStopReasons": reasons,
        "semanticEosObserved": any(
            bool(item.get("endedByEos")) for item in normalized
        ),
    }


class GPTSoVITSEngine:
    def __init__(
        self,
        root: Path,
        gpt_model: Path,
        sovits_model: Path,
        default_ref_audio: Path,
        default_ref_text: Path,
        style_map: dict[str, dict[str, str]],
        ref_language: str,
        target_language: str,
        rhubarb_exe: Path | None,
        instance_signature: str = "",
        bootstrap_gpt_model: Path | None = None,
        bootstrap_sovits_model: Path | None = None,
        max_segment_chars: int = DEFAULT_MAX_SYNTHESIS_CHARS,
        gpt_model_routes: dict[str, Path] | None = None,
        gpt_route_short_max_chars: int = DEFAULT_SHORT_ROUTE_MAX_CHARS,
        gpt_route_long_min_chars: int = 60,
    ) -> None:
        self.root = root.resolve()
        self.gpt_model = gpt_model.resolve()
        self.sovits_model = sovits_model.resolve()
        self.default_ref_audio = default_ref_audio.resolve()
        self.default_ref_text = default_ref_text.resolve()
        self.style_map = style_map
        self.ref_language = ref_language
        self.target_language = target_language
        self.rhubarb_exe = rhubarb_exe.resolve() if rhubarb_exe else None
        self.instance_signature = str(instance_signature or "")
        self.bootstrap_gpt_model = (bootstrap_gpt_model or self.gpt_model).resolve()
        self.bootstrap_sovits_model = (bootstrap_sovits_model or self.sovits_model).resolve()
        self.max_segment_chars = max(24, min(72, int(max_segment_chars)))
        self.gpt_model_routes = {
            str(route).strip().lower(): Path(path).resolve()
            for route, path in (gpt_model_routes or {}).items()
            if str(route).strip().lower() in {"short", "long"}
        }
        self.gpt_route_short_max_chars = max(1, int(gpt_route_short_max_chars))
        self.gpt_route_long_min_chars = max(
            self.gpt_route_short_max_chars + 1,
            int(gpt_route_long_min_chars),
        )
        self.lock = threading.Lock()
        self.interactive_priority_lock = threading.Lock()
        self.interactive_synthesis_count = 0
        self.last_interactive_synthesis_at = time.monotonic()
        self.aligner_lock = threading.Lock()
        self.aligner = None
        self.last_synthesis_diagnostics: dict[str, Any] = {}
        self.reference_noise_metrics: dict[str, dict[str, Any]] = {}
        self.deferred_alignment_lock = threading.Lock()
        self.deferred_alignment_jobs: dict[str, dict[str, Any]] = {}
        self.semantic_decode_context = threading.local()
        self.cancellation_lock = threading.Lock()
        self.cancelled_request_ids: set[str] = set()
        self.active_request_ids: set[str] = set()
        self.synthesis_request_context = threading.local()

        # inference_webui builds part of its runtime state while importing. V4-E2
        # was evaluated after an EmotionLite bootstrap and becomes unintelligible
        # when E2 is used for that first import. Keep bootstrap and target weights
        # explicit so portable builds are deterministic without changing V4's
        # original initialization path.
        os.environ["gpt_path"] = str(self.bootstrap_gpt_model)
        os.environ["sovits_path"] = str(self.bootstrap_sovits_model)
        os.environ.setdefault("version", "v2")
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

        # GPT-SoVITS resets RNG seeds per utterance, but CUDA kernels can still
        # select nondeterministic algorithms. That made identical requests vary
        # enough to occasionally produce a noisy or discontinuous take.
        import torch

        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        torch.use_deterministic_algorithms(True, warn_only=True)

        os.chdir(self.root)
        sys.path.insert(0, str(self.root))
        sys.path.insert(0, str(self.root / "GPT_SoVITS"))

        from tools.i18n.i18n import I18nAuto
        weight_state = os.environ.get("AMADEUS_GPT_SOVITS_WEIGHT_JSON", "").strip()
        redirected_open = None
        if weight_state:
            _weight_state_path, redirected_open = prepare_inference_weight_state_redirect(
                Path(weight_state), self.root
            )
        original_builtin_open = builtins.open
        if redirected_open is not None:
            builtins.open = redirected_open
        try:
            from GPT_SoVITS import inference_webui
        finally:
            builtins.open = original_builtin_open
        if redirected_open is not None:
            inference_webui.open = redirected_open
        from GPT_SoVITS.inference_webui import change_gpt_weights, change_sovits_weights, get_tts_wav

        self.i18n = I18nAuto()
        self.change_gpt_weights = change_gpt_weights
        self.change_sovits_weights = change_sovits_weights
        self.get_tts_wav = get_tts_wav
        self.inference_webui = inference_webui
        self.change_gpt_weights(str(self.gpt_model))
        next(self.change_sovits_weights(str(self.sovits_model), self.i18n(ref_language), self.i18n(target_language)))
        self._gpt_runtime_models: dict[str, dict[str, Any]] = {}
        self._active_gpt_route = "default"
        self._capture_gpt_runtime("default", self.gpt_model)
        for route, model_path in self.gpt_model_routes.items():
            if not model_path.is_file():
                raise FileNotFoundError(f"GPT route model does not exist: {route}={model_path}")
            self.change_gpt_weights(str(model_path))
            self._capture_gpt_runtime(route, model_path)
        self._activate_gpt_runtime("default")
        self._install_semantic_decode_observers()
        self._warm_up_runtime()

    def begin_request(self, request_id: Any) -> None:
        normalized = str(request_id or "").strip()[:160]
        self.synthesis_request_context.request_id = normalized
        if normalized:
            with self.cancellation_lock:
                self.cancelled_request_ids.discard(normalized)
                self.active_request_ids.add(normalized)

    def cancel_request(self, request_id: Any) -> bool:
        normalized = str(request_id or "").strip()[:160]
        if not normalized:
            return False
        with self.cancellation_lock:
            if normalized not in self.active_request_ids:
                return False
            self.cancelled_request_ids.add(normalized)
        return True

    def end_request(self) -> None:
        normalized = str(getattr(self.synthesis_request_context, "request_id", "") or "")
        self.synthesis_request_context.request_id = ""
        if normalized:
            with self.cancellation_lock:
                self.active_request_ids.discard(normalized)
                self.cancelled_request_ids.discard(normalized)

    def raise_if_cancelled(self) -> None:
        context = getattr(self, "synthesis_request_context", None)
        cancellation_lock = getattr(self, "cancellation_lock", None)
        cancelled_ids = getattr(self, "cancelled_request_ids", None)
        normalized = str(getattr(context, "request_id", "") or "")
        if not normalized or cancellation_lock is None or cancelled_ids is None:
            return
        with cancellation_lock:
            cancelled = normalized in cancelled_ids
        if cancelled:
            raise SynthesisCancelled("speech synthesis cancelled by user")

    @contextmanager
    def cancellable_synthesis_lock(self):
        started = time.perf_counter()
        acquired = False
        try:
            while not self.lock.acquire(timeout=0.1):
                self.raise_if_cancelled()
            acquired = True
            wait_ms = round((time.perf_counter() - started) * 1000)
            self.raise_if_cancelled()
            yield wait_ms
        finally:
            if acquired:
                self.lock.release()
            print("TTS_LOCK " + json.dumps({
                "atUnixMs": round(time.time() * 1000),
                "requestId": str(getattr(getattr(self, "synthesis_request_context", None), "request_id", "") or ""),
                "acquired": acquired,
                "waitMs": wait_ms if acquired else round((time.perf_counter() - started) * 1000),
            }), flush=True)

    def _capture_gpt_runtime(self, route: str, model_path: Path) -> None:
        self._gpt_runtime_models[route] = {
            "model": self.inference_webui.t2s_model,
            "config": self.inference_webui.config,
            "max_sec": self.inference_webui.max_sec,
            "gpt_path": str(model_path.resolve()),
            "cuda_graph": None,
        }

    def _activate_gpt_runtime(self, route: str) -> None:
        runtimes = getattr(self, "_gpt_runtime_models", {})
        if not runtimes:
            self._active_gpt_route = "default"
            return
        selected = route if route in runtimes else "default"
        current = runtimes.get(getattr(self, "_active_gpt_route", "default"))
        if current is not None:
            current["cuda_graph"] = self.inference_webui.t2s_model_cudagraph
        runtime = runtimes[selected]
        self.inference_webui.t2s_model = runtime["model"]
        self.inference_webui.config = runtime["config"]
        self.inference_webui.max_sec = runtime["max_sec"]
        # The v2 semantic codes used by the active SoVITS model are 25 Hz.
        # Upstream inference_webui resets this value to 50 whenever GPT weights
        # change, doubling the effective audio duration of every early-stop
        # budget. Restore the actual semantic rate on every route activation.
        self.inference_webui.hz = int(round(self._semantic_frame_rate_hz()))
        self.inference_webui.gpt_path_global = runtime["gpt_path"]
        self.inference_webui.t2s_model_cudagraph = runtime.get("cuda_graph")
        self._active_gpt_route = selected

    @staticmethod
    def _tensor_scalar(value: Any) -> int:
        try:
            if hasattr(value, "reshape"):
                value = value.reshape(-1)[0]
            if hasattr(value, "item"):
                value = value.item()
            return int(value)
        except (IndexError, TypeError, ValueError):
            return 0

    def _record_semantic_decode(
        self,
        *,
        backend: str,
        token_count: int,
        token_budget: int,
        phoneme_count: int,
        error: str = "",
    ) -> None:
        context = getattr(self, "semantic_decode_context", None)
        active = getattr(context, "active", None) if context is not None else None
        if not isinstance(active, dict):
            return
        active.setdefault("events", []).append(semantic_decode_observation(
            backend=backend,
            token_count=token_count,
            token_budget=token_budget,
            phoneme_count=phoneme_count,
            route=str(active.get("route") or "default"),
            gpt_model=str(active.get("gptModel") or ""),
            visible_chars=int(active.get("visibleChars") or 0),
            semantic_frame_rate_hz=float(active.get("semanticFrameRateHz") or 25.0),
            decoder_budget_rate_hz=float(active.get("decoderBudgetRateHz") or 50.0),
            error=error,
        ))

    def _semantic_frame_rate_hz(self) -> float:
        hps = getattr(self.inference_webui, "hps", None)
        model = getattr(hps, "model", None)
        raw_rate = str(getattr(model, "semantic_frame_rate", "25hz") or "25hz")
        match = re.search(r"\d+(?:\.\d+)?", raw_rate)
        return max(1.0, float(match.group(0))) if match else 25.0

    def _begin_semantic_decode_observation(self, text: str) -> None:
        context = getattr(self, "semantic_decode_context", None)
        if context is None:
            context = threading.local()
            self.semantic_decode_context = context
        route = str(getattr(self, "_active_gpt_route", "default") or "default")
        context.active = {
            "route": route,
            "gptModel": self.gpt_model_name_for_route(route),
            "visibleChars": visible_speech_chars(text),
            "semanticFrameRateHz": self._semantic_frame_rate_hz(),
            "decoderBudgetRateHz": float(getattr(self.inference_webui, "hz", 50) or 50),
            "events": [],
        }

    def _end_semantic_decode_observation(self) -> list[dict[str, Any]]:
        context = getattr(self, "semantic_decode_context", None)
        active = getattr(context, "active", None) if context is not None else None
        if context is not None:
            context.active = None
        if not isinstance(active, dict):
            return []
        return [
            dict(item)
            for item in active.get("events", [])
            if isinstance(item, dict)
        ]

    def _install_semantic_decode_observers(self) -> None:
        """Wrap both GPT decode backends without altering their outputs."""

        for runtime in self._gpt_runtime_models.values():
            lightning_model = runtime.get("model")
            decoder = getattr(lightning_model, "model", None)
            original = getattr(decoder, "infer_panel", None)
            if not callable(original) or getattr(decoder, "_amadeus_decode_observed", False):
                continue

            def observed_infer_panel(*args: Any, _original=original, **kwargs: Any):
                try:
                    result = _original(*args, **kwargs)
                except Exception as exc:
                    self._record_semantic_decode(
                        backend="standard",
                        token_count=0,
                        token_budget=int(kwargs.get("early_stop_num") or 0),
                        phoneme_count=self._tensor_scalar(args[1] if len(args) > 1 else 0),
                        error=str(exc),
                    )
                    raise
                token_count = self._tensor_scalar(result[1] if len(result) > 1 else 0)
                self._record_semantic_decode(
                    backend="standard",
                    token_count=token_count,
                    token_budget=int(kwargs.get("early_stop_num") or 0),
                    phoneme_count=self._tensor_scalar(args[1] if len(args) > 1 else 0),
                )
                return result

            decoder.infer_panel = observed_infer_panel
            decoder._amadeus_decode_observed = True

        from AR.models.t2s_model_cudagraph import CUDAGraphRunner

        original_generate = getattr(CUDAGraphRunner, "generate", None)
        if not callable(original_generate) or getattr(
            CUDAGraphRunner,
            "_amadeus_decode_observed",
            False,
        ):
            return

        def observed_cudagraph_generate(runner: Any, request: Any):
            try:
                result = original_generate(runner, request)
            except Exception as exc:
                self._record_semantic_decode(
                    backend="cuda_graph",
                    token_count=0,
                    token_budget=int(getattr(request, "early_stop_num", 0) or 0),
                    phoneme_count=self._tensor_scalar(getattr(request, "x_lens", 0)),
                    error=str(exc),
                )
                raise
            result_items = getattr(result, "result", None) or []
            token_count = 0
            if result_items:
                first = result_items[0]
                token_count = int(first.numel()) if hasattr(first, "numel") else len(first)
            self._record_semantic_decode(
                backend="cuda_graph",
                token_count=token_count,
                token_budget=int(getattr(request, "early_stop_num", 0) or 0),
                phoneme_count=self._tensor_scalar(getattr(request, "x_lens", 0)),
                error=str(getattr(result, "exception", None) or ""),
            )
            return result

        CUDAGraphRunner.generate = observed_cudagraph_generate
        CUDAGraphRunner._amadeus_decode_observed = True

    def gpt_route_for_text(self, text: str) -> str:
        visible_chars = visible_speech_chars(text)
        runtimes = getattr(self, "_gpt_runtime_models", {})
        short_max = int(getattr(
            self,
            "gpt_route_short_max_chars",
            DEFAULT_SHORT_ROUTE_MAX_CHARS,
        ))
        long_min = int(getattr(self, "gpt_route_long_min_chars", 60))
        if "short" in runtimes and visible_chars <= short_max:
            return "short"
        if "long" in runtimes and visible_chars >= long_min:
            return "long"
        return "default"

    def gpt_model_name_for_route(self, route: str) -> str:
        runtime = getattr(self, "_gpt_runtime_models", {}).get(route)
        if runtime and runtime.get("gpt_path"):
            return Path(str(runtime["gpt_path"])).name
        configured = getattr(self, "gpt_model", None)
        return Path(str(configured)).name if configured else "default"

    def _warm_up_runtime(self) -> None:
        """Warm every GPT route once, then queue the remaining references.

        Loading checkpoint tensors is not sufficient for this runtime: the first
        semantic decode and vocoder pass also initialize CUDA kernels/graphs and
        route-local state.  Deferring *all* inference until after the first real
        response made the first user utterance the effective warm-up, especially
        in developer mode where startup greetings are intentionally suppressed.
        """
        try:
            references: dict[tuple[str, str], tuple[Path, Path]] = {}
            for item in self.style_map.values():
                if not isinstance(item, dict):
                    continue
                audio_path = Path(str(item.get("refAudio") or self.default_ref_audio)).resolve()
                text_path = Path(str(item.get("refText") or self.default_ref_text)).resolve()
                references[(str(audio_path), str(text_path))] = (audio_path, text_path)
            default_reference_key = (
                str(Path(self.default_ref_audio).resolve()),
                str(Path(self.default_ref_text).resolve()),
            )
            self.background_warmup_references = [
                reference
                for key, reference in references.items()
                if key != default_reference_key
            ]
            self._deferred_warmup_started = False
            self._deferred_warmup_start_lock = threading.Lock()
            self.critical_warmup_routes: list[str] = []
            self.critical_warmup_elapsed_ms = 0

            started = time.perf_counter()
            previous_gpt_route = getattr(self, "_active_gpt_route", "default")
            original_max_sec = self.inference_webui.max_sec
            runtime_models = getattr(self, "_gpt_runtime_models", {})
            ordered_routes = [
                route
                for route in ("short", "default", "long")
                if route == "default" or route in runtime_models
            ]
            try:
                self.inference_webui.max_sec = min(original_max_sec, 6)
                for index, route in enumerate(ordered_routes, 1):
                    self._activate_gpt_runtime(route)
                    self._warm_up_reference(
                        self.default_ref_audio,
                        self.default_ref_text,
                        732000 + index,
                    )
                    self.critical_warmup_routes.append(route)
            finally:
                self.inference_webui.max_sec = original_max_sec
                self._activate_gpt_runtime(previous_gpt_route)
            self.critical_warmup_elapsed_ms = round(
                (time.perf_counter() - started) * 1000
            )
            print(
                "GPT-SoVITS critical warm-up complete "
                f"(routes={','.join(self.critical_warmup_routes)}; "
                f"criticalWarmupMs={self.critical_warmup_elapsed_ms}; "
                f"atUnixMs={round(time.time() * 1000)}; "
                f"{len(self.background_warmup_references)} reference warm-ups deferred)",
                flush=True,
            )
        except Exception as exc:
            self.background_warmup_references = []
            self._deferred_warmup_started = False
            self._deferred_warmup_start_lock = threading.Lock()
            print(f"GPT-SoVITS warm-up skipped: {exc}", flush=True)

    def _warm_up_reference(self, audio_path: Path, text_path: Path, seed: int) -> None:
        self.inference_webui.set_seed(seed)
        warmup_text = "准备完成。" if is_chinese_target_language(self.target_language) else "準備完了。"
        generated = list(self.get_tts_wav(
            ref_wav_path=str(audio_path),
            prompt_text=text_path.read_text(encoding="utf-8-sig").strip(),
            prompt_language=self.i18n(self.ref_language),
            text=warmup_text,
            text_language=self.i18n(self.target_language),
            top_p=0.65,
            temperature=0.55,
            speed=1.0,
        ))
        if not generated or not any(
            getattr(item[1], "size", len(item[1])) > 0
            for item in generated
            if isinstance(item, (tuple, list)) and len(item) >= 2
        ):
            raise RuntimeError(f"critical warm-up produced no audio: {audio_path.name}")

    def _begin_interactive_synthesis(self) -> None:
        priority_lock = getattr(self, "interactive_priority_lock", None)
        if priority_lock is None:
            return
        with priority_lock:
            self.interactive_synthesis_count = (
                int(getattr(self, "interactive_synthesis_count", 0)) + 1
            )

    def _end_interactive_synthesis(self) -> None:
        priority_lock = getattr(self, "interactive_priority_lock", None)
        if priority_lock is None:
            return
        with priority_lock:
            self.interactive_synthesis_count = max(
                0,
                int(getattr(self, "interactive_synthesis_count", 0)) - 1,
            )
            self.last_interactive_synthesis_at = time.monotonic()

    def _interactive_synthesis_pending(self) -> bool:
        priority_lock = getattr(self, "interactive_priority_lock", None)
        if priority_lock is None:
            return False
        with priority_lock:
            return int(getattr(self, "interactive_synthesis_count", 0)) > 0

    def _interactive_idle_seconds(self) -> float:
        priority_lock = getattr(self, "interactive_priority_lock", None)
        if priority_lock is None:
            return float("inf")
        with priority_lock:
            last = float(
                getattr(self, "last_interactive_synthesis_at", 0.0) or 0.0
            )
        return max(0.0, time.monotonic() - last)

    def warm_up_remaining_references(self, delay_seconds: float = 30.0) -> None:
        """Warm emotion references after the first user-visible synthesis."""

        references = list(getattr(self, "background_warmup_references", []))
        if not references:
            return
        time.sleep(max(0.0, float(delay_seconds)))
        completed = 0
        for index, (audio_path, text_path) in enumerate(references, 1):
            try:
                while True:
                    if (
                        self._interactive_synthesis_pending()
                        or self._interactive_idle_seconds() < 2.0
                    ):
                        time.sleep(0.05)
                        continue
                    acquired = self.lock.acquire(timeout=0.1)
                    if not acquired:
                        continue
                    try:
                        if (
                            self._interactive_synthesis_pending()
                            or self._interactive_idle_seconds() < 2.0
                        ):
                            continue
                        previous_gpt_route = getattr(self, "_active_gpt_route", "default")
                        self._activate_gpt_runtime("default")
                        original_max_sec = self.inference_webui.max_sec
                        try:
                            self.inference_webui.max_sec = min(original_max_sec, 6)
                            self._warm_up_reference(audio_path, text_path, 733000 + index)
                        finally:
                            self.inference_webui.max_sec = original_max_sec
                            self._activate_gpt_runtime(previous_gpt_route)
                        break
                    finally:
                        self.lock.release()
                completed += 1
                time.sleep(0.25)
            except Exception as exc:
                print(f"GPT-SoVITS deferred warm-up skipped {audio_path.name}: {exc}", flush=True)
        print(f"GPT-SoVITS deferred warm-up complete ({completed}/{len(references)})", flush=True)

    def start_deferred_warmup(self, delay_seconds: float = 15.0) -> None:
        """Start background warm-up once the first real response has been delivered."""

        if not getattr(self, "background_warmup_references", []):
            return
        with self._deferred_warmup_start_lock:
            if self._deferred_warmup_started:
                return
            self._deferred_warmup_started = True
        threading.Thread(
            target=self.warm_up_remaining_references,
            args=(delay_seconds,),
            daemon=True,
            name="gpt-sovits-deferred-warmup",
        ).start()

    def style_ref(self, style: str, fallback_style: str = "", intensity: str = "normal") -> tuple[Path, Path]:
        level = normalize_voice_intensity(intensity)
        item = (
            self.style_map.get(f"{style}:{level}")
            or self.style_map.get(style)
            or self.style_map.get(f"{fallback_style}:{level}")
            or self.style_map.get(fallback_style)
            or self.style_map.get("default")
            or {}
        )
        ref_audio = Path(item.get("refAudio") or self.default_ref_audio)
        ref_text = Path(item.get("refText") or self.default_ref_text)
        return ref_audio.resolve(), ref_text.resolve()

    def coherent_reference_fallback(
        self,
        style: str,
        fallback_style: str,
        intensity: str,
        current_audio: Path,
    ) -> tuple[Path, Path] | None:
        """Choose one stable reference for a full visible-segment retry.

        A reference is never switched inside an already accepted waveform.
        Prefer the same emotional tone at normal intensity, then its broader
        voice style, and use calm-normal only as the final stable baseline.
        """

        candidate_keys = [
            # Keep the semantic tone and relax only its intensity first. The
            # previous order preferred the coarse fallback style (often calm)
            # before ``shy:normal``/``affectionate:normal``, causing an audible
            # mood jump even when a stable same-tone reference was available.
            f"{style}:normal",
            style,
            f"{style}:strong",
            f"{style}:subtle",
            f"{fallback_style}:{intensity}" if fallback_style else "",
            f"{fallback_style}:normal" if fallback_style else "",
            fallback_style,
            # The selected calm/neutral/serious normal reference may also be
            # the default reference.  In that case the entries above all
            # resolve to the same file, so explicitly allow the matching
            # strong reference to own the final coherent rescue.
            f"{fallback_style}:strong" if fallback_style else "",
            "calm:normal",
            "default",
        ]
        current = current_audio.resolve()
        seen: set[tuple[str, str]] = set()
        for key in candidate_keys:
            if not key:
                continue
            item = self.style_map.get(key) or {}
            audio = Path(item.get("refAudio") or self.default_ref_audio).resolve()
            text = Path(item.get("refText") or self.default_ref_text).resolve()
            identity = (str(audio), str(text))
            if identity in seen:
                continue
            seen.add(identity)
            if audio == current or not audio.is_file() or not text.is_file():
                continue
            return audio, text
        return None

    def synthesize(
        self,
        text: str,
        style: str,
        locale: str,
        speech_speed: float = 1.0,
        fallback_style: str = "",
        intensity: str = "normal",
        segment_mode: str = "auto",
        semantic_units: Any = None,
        speech_prosody: str = "natural",
        delivery_post_processing_enabled: bool = False,
        reference_override: tuple[Path, Path] | None = None,
        gpt_route_override: str | None = None,
    ) -> bytes:
        import numpy as np

        self.raise_if_cancelled()
        level = normalize_voice_intensity(intensity)
        ref_audio, ref_text_path = (
            (Path(reference_override[0]).resolve(), Path(reference_override[1]).resolve())
            if reference_override is not None
            else self.style_ref(style, fallback_style, level)
        )
        ref_text = ref_text_path.read_text(encoding="utf-8-sig")
        speed = max(0.62, min(1.45, float(speech_speed or 1.0)))
        prosody_profile = normalize_speech_prosody(speech_prosody)
        synthesis_text = normalize_technical_speech_text(text, locale)
        target_language = target_language_for_text(
            self.target_language or language_from_locale(locale),
            synthesis_text,
        )
        # Retain an utterance-level value only as a trace identifier. Actual
        # sampling is seeded per acoustic segment inside ``generate`` below.
        utterance_seed = inference_segment_seed(
            synthesis_text,
            ref_audio,
            style,
            level,
        )
        diagnostic_root_value = os.environ.get("AMADEUS_TTS_DIAGNOSTIC_DIR", "").strip()
        diagnostic_root = Path(diagnostic_root_value) if diagnostic_root_value else None
        diagnostic_trace_id = f"tts_{int(time.time() * 1000)}_{utterance_seed}"

        def generate(
            segment: str,
            top_p: float,
            temperature: float,
            audio_path: Path = ref_audio,
            prompt: str = ref_text,
            seed_offset: int = 0,
        ) -> tuple[list[Any], list[dict[str, Any]]]:
            segment_seed = inference_segment_seed(
                segment,
                audio_path,
                style,
                level,
            )
            self.inference_webui.set_seed((segment_seed + seed_offset) % 900000)
            segment_target_language = target_language_for_text(
                self.target_language or language_from_locale(locale),
                segment,
            )
            self._begin_semantic_decode_observation(segment)
            try:
                result = self.get_tts_wav(
                    ref_wav_path=str(audio_path), prompt_text=prompt,
                    prompt_language=self.i18n(self.ref_language), text=segment,
                    text_language=self.i18n(segment_target_language), top_p=top_p,
                    temperature=temperature,
                    speed=speed,
                )
                generated = list(result)
            finally:
                decode_observations = self._end_semantic_decode_observation()
            return generated, decode_observations

        required_semantic_units = validate_semantic_units(text, semantic_units)
        normalized_semantic_units = [
            normalize_technical_speech_text(unit, locale)
            for unit in required_semantic_units
        ]
        # Display-only emoji units do not create an acoustic obligation. Mixed
        # units retain all speakable text and their order; units that become
        # empty after projection are intentionally silent.
        normalized_semantic_units = [
            unit for unit in normalized_semantic_units if visible_speech_chars(unit) > 0
        ]
        emoji_filtered_for_speech = strip_emoji_for_speech(text) != re.sub(
            r"\s+", " ", str(text or "")
        ).strip()
        segments = synthesis_segments(
            synthesis_text,
            max_chars=self.max_segment_chars,
            segment_mode=segment_mode,
            # A caller-supplied contract is authoritative and receives the
            # strict completeness preflight.  Legacy direct engine callers
            # without semanticUnits retain one atomic fallback unit.
            semantic_units=(normalized_semantic_units if semantic_units is not None else None),
            locale=locale,
            prosody_profile=prosody_profile,
        )
        resegment_on_failure = tts_resegmentation_enabled()
        adaptive_retry_shadow = tts_adaptive_retry_shadow_enabled()
        bounded_retry = tts_bounded_retry_enabled()
        # One initial sample plus up to two same-reference new-seed retries.
        # Reference/model fallback owns every later recovery attempt.
        normal_attempt_limit = MAX_NORMAL_ATTEMPTS
        conservative_attempt_limit = MAX_CONSERVATIVE_ATTEMPTS
        high_confidence_resegment = bool(
            resegment_on_failure
            and tts_high_confidence_resegment_enabled()
        )
        if resegment_on_failure:
            segments = stabilize_inference_segments(
                segments,
                max_chars=self.max_segment_chars,
            )
        segment_acoustic_texts: list[str] = []
        segment_leading_ellipsis: list[str] = []
        segment_leading_pause_seconds: list[float] = []
        for segment in segments:
            acoustic_text, ellipsis = project_leading_ellipsis_for_synthesis(segment)
            segment_acoustic_texts.append(acoustic_text)
            segment_leading_ellipsis.append(ellipsis)
            segment_leading_pause_seconds.append(
                segment_pause_seconds(
                    ellipsis,
                    speed,
                    locale=locale,
                    prosody_profile=prosody_profile,
                )
                if ellipsis
                else 0.0
            )
        segment_target_languages = [
            target_language_for_text(
                self.target_language or language_from_locale(locale),
                acoustic_text,
            )
            for acoustic_text in segment_acoustic_texts
        ]
        generated_segments: list[tuple[int, Any]] = []
        accepted_raw_segments: list[tuple[int, Any]] = []
        accepted_segment_texts: list[str] = []
        recovered_segments = 0
        segment_durations: list[float] = []
        segment_quality: list[dict[str, Any]] = []
        attempt_records: list[dict[str, Any]] = []
        coherent_fallback_attempted = False
        coherent_fallback_applied = False
        self._begin_interactive_synthesis()
        with self.cancellable_synthesis_lock() as synthesis_lock_wait_ms:
            requested_gpt_route = str(gpt_route_override or "").strip()
            selected_gpt_route = (
                requested_gpt_route
                if requested_gpt_route in getattr(self, "_gpt_runtime_models", {})
                else self.gpt_route_for_text(synthesis_text)
            )
            self._activate_gpt_runtime(selected_gpt_route)
            effective_normal_attempt_limit = normal_attempt_limit
            original_max_sec = self.inference_webui.max_sec
            try:
                for index, segment in enumerate(segments, 1):
                    self.raise_if_cancelled()
                    acoustic_segment = segment_acoustic_texts[index - 1]
                    leading_ellipsis = segment_leading_ellipsis[index - 1]
                    leading_pause_seconds = segment_leading_pause_seconds[index - 1]
                    visible_chars = visible_speech_chars(segment)
                    segment_normal_attempt_limit = effective_normal_attempt_limit
                    segment_conservative_attempt_limit = conservative_attempt_limit
                    expected_duration, minimum_duration, maximum_duration, semantic_budget = segment_timing_bounds(
                        visible_chars, speed
                    )
                    acceptance_minimum = segment_acceptance_minimum(visible_chars, minimum_duration)
                    can_merge_with_next = False
                    # A small early-stop budget can terminate valid E2 sentences
                    # before their final clause. Duration guards below reject
                    # runaway output, so give semantic decoding enough room to
                    # finish instead of cutting the resulting waveform.
                    self.inference_webui.max_sec = min(original_max_sec, semantic_budget)
                    accepted = None
                    accepted_raw = None
                    candidates: list[tuple[float, int, Any, dict[str, Any], Any]] = []
                    attempted_config_hashes: set[str] = set()
                    early_resegment_requested = False
                    early_reference_fallback_requested = False
                    for attempt, (top_p, temperature) in enumerate(
                        (
                            (0.82, 0.68),
                            (0.65, 0.55),
                            (0.92, 0.82),
                        )[:segment_normal_attempt_limit],
                        1,
                    ):
                        self.raise_if_cancelled()
                        config_hash = tts_attempt_config_hash(
                            acoustic_segment,
                            boundary_version=0,
                            top_p=top_p,
                            temperature=temperature,
                            reference_audio=ref_audio,
                        )
                        if config_hash in attempted_config_hashes:
                            raise RuntimeError(
                                "duplicate GPT-SoVITS attempt configuration rejected"
                            )
                        attempted_config_hashes.add(config_hash)
                        attempt_seed = (
                            inference_segment_seed(
                                acoustic_segment,
                                ref_audio,
                                style,
                                level,
                            )
                            + attempt * 17
                        ) % 900000
                        generation_started = time.perf_counter()
                        generated, semantic_decodes = generate(
                            acoustic_segment,
                            top_p,
                            temperature,
                            seed_offset=attempt * 17,
                        )
                        self.raise_if_cancelled()
                        semantic_summary = summarize_semantic_decodes(semantic_decodes)
                        generation_ms = round(
                            (time.perf_counter() - generation_started) * 1000
                        )
                        if not generated:
                            attempt_records.append({
                                "chunkIndex": index,
                                "chunkTextFingerprint": runtime_fingerprint(segment),
                                "acousticTextFingerprint": runtime_fingerprint(acoustic_segment),
                                "leadingEllipsisProjected": bool(leading_ellipsis),
                                "leadingPauseMs": round(leading_pause_seconds * 1000),
                                "boundaryVersion": 0,
                                "parameterProfile": (
                                    "normal" if attempt == 1 else "stable"
                                ),
                                "referenceAudio": ref_audio.name,
                                "attemptIndex": attempt,
                                "attemptConfigHash": config_hash,
                                "segmentSeed": attempt_seed,
                                "sameReferenceSeedRetry": attempt > 1,
                                "gptModelRoute": selected_gpt_route,
                                "gptModel": self.gpt_model_name_for_route(selected_gpt_route),
                                **semantic_summary,
                                "qualityTier": "fail",
                                "qualityConfidence": 0.0,
                                "qualityGateReason": "no_audio",
                                "generationMs": generation_ms,
                                "failureType": "no_audio",
                                "recommendedAction": "conservative_retry",
                            })
                            print(
                                "TTS_ATTEMPT "
                                + json.dumps(attempt_records[-1], ensure_ascii=False),
                                flush=True,
                            )
                            continue
                        sample_rate, audio = generated[-1]
                        duration = len(audio) / max(1, sample_rate)
                        normalized_audio = pcm_to_float32(audio)
                        compacted_audio, quality = compact_segment_silence(normalized_audio, sample_rate)
                        quality = {
                            **quality,
                            **segment_completeness_metrics(quality, visible_chars),
                            **segment_audibility_metrics(compacted_audio, sample_rate),
                            "semanticLimitReached": semantic_summary.get(
                                "semanticStopReason"
                            ) in {"max_duration", "decoder_limit"},
                            "referenceAudio": ref_audio.name,
                            "attempt": attempt,
                            "generationMs": generation_ms,
                        }
                        compacted_duration = len(compacted_audio) / max(1, sample_rate)
                        quality_decision = segment_quality_gate(
                            quality,
                            compacted_duration=compacted_duration,
                            acceptance_minimum=acceptance_minimum,
                            maximum_duration=maximum_duration,
                            upper_margin=0.2,
                        )
                        if quality.get("semanticLimitReached"):
                            quality_decision = {
                                **quality_decision,
                                "qualityTier": "fail",
                                "qualityConfidence": 0.0,
                                "qualityGateReason": "semantic_limit_without_eos",
                            }
                        quality.update(quality_decision)
                        accepted_candidate = quality_decision["qualityTier"] in {
                            "pass",
                            "gray_pass",
                        }
                        failure_type = None
                        high_confidence_failure = False
                        if not accepted_candidate:
                            failure_type, high_confidence_failure = classify_tts_attempt_failure(
                                quality,
                                segment_text=acoustic_segment,
                                compacted_duration=compacted_duration,
                                acceptance_minimum=acceptance_minimum,
                                maximum_duration=maximum_duration,
                            )
                        recommended_action = (
                            "accept"
                            if accepted_candidate
                            else adaptive_retry_recommendation(
                                str(failure_type or "quality_gate_rejection"),
                                high_confidence_boundary_failure=high_confidence_failure,
                                has_following_chunk=can_merge_with_next,
                            )
                        )
                        quality.update({
                            "failureType": failure_type,
                            "highConfidenceBoundaryFailure": high_confidence_failure,
                            "adaptiveRetryRecommendation": recommended_action,
                            "attemptConfigHash": config_hash,
                            "segmentSeed": attempt_seed,
                        })
                        attempt_record = {
                            "chunkIndex": index,
                            "chunkTextFingerprint": runtime_fingerprint(segment),
                            "acousticTextFingerprint": runtime_fingerprint(acoustic_segment),
                            "leadingEllipsisProjected": bool(leading_ellipsis),
                            "leadingPauseMs": round(leading_pause_seconds * 1000),
                            "boundaryVersion": 0,
                            "parameterProfile": (
                                "normal" if attempt == 1 else "stable"
                            ),
                            "referenceAudio": ref_audio.name,
                            "attemptIndex": attempt,
                            "attemptConfigHash": config_hash,
                            "segmentSeed": attempt_seed,
                            "sameReferenceSeedRetry": attempt > 1,
                            "gptModelRoute": selected_gpt_route,
                            "gptModel": self.gpt_model_name_for_route(selected_gpt_route),
                            **semantic_summary,
                            "qualityTier": quality.get("qualityTier"),
                            "qualityConfidence": quality.get("qualityConfidence"),
                            "qualityGateReason": quality.get("qualityGateReason"),
                            "silenceOverrunMs": quality.get("silenceOverrunMs"),
                            "generationMs": generation_ms,
                            "rawAudioMs": round(duration * 1000),
                            "compactedAudioMs": round(compacted_duration * 1000),
                            "activeSpeechMs": int(quality.get("activeSpeechMs") or 0),
                            "trimmedEdgeMs": int(quality.get("trimmedEdgeMs") or 0),
                            "compactedSilenceMs": int(quality.get("compactedSilenceMs") or 0),
                            "silenceRatio": round(
                                1.0 - float(quality.get("activeRatio") or 0.0),
                                4,
                            ),
                            "failureType": failure_type,
                            "recommendedAction": recommended_action,
                        }
                        attempt_records.append(attempt_record)
                        print(
                            "TTS_ATTEMPT "
                            + json.dumps(attempt_record, ensure_ascii=False),
                            flush=True,
                        )
                        candidates.append((
                            compacted_duration,
                            sample_rate,
                            compacted_audio,
                            quality,
                            normalized_audio,
                        ))
                        if accepted_candidate:
                            accepted = (sample_rate, fade_segment_edges(compacted_audio, sample_rate))
                            accepted_raw = normalized_audio
                            accepted_quality = quality
                            if attempt > 1:
                                print(f"TTS inference chunk {index}/{len(segments)} recovered on attempt {attempt}", flush=True)
                            break
                        if (
                            high_confidence_resegment
                            and high_confidence_failure
                            and failure_type == "excessive_silence"
                        ):
                            if attempt < segment_normal_attempt_limit:
                                print(
                                    f"TTS inference chunk {index}/{len(segments)} "
                                    f"will retry the same reference with new seed "
                                    f"({attempt + 1}/{segment_normal_attempt_limit})",
                                    flush=True,
                                )
                                continue
                            early_reference_fallback_requested = True
                            print(
                                f"TTS inference chunk {index}/{len(segments)} "
                                f"will change the semantic-unit reference after "
                                f"high-confidence {failure_type}",
                                flush=True,
                            )
                            break
                        print(
                            f"TTS inference chunk {index}/{len(segments)} duration guard: "
                            f"raw={duration:.2f}s compacted={compacted_duration:.2f}s "
                            f"range={minimum_duration:.2f}-{maximum_duration:.2f}s "
                            f"acceptMin={acceptance_minimum:.2f}s quality={quality}",
                            flush=True,
                        )
                    viable = [
                        item for item in candidates
                        if (
                            acceptance_minimum <= item[0] < maximum_duration - 0.05
                            and item[3]["validSpeech"]
                            and item[3]["completeSpeech"]
                            and item[3]["audibleSpeech"]
                            and item[3].get("qualityTier") in {"pass", "gray_pass"}
                        )
                    ]
                    if (
                        accepted is None
                        and not viable
                        and not early_resegment_requested
                        and not early_reference_fallback_requested
                    ):
                        # Keep one reference voice for the entire utterance. An
                        # alternate calm prompt can rescue a segment, but it
                        # also makes that sentence sound like a different mood.
                        for recovery_attempt, (top_p, temperature) in enumerate(
                            (
                                (0.58, 0.46),
                                (0.72, 0.58),
                                (0.5, 0.4),
                            )[:segment_conservative_attempt_limit],
                            1,
                        ):
                            self.raise_if_cancelled()
                            config_hash = tts_attempt_config_hash(
                                acoustic_segment,
                                boundary_version=0,
                                top_p=top_p,
                                temperature=temperature,
                                reference_audio=ref_audio,
                            )
                            if config_hash in attempted_config_hashes:
                                raise RuntimeError(
                                    "duplicate GPT-SoVITS attempt configuration rejected"
                                )
                            attempted_config_hashes.add(config_hash)
                            attempt_seed = (
                                inference_segment_seed(
                                    acoustic_segment,
                                    ref_audio,
                                    style,
                                    level,
                                )
                                + 100
                                + recovery_attempt * 23
                            ) % 900000
                            generation_started = time.perf_counter()
                            generated, semantic_decodes = generate(
                                acoustic_segment,
                                top_p,
                                temperature,
                                ref_audio,
                                ref_text,
                                seed_offset=100 + recovery_attempt * 23,
                            )
                            self.raise_if_cancelled()
                            semantic_summary = summarize_semantic_decodes(semantic_decodes)
                            generation_ms = round(
                                (time.perf_counter() - generation_started) * 1000
                            )
                            if not generated:
                                attempt_records.append({
                                    "chunkIndex": index,
                                    "chunkTextFingerprint": runtime_fingerprint(segment),
                                    "acousticTextFingerprint": runtime_fingerprint(acoustic_segment),
                                    "leadingEllipsisProjected": bool(leading_ellipsis),
                                    "leadingPauseMs": round(leading_pause_seconds * 1000),
                                    "boundaryVersion": 0,
                                    "parameterProfile": "conservative",
                                    "referenceAudio": ref_audio.name,
                                    "attemptIndex": segment_normal_attempt_limit + recovery_attempt,
                                    "attemptConfigHash": config_hash,
                                    "segmentSeed": attempt_seed,
                                    "gptModelRoute": selected_gpt_route,
                                    "gptModel": self.gpt_model_name_for_route(selected_gpt_route),
                                    **semantic_summary,
                                    "qualityTier": "fail",
                                    "qualityConfidence": 0.0,
                                    "qualityGateReason": "no_audio",
                                    "generationMs": generation_ms,
                                    "failureType": "no_audio",
                                    "recommendedAction": "resegment_with_next",
                                })
                                print(
                                    "TTS_ATTEMPT "
                                    + json.dumps(attempt_records[-1], ensure_ascii=False),
                                    flush=True,
                                )
                                continue
                            sample_rate, audio = generated[-1]
                            normalized_audio = pcm_to_float32(audio)
                            compacted_audio, quality = compact_segment_silence(normalized_audio, sample_rate)
                            quality = {
                                **quality,
                                **segment_completeness_metrics(quality, visible_chars),
                                **segment_audibility_metrics(compacted_audio, sample_rate),
                                "semanticLimitReached": semantic_summary.get(
                                    "semanticStopReason"
                                ) in {"max_duration", "decoder_limit"},
                                "referenceAudio": ref_audio.name,
                                "attempt": segment_normal_attempt_limit + recovery_attempt,
                                "conservativeRecovery": True,
                                "generationMs": generation_ms,
                            }
                            compacted_duration = len(compacted_audio) / max(1, sample_rate)
                            quality_decision = segment_quality_gate(
                                quality,
                                compacted_duration=compacted_duration,
                                acceptance_minimum=acceptance_minimum,
                                maximum_duration=maximum_duration,
                                upper_margin=0.05,
                            )
                            if quality.get("semanticLimitReached"):
                                quality_decision = {
                                    **quality_decision,
                                    "qualityTier": "fail",
                                    "qualityConfidence": 0.0,
                                    "qualityGateReason": "semantic_limit_without_eos",
                                }
                            quality.update(quality_decision)
                            accepted_candidate = quality_decision["qualityTier"] in {
                                "pass",
                                "gray_pass",
                            }
                            failure_type = None
                            high_confidence_failure = False
                            if not accepted_candidate:
                                failure_type, high_confidence_failure = classify_tts_attempt_failure(
                                    quality,
                                    segment_text=acoustic_segment,
                                    compacted_duration=compacted_duration,
                                    acceptance_minimum=acceptance_minimum,
                                    maximum_duration=maximum_duration,
                                )
                            recommended_action = (
                                "accept"
                                if accepted_candidate
                                else adaptive_retry_recommendation(
                                    str(failure_type or "quality_gate_rejection"),
                                    high_confidence_boundary_failure=high_confidence_failure,
                                    has_following_chunk=can_merge_with_next,
                                )
                            )
                            quality.update({
                                "failureType": failure_type,
                                "highConfidenceBoundaryFailure": high_confidence_failure,
                                "adaptiveRetryRecommendation": recommended_action,
                                "attemptConfigHash": config_hash,
                                "segmentSeed": attempt_seed,
                            })
                            attempt_record = {
                                "chunkIndex": index,
                                "chunkTextFingerprint": runtime_fingerprint(segment),
                                "acousticTextFingerprint": runtime_fingerprint(acoustic_segment),
                                "leadingEllipsisProjected": bool(leading_ellipsis),
                                "leadingPauseMs": round(leading_pause_seconds * 1000),
                                "boundaryVersion": 0,
                                "parameterProfile": "conservative",
                                "referenceAudio": ref_audio.name,
                                "attemptIndex": segment_normal_attempt_limit + recovery_attempt,
                                "attemptConfigHash": config_hash,
                                "segmentSeed": attempt_seed,
                                "gptModelRoute": selected_gpt_route,
                                "gptModel": self.gpt_model_name_for_route(selected_gpt_route),
                                **semantic_summary,
                                "qualityTier": quality.get("qualityTier"),
                                "qualityConfidence": quality.get("qualityConfidence"),
                                "qualityGateReason": quality.get("qualityGateReason"),
                                "silenceOverrunMs": quality.get("silenceOverrunMs"),
                                "generationMs": generation_ms,
                                "rawAudioMs": round(len(normalized_audio) / max(1, sample_rate) * 1000),
                                "compactedAudioMs": round(compacted_duration * 1000),
                                "activeSpeechMs": int(quality.get("activeSpeechMs") or 0),
                                "trimmedEdgeMs": int(quality.get("trimmedEdgeMs") or 0),
                                "compactedSilenceMs": int(quality.get("compactedSilenceMs") or 0),
                                "silenceRatio": round(
                                    1.0 - float(quality.get("activeRatio") or 0.0),
                                    4,
                                ),
                                "failureType": failure_type,
                                "recommendedAction": recommended_action,
                            }
                            attempt_records.append(attempt_record)
                            print(
                                "TTS_ATTEMPT "
                                + json.dumps(attempt_record, ensure_ascii=False),
                                flush=True,
                            )
                            candidate = (
                                compacted_duration,
                                sample_rate,
                                compacted_audio,
                                quality,
                                normalized_audio,
                            )
                            candidates.append(candidate)
                            if accepted_candidate:
                                viable.append(candidate)
                                print(
                                    f"TTS inference chunk {index}/{len(segments)} recovered with the requested reference "
                                    f"{ref_audio.name} on conservative attempt {recovery_attempt}",
                                    flush=True,
                                )
                                break
                    if accepted is None and viable:
                        duration, sample_rate, normalized_audio, accepted_quality, accepted_raw = min(
                            viable,
                            key=lambda item: abs(item[0] - expected_duration),
                        )
                        accepted = (sample_rate, fade_segment_edges(normalized_audio, sample_rate, fade_ms=12.0))
                        recovered_segments += 1
                        print(
                            f"TTS inference chunk {index}/{len(segments)} bounded recovery: "
                            f"{duration:.2f}s -> {len(accepted[1]) / sample_rate:.2f}s",
                            flush=True,
                        )
                    if accepted is None and not early_reference_fallback_requested:
                        near_complete = [
                            item for item in candidates
                            if near_complete_compact_candidate(
                                item,
                                acceptance_minimum,
                                maximum_duration,
                            )
                        ]
                        if near_complete:
                            duration, sample_rate, normalized_audio, accepted_quality, accepted_raw = min(
                                near_complete,
                                key=lambda item: abs(item[0] - expected_duration),
                            )
                            accepted_quality = {
                                **accepted_quality,
                                "nearThresholdRecovery": True,
                                "qualityGateVersion": "three-tier-v1",
                                "qualityTier": "gray_pass",
                                "qualityConfidence": max(
                                    0.82,
                                    float(accepted_quality.get("qualityConfidence") or 0.0),
                                ),
                                "qualityConfidenceCalibrated": False,
                                "qualityGateReason": "near_complete_active_speech",
                            }
                            accepted = (
                                sample_rate,
                                fade_segment_edges(normalized_audio, sample_rate, fade_ms=12.0),
                            )
                            recovered_segments += 1
                            print(
                                f"TTS inference chunk {index}/{len(segments)} near-threshold recovery: "
                                f"{duration:.2f}s active={accepted_quality.get('activeSpeechMs', 0)}ms",
                                flush=True,
                            )
                    if accepted is None:
                        self.last_synthesis_diagnostics = {
                            "status": "rejected",
                            "segments": len(segments),
                            "plannedSegments": len(segments),
                            "failedSegment": index,
                            "failedSegmentText": segment,
                            "resegmentationEnabled": resegment_on_failure,
                            "adaptiveRetryShadowEnabled": adaptive_retry_shadow,
                            "boundedRetryEnabled": bounded_retry,
                            "maxNormalAttempts": effective_normal_attempt_limit,
                            "maxConservativeAttempts": conservative_attempt_limit,
                            "highConfidenceResegmentEnabled": high_confidence_resegment,
                            "coherentFallbackRecommended": early_reference_fallback_requested,
                            "attemptCount": len(attempt_records),
                            "sameReferenceSeedRetryCount": sum(
                                1
                                for item in attempt_records
                                if item.get("sameReferenceSeedRetry")
                            ),
                            "retryCount": max(0, len(attempt_records) - len(generated_segments)),
                            "failedInferenceMs": sum(
                                int(item.get("generationMs") or 0)
                                for item in attempt_records
                                if item.get("failureType")
                            ),
                            "inferenceGenerationMs": sum(
                                int(item.get("generationMs") or 0)
                                for item in attempt_records
                            ),
                            "attempts": list(attempt_records),
                            "failureReasons": list(dict.fromkeys(
                                str(item.get("failureType") or "")
                                for item in attempt_records
                                if item.get("failureType")
                            )),
                            "rejectionCandidates": [
                                {
                                    "durationSec": round(float(duration), 4),
                                    **quality,
                                }
                                for duration, _sample_rate, _audio, quality, _raw_audio in candidates
                            ],
                            "referenceAudio": ref_audio.name,
                            "voiceTone": str(style or "calm"),
                            "fallbackStyle": str(fallback_style or "calm"),
                            "voiceIntensity": level,
                            "gptModelRoute": selected_gpt_route,
                            "gptModel": self.gpt_model_name_for_route(selected_gpt_route),
                            "utteranceSeed": utterance_seed,
                            "seedScope": "inference_segment",
                            "maxSegmentChars": self.max_segment_chars,
                            "semanticUnitContract": True,
                            "semanticUnitCount": len(required_semantic_units),
                            "semanticUnitsComplete": False,
                            "technicalTextNormalized": synthesis_text != re.sub(
                                r"\s+",
                                " ",
                                str(text or ""),
                            ).strip(),
                            "emojiFilteredForSpeech": emoji_filtered_for_speech,
                        }
                        raise RuntimeError(
                            f"GPT-SoVITS inference chunk {index}/{len(segments)} "
                            "produced no audio after retries"
                        )
                    if leading_pause_seconds > 0:
                        pause_samples = max(
                            1,
                            round(accepted[0] * leading_pause_seconds),
                        )
                        accepted = (
                            accepted[0],
                            np.concatenate((
                                np.zeros(pause_samples, dtype=np.float32),
                                np.asarray(accepted[1], dtype=np.float32),
                            )),
                        )
                        accepted_quality = {
                            **accepted_quality,
                            "leadingEllipsisProjected": True,
                            "leadingPauseMs": round(leading_pause_seconds * 1000),
                        }
                    generated_segments.append(accepted)
                    accepted_raw_segments.append((
                        accepted[0],
                        accepted_raw if accepted_raw is not None else accepted[1],
                    ))
                    segment_durations.append(round(len(accepted[1]) / accepted[0], 4))
                    segment_quality.append(accepted_quality)
                    if not (
                        accepted_quality.get("resegmentedAfterFailure")
                        and accepted_segment_texts
                        and accepted_segment_texts[-1] == segment
                    ):
                        accepted_segment_texts.append(segment)

            finally:
                self.inference_webui.max_sec = original_max_sec
                self._end_interactive_synthesis()

        if not generated_segments:
            self.last_synthesis_diagnostics = {
                "status": "rejected",
                "segments": 0,
                "plannedSegments": len(segments),
                "normalizedTextChars": visible_speech_chars(synthesis_text),
                "gptModelRoute": selected_gpt_route,
                "gptModel": self.gpt_model_name_for_route(selected_gpt_route),
                "failureReasons": ["empty_inference_plan"],
            }
            raise RuntimeError("GPT-SoVITS produced no audio")
        sample_rate = generated_segments[0][0]
        if any(rate != sample_rate for rate, _audio in generated_segments):
            raise RuntimeError("GPT-SoVITS returned inconsistent sample rates")
        if any(rate != sample_rate for rate, _audio in accepted_raw_segments):
            raise RuntimeError("GPT-SoVITS raw chunks used inconsistent sample rates")
        pause_seconds = [
            segment_pause_seconds(
                accepted_segment_texts[index],
                speed,
                locale=locale,
                prosody_profile=prosody_profile,
            )
            for index in range(max(0, len(accepted_segment_texts) - 1))
        ]
        raw_model_audio, raw_boundaries, _raw_pause_ms = join_audio_segments_legacy(
            accepted_raw_segments,
            pause_seconds,
        )
        stage_paths: dict[str, Any] = {}
        stage_paths["01_raw_model"] = write_tts_diagnostic_stage(
            diagnostic_root,
            diagnostic_trace_id,
            "01_raw_model.wav",
            raw_model_audio,
            sample_rate,
        )
        # The current delivery chain accepts only one model sample rate, so no
        # resampling is necessary.  Keep an explicit stage to prove that fact
        # when comparing captures; future resampling must happen exactly once.
        stage_paths["02_resampled"] = write_tts_diagnostic_stage(
            diagnostic_root,
            diagnostic_trace_id,
            "02_resampled.wav",
            raw_model_audio,
            sample_rate,
        )
        stage_paths["03_chunk_raw"] = [
            write_tts_diagnostic_stage(
                diagnostic_root,
                diagnostic_trace_id,
                f"03_chunk_raw_{index}.wav",
                chunk_audio,
                chunk_rate,
            )
            for index, (chunk_rate, chunk_audio) in enumerate(accepted_raw_segments, 1)
        ]
        generated_segments, level_matching = normalize_segment_levels(generated_segments)
        audio, stitched_audio, stitched_boundaries, segment_pauses_ms, post_roll_ms, peak_protection_db, delivery_mode = assemble_delivery_audio(
            generated_segments,
            pause_seconds,
            post_processing_enabled=bool(delivery_post_processing_enabled),
        )
        stage_paths["04_stitched"] = write_tts_diagnostic_stage(
            diagnostic_root,
            diagnostic_trace_id,
            "04_stitched.wav",
            stitched_audio,
            sample_rate,
        )

        reference_cache_key = ""
        reference_metrics: dict[str, Any] = {}
        try:
            reference_cache_key = f"{ref_audio.resolve()}:{ref_audio.stat().st_mtime_ns}"
            reference_metrics = self.reference_noise_metrics.get(reference_cache_key, {})
            if not reference_metrics:
                reference_audio, reference_rate = sf.read(ref_audio, dtype="float32")
                if getattr(reference_audio, "ndim", 1) > 1:
                    reference_audio = np.mean(reference_audio, axis=1, dtype=np.float32)
                reference_metrics = audio_delivery_metrics(reference_audio, int(reference_rate))
                self.reference_noise_metrics = {reference_cache_key: reference_metrics}
        except Exception:
            reference_metrics = {}

        raw_metrics = audio_delivery_metrics(
            raw_model_audio,
            sample_rate,
            boundary_samples=raw_boundaries,
            reference_metrics=reference_metrics,
        )
        stitched_metrics = audio_delivery_metrics(
            stitched_audio,
            sample_rate,
            boundary_samples=stitched_boundaries,
            reference_metrics=reference_metrics,
        )
        final_metrics = audio_delivery_metrics(
            audio,
            sample_rate,
            boundary_samples=stitched_boundaries,
            reference_metrics=reference_metrics,
        )
        stage_flags = {
            "01_raw_model": list(audio_noise_flags(raw_metrics)),
            "02_resampled": list(audio_noise_flags(raw_metrics)),
            "04_stitched": list(audio_noise_flags(stitched_metrics)),
            "05_final_delivery": list(audio_noise_flags(final_metrics)),
        }
        earliest_noisy_stage = next(
            (stage for stage, flags in stage_flags.items() if flags),
            "",
        )
        stage_paths["05_final_delivery"] = write_tts_diagnostic_stage(
            diagnostic_root,
            diagnostic_trace_id,
            "05_final_delivery.wav",
            audio,
            sample_rate,
            final_delivery=True,
        )
        self.last_synthesis_diagnostics = {
            "segments": len(generated_segments),
            "synthesisLockWaitMs": synthesis_lock_wait_ms,
            "plannedSegments": len(segments),
            "resegmentationEnabled": resegment_on_failure,
            "adaptiveRetryShadowEnabled": adaptive_retry_shadow,
            "boundedRetryEnabled": bounded_retry,
            "maxNormalAttempts": effective_normal_attempt_limit,
            "maxConservativeAttempts": conservative_attempt_limit,
            "highConfidenceResegmentEnabled": high_confidence_resegment,
            "attemptCount": len(attempt_records),
            "sameReferenceSeedRetryCount": sum(
                1
                for item in attempt_records
                if item.get("sameReferenceSeedRetry")
            ),
            "retryCount": max(0, len(attempt_records) - len(generated_segments)),
            "attempts": list(attempt_records),
            "failedInferenceMs": sum(
                int(item.get("generationMs") or 0)
                for item in attempt_records
                if item.get("failureType")
            ),
            "inferenceGenerationMs": sum(
                int(item.get("generationMs") or 0)
                for item in attempt_records
            ),
            "failureReasons": list(dict.fromkeys(
                str(item.get("failureType") or "")
                for item in attempt_records
                if item.get("failureType")
            )),
            "resegmentedSegments": sum(
                1
                for item in segment_quality
                if item.get("resegmentedAfterFailure")
            ),
            "recoveredSegments": recovered_segments,
            "qualityGateVersion": "three-tier-v1",
            "grayAcceptedSegments": sum(
                1
                for item in segment_quality
                if item.get("qualityTier") == "gray_pass"
            ),
            "skippedTerminalMicroSegments": [],
            "partialTailOmitted": False,
            "semanticUnitContract": True,
            "semanticUnitCount": len(required_semantic_units),
            "semanticUnitsComplete": True,
            "segmentDurations": segment_durations,
            "segmentQuality": segment_quality,
            "segmentLevelMatching": level_matching,
            "segmentPausesMs": segment_pauses_ms,
            "segmentLeadingPausesMs": [
                round(value * 1000) for value in segment_leading_pause_seconds
            ],
            "leadingEllipsisProjected": any(segment_leading_ellipsis),
            "deliveryPostProcessingEnabled": bool(delivery_post_processing_enabled),
            "deliveryMode": delivery_mode,
            "postRollMs": post_roll_ms,
            "peakProtectionDb": peak_protection_db,
            "resampleApplied": False,
            "resampleCount": 0,
            "diagnosticTraceId": diagnostic_trace_id,
            "diagnosticStages": stage_paths if diagnostic_root is not None else {},
            "stageMetrics": {
                "01_raw_model": raw_metrics,
                "02_resampled": raw_metrics,
                "04_stitched": stitched_metrics,
                "05_final_delivery": final_metrics,
            },
            "stageNoiseFlags": stage_flags,
            "earliestNoisyStage": earliest_noisy_stage,
            "coherentFallbackAttempted": coherent_fallback_attempted,
            "coherentReferenceFallback": coherent_fallback_applied,
            "referenceAudio": ref_audio.name,
            "voiceTone": str(style or "calm"),
            "fallbackStyle": str(fallback_style or "calm"),
            "voiceIntensity": level,
            "gptModelRoute": selected_gpt_route,
            "gptModel": self.gpt_model_name_for_route(selected_gpt_route),
            "utteranceSeed": utterance_seed,
            "seedScope": "inference_segment",
            "compactedSilenceMs": sum(int(item.get("compactedSilenceMs") or 0) for item in segment_quality),
            "sampleRate": sample_rate,
            "maxSegmentChars": self.max_segment_chars,
            "technicalTextNormalized": synthesis_text != re.sub(r"\s+", " ", str(text or "")).strip(),
            "emojiFilteredForSpeech": emoji_filtered_for_speech,
            "speechProsody": prosody_profile,
            "segmentTargetLanguages": segment_target_languages,
        }
        output = io.BytesIO()
        # Float32 is preserved through the full processing chain. Quantization
        # occurs once, at the delivery boundary.
        write_delivery_wav(output, audio, sample_rate)
        return output.getvalue()

    def synthesize_with_coherent_fallback(
        self,
        text: str,
        style: str,
        locale: str,
        *,
        speech_speed: float = 1.0,
        fallback_style: str = "",
        intensity: str = "normal",
        segment_mode: str = "auto",
        semantic_units: Any = None,
        speech_prosody: str = "natural",
        delivery_post_processing_enabled: bool = False,
    ) -> bytes:
        """Synthesize semantic units, retrying only the failed unit coherently.

        One semantic unit remains the smallest reference-fallback boundary: a
        failed internal acoustic chunk restarts that unit with one stable
        reference, never just the internal chunk.  If a caller supplies several
        complete semantic units, successful units are preserved and only the
        failed unit is rescued.
        """

        import numpy as np

        self.raise_if_cancelled()

        required_semantic_units = validate_semantic_units(text, semantic_units)
        if len(required_semantic_units) > 1:
            unit_audio_segments: list[tuple[int, Any]] = []
            unit_diagnostics: list[dict[str, Any]] = []
            for unit_index, unit in enumerate(required_semantic_units, 1):
                self.raise_if_cancelled()
                try:
                    unit_wav = self.synthesize_with_coherent_fallback(
                        unit,
                        style,
                        locale,
                        speech_speed=speech_speed,
                        fallback_style=fallback_style,
                        intensity=intensity,
                        segment_mode=segment_mode,
                        semantic_units=[unit],
                        speech_prosody=speech_prosody,
                        delivery_post_processing_enabled=delivery_post_processing_enabled,
                    )
                except RuntimeError as unit_error:
                    failed_diagnostics = dict(
                        getattr(self, "last_synthesis_diagnostics", {}) or {}
                    )
                    failed_unit_diagnostic = dict(failed_diagnostics)
                    failed_diagnostics.update({
                        "semanticUnitIsolation": True,
                        "semanticUnitCount": len(required_semantic_units),
                        "semanticUnitsComplete": False,
                        "failedSemanticUnit": unit_index,
                        "failedSemanticUnitText": unit,
                        "completedSemanticUnits": unit_index - 1,
                        "semanticUnitDiagnostics": unit_diagnostics + [
                            failed_unit_diagnostic
                        ],
                    })
                    self.last_synthesis_diagnostics = failed_diagnostics
                    raise RuntimeError(
                        f"GPT-SoVITS semantic unit {unit_index}/"
                        f"{len(required_semantic_units)} failed: {unit_error}"
                    ) from unit_error

                unit_diagnostic = dict(
                    getattr(self, "last_synthesis_diagnostics", {}) or {}
                )
                decoded_audio, sample_rate = sf.read(
                    io.BytesIO(unit_wav),
                    dtype="float32",
                )
                if getattr(decoded_audio, "ndim", 1) > 1:
                    decoded_audio = np.mean(decoded_audio, axis=1, dtype=np.float32)
                unit_audio_segments.append((
                    int(sample_rate),
                    np.asarray(decoded_audio, dtype=np.float32),
                ))
                unit_diagnostics.append(unit_diagnostic)

            if any(
                rate != unit_audio_segments[0][0]
                for rate, _audio in unit_audio_segments[1:]
            ):
                raise RuntimeError(
                    "GPT-SoVITS semantic units returned inconsistent sample rates"
                )
            pauses_seconds = [
                segment_pause_seconds(
                    unit,
                    speech_speed,
                    locale=locale,
                    prosody_profile=normalize_speech_prosody(speech_prosody),
                )
                for unit in required_semantic_units[:-1]
            ]
            audio, _boundaries, pause_ms = stitch_audio_segments(
                unit_audio_segments,
                pauses_seconds,
            )
            attempt_count = sum(
                int(item.get("attemptCount") or 0) for item in unit_diagnostics
            )
            retry_count = sum(
                int(item.get("retryCount") or 0) for item in unit_diagnostics
            )
            reference_audios = list(dict.fromkeys(
                str(item.get("referenceAudio") or "")
                for item in unit_diagnostics
                if item.get("referenceAudio")
            ))
            self.last_synthesis_diagnostics = {
                **unit_diagnostics[-1],
                "status": "accepted",
                "semanticUnitIsolation": True,
                "semanticUnitCount": len(required_semantic_units),
                "semanticUnitsComplete": True,
                "completedSemanticUnits": len(required_semantic_units),
                "semanticUnitDiagnostics": unit_diagnostics,
                "semanticUnitFallbackCount": sum(
                    1
                    for item in unit_diagnostics
                    if item.get("coherentReferenceFallback")
                ),
                "coherentFallbackAttempted": any(
                    item.get("coherentFallbackAttempted")
                    for item in unit_diagnostics
                ),
                "coherentReferenceFallback": any(
                    item.get("coherentReferenceFallback")
                    for item in unit_diagnostics
                ),
                "attemptCount": attempt_count,
                "sameReferenceSeedRetryCount": sum(
                    int(item.get("sameReferenceSeedRetryCount") or 0)
                    for item in unit_diagnostics
                ),
                "retryCount": retry_count,
                "failedInferenceMs": sum(
                    int(item.get("failedInferenceMs") or 0)
                    for item in unit_diagnostics
                ),
                "inferenceGenerationMs": sum(
                    int(item.get("inferenceGenerationMs") or 0)
                    for item in unit_diagnostics
                ),
                "failureReasons": list(dict.fromkeys(
                    reason
                    for item in unit_diagnostics
                    for reason in list(item.get("failureReasons") or [])
                )),
                "segments": sum(
                    int(item.get("segments") or 0) for item in unit_diagnostics
                ),
                "plannedSegments": sum(
                    int(item.get("plannedSegments") or 0)
                    for item in unit_diagnostics
                ),
                "segmentDurations": [
                    duration
                    for item in unit_diagnostics
                    for duration in list(item.get("segmentDurations") or [])
                ],
                "segmentQuality": [
                    quality
                    for item in unit_diagnostics
                    for quality in list(item.get("segmentQuality") or [])
                ],
                "segmentPausesMs": pause_ms,
                "referenceAudios": reference_audios,
                "referenceAudio": (
                    reference_audios[0]
                    if len(reference_audios) == 1
                    else "mixed"
                ),
                "deliveryMode": "semantic_unit_isolated_stitch",
                "sampleRate": unit_audio_segments[0][0],
            }
            output = io.BytesIO()
            write_delivery_wav(
                output,
                audio,
                unit_audio_segments[0][0],
            )
            return output.getvalue()

        primary_audio, _primary_text = self.style_ref(
            style,
            fallback_style,
            intensity,
        )
        try:
            return self.synthesize(
                text,
                style,
                locale,
                speech_speed=speech_speed,
                fallback_style=fallback_style,
                intensity=intensity,
                segment_mode=segment_mode,
                semantic_units=semantic_units,
                speech_prosody=speech_prosody,
                delivery_post_processing_enabled=delivery_post_processing_enabled,
            )
        except RuntimeError as primary_error:
            if "produced no audio after retries" not in str(primary_error):
                raise
            primary_diagnostics = dict(
                getattr(self, "last_synthesis_diagnostics", {}) or {}
            )
            primary_route = str(primary_diagnostics.get("gptModelRoute") or "default")
            primary_model = str(primary_diagnostics.get("gptModel") or "")
            fallback_route_override: str | None = None
            reference_fallback_has_priority = bool(
                primary_diagnostics.get("coherentFallbackRecommended")
            )

            # A routed candidate is an optimization, not the final recovery
            # boundary. If it rejects a complete visible utterance, retry that
            # same utterance with the stable base GPT and the same approved
            # reference before changing the speaker prompt. This preserves the
            # requested emotion while preventing one bad routed-model sample
            # from becoming a user-visible no-audio failure.
            if (
                not reference_fallback_has_priority
                and primary_route != "default"
                and "default" in getattr(self, "_gpt_runtime_models", {})
            ):
                print(
                    "TTS coherent model-route fallback "
                    f"{primary_route} -> default with {primary_audio.name}",
                    flush=True,
                )
                try:
                    audio = self.synthesize(
                        text,
                        style,
                        locale,
                        speech_speed=speech_speed,
                        fallback_style=fallback_style,
                        intensity=intensity,
                        segment_mode=segment_mode,
                        semantic_units=semantic_units,
                        speech_prosody=speech_prosody,
                        delivery_post_processing_enabled=delivery_post_processing_enabled,
                        gpt_route_override="default",
                    )
                except RuntimeError as model_fallback_error:
                    if "produced no audio after retries" not in str(model_fallback_error):
                        raise
                    model_fallback_diagnostics = dict(
                        getattr(self, "last_synthesis_diagnostics", {}) or {}
                    )
                    model_fallback_diagnostics.update({
                        "modelFallbackAttempted": True,
                        "modelFallbackApplied": False,
                        "primaryGptModelRoute": primary_route,
                        "primaryGptModel": primary_model,
                        "fallbackGptModelRoute": "default",
                        "fallbackGptModel": self.gpt_model_name_for_route("default"),
                        "primaryAttemptCount": int(
                            primary_diagnostics.get("attemptCount") or 0
                        ),
                        "primaryFailedInferenceMs": int(
                            primary_diagnostics.get("failedInferenceMs") or 0
                        ),
                        "primaryFailureReasons": list(
                            primary_diagnostics.get("failureReasons") or []
                        ),
                        "attemptCount": (
                            int(primary_diagnostics.get("attemptCount") or 0)
                            + int(model_fallback_diagnostics.get("attemptCount") or 0)
                        ),
                        "sameReferenceSeedRetryCount": (
                            int(primary_diagnostics.get("sameReferenceSeedRetryCount") or 0)
                            + int(model_fallback_diagnostics.get("sameReferenceSeedRetryCount") or 0)
                        ),
                        "retryCount": (
                            int(primary_diagnostics.get("retryCount") or 0)
                            + int(model_fallback_diagnostics.get("retryCount") or 0)
                            + 1
                        ),
                        "failedInferenceMs": (
                            int(primary_diagnostics.get("failedInferenceMs") or 0)
                            + int(model_fallback_diagnostics.get("failedInferenceMs") or 0)
                        ),
                        "failureReasons": list(dict.fromkeys([
                            *list(primary_diagnostics.get("failureReasons") or []),
                            *list(model_fallback_diagnostics.get("failureReasons") or []),
                        ])),
                    })
                    primary_diagnostics = model_fallback_diagnostics
                    fallback_route_override = "default"
                else:
                    model_fallback_diagnostics = dict(
                        getattr(self, "last_synthesis_diagnostics", {}) or {}
                    )
                    model_fallback_diagnostics.update({
                        "modelFallbackAttempted": True,
                        "modelFallbackApplied": True,
                        "primaryGptModelRoute": primary_route,
                        "primaryGptModel": primary_model,
                        "fallbackGptModelRoute": "default",
                        "fallbackGptModel": self.gpt_model_name_for_route("default"),
                        "primaryAttemptCount": int(
                            primary_diagnostics.get("attemptCount") or 0
                        ),
                        "primaryFailedInferenceMs": int(
                            primary_diagnostics.get("failedInferenceMs") or 0
                        ),
                        "primaryFailureReasons": list(
                            primary_diagnostics.get("failureReasons") or []
                        ),
                        "attemptCount": (
                            int(primary_diagnostics.get("attemptCount") or 0)
                            + int(model_fallback_diagnostics.get("attemptCount") or 0)
                        ),
                        "sameReferenceSeedRetryCount": (
                            int(primary_diagnostics.get("sameReferenceSeedRetryCount") or 0)
                            + int(model_fallback_diagnostics.get("sameReferenceSeedRetryCount") or 0)
                        ),
                        "retryCount": (
                            int(primary_diagnostics.get("retryCount") or 0)
                            + int(model_fallback_diagnostics.get("retryCount") or 0)
                            + 1
                        ),
                        "failedInferenceMs": (
                            int(primary_diagnostics.get("failedInferenceMs") or 0)
                            + int(model_fallback_diagnostics.get("failedInferenceMs") or 0)
                        ),
                        "failureReasons": list(dict.fromkeys([
                            *list(primary_diagnostics.get("failureReasons") or []),
                            *list(model_fallback_diagnostics.get("failureReasons") or []),
                        ])),
                    })
                    self.last_synthesis_diagnostics = model_fallback_diagnostics
                    return audio

            fallback_reference = self.coherent_reference_fallback(
                style,
                fallback_style,
                intensity,
                primary_audio,
            )
            if fallback_reference is None:
                primary_diagnostics.update({
                    "coherentFallbackAttempted": False,
                    "coherentReferenceFallback": False,
                    "coherentFallbackUnavailable": True,
                })
                self.last_synthesis_diagnostics = primary_diagnostics
                raise
            fallback_audio, fallback_text = fallback_reference
            print(
                "TTS coherent visible-segment fallback "
                f"{primary_audio.name} -> {fallback_audio.name}",
                flush=True,
            )
            try:
                audio = self.synthesize(
                    text,
                    style,
                    locale,
                    speech_speed=speech_speed,
                    fallback_style=fallback_style,
                    intensity=intensity,
                    segment_mode=segment_mode,
                    semantic_units=semantic_units,
                    speech_prosody=speech_prosody,
                    delivery_post_processing_enabled=delivery_post_processing_enabled,
                    reference_override=(fallback_audio, fallback_text),
                    gpt_route_override=fallback_route_override,
                )
            except RuntimeError as fallback_error:
                failed_fallback_diagnostics = dict(
                    getattr(self, "last_synthesis_diagnostics", {}) or {}
                )
                combined_diagnostics = {
                    **primary_diagnostics,
                    "coherentFallbackAttempted": True,
                    "coherentReferenceFallback": False,
                    "attempts": [
                        *list(primary_diagnostics.get("attempts") or []),
                        *list(failed_fallback_diagnostics.get("attempts") or []),
                    ],
                    "modelFallbackAttempted": bool(
                        primary_diagnostics.get("modelFallbackAttempted")
                    ),
                    "modelFallbackApplied": bool(
                        primary_diagnostics.get("modelFallbackApplied")
                    ),
                    "primaryGptModelRoute": primary_diagnostics.get(
                        "primaryGptModelRoute"
                    ),
                    "primaryGptModel": primary_diagnostics.get("primaryGptModel"),
                    "fallbackGptModelRoute": primary_diagnostics.get(
                        "fallbackGptModelRoute"
                    ),
                    "fallbackGptModel": primary_diagnostics.get("fallbackGptModel"),
                    "primaryReferenceAudio": primary_audio.name,
                    "fallbackReferenceAudio": fallback_audio.name,
                    "referenceFallbackPriorityReason": (
                        "high_confidence_reference_conditioning_failure"
                        if reference_fallback_has_priority
                        else ""
                    ),
                    "primaryAttemptCount": int(primary_diagnostics.get("attemptCount") or 0),
                    "primaryFailedInferenceMs": int(
                        primary_diagnostics.get("failedInferenceMs") or 0
                    ),
                    "primaryFailureReasons": list(
                        primary_diagnostics.get("failureReasons") or []
                    ),
                    "fallbackFailedSegment": int(
                        failed_fallback_diagnostics.get("failedSegment") or 0
                    ),
                    "fallbackAttemptCount": int(
                        failed_fallback_diagnostics.get("attemptCount") or 0
                    ),
                    "fallbackFailedInferenceMs": int(
                        failed_fallback_diagnostics.get("failedInferenceMs") or 0
                    ),
                    "fallbackFailureReasons": list(
                        failed_fallback_diagnostics.get("failureReasons") or []
                    ),
                    "fallbackError": str(fallback_error)[:500],
                    "attemptCount": (
                        int(primary_diagnostics.get("attemptCount") or 0)
                        + int(failed_fallback_diagnostics.get("attemptCount") or 0)
                    ),
                    "sameReferenceSeedRetryCount": (
                        int(primary_diagnostics.get("sameReferenceSeedRetryCount") or 0)
                        + int(failed_fallback_diagnostics.get("sameReferenceSeedRetryCount") or 0)
                    ),
                    "retryCount": (
                        int(primary_diagnostics.get("retryCount") or 0)
                        + int(failed_fallback_diagnostics.get("retryCount") or 0)
                        + 1
                    ),
                    "failedInferenceMs": (
                        int(primary_diagnostics.get("failedInferenceMs") or 0)
                        + int(failed_fallback_diagnostics.get("failedInferenceMs") or 0)
                    ),
                }
                self.last_synthesis_diagnostics = combined_diagnostics
                # The fallback is an attempted recovery, not the owner of the
                # original rejection. Preserve the primary failed chunk in the
                # user-visible error while retaining complete fallback details
                # in diagnostics; otherwise a tail failure misleadingly appears
                # as "chunk 1" when the alternate reference also rejects.
                raise primary_error from fallback_error
            fallback_diagnostics = dict(
                getattr(self, "last_synthesis_diagnostics", {}) or {}
            )
            fallback_diagnostics.update({
                "coherentFallbackAttempted": True,
                "coherentReferenceFallback": True,
                "attempts": [
                    *list(primary_diagnostics.get("attempts") or []),
                    *list(fallback_diagnostics.get("attempts") or []),
                ],
                "modelFallbackAttempted": bool(
                    primary_diagnostics.get("modelFallbackAttempted")
                ),
                "modelFallbackApplied": bool(
                    primary_diagnostics.get("modelFallbackApplied")
                ),
                "primaryGptModelRoute": primary_diagnostics.get(
                    "primaryGptModelRoute"
                ),
                "primaryGptModel": primary_diagnostics.get("primaryGptModel"),
                "fallbackGptModelRoute": primary_diagnostics.get(
                    "fallbackGptModelRoute"
                ),
                "fallbackGptModel": primary_diagnostics.get("fallbackGptModel"),
                "primaryReferenceAudio": primary_audio.name,
                "fallbackReferenceAudio": fallback_audio.name,
                "referenceFallbackPriorityReason": (
                    "high_confidence_reference_conditioning_failure"
                    if reference_fallback_has_priority
                    else ""
                ),
                "primaryAttemptCount": int(
                    primary_diagnostics.get("attemptCount") or 0
                ),
                "primaryFailedInferenceMs": int(
                    primary_diagnostics.get("failedInferenceMs") or 0
                ),
                "primaryFailureReasons": list(
                    primary_diagnostics.get("failureReasons") or []
                ),
                "attemptCount": (
                    int(primary_diagnostics.get("attemptCount") or 0)
                    + int(fallback_diagnostics.get("attemptCount") or 0)
                ),
                "sameReferenceSeedRetryCount": (
                    int(primary_diagnostics.get("sameReferenceSeedRetryCount") or 0)
                    + int(fallback_diagnostics.get("sameReferenceSeedRetryCount") or 0)
                ),
                "retryCount": (
                    int(primary_diagnostics.get("retryCount") or 0)
                    + int(fallback_diagnostics.get("retryCount") or 0)
                    + 1
                ),
                "failedInferenceMs": (
                    int(primary_diagnostics.get("failedInferenceMs") or 0)
                    + int(fallback_diagnostics.get("failedInferenceMs") or 0)
                ),
                "failureReasons": list(dict.fromkeys([
                    *list(primary_diagnostics.get("failureReasons") or []),
                    *list(fallback_diagnostics.get("failureReasons") or []),
                ])),
            })
            self.last_synthesis_diagnostics = fallback_diagnostics
            return audio

    def timed_synthesize(
        self,
        text: str,
        style: str,
        locale: str,
        align_model: str,
        speech_speed: float = 1.0,
        fallback_style: str = "",
        intensity: str = "normal",
        segment_mode: str = "auto",
        semantic_units: Any = None,
        speech_prosody: str = "natural",
        delivery_post_processing_enabled: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        audio = self.synthesize_with_coherent_fallback(
            text,
            style,
            locale,
            speech_speed=speech_speed,
            fallback_style=fallback_style,
            intensity=intensity,
            segment_mode=segment_mode,
            semantic_units=semantic_units,
            speech_prosody=speech_prosody,
            delivery_post_processing_enabled=delivery_post_processing_enabled,
        )
        synthesis_ms = round((time.perf_counter() - started) * 1000)
        align_started = time.perf_counter()
        alignment = self.fast_align_audio(audio, text) if align_model == "fast" else self.align_audio(audio, text, align_model)
        alignment_ms = round((time.perf_counter() - align_started) * 1000)
        return {
            "ok": True,
            "contentType": "audio/wav",
            "audioBase64": base64.b64encode(audio).decode("ascii"),
            "alignment": alignment,
            "metrics": {
                "synthesisMs": synthesis_ms,
                "alignmentMs": alignment_ms,
                "totalMs": round((time.perf_counter() - started) * 1000),
                **self.last_synthesis_diagnostics,
            },
        }

    def prepare_synthesis(
        self,
        text: str,
        style: str,
        locale: str,
        align_model: str,
        speech_speed: float = 1.0,
        fallback_style: str = "",
        intensity: str = "normal",
        segment_mode: str = "auto",
        semantic_units: Any = None,
        speech_prosody: str = "natural",
        delivery_post_processing_enabled: bool = False,
    ) -> dict[str, Any]:
        """Return synthesized audio immediately and align that exact WAV in the background."""
        started = time.perf_counter()
        audio = self.synthesize_with_coherent_fallback(
            text,
            style,
            locale,
            speech_speed=speech_speed,
            fallback_style=fallback_style,
            intensity=intensity,
            segment_mode=segment_mode,
            semantic_units=semantic_units,
            speech_prosody=speech_prosody,
            delivery_post_processing_enabled=delivery_post_processing_enabled,
        )
        synthesis_ms = round((time.perf_counter() - started) * 1000)
        provisional_alignment = self.provisional_align_audio(audio, text)
        alignment_id = uuid.uuid4().hex
        job = {
            "status": "pending",
            "createdAt": time.time(),
            "alignment": provisional_alignment,
            "alignmentMs": 0,
            "error": "",
        }
        with self.deferred_alignment_lock:
            self._prune_deferred_alignment_jobs_locked()
            self.deferred_alignment_jobs[alignment_id] = job

        threading.Thread(
            target=self._run_deferred_alignment,
            args=(alignment_id, audio, text, align_model),
            daemon=True,
            name=f"tts-align-{alignment_id[:8]}",
        ).start()
        return {
            "ok": True,
            "contentType": "audio/wav",
            "audioBase64": base64.b64encode(audio).decode("ascii"),
            "alignmentId": alignment_id,
            "alignment": provisional_alignment,
            "metrics": {
                "synthesisMs": synthesis_ms,
                "alignmentMs": 0,
                "totalMs": synthesis_ms,
                "alignmentDeferred": True,
                **self.last_synthesis_diagnostics,
            },
        }

    def deferred_alignment_result(self, alignment_id: str) -> dict[str, Any]:
        with self.deferred_alignment_lock:
            self._prune_deferred_alignment_jobs_locked()
            job = self.deferred_alignment_jobs.get(str(alignment_id or ""))
            if not job:
                return {"ok": False, "status": "missing", "error": "alignment_not_found"}
            return {
                "ok": job["status"] != "failed",
                "status": job["status"],
                "alignment": job.get("alignment"),
                "alignmentMs": int(job.get("alignmentMs") or 0),
                "error": str(job.get("error") or ""),
            }

    def _run_deferred_alignment(self, alignment_id: str, audio: bytes, text: str, align_model: str) -> None:
        started = time.perf_counter()
        try:
            alignment = self.fast_align_audio(audio, text) if align_model == "fast" else self.align_audio(audio, text, align_model)
            update = {
                "status": "ready",
                "alignment": alignment,
                "alignmentMs": round((time.perf_counter() - started) * 1000),
                "error": "",
            }
        except Exception as error:
            update = {
                "status": "failed",
                "alignment": None,
                "alignmentMs": round((time.perf_counter() - started) * 1000),
                "error": str(error),
            }
        with self.deferred_alignment_lock:
            job = self.deferred_alignment_jobs.get(alignment_id)
            if job:
                job.update(update)

    def _prune_deferred_alignment_jobs_locked(self) -> None:
        cutoff = time.time() - 300
        expired = [key for key, value in self.deferred_alignment_jobs.items() if float(value.get("createdAt") or 0) < cutoff]
        for key in expired:
            self.deferred_alignment_jobs.pop(key, None)
        if len(self.deferred_alignment_jobs) > 64:
            ordered = sorted(self.deferred_alignment_jobs, key=lambda key: float(self.deferred_alignment_jobs[key].get("createdAt") or 0))
            for key in ordered[: len(self.deferred_alignment_jobs) - 64]:
                self.deferred_alignment_jobs.pop(key, None)

    def fast_align_audio(self, audio: bytes, requested_text: str) -> dict[str, Any]:
        import numpy as np

        audio_data, sample_rate = sf.read(io.BytesIO(audio), dtype="float32")
        if getattr(audio_data, "ndim", 1) > 1:
            audio_data = audio_data.mean(axis=1)
        duration = len(audio_data) / float(sample_rate) if sample_rate else 0.0
        frame_size = max(1, int(sample_rate * 0.01))
        usable = len(audio_data) - (len(audio_data) % frame_size)
        speech_start = 0.0
        speech_end = max(0.01, duration)
        if usable >= frame_size:
            frames = audio_data[:usable].reshape(-1, frame_size)
            rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-10)
            peak = float(np.percentile(rms, 95)) if len(rms) else 0.0
            floor = float(np.percentile(rms, 20)) if len(rms) else 0.0
            threshold = max(0.0035, floor * 2.8, peak * 0.075)
            active = np.flatnonzero(rms >= threshold)
            if len(active):
                speech_start = max(0.0, active[0] * 0.01 - 0.025)
                speech_end = min(duration, (active[-1] + 1) * 0.01 + 0.045)
        words = [{"text": requested_text, "start": round(speech_start, 4), "end": round(speech_end, 4)}]
        alignment_language = self.target_language or "日文"
        mouth_cues = (
            provisional_mouth_cues(
                requested_text,
                speech_start,
                speech_end,
                alignment_language,
            )
            if is_chinese_target_language(alignment_language)
            else self.rhubarb_mouth_cues(audio, requested_text)
        )
        return {
            "source": (
                "pypinyin-anime+audio-envelope"
                if is_chinese_target_language(alignment_language)
                else "rhubarb-visemes+audio-envelope"
                if mouth_cues
                else "audio-envelope-anime"
            ),
            "model": "fast",
            "requestedText": requested_text,
            "asrText": "",
            "duration": round(duration, 4),
            "speechStart": round(speech_start, 4),
            "speechEnd": round(speech_end, 4),
            "words": words,
            "recognizedItems": [],
            "items": align_requested_text_to_span(requested_text, words, duration),
            "mouthCues": mouth_cues,
        }

    def provisional_align_audio(self, audio: bytes, requested_text: str) -> dict[str, Any]:
        """Create low-frequency anime visemes without waiting for Rhubarb."""
        import numpy as np

        audio_data, sample_rate = sf.read(io.BytesIO(audio), dtype="float32")
        if getattr(audio_data, "ndim", 1) > 1:
            audio_data = audio_data.mean(axis=1)
        duration = len(audio_data) / float(sample_rate) if sample_rate else 0.0
        frame_size = max(1, int(sample_rate * 0.01))
        usable = len(audio_data) - (len(audio_data) % frame_size)
        speech_start = 0.0
        speech_end = max(0.01, duration)
        if usable >= frame_size:
            frames = audio_data[:usable].reshape(-1, frame_size)
            rms = np.sqrt(np.mean(np.square(frames), axis=1) + 1e-10)
            peak = float(np.percentile(rms, 95)) if len(rms) else 0.0
            floor = float(np.percentile(rms, 20)) if len(rms) else 0.0
            active = np.flatnonzero(rms >= max(0.0035, floor * 2.8, peak * 0.075))
            if len(active):
                speech_start = max(0.0, active[0] * 0.01 - 0.025)
                speech_end = min(duration, (active[-1] + 1) * 0.01 + 0.045)
        alignment_language = self.target_language or "日文"
        mouth_cues = provisional_mouth_cues(
            requested_text,
            speech_start,
            speech_end,
            alignment_language,
        )
        words = [{"text": requested_text, "start": round(speech_start, 4), "end": round(speech_end, 4)}]
        return {
            "source": (
                "pypinyin-anime+audio-envelope"
                if is_chinese_target_language(alignment_language)
                else "pyopenjtalk-anime+audio-envelope"
            ),
            "model": "provisional-anime",
            "requestedText": requested_text,
            "asrText": "",
            "duration": round(duration, 4),
            "speechStart": round(speech_start, 4),
            "speechEnd": round(speech_end, 4),
            "words": words,
            "recognizedItems": [],
            "items": align_requested_text_to_span(requested_text, words, duration),
            "mouthCues": mouth_cues,
        }

    def align_audio(self, audio: bytes, requested_text: str, model_size: str) -> dict[str, Any]:
        with self.aligner_lock:
            if self.aligner is None:
                from faster_whisper import WhisperModel

                self.aligner = WhisperModel(model_size, device="cpu", compute_type="int8")
            audio_data, sample_rate = sf.read(io.BytesIO(audio), dtype="float32")
            if getattr(audio_data, "ndim", 1) > 1:
                audio_data = audio_data.mean(axis=1)
            duration = len(audio_data) / float(sample_rate) if sample_rate else 0.0
            if sample_rate != 16000:
                import librosa

                audio_data = librosa.resample(audio_data, orig_sr=sample_rate, target_sr=16000)
            asr_language = "zh" if is_chinese_target_language(self.target_language) else "ja"
            segments, _info = self.aligner.transcribe(
                audio_data,
                language=asr_language,
                vad_filter=False,
                word_timestamps=True,
            )
            words: list[dict[str, Any]] = []
            for segment in segments:
                for word in segment.words or []:
                    token = str(word.word or "").strip()
                    if not token:
                        continue
                    words.append({
                        "text": token,
                        "start": round(float(word.start), 4),
                        "end": round(float(word.end), 4),
                    })
        recognized_items = expand_words_to_chars(words)
        items = align_requested_text_to_span(requested_text, words, duration)
        alignment_language = self.target_language or "日文"
        mouth_cues = (
            provisional_mouth_cues(requested_text, 0.0, duration, alignment_language)
            if is_chinese_target_language(alignment_language)
            else self.rhubarb_mouth_cues(audio, requested_text)
        )
        return {
            "source": (
                "pypinyin-anime+faster-whisper-span"
                if is_chinese_target_language(alignment_language)
                else "rhubarb-visemes+faster-whisper-span"
                if mouth_cues
                else "faster-whisper-span-requested-text"
            ),
            "model": model_size,
            "requestedText": requested_text,
            "asrText": "".join(item["text"] for item in words),
            "duration": round(duration, 4),
            "words": words,
            "recognizedItems": recognized_items,
            "items": items,
            "mouthCues": mouth_cues,
        }

    def rhubarb_mouth_cues(self, audio: bytes, requested_text: str) -> list[dict[str, Any]]:
        if not self.rhubarb_exe or not self.rhubarb_exe.exists():
            return []
        with tempfile.TemporaryDirectory(prefix="amadeus_rhubarb_") as tmp:
            tmp_path = Path(tmp)
            wav_path = tmp_path / "speech.wav"
            dialog_path = tmp_path / "dialog.txt"
            json_path = tmp_path / "mouth.json"
            wav_path.write_bytes(audio)
            dialog_path.write_text(requested_text, encoding="utf-8")
            command = [
                str(self.rhubarb_exe),
                "-r",
                "phonetic",
                "--extendedShapes",
                "GHX",
                "-f",
                "json",
                "-d",
                str(dialog_path),
                "-o",
                str(json_path),
                str(wav_path),
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, timeout=60)
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception as error:
                print(f"Rhubarb alignment failed: {error}", file=sys.stderr)
                return []
        cues = payload.get("mouthCues") if isinstance(payload, dict) else []
        result: list[dict[str, Any]] = []
        for cue in (cues if isinstance(cues, list) else []):
            result.append({
                "start": round(float(cue.get("start") or 0.0), 4),
                "end": round(float(cue.get("end") or 0.0), 4),
                "value": str(cue.get("value") or "X"),
            })
        return result


def align_requested_text_to_span(text: str, words: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    chars = [char for char in str(text or "") if not char.isspace()]
    if not chars:
        return []
    if words:
        start = max(0.0, float(words[0].get("start") or 0.0))
        end = max(start + 0.01, float(words[-1].get("end") or duration or start + 0.01))
    else:
        start = 0.0
        end = max(0.01, duration)
    weights = [char_duration_weight(char) for char in chars]
    total = sum(weights) or len(chars)
    span = max(0.01, end - start)
    cursor = start
    items: list[dict[str, Any]] = []
    for char, weight in zip(chars, weights):
        next_cursor = cursor + span * (weight / total)
        items.append({
            "text": char,
            "start": round(cursor, 4),
            "end": round(next_cursor, 4),
        })
        cursor = next_cursor
    if items:
        items[-1]["end"] = round(end, 4)
    return items


def char_duration_weight(char: str) -> float:
    if re_match(r"[。！？.!?]", char):
        return 0.45
    if re_match(r"[，、；：,;:]", char):
        return 0.35
    if re_match(r"[ぁぃぅぇぉゃゅょァィゥェォャュョっッー]", char):
        return 0.55
    if re_match(r"[んン]", char):
        return 0.75
    return 1.0


def re_match(pattern: str, text: str) -> bool:
    import re

    return bool(re.match(pattern, text))


def expand_words_to_chars(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for word in words:
        text = str(word.get("text") or "")
        chars = [char for char in text if not char.isspace()]
        if not chars:
            continue
        start = float(word.get("start") or 0.0)
        end = max(start, float(word.get("end") or start))
        step = (end - start) / max(1, len(chars))
        for index, char in enumerate(chars):
            item_start = start + step * index
            item_end = start + step * (index + 1)
            items.append({
                "text": char,
                "start": round(item_start, 4),
                "end": round(item_end, 4),
            })
    return items


def make_handler(engine: GPTSoVITSEngine):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/health"):
                reference_profile_version = runtime_fingerprint(json.dumps(
                    engine.style_map,
                    ensure_ascii=False,
                    sort_keys=True,
                ))
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ok": True,
                    "service": "gpt-sovits-tts",
                    "pid": os.getpid(),
                    "instanceSignature": engine.instance_signature,
                    "gptModel": engine.gpt_model.name,
                    "gptModelRoutes": {
                        route: path.name for route, path in engine.gpt_model_routes.items()
                    },
                    "sovitsModel": engine.sovits_model.name,
                    "bootstrapGptModel": engine.bootstrap_gpt_model.name,
                    "bootstrapSovitsModel": engine.bootstrap_sovits_model.name,
                    "refLanguage": engine.ref_language,
                    "targetLanguage": engine.target_language,
                    "speechLanguage": (
                        "zh-CN" if is_chinese_target_language(engine.target_language) else "ja-JP"
                    ),
                    "criticalWarmup": {
                        "ready": bool(getattr(engine, "critical_warmup_routes", [])),
                        "routes": list(getattr(engine, "critical_warmup_routes", [])),
                        "elapsedMs": int(getattr(engine, "critical_warmup_elapsed_ms", 0)),
                    },
                    "capabilities": {
                        **tts_runtime_capabilities(),
                        "referenceProfileVersion": reference_profile_version,
                        "criticalRouteWarmup": bool(
                            getattr(engine, "critical_warmup_routes", [])
                        ),
                    },
                }).encode("utf-8"))
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if self.path.startswith("/shutdown"):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if str(body.get("instanceSignature") or "") != engine.instance_signature:
                    self.send_error(409, "instance signature mismatch")
                    return
                payload = json.dumps({"ok": True, "stopping": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path.startswith("/tts_cancel"):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                request_id = str(body.get("requestId") or "").strip()
                cancelled = engine.cancel_request(request_id)
                payload = json.dumps({
                    "ok": bool(cancelled),
                    "cancelled": bool(cancelled),
                    "requestId": request_id,
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200 if cancelled else 400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if not self.path.startswith(("/tts_prepare", "/tts_alignment", "/tts_timed", "/tts")):
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                text = str(body.get("text") or "").strip()
                if not text:
                    if not self.path.startswith("/tts_alignment"):
                        raise ValueError("text is empty")
                expected_speech_language = (
                    "zh-CN" if is_chinese_target_language(engine.target_language) else "ja-JP"
                )
                requested_speech_language = str(
                    body.get("speechLanguage") or body.get("locale") or expected_speech_language
                ).strip().lower().replace("_", "-")
                requested_speech_language = (
                    "zh-CN"
                    if requested_speech_language.startswith("zh") or "中文" in requested_speech_language
                    else "ja-JP"
                )
                if requested_speech_language != expected_speech_language:
                    raise ValueError(
                        "request speechLanguage does not match the running GPT-SoVITS target language"
                    )
                if self.path.startswith("/tts_alignment"):
                    payload = engine.deferred_alignment_result(str(body.get("alignmentId") or ""))
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(200 if payload.get("status") != "missing" else 404)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path.startswith("/tts_prepare"):
                    engine.begin_request(body.get("requestId"))
                    try:
                        payload = engine.prepare_synthesis(
                            text=text,
                            style=str(body.get("voiceTone") or body.get("voiceStyle") or "calm"),
                            fallback_style=str(body.get("voiceStyle") or "calm"),
                            intensity=str(body.get("voiceIntensity") or "normal"),
                            locale=str(body.get("locale") or "ja-JP"),
                            align_model=str(body.get("alignModel") or "fast"),
                            speech_speed=float(body.get("speechSpeed") or 1.0),
                            segment_mode=str(body.get("segmentMode") or "auto"),
                            semantic_units=body.get("semanticUnits"),
                            speech_prosody=str(body.get("speechProsody") or "natural"),
                            delivery_post_processing_enabled=bool(body.get("deliveryPostProcessingEnabled", False)),
                        )
                    finally:
                        engine.end_request()
                    payload.setdefault("metrics", {})["emotionIntensity"] = body.get("emotionIntensity")
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    engine.start_deferred_warmup()
                    return
                if self.path.startswith("/tts_timed"):
                    engine.begin_request(body.get("requestId"))
                    try:
                        payload = engine.timed_synthesize(
                            text=text,
                            style=str(body.get("voiceTone") or body.get("voiceStyle") or "calm"),
                            fallback_style=str(body.get("voiceStyle") or "calm"),
                            intensity=str(body.get("voiceIntensity") or "normal"),
                            locale=str(body.get("locale") or "ja-JP"),
                            align_model=str(body.get("alignModel") or "small"),
                            speech_speed=float(body.get("speechSpeed") or 1.0),
                            segment_mode=str(body.get("segmentMode") or "auto"),
                            semantic_units=body.get("semanticUnits"),
                            speech_prosody=str(body.get("speechProsody") or "natural"),
                            delivery_post_processing_enabled=bool(body.get("deliveryPostProcessingEnabled", False)),
                        )
                    finally:
                        engine.end_request()
                    payload.setdefault("metrics", {})["emotionIntensity"] = body.get("emotionIntensity")
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    engine.start_deferred_warmup()
                    return
                engine.begin_request(body.get("requestId"))
                try:
                    audio = engine.synthesize_with_coherent_fallback(
                        text=text,
                        style=str(body.get("voiceTone") or body.get("voiceStyle") or "calm"),
                        fallback_style=str(body.get("voiceStyle") or "calm"),
                        intensity=str(body.get("voiceIntensity") or "normal"),
                        locale=str(body.get("locale") or "ja-JP"),
                        speech_speed=float(body.get("speechSpeed") or 1.0),
                        segment_mode=str(body.get("segmentMode") or "auto"),
                        semantic_units=body.get("semanticUnits"),
                        speech_prosody=str(body.get("speechProsody") or "natural"),
                        delivery_post_processing_enabled=bool(body.get("deliveryPostProcessingEnabled", False)),
                    )
                finally:
                    engine.end_request()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
                engine.start_deferred_warmup()
            except SynthesisCancelled as error:
                payload = json.dumps({
                    "ok": False,
                    "cancelled": True,
                    "error": str(error),
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(409)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as error:
                diagnostics = dict(engine.last_synthesis_diagnostics)
                payload = json.dumps({
                    "ok": False,
                    "error": str(error),
                    "diagnostics": {
                        key: diagnostics.get(key)
                        for key in (
                            "status", "segments", "plannedSegments",
                            "failedSegment", "resegmentationEnabled",
                            "adaptiveRetryShadowEnabled", "boundedRetryEnabled",
                            "highConfidenceResegmentEnabled", "attemptCount",
                            "retryCount", "failedInferenceMs",
                            "inferenceGenerationMs", "failureReasons", "attempts",
                            "referenceAudio", "voiceTone", "voiceIntensity",
                            "gptModelRoute", "gptModel",
                            "modelFallbackAttempted", "modelFallbackApplied",
                            "primaryGptModelRoute", "primaryGptModel",
                            "fallbackGptModelRoute", "fallbackGptModel",
                            "coherentFallbackAttempted",
                            "coherentReferenceFallback",
                            "coherentFallbackUnavailable",
                            "primaryReferenceAudio", "primaryAttemptCount",
                            "primaryFailedInferenceMs", "primaryFailureReasons",
                            "fallbackReferenceAudio", "fallbackFailedSegment",
                            "fallbackAttemptCount", "fallbackFailedInferenceMs",
                            "fallbackFailureReasons", "fallbackError",
                            "semanticUnitContract", "semanticUnitCount",
                            "semanticUnitsComplete", "partialTailOmitted",
                        )
                        if diagnostics.get(key) is not None
                    },
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            print("%s - %s" % (self.address_string(), format % args))

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9881)
    parser.add_argument("--gpt-sovits-root", type=Path, default=Path(".sources/GPT-SoVITS"))
    parser.add_argument("--gpt-model", type=Path, required=True)
    parser.add_argument("--sovits-model", type=Path, required=True)
    parser.add_argument("--bootstrap-gpt-model", type=Path)
    parser.add_argument("--bootstrap-sovits-model", type=Path)
    parser.add_argument(
        "--gpt-model-route",
        action="append",
        default=[],
        metavar="SHORT_OR_LONG=PATH",
    )
    parser.add_argument(
        "--gpt-route-short-max-chars",
        type=int,
        default=DEFAULT_SHORT_ROUTE_MAX_CHARS,
    )
    parser.add_argument("--gpt-route-long-min-chars", type=int, default=60)
    parser.add_argument("--instance-signature", default="")
    parser.add_argument("--default-ref-audio", type=Path, required=True)
    parser.add_argument("--default-ref-text", type=Path, required=True)
    parser.add_argument("--style-map", type=Path)
    parser.add_argument("--ref-language", default="日文")
    parser.add_argument("--target-language", default="日文")
    parser.add_argument(
        "--max-segment-chars",
        type=int,
        default=DEFAULT_MAX_SYNTHESIS_CHARS,
        help=(
            "Preferred acoustic inference size. Japanese planning may keep a complete "
            "phrase up to 20 percent longer (48 characters at the default 40), then "
            "uses punctuation or balanced fallback boundaries."
        ),
    )
    parser.add_argument(
        "--rhubarb-exe",
        type=Path,
        default=PROJECT_ROOT / ".tools" / "rhubarb" / "Rhubarb-Lip-Sync-1.14.0-Windows" / "rhubarb.exe",
    )
    args = parser.parse_args()
    gpt_model_routes: dict[str, Path] = {}
    for raw_route in args.gpt_model_route:
        try:
            route, path_text = raw_route.split("=", 1)
        except ValueError:
            parser.error("GPT model routes must use short=PATH or long=PATH")
        route = route.strip().lower()
        if route not in {"short", "long"}:
            parser.error(f"unsupported GPT model route: {route}")
        model_path = Path(path_text).resolve()
        if not model_path.is_file():
            parser.error(f"GPT model route does not exist: {model_path}")
        gpt_model_routes[route] = model_path

    initialization_started = time.perf_counter()
    engine = GPTSoVITSEngine(
        root=args.gpt_sovits_root,
        gpt_model=args.gpt_model,
        sovits_model=args.sovits_model,
        default_ref_audio=args.default_ref_audio,
        default_ref_text=args.default_ref_text,
        style_map=load_style_map(args.style_map),
        ref_language=args.ref_language,
        target_language=args.target_language,
        rhubarb_exe=args.rhubarb_exe,
        instance_signature=args.instance_signature,
        bootstrap_gpt_model=args.bootstrap_gpt_model,
        bootstrap_sovits_model=args.bootstrap_sovits_model,
        max_segment_chars=args.max_segment_chars,
        gpt_model_routes=gpt_model_routes,
        gpt_route_short_max_chars=args.gpt_route_short_max_chars,
        gpt_route_long_min_chars=args.gpt_route_long_min_chars,
    )
    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine))
    print("TTS_READY " + json.dumps({
        "atUnixMs": round(time.time() * 1000),
        "engineInitializationMs": round((time.perf_counter() - initialization_started) * 1000),
        "criticalWarmupMs": int(getattr(engine, "critical_warmup_elapsed_ms", 0)),
    }), flush=True)
    print(f"GPT-SoVITS TTS server: http://{args.host}:{args.port}/tts", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
