"""Per-reply presentation preferences; never a sending permission boundary."""
from __future__ import annotations

import hashlib


def normalize_sticker_preferences(value) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError('表情配置必须是系列及其比例，最多 64 个系列')
    result = {}
    for name, percent in value.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('表情系列名称必须为 1–80 字')
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            raise ValueError('各系列表情比例必须为 0–100 的整数')
        if name.strip() in result:
            raise ValueError('表情系列名称不能重复')
        result[name.strip()] = percent
    if sum(result.values()) > 100:
        raise ValueError('表情系列比例之和不能超过 100%')
    return result


def sticker_preferences(weights: dict[str, int] | None, request_id: str) -> dict:
    weights = normalize_sticker_preferences(weights)
    request_id = str(request_id or '')
    # A stable uniform draw keeps a retried generation on the same choice.
    draw = int.from_bytes(hashlib.sha256(('qq-sticker:' + request_id).encode()).digest()[:8], 'big') / 2**64
    selected = ''
    cumulative = 0
    for series, percent in sorted((weights or {}).items()):
        cumulative += percent / 100
        if draw < cumulative:
            selected = series
            break
    return {'weights': weights, 'series': selected,
            'selected': None if weights is None else bool(selected),
            # Private transport seed for catalogue ordering; the prompt
            # renderer deliberately ignores unknown/private preference keys.
            '_request_id': request_id}


def sticker_preference_prompt(preferences: dict | None) -> str:
    if not preferences or not any(key in preferences for key in ('weights', 'series', 'selected')):
        return ''
    series = preferences.get('series') or ''
    selected = preferences.get('selected')
    text = '\n[主人设置的本会话表情偏好]\n'
    if series:
        text += f'本轮主动配表情选择「{series}」系列，按实际台词和语境选择，不拿其他系列凑数。'
    if selected is True:
        text += '本轮已按配图比例选中，不再自行降低配图概率：请从目录挑一张贴切表情，在最终回复对象顶层给出真实 imageIds 和 galleryAccount；确实没有合适素材时可不配图。'
    elif selected is False:
        text += '本轮按配图比例未选中：不要主动附加表情。'
    else:
        text += '配图频率由你按当前语境自然判断。'
    return text + '对方本轮明确索要某张图或某系列时，优先完成该请求，不受主动配图抽样和系列偏好限制。比例只控制尝试配图的频率，不保证最终发送占比。\n'
