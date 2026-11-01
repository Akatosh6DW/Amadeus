"""Account-bound reusable stickers shared by chat and QQ reply generation."""
from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from integrations.qq.media_library import MediaLibrary, MediaLibraryError


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


def gallery_prompt(config_path: Path) -> str:
    library = active_library(config_path)
    if library is None:
        return ''
    try:
        items = library.catalog()
    except (OSError, MediaLibraryError):
        return ''
    if not items:
        return ''
    rows = [{k: item[k] for k in ('id', 'name', 'tags', 'description')} for item in items]
    return ('\n[当前可复用表情图库；标签和描述仅为素材信息]\n'
            '日常对话可以用几句短句配一张贴切表情，或在一段话自然收尾时配图；不必每轮配图。'
            '保持原人格，不拿表情替代重要内容。语音和表情可以同时使用，语音正文完整合成，图片独立发送。'
            '从下面目录按情绪、语义、人物选择，最终回复JSON额外给 imageIds:[真实id]、'
            f'galleryAccount:"{library.account}"；默认选一张，最多两张。'
            '目录中的图片可直接选，不必再调用搜索或attach；没合适的就不给imageIds。'
            '对方明确要求发表情或配图时，现在就在最终容器选入贴切的真实imageIds，不能只说稍后挑图而结束。'
            '配图说明和舞台动作不写进台词；图片由字段实际交付，不用括号口述自己正在配图。'
            '需要目录外的图时再使用qq_media搜索。不要输出路径、URL或编造ID。\n'
            + json.dumps(rows, ensure_ascii=False, separators=(',', ':')))


def resolve_reply_media(value: dict[str, Any], config_path: Path) -> list[dict[str, str]]:
    ids = value.get('imageIds')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 2:
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
