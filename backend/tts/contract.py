"""纯语音分段与排队 TTS 请求的完整性校验。"""

from __future__ import annotations

import re
from typing import Any, Callable


MAX_SPEECH_UNITS = 6
STRONG_SPEECH_BOUNDARIES = frozenset("。！？!?")
SPEECH_BOUNDARY_CLOSERS = frozenset("”’\"'」』】）》〕〉)}]….")


def batch_model_speech_units(units: Any, *, max_units: int = MAX_SPEECH_UNITS) -> list[dict[str, Any]]:
    """Bound delivery batches without imposing a limit on model content.

    Adjacent bilingual units are grouped together, never dropped or deduplicated.
    A batch inherits its first unit's delivery style and its last unit's pause.
    """
    if not isinstance(units, list) or not units or any(not isinstance(u, dict) for u in units):
        return []
    limit = max(1, int(max_units))
    if len(units) <= limit:
        return [dict(unit) for unit in units]
    grouped = []
    for index in range(limit):
        batch = units[index * len(units) // limit:(index + 1) * len(units) // limit]
        zh = "\n\n".join(str(u.get("zh") or u.get("textZh") or u.get("reply") or "").strip() for u in batch)
        ja = "\n\n".join(str(u.get("ja") or u.get("spokenTextJa") or "").strip() for u in batch)
        grouped.append({**batch[0], "zh": zh, "textZh": zh, "reply": zh,
                        "ja": ja, "spokenTextJa": ja,
                        "boundaryAfter": batch[-1].get("boundaryAfter") or
                            ("turn_pause" if index == limit - 1 else "phrase_pause")})
    return grouped


def semantic_unit_has_weak_boundary(text: Any) -> bool:
    """识别逗号或连接词等尚未收束的弱句尾，避免把半句话独立播报。"""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return True
    return bool(re.search(
        r"(?:[、，,；;：:]|"
        r"(?:けど|けれど|ので|のに|から|なら|ても|では|そして|それで|"
        r"でも|また|つまり|だから|一方で)|"
        r"(?:但是|不过|可是|然而|因为|所以|如果|虽然|而且|然后|以及|至于))\s*$",
        value,
        re.I,
    ))


def split_text_at_strong_punctuation(text: Any) -> list[str]:
    """按强句末标点和换段切分纯文本。

    省略号表示停顿，不单独成句；小数点也不切分。空白归一化后保留完整内容，
    标点不会被拆成独立播报单元。
    """

    value = str(text or "")
    units: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        candidate = re.sub(r"\s+", " ", "".join(buffer)).strip()
        buffer.clear()
        if not candidate:
            return
        if units and not re.search(r"[^\W_]", candidate, flags=re.UNICODE):
            units[-1] += candidate
            return
        units.append(candidate)

    index = 0
    while index < len(value):
        character = value[index]
        if character in "\r\n":
            while index + 1 < len(value) and value[index + 1] in "\r\n":
                index += 1
            flush()
            index += 1
            continue
        if character == ".":
            run_end = index
            while run_end + 1 < len(value) and value[run_end + 1] == ".":
                run_end += 1
            buffer.extend(value[index:run_end + 1])
            if run_end > index:
                # Two or more ASCII dots are an ellipsis/pause, never a split.
                index = run_end + 1
                continue
            previous = value[index - 1] if index else ""
            following = value[index + 1] if index + 1 < len(value) else ""
            decimal_point = previous.isdigit() and following.isdigit()
            if not decimal_point and (
                not following
                or following.isspace()
                or following in SPEECH_BOUNDARY_CLOSERS
            ):
                flush()
            index += 1
            continue
        buffer.append(character)
        if character in STRONG_SPEECH_BOUNDARIES:
            while (
                index + 1 < len(value)
                and (
                    value[index + 1] in STRONG_SPEECH_BOUNDARIES
                    or value[index + 1] in SPEECH_BOUNDARY_CLOSERS
                )
            ):
                index += 1
                buffer.append(value[index])
            flush()
        index += 1
    flush()
    return units


def normalize_speech_plan_units(
    units: Any,
    *,
    frozen_reply: str,
    frozen_spoken: str,
    require_spoken: bool = True,
    weak_boundary: Callable[[Any], bool] = semantic_unit_has_weak_boundary,
    max_units: int = MAX_SPEECH_UNITS,
) -> list[dict[str, Any]]:
    """校验语音计划完整覆盖已确定的中文与日文轨道。"""

    if not isinstance(units, list) or not 1 <= len(units) <= max_units:
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(units):
        if not isinstance(item, dict):
            return []
        text_zh = re.sub(
            r"\s+", " ", str(item.get("textZh") or item.get("zh") or "")
        ).strip()
        spoken_ja = re.sub(
            r"\s+", " ", str(item.get("spokenTextJa") or item.get("ja") or "")
        ).strip()
        if (
            not text_zh
            or (require_spoken and not spoken_ja)
            or weak_boundary(text_zh)
            or (require_spoken and weak_boundary(spoken_ja))
        ):
            return []
        boundary_after = str(item.get("boundaryAfter") or "").strip()
        if boundary_after not in {"continue", "micro_pause", "phrase_pause", "turn_pause"}:
            boundary_after = "turn_pause" if index == len(units) - 1 else "phrase_pause"
        normalized.append({
            **item,
            "textZh": text_zh,
            "spokenTextJa": spoken_ja,
            "boundaryAfter": boundary_after,
        })

    compact = lambda candidate: re.sub(r"\s+", "", str(candidate or ""))
    if compact("".join(item["textZh"] for item in normalized)) != compact(frozen_reply):
        return []
    if require_spoken and (
        compact("".join(item["spokenTextJa"] for item in normalized))
        != compact(frozen_spoken)
    ):
        return []
    return normalized


def punctuation_fallback_speech_units(
    reply: Any,
    spoken: Any,
    *,
    max_units: int = MAX_SPEECH_UNITS,
    split_text: Callable[[Any], list[str]] = split_text_at_strong_punctuation,
    normalize_units: Callable[..., list[dict[str, Any]]] = normalize_speech_plan_units,
) -> list[dict[str, Any]]:
    """模型只返回散文时，按强标点构造确定性的回退播报计划。

    两条语言轨道的分段数量必须一致；否则交给调用方修复或保持整体播报。
    """

    frozen_reply = str(reply or "").strip()
    frozen_spoken = str(spoken or "").strip()
    reply_units = split_text(frozen_reply)
    spoken_units = split_text(frozen_spoken) if frozen_spoken else []
    if not reply_units:
        return []
    if frozen_spoken and len(reply_units) != len(spoken_units):
        return []

    group_count = min(max(1, int(max_units)), len(reply_units))
    grouped: list[dict[str, Any]] = []
    for group_index in range(group_count):
        start = group_index * len(reply_units) // group_count
        end = (group_index + 1) * len(reply_units) // group_count
        grouped.append({
            "textZh": "".join(reply_units[start:end]),
            "spokenTextJa": (
                "".join(spoken_units[start:end]) if frozen_spoken else ""
            ),
            "boundaryAfter": (
                "turn_pause" if group_index == group_count - 1 else "phrase_pause"
            ),
        })
    return normalize_units(
        grouped,
        frozen_reply=frozen_reply,
        frozen_spoken=frozen_spoken,
        require_spoken=bool(frozen_spoken),
    )


def validate_tts_request_preflight(
    body: dict[str, Any], *, weak_boundary: Callable[[Any], bool] = semantic_unit_has_weak_boundary,
) -> dict[str, Any]:
    """启动语音服务前拒绝空文本或不完整分段，避免无效请求触发推理。"""

    text = re.sub(r"\s+", " ", str(body.get("text") or "")).strip()
    if not text or not re.search(r"[^\W_]", text, flags=re.UNICODE):
        raise ValueError("TTS text is empty.")
    supplied = body.get("semanticUnits")
    if supplied is None:
        return {"ok": True, "unitCount": 1, "segmentationSource": "atomic_legacy"}
    if not isinstance(supplied, list) or not supplied:
        raise ValueError("semanticUnits must be a non-empty array")
    units = [re.sub(r"\s+", " ", str(item or "")).strip() for item in supplied]
    if any(not item or not re.search(r"[^\W_]", item, flags=re.UNICODE) for item in units):
        raise ValueError("semanticUnits contains an empty or punctuation-only unit")
    compact = lambda value: re.sub(r"\s+", "", str(value or ""))
    if compact("".join(units)) != compact(text):
        raise ValueError("semanticUnits do not cover the complete TTS text")
    incomplete = [
        index for index, unit in enumerate(units, 1)
        if weak_boundary(unit)
    ]
    if incomplete:
        indexes = ",".join(str(item) for item in incomplete)
        raise ValueError(f"semanticUnits contains incomplete weak boundary at unit {indexes}")
    return {"ok": True, "unitCount": len(units), "segmentationSource": "caller_contract"}
