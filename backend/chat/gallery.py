"""Account-bound reusable stickers shared by chat and QQ reply generation."""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from integrations.qq.media_library import MAX_REPLY_IMAGES, MediaLibrary, MediaLibraryError


def recent_gallery_ids(delivery_history: Iterable[Mapping[str, Any]] | None,
                       *, limit: int = 64) -> list[str]:
    """Return successful gallery occurrences, newest receipt first.

    QQ gateway history is already scoped to one account/conversation and is
    ordered newest first.  Only rows with a confirmed ``sent`` state and a
    platform ``message_id`` can influence variety.  Draft, failed, and unknown
    outcomes deliberately remain invisible here because they were not proven
    to reach QQ.  A repeated platform message/asset receipt is ignored after
    its first occurrence, while the same asset in different messages remains
    repeated input so the catalogue can lower its weight.  The returned IDs
    are advisory and do not remove candidates from the model-facing catalogue.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return []
    result: list[str] = []
    seen_receipts: set[tuple[str, str]] = set()
    for row in delivery_history or ():
        if not isinstance(row, Mapping) or row.get('state') != 'sent':
            continue
        message_id = row.get('message_id', row.get('messageId'))
        if message_id is None or message_id == '':
            continue
        media = row.get('galleryMedia')
        if not isinstance(media, list):
            continue
        for item in media:
            if not isinstance(item, Mapping):
                continue
            asset_id = item.get('id')
            if not isinstance(asset_id, str) or not asset_id:
                continue
            receipt_key = (str(message_id), asset_id)
            if receipt_key in seen_receipts:
                continue
            seen_receipts.add(receipt_key)
            result.append(asset_id)
            if len(result) >= limit:
                return result
    return result


def preserve_gallery_selection(original: dict[str, Any], replacement: dict[str, Any]) -> None:
    """A text/voice repair must not erase an already verified media selection."""
    if original.get('galleryMedia') and not replacement.get('imageIds'):
        for key in ('imageIds', 'galleryAccount', 'galleryMedia'):
            replacement[key] = deepcopy(original.get(key))


def active_library(config_path: Path) -> MediaLibrary | None:
    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
        account = str(config.get('policy', {}).get('account_id') or '')
        # regex-purpose: resource
        if not re.fullmatch(r'[0-9]{5,20}', account):
            return None
        if not (config_path.parent / 'media-library' / account / 'manifest.json').is_file():
            return None
        return MediaLibrary(config_path.parent, account)
    except (OSError, ValueError, TypeError, MediaLibraryError):
        return None


def gallery_prompt(config_path: Path, *, reply_preferences: dict | None = None,
                   recent_ids: Iterable[str] | None = None) -> str:
    library = active_library(config_path)
    if library is None:
        return ''
    if recent_ids is None and isinstance(reply_preferences, dict):
        recent_ids = reply_preferences.get('_recent_image_ids')
    recent_values = ((recent_ids,) if isinstance(recent_ids, str) else (recent_ids or ()))
    recent = [value for value in recent_values
              if isinstance(value, str) and value]
    request_id = ((reply_preferences or {}).get('_request_id')
                  if isinstance(reply_preferences, dict) else '')
    if not isinstance(request_id, str) or not request_id:
        # A transport-only history hint still changes between turns.  Use it
        # as a private seed so a caller without sticker settings does not keep
        # presenting one fixed first candidate, while identical retries remain
        # stable.
        request_id = 'history:' + '|'.join(recent) if recent else ''
    try:
        items = library.catalog(preferred_series=(reply_preferences or {}).get('series', ''),
                                recent_ids=recent, request_id=request_id)
    except (OSError, MediaLibraryError):
        return ''
    if not items:
        return ''
    rows = [{k: item[k] for k in ('id', 'name', 'tags', 'description')} for item in items]
    from integrations.qq.sticker_preferences import sticker_preference_prompt
    frequency = ('配图频率按下方主人设置的本轮抽样结果执行。'
                 if reply_preferences and reply_preferences.get('selected') is not None else '不必每轮配图。')
    variety = ('近期成功回执中的图片会按出现次数和近期程度降低抽样权重；连续重复降低更多，'
               '但重复完全可选。语境更贴切或对方明确点名某张图、某个系列时仍可再次使用。'
               if recent else
               '如果当前会话已有成功发送回执，重复图片仍完全可选；近期和连续重复只降低抽样权重，'
               '不是禁用清单。语境更贴切或对方明确点名时可以再次使用。')
    return ('\n[当前可复用表情图库；标签和描述仅为素材信息]\n'
            '日常对话可以用几句短句配一张贴切表情，或在一段话自然收尾时配图；' + frequency +
            '保持原人格，不拿表情替代重要内容。语音和表情可以同时使用，语音正文完整合成，图片独立发送。'
            '从下面目录按情绪、语义、人物选择，最终回复JSON额外给 imageIds:[真实id]、'
            f'galleryAccount:"{library.account}"；默认选一张，明确要求多张时按要求选择，每轮最多{MAX_REPLY_IMAGES}张。'
            + variety
            + '失败、取消或未知结果不算已成功发送；成功回执仅用于减少近重复，不是禁用清单。'
            '目录中的图片可直接选，不必再调用搜索或attach；没合适的就不给imageIds。'
            '对方明确要求发表情或配图时，现在就在最终容器选入贴切的真实imageIds，不能只说稍后挑图而结束。'
            '配图说明和舞台动作不写进台词；图片由字段实际交付，不用括号口述自己正在配图。'
            '需要目录外的图时再使用qq_media搜索。不要输出路径、URL或编造ID。\n'
            + json.dumps(rows, ensure_ascii=False, separators=(',', ':'))
            + sticker_preference_prompt(reply_preferences))


def resolve_reply_media(value: dict[str, Any], config_path: Path) -> list[dict[str, str]]:
    ids = value.get('imageIds')
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_REPLY_IMAGES:
        return []
    library = active_library(config_path)
    if library is None or str(value.get('galleryAccount') or '') != library.account:
        return []
    try:
        found = library.get(ids, reusable_only=True)
        items = found.get('items', [])
        if not found.get('ok') or found.get('missing') or len(items) != len(set(ids)):
            return []
        return [{'id': item['id'], 'account': library.account, 'name': item['name'],
                 'url': f"/api/gallery/{library.account}/{item['id']}"} for item in items]
    except (OSError, TypeError, MediaLibraryError):
        return []


def gallery_bytes(config_path: Path, account: str, asset_id: str) -> tuple[bytes, str] | None:
    media = resolve_reply_media({'imageIds': [asset_id], 'galleryAccount': account}, config_path)
    if not media:
        return None
    library = active_library(config_path)
    if library is None or library.account != account:
        return None
    try:
        segments = library.segments([asset_id])
        if not segments:
            return None
        raw = base64.b64decode(segments[0]['data']['file'].removeprefix('base64://'))
        mime = 'image/' + ('jpeg' if raw[:2] == b'\xff\xd8' else 'gif' if raw[:3] == b'GIF'
                         else 'png' if raw[:4] == b'\x89PNG' else 'webp')
        return raw, mime
    except (OSError, ValueError, IndexError, MediaLibraryError):
        return None
