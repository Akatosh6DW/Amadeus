"""Bounded images from actual OneBot events; never fetch message-text URLs."""
from __future__ import annotations

import asyncio
import base64
from functools import lru_cache
import io
import json
from pathlib import Path
from urllib.parse import urlparse

MAX_BYTES = 8 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def face_names() -> dict[str, str]:
    names = {}
    for path in sorted((ROOT / '.runtime-assets/qq-agent/qqnt/Files/versions').glob('*/resources/app/resource/default-emojis/default_config.json')):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            for group in value.get('normalPanelResult', {}).get('SysEmojiGroupList', []):
                for face in group.get('SysEmojiList', []):
                    names[str(face['emojiId'])] = str(face.get('describe', '')).lstrip('/')[:80]
        except (OSError, ValueError, KeyError):
            continue
    return names


def segment_description(segment: dict) -> str:
    data = segment.get('data') or {}
    kind = segment.get('type')
    if kind == 'face':
        ident = str(data.get('id', ''))
        return '[QQ表情：' + (face_names().get(ident) or '编号' + ident) + ']'
    if kind in {'image', 'mface'}:
        return '[图片/表情包' + ('：' + str(data['summary'])[:120] if data.get('summary') else '') + ']'
    return ''


def media_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or '').lower()
    return (parsed.scheme in {'https', 'http'} and not parsed.username and not parsed.password
            and parsed.port in {None, 80, 443}
            and (host == 'qpic.cn' or host.endswith('.qpic.cn') or host in {'multimedia.nt.qq.com', 'multimedia.nt.qq.com.cn'}))


def image_segments(event: dict) -> list[dict]:
    message = event.get('message', [])
    return [s for s in message if isinstance(s, dict) and s.get('type') in {'image', 'mface'}] if isinstance(message, list) else []


async def collect_images(call, event: dict) -> tuple[list[dict], list[str]]:
    from aiohttp import ClientSession, ClientTimeout, ClientError
    images, notes = [], []
    segments = image_segments(event)
    if not segments:
        return images, notes
    async with ClientSession(timeout=ClientTimeout(total=8), trust_env=False) as session:
        for index, segment in enumerate(segments[:2]):
            data = segment.get('data') or {}
            try:
                url = str(data.get('url') or '')
                if not media_url(url) and (data.get('file_id') or data.get('file')):
                    info = await asyncio.wait_for(call('get_image', {'file': data.get('file_id') or data.get('file')}), 5)
                    url = str((info.get('data') or {}).get('url') or '')
                if not media_url(url):
                    raise ValueError('image URL unavailable')
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200 or (response.content_length or 0) > MAX_BYTES:
                        raise ValueError('image response unavailable')
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError('image too large')
                        chunks.append(chunk)
                images.append({'name': f'qq-image-{index+1}', 'dataBase64': base64.b64encode(b''.join(chunks)).decode('ascii')})
            except (OSError, ValueError, TimeoutError, ClientError):
                notes.append(f'第 {index+1} 张图片未取得像素，只能使用消息描述，不能声称看见内容')
    if len(segments) > 2:
        notes.append('本条只读取前两张图片，其他图片尚未读取')
    return images, notes


def image_attachments(images: list[dict]) -> tuple[list[dict], list[str]]:
    from PIL import Image, ImageOps
    attachments, notes = [], []
    for number, item in enumerate(images[:2]):
        try:
            raw = base64.b64decode(item['dataBase64'], validate=True)
            if len(raw) > MAX_BYTES:
                raise ValueError('image too large')
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.width * picture.height > 20_000_000:
                    raise ValueError('image dimensions too large')
                frames = min(getattr(picture, 'n_frames', 1), 300)
                indexes = sorted({0, frames // 2}) if frames > 1 else [0]
                for frame in indexes:
                    picture.seek(frame)
                    image = ImageOps.exif_transpose(picture).convert('RGB')
                    edge = 1280 if frames > 1 else 2400
                    image.thumbnail((edge, edge))
                    buffer = io.BytesIO()
                    image.save(buffer, format='PNG')
                    source = str(item.get('sourceMessageId', ''))
                    linked = source.lstrip('-').isascii() and source.lstrip('-').isdigit() and len(source) <= 32
                    label = ('QQ引用图' if item.get('sourceRelation') == 'reply' else 'QQ当前图') + f'（消息{source}）' if linked else 'QQ图片'
                    attachments.append({'name': f'{label}{number+1}-帧{frame+1}.png', 'mimeType':'image/png',
                        'dataBase64': base64.b64encode(buffer.getvalue()).decode('ascii')})
                if frames > 1:
                    notes.append(f'第 {number+1} 张是动态表情，仅读取开头和中间代表帧，不能推断完整动画')
        except (OSError, ValueError, KeyError, Image.DecompressionBombError):
            notes.append(f'第 {number+1} 张图片无法解码，不能声称读到了图片内容')
    return attachments, notes
