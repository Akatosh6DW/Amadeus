"""One-hop QQ reply references, separate from the decision to wake the agent."""
from __future__ import annotations

import asyncio
import re

from .identity import event_sender

_ID = re.compile(r'-?[0-9]{1,32}\Z')
_CQ_REPLY = re.compile(r'\[CQ:reply,id=(-?[0-9]{1,32})(?:,[^\]]*)?\]')


def reply_id(event: dict) -> str:
    message = event.get('message', event.get('raw_message', ''))
    if isinstance(message, str):
        match = _CQ_REPLY.search(message)
        return match.group(1) if match else ''
    for segment in message if isinstance(message, list) else []:
        if isinstance(segment, dict) and segment.get('type') == 'reply' and isinstance(segment.get('data'), dict):
            ident = str(segment['data'].get('id', ''))
            if _ID.fullmatch(ident):
                return ident
    return ''


def reply_text(event: dict) -> str:
    ident = reply_id(event)
    return f'[引用消息:{ident}]' if ident else '[reply]'


def replace_cq_replies(text: str) -> str:
    return _CQ_REPLY.sub(lambda m: f'[引用消息:{m.group(1)}]', text)


async def resolve_reply(call, event: dict, account: str, kind: str, target: str,
                        history: dict | None = None) -> tuple[dict, dict | None]:
    """Resolve the selected message, never recursively expand its own quote."""
    ident = reply_id(event)
    if not ident:
        return {}, None
    unavailable = {'status': 'unavailable', 'messageId': ident,
                   'message': '本条明确引用了这条消息，但原消息暂时不可读取，不能猜测其图片或文字'}
    try:
        response = await asyncio.wait_for(call('get_msg', {'message_id': int(ident)}), 5)
        raw = response.get('data')
        if (response.get('retcode') != 0 or not isinstance(raw, dict)
                or str(raw.get('message_id')) != ident or str(raw.get('self_id', account)) != account
                or raw.get('message_type', kind) != kind):
            return unavailable, None
        if kind == 'group' and str(raw.get('group_id', '')) != target:
            return unavailable, None
        if kind == 'private':
            sender = event_sender(raw)
            if sender not in {account, target}:
                return unavailable, None
            # Incoming sender identifies the private peer; own outgoing messages
            # require the explicit peer or evidence from this peer's history.
            known = any(str(row.get('messageId')) == ident for row in (history or {}).get('messages', []))
            if sender == account and str(raw.get('target_id', '')) != target and not known:
                return unavailable, None
            if raw.get('group_id') not in (None, '', 0, '0'):
                return unavailable, None
        from .history import _message
        row = _message(raw, account, kind, target, 0)
        result = {key: row[key] for key in ('messageId', 'senderId', 'senderName', 'text', 'mentions')}
        result['text'] = result['text'][:3000]
        result.update(status='resolved', imageCount=len(row['imageRefs']))
        return result, raw
    except (TimeoutError, ConnectionError, OSError, ValueError, TypeError):
        return unavailable, None


def message_context(event: dict, account: str, kind: str, target: str,
                    reply: dict, history: dict | None = None) -> dict:
    from .identity import mentions
    context = {'account': account, 'targetType': kind, 'targetId': target,
               'messageId': str(event.get('message_id', '')), 'mentions': mentions(event),
               'replyTo': reply, 'recentMessages': []}
    for row in (history or {}).get('messages', [])[-10:]:
        item = {key: row[key] for key in ('messageId', 'senderId', 'senderName', 'text', 'mentions', 'replyTo') if key in row}
        item['text'] = str(item.get('text', ''))[:1000]
        item['imageCount'] = len(row.get('imageRefs', []))
        context['recentMessages'].append(item)
    return context
