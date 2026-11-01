"""Expose this QQ conversation's images to the ordinary lazy visual reader."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from .media import image_attachments


class QQImageSources:
    def __init__(self, base, config_path: Path, account: str, kind: str, target: str, *, log):
        self.base, self.config_path = base, config_path
        self.account, self.kind, self.target = account, kind, target
        self.log = log
        self.references = {}

    def __getattr__(self, name):
        return getattr(self.base, name)

    def remember(self, history):
        if (not history.get('ok') or (str(history.get('account')), history.get('targetType'), str(history.get('targetId')))
                != (self.account, self.kind, self.target)):
            return
        for row in [*history.get('messages', []), *history.get('referencedMessages', [])]:
            for ref in row.get('imageRefs', []):
                if not isinstance(ref.get('token'), str) or type(ref.get('index')) is not int:
                    continue
                key = json.dumps([self.account, self.kind, self.target, row['messageId'], ref['index']])
                ident = 'qq-image-' + hashlib.sha256(key.encode()).hexdigest()[:32]
                self.references.pop(ident, None)
                self.references[ident] = {
                    'id': ident, 'kind': 'image', 'sourceType': 'qq_message_image', 'channelReference': True,
                    'name': ('本条消息引用的图片：' if row.get('sourceRelation') == 'reply' else '') + f"{row.get('senderName') or row.get('senderId')} 的图片（消息 {row['messageId']}，第 {ref['index']+1} 张）",
                    '_sourceTurnId': str(row['messageId']), '_sourceUser': str(row.get('text', ''))[:120],
                    '_sourceTime': str(row.get('time', '')),
                    '_request': {'expectedAccount': self.account, 'targetType': self.kind, 'targetId': self.target,
                                 'messageId': str(row['messageId']), 'index': ref['index'], 'token': ref['token']}}
        self.references = dict(list(self.references.items())[-60:])

    def image_references(self, body):
        return dict(reversed(list(self.references.items())))

    def remember_media(self, result):
        """Let the model inspect library candidates before selecting a meme."""
        if not result.get('ok') or str(result.get('account')) != self.account:
            return
        for item in result.get('items', [])[:20]:
            asset_id = item.get('id') if isinstance(item, dict) else None
            if not isinstance(asset_id, str) or not re.fullmatch(r'[0-9a-f]{64}', asset_id):
                continue
            ident = 'qq-media-' + asset_id
            self.references.pop(ident, None)
            self.references[ident] = {'id': ident, 'kind': 'image', 'sourceType': 'qq_library_image',
                'channelReference': True, 'name': 'QQ 图库：' + str(item.get('name', '图片'))[:120],
                '_mediaAssetId': asset_id, '_sourceUser': str(item.get('description') or '')[:120]}
        self.references = dict(list(self.references.items())[-60:])

    def fetch(self, reference):
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        if reference.get('_mediaAssetId'):
            if str(config.get('policy', {}).get('account_id')) != self.account:
                raise ValueError('QQ account changed')
            from .media_library import MediaLibrary
            segments = MediaLibrary(self.config_path.parent, self.account).segments([reference['_mediaAssetId']])
            return {'ok': True, 'images': [{'name': reference['name'],
                'dataBase64': segment['data']['file'].removeprefix('base64://')}
                for segment in segments], 'notes': []}
        request = urllib.request.Request(f"http://127.0.0.1:{int(config['control_port'])}/api/message-image",
            data=json.dumps(reference['_request']).encode('utf-8'),
            headers={'Authorization': 'Bearer ' + config['control_token'], 'Content-Type': 'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=22) as response:
            return json.load(response)

    def load_referenced_image(self, source, settings):
        reference = self.references.get(source.get('id'))
        if not reference:
            return {}
        try:
            response = self.fetch(reference)
            attachments, notes = image_attachments(response.get('images', []) if response.get('ok') else [])
            notes = response.get('notes', []) + notes
            self.log('qq_history_image_loaded', messageId=reference.get('_request', {}).get('messageId', ''),
                     assetId=reference.get('_mediaAssetId', ''),
                     imageCount=len(attachments), notes=notes)
            if not attachments:
                return {}
            parts = [{'type': 'text', 'text': reference['name'] + ('；' + '；'.join(notes) if notes else '')}]
            for attachment in attachments:
                parts.append({'type': 'image_url', 'image_url': {
                    'url': 'data:image/png;base64,' + attachment['dataBase64'], 'detail': 'high'}})
            return {'parts': parts, 'imageCount': len(attachments)}
        except (OSError, ValueError, urllib.error.URLError) as exc:
            self.log('qq_history_image_unavailable', messageId=reference.get('_request', {}).get('messageId', ''),
                     assetId=reference.get('_mediaAssetId', ''), error=type(exc).__name__)
            return {}
