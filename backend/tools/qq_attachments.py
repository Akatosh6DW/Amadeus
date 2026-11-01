"""Copy explicitly selected chat image originals into the current QQ library."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re

from backend.attachments.storage import AttachmentStorage


def import_chat_images(data_root: Path, attachment_ids: list[str], account: str, call) -> list[str]:
    """IDs come from the conversation's image index; never infer the latest image.

    This only prepares media. Recipient authorization, review, idempotency and
    delivery remain in the QQ send/draft flow after this function returns.
    """
    if (not isinstance(attachment_ids, list) or not 1 <= len(attachment_ids) <= 2
            or any(not isinstance(value, str) for value in attachment_ids)):
        raise ValueError('请选择一至两张主对话图片的真实 attachmentIds')
    # regex-purpose: protocol
    if not re.fullmatch(r'[0-9]{1,20}', account):
        raise ValueError('图片发送缺少明确的 QQ 账号')
    storage = AttachmentStorage(data_root / 'chat-attachments')
    items = []
    expected_ids = set()
    for ident in dict.fromkeys(attachment_ids):
        if storage.normalized_id(ident) != ident:
            raise ValueError('图片引用无效，请使用主对话图片索引中的 ID')
        metadata = storage.read_metadata(ident)
        if not metadata or metadata.get('kind') != 'image':
            raise ValueError('找不到这张主对话图片，请重新选择图片引用')
        path = storage.file_path(ident, metadata.get('storedName') or metadata.get('name'))
        if not path or not path.is_file():
            raise ValueError('这张主对话图片的原件已不可用')
        if not 0 < path.stat().st_size <= 10 * 1024 * 1024:
            raise ValueError('QQ 图片原件必须在 10 MiB 以内')
        # Read the original bytes. In particular, do not forward a caption,
        # resized vision preview or the representative frame of an animated GIF.
        original = path.read_bytes()
        if not 0 < len(original) <= 10 * 1024 * 1024:
            raise ValueError('QQ 图片原件必须在 10 MiB 以内')
        items.append({'name': str(metadata.get('name') or path.name), 'kind': 'image',
                      'base64': base64.b64encode(original).decode('ascii')})
        expected_ids.add(hashlib.sha256(original).hexdigest())
    result = call('media', {'expectedAccount': account, 'action': 'import', 'items': items,
                          'reusable': False})
    returned = result.get('items') if isinstance(result, dict) else None
    if (not isinstance(result, dict) or not result.get('ok') or str(result.get('account')) != account or not isinstance(returned, list)
            or not returned or len(returned) > len(items)):
        raise ValueError('QQ 图片准备失败，尚未发送')
    ids = [item.get('id') for item in returned if isinstance(item, dict)]
    # regex-purpose: resource
    if len(ids) != len(returned) or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in ids):
        raise ValueError('QQ 图库返回的图片引用无效，尚未发送')
    if set(ids) != expected_ids:
        raise ValueError('QQ 图库未完整接收所选原图，尚未发送')
    return list(dict.fromkeys(ids))
