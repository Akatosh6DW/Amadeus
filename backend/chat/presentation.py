"""语音、表情与舞台说明的纯表现契约。"""
from __future__ import annotations
import re
from typing import Any
from backend.common.values import clamp_float
from .parsing import parse_json_object


SPEECH_PACE_SPEED = {
    "very_slow": 0.66,
    "slow": 0.8,
    "normal": 1.0,
    "fast": 1.16,
    "very_fast": 1.32,
}


ALLOWED_VOICE_TONES = {"neutral", "dry", "gentle", "shy", "shy_annoyed", "flustered", "startled", "serious", "playful", "laughing", "comforting", "urgent", "warm", "cold"}


ALLOWED_VOICE_INTENSITIES = {"subtle", "normal", "strong"}


VOICE_TONE_ALIASES = {
    "calm": "neutral",
    "soft": "gentle",
    "sharp": "dry",
    "teasing": "playful",
    "energetic": "urgent",
    "embarrassed": "shy",
    "surprised": "startled",
    "shocked": "startled",
    "laugh": "laughing",
    "laughter": "laughing",
    "joyful": "laughing",
}


ALLOWED_EMOTIONS = {
    "neutral", "thinking", "focused", "skeptical", "awkward", "annoyed", "furious", "teasing", "amused", "laughing",
    "shy", "flustered", "affectionate", "concerned", "protective", "excited",
    "startled", "proud", "melancholic",
}


EMOTION_ALIASES = {
    "serious": "thinking",
    "scientific": "focused",
    "concentrated": "focused",
    "doubtful": "skeptical",
    "suspicious": "skeptical",
    "uncomfortable": "awkward",
    "embarrassing": "awkward",
    "sarcastic": "teasing",
    "angry": "annoyed",
    "irritated": "annoyed",
    "furious": "furious",
    "enraged": "furious",
    "embarrassed_angry": "flustered",
    "embarrassed": "shy",
    "soft": "concerned",
    "worried": "concerned",
    "caring": "affectionate",
    "protect": "protective",
    "surprised": "startled",
    "happy_small": "amused",
    "happy_big": "laughing",
    "laugh": "laughing",
    "laughter": "laughing",
    "joyful": "laughing",
    "sad": "melancholic",
}


ALLOWED_GESTURES = {"idle", "nod", "tilt", "look_away", "glitch", "lean", "soften", "laugh", "tap", "boot", "recoil", "explain", "perk"}


GESTURE_ALIASES = {
    "slight_head_tilt": "tilt",
    "look_aside": "look_away",
    "turn_head": "look_away",
    "turn_head_side": "look_away",
    "side_head": "look_away",
    "shake_head": "tilt",
    "tap_chin": "explain",
    "arms_crossed": "tilt",
    "lean_forward": "lean",
    "small_smile": "idle",
    "shrug": "tilt",
    "slight_shrug": "tilt",
    "point": "explain",
    "point_up": "explain",
    "pointup": "explain",
    "thinking": "explain",
    "smile": "soften",
    "laughing": "laugh",
    "laughter": "laugh",
    "big_laugh": "laugh",
    "default": "idle",
    "none": "idle",
    "neutral": "idle",
}


SPEECH_LANGUAGE_PROFILES = {
    "ja-JP": {"locale": "ja-JP", "targetLanguage": "日文"},
    "zh-CN": {"locale": "zh-CN", "targetLanguage": "中文"},
}


ROLEPLAY_STAGE_DIRECTION_TERMS = re.compile(
    r"(?:"
    r"眯(?:起)?眼|睁(?:开)?眼|闭(?:上)?眼|眼神|视线|目光|嘴角|口元|"
    r"偏(?:过)?头|侧(?:过)?头|歪(?:了)?(?:歪)?头|低头|抬头|转头|别开(?:脸|视线)|移开视线|"
    r"看向|望向|盯着|注视|回避视线|"
    r"微笑|苦笑|冷笑|笑意|笑容|轻笑|偷笑|发笑|笑了|"
    r"叹气|叹了口气|脸红|红晕|涨红|皱眉|挑眉|扬眉|"
    r"耸肩|抱臂|交叉双臂|点头|摇头|后退|前倾|靠近|缩了缩|"
    r"轻哼|哼了一声|沉默|停顿|语气|声音|表情|神情|动作|姿势|"
    r"心想|心里|心中|脑海|内心|心理|暗自|假装|故作|装作|若无其事|"
    r"目を|目が|目線|視線|顔を|顔が|首を|俯(?:く|いた)|見つめ|見(?:る|た|て)|"
    r"微笑(?:む|んだ)|笑み|笑(?:う|った)|ため息|頬|赤ら|眉|腕を|身を|"
    r"そっぽ|声で|口元|黙(?:る|った)|考え込|"
    r"smiles?|smirks?|sighs?|looks?\s+away|tilts?\s+(?:her\s+)?head|"
    r"blush(?:es|ed)?|frowns?|nods?|shakes?\s+(?:her\s+)?head"
    r")",
    re.IGNORECASE,
)


ROLEPLAY_STAGE_BLOCK_PATTERNS = (
    re.compile(r"（([^（）\r\n]{1,220})）"),
    re.compile(r"\(([^()\r\n]{1,220})\)"),
    re.compile(r"【([^【】\r\n]{1,220})】"),
    re.compile(r"\[([^\[\]\r\n]{1,220})\]"),
    re.compile(r"\*([^*\r\n]{1,220})\*"),
)


def normalize_speech_language(
    value: Any,
    *,
    voice_locale: Any = "",
    target_language: Any = "",
) -> str:
    """Return the product speech language while migrating legacy settings."""
    candidate = str(value or "").strip().lower().replace("_", "-")
    if candidate in {"zh", "zh-cn", "zh-hans", "中文", "chinese"}:
        return "zh-CN"
    if candidate in {"ja", "ja-jp", "jp", "日文", "日语", "日語", "japanese"}:
        return "ja-JP"
    legacy = " ".join([str(voice_locale or ""), str(target_language or "")]).lower()
    if "zh" in legacy or "中文" in legacy or "汉语" in legacy or "漢語" in legacy:
        return "zh-CN"
    return "ja-JP"


def speech_language_profile(settings: dict[str, Any]) -> dict[str, str]:
    language = normalize_speech_language(
        settings.get("speechLanguage"),
        voice_locale=settings.get("voiceLocale"),
        target_language=settings.get("gptSovitsTargetLanguage"),
    )
    return {"speechLanguage": language, **SPEECH_LANGUAGE_PROFILES[language]}


def normalize_speech_pace(value: Any, speech_speed: Any = None) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "veryslow": "very_slow",
        "very_slowly": "very_slow",
        "slower": "slow",
        "normal_speed": "normal",
        "faster": "fast",
        "veryfast": "very_fast",
    }
    pace = aliases.get(raw, raw)
    if pace in SPEECH_PACE_SPEED:
        return pace
    speed = clamp_float(speech_speed, 0.62, 1.45, 1.0)
    if speed <= 0.7:
        return "very_slow"
    if speed < 0.92:
        return "slow"
    if speed >= 1.26:
        return "very_fast"
    if speed > 1.08:
        return "fast"
    return "normal"


def normalize_voice_tone(value: Any, fallback: str = "neutral") -> str:
    tone = str(value or "").strip().lower()
    fallback_tone = VOICE_TONE_ALIASES.get(str(fallback or "").strip().lower(), str(fallback or "neutral").strip().lower())
    if tone in ALLOWED_VOICE_TONES:
        return tone
    mapped = VOICE_TONE_ALIASES.get(tone)
    if mapped:
        return mapped
    return fallback_tone if fallback_tone in ALLOWED_VOICE_TONES else "neutral"


def normalize_voice_intensity(value: Any) -> str:
    if isinstance(value, (int, float)):
        number = clamp_float(value, 0, 1, 0.5)
        return "subtle" if number < 0.34 else "strong" if number > 0.72 else "normal"
    raw = str(value or "normal").strip().lower().replace("-", "_")
    aliases = {"low": "subtle", "mild": "subtle", "medium": "normal", "moderate": "normal", "high": "strong", "intense": "strong"}
    normalized = aliases.get(raw, raw)
    return normalized if normalized in ALLOWED_VOICE_INTENSITIES else "normal"


def resolve_voice_intensity(value: Any, emotion_intensity: float, emotion_delta: float = 0.0) -> tuple[str, str]:
    """Keep the categorical TTS prompt aligned with the continuous expression strength."""
    effective = clamp_float(float(emotion_intensity) + float(emotion_delta) * 0.65, 0.2, 2.5, 1.0)
    inferred = "subtle" if effective <= 0.76 else "strong" if effective >= 1.08 else "normal"
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "low": "subtle", "mild": "subtle", "medium": "normal",
        "moderate": "normal", "high": "strong", "intense": "strong",
    }
    requested = aliases.get(raw, raw)
    if requested not in ALLOWED_VOICE_INTENSITIES:
        return inferred, "derived"
    if requested == "normal" and inferred != "normal":
        return inferred, "continuous-intensity"
    if {requested, inferred} == {"subtle", "strong"}:
        return "normal", "conflict-guard"
    return requested, "model"


def sanitize_roleplay_text(value: Any) -> tuple[str, list[str]]:
    """Keep spoken dialogue while extracting bracketed Live2D stage directions."""
    text = str(value or "").strip()
    if not text:
        return "", []
    directions: list[str] = []

    def remove_if_stage(match: re.Match[str]) -> str:
        candidate = str(match.group(1) or "").strip()
        if ROLEPLAY_STAGE_DIRECTION_TERMS.search(candidate):
            directions.append(candidate)
            return ""
        return match.group(0)

    for pattern in ROLEPLAY_STAGE_BLOCK_PATTERNS:
        text = pattern.sub(remove_if_stage, text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    quote_pairs = (("\"", "\""), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for opening, closing in quote_pairs:
            if text.startswith(opening) and text.endswith(closing):
                text = text[len(opening):-len(closing)].strip()
                changed = True
                break
    return text, directions


def roleplay_visual_hints(directions: list[str]) -> dict[str, Any]:
    """Recover visual intent when a model leaked it into prose instead of JSON fields."""
    text = " ".join(str(item or "") for item in directions)
    if not text:
        return {}
    hints: dict[str, Any] = {}
    if re.search(r"脸红|红晕|涨红|害羞|羞|頬|赤ら|blush", text, re.I):
        hints.update({
            "emotion": "shy", "gesture": "soften", "emotionIntensity": 0.94,
            "blushIntensity": 0.3, "eyeContact": 0.42, "gazeX": -0.18,
            "gazeY": 0.12, "tension": 0.28,
        })
    elif re.search(r"皱眉|恼|生气|不满|冷笑|frown", text, re.I):
        hints.update({"emotion": "annoyed", "gesture": "tilt", "emotionIntensity": 0.94, "tension": 0.48})
    elif re.search(r"大笑|开怀|笑出声|哈哈|あはは|ハハハ|laughs?\s+out\s+loud", text, re.I):
        hints.update({"emotion": "laughing", "gesture": "laugh", "emotionIntensity": 1.08, "tension": 0.08})
    elif re.search(r"微笑|苦笑|笑意|笑容|轻笑|偷笑|smile|smirk|笑み|微笑", text, re.I):
        hints.update({"emotion": "amused", "emotionIntensity": 0.88, "tension": 0.12})
    elif re.search(r"叹气|沉默|ため息|黙", text, re.I):
        hints.update({"emotion": "melancholic", "emotionIntensity": 0.84, "tension": 0.2})

    if re.search(r"偏(?:过)?头|侧(?:过)?头|转头|别开|移开视线|看向.*(?:边|旁)|回避视线|そっぽ|顔をそら|looks?\s+away|turns?\s+(?:her\s+)?head", text, re.I):
        hints.update({"gesture": "look_away", "eyeContact": 0.38, "gazeX": -0.24, "gazeY": 0.04})
    elif re.search(r"歪(?:了)?(?:歪)?头|tilts?\s+(?:her\s+)?head", text, re.I):
        hints.update({"gesture": "tilt", "eyeContact": 0.5, "gazeX": -0.1, "gazeY": 0.02})
    elif re.search(r"低头|俯(?:く|いた)", text, re.I):
        hints.update({"gesture": "soften", "eyeContact": 0.44, "gazeY": 0.18})
    elif re.search(r"点头|nod", text, re.I):
        hints["gesture"] = "nod"
    elif re.search(r"后退|recoil", text, re.I):
        hints["gesture"] = "recoil"
    return hints


def fallback_spoken_text_ja(reply: str) -> str:
    text = str(reply or "").strip()
    if re.search(r"接口|错误|失败|正常回应", text):
        return "接続に問題があるみたい。ローカルの応答に切り替えるわ。"
    if re.search(r"难受|焦虑|痛苦|累|陪", text):
        return "少し落ち着いて。大丈夫、ここにいるから。"
    if re.search(r"变量|实验|模型|接口|代码|算法", text):
        return "まず変数を分けて考えましょう。そこから検証できる形に直すの。"
    if re.search(r"突然|评价|参考|继续", text):
        return "きゅ、急に何を言ってるのよ。まあ、続けて。聞いてるから。"
    if re.search(r"称呼|奇怪|调侃", text):
        return "変な呼び方をしないで。まったく、次は何の実験なの？"
    return "うん、分かった。続けて。ちゃんと聞いてるから。"


def model_emotion_label(value: Any) -> str:
    """Return a provider-supplied emotion label without inferring one from prose."""

    parsed = parse_json_object(value) if isinstance(value, str) else value
    raw = ""
    if isinstance(parsed, dict):
        raw = str(parsed.get("emotion") or parsed.get("expression") or "").strip()
    elif isinstance(value, str):
        raw = value.strip().strip("`\"' ")
    if raw in ALLOWED_EMOTIONS:
        return raw
    alias = EMOTION_ALIASES.get(raw)
    return alias if alias in ALLOWED_EMOTIONS else ""


def prepare_spoken_japanese_terms(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(
        r"(?i)(?<![A-Za-z])A[\s._-]*M[\s._-]*A[\s._-]*D[\s._-]*E[\s._-]*U[\s._-]*S(?![A-Za-z])",
        "アマデウス",
        text,
    )
    text = re.sub(r"ア\s*マ\s*デ\s*ウ\s*ス", "アマデウス", text)
    # A katakana ゴホン is often synthesized as a forceful throat-clear and
    # can make the surrounding line sound rough. Keep the same spoken beat,
    # but use the lighter conversational rendering.
    text = re.sub(r"ゴホン", "こほん", text)
    return text


def apply_spoken_japanese_pronunciations(text: str, entries: list[dict[str, Any]]) -> str:
    """使用传入词音表替换文字；文件读取留给 server 惰性入口。"""
    replacements: list[tuple[str, str]] = []
    for entry in entries:
        reading = str(entry["reading"])
        replacements.extend((alias, reading) for alias in entry["aliases"] if alias != reading)
    for alias, reading in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        if re.search(r"[A-Za-z]", alias):
            text = re.sub(re.escape(alias), lambda _: reading, text, flags=re.IGNORECASE)
        else:
            text = text.replace(alias, reading)
    return text


SPEECH_PROSODY_PROFILES = {"compact", "natural", "deliberate"}


def normalize_speech_prosody(value: Any) -> str:
    candidate = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "compact": "compact", "tight": "compact", "紧凑": "compact", "緊湊": "compact",
        "natural": "natural", "normal": "natural", "自然": "natural",
        "deliberate": "deliberate", "relaxed": "deliberate", "舒缓": "deliberate", "舒緩": "deliberate",
    }
    return aliases.get(candidate, "natural")


def requested_speech_pace(user_message: Any) -> str:
    """Extract an explicit delivery-speed request from the user's own words."""

    text = re.sub(r"\s+", "", str(user_message or ""))
    if not text:
        return ""
    if re.search(r"非常慢|特别慢|尽量慢|一字一句|慢慢(?:说|念|读|告诉)", text):
        return "very_slow"
    if re.search(r"慢点|慢一点|放慢|用慢速|(?:说|念|读)慢(?:点|一点)", text):
        return "slow"
    if re.search(r"非常快|特别快|用最快|尽快(?:念|读)", text):
        return "very_fast"
    if re.search(r"快点(?:说|念|读|告诉)|快速(?:说|念|读|告诉)|(?:说|念|读)快点|加快语速", text):
        return "fast"
    return ""
