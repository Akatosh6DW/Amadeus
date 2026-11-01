"""Cheap group wake-up rules. Only quoted-message metadata needs a QQ call."""
from __future__ import annotations

import asyncio
from .references import reply_id
from .identity import mentions


def segments(event: dict, kind: str) -> list[dict]:
    message = event.get('message')
    return [s['data'] for s in message if isinstance(s, dict) and s.get('type') == kind
            and isinstance(s.get('data'), dict)] if isinstance(message, list) else []


def sender_id(event: dict) -> str:
    sender = event.get('sender') or {}
    return str(sender.get('user_id') or event.get('user_id') or '')


def mentions_account(event: dict, account: str) -> bool:
    return any(str(s.get('qq', '')) == account for s in segments(event, 'at'))


def local_group_match(event: dict, account: str, policy: dict) -> str:
    modes = policy.get('group_triggers', ['mention'])
    if 'all' in modes:
        return 'all'
    if 'mention' in modes and mentions_account(event, account):
        return 'mention'
    if 'keyword' in modes:
        text = ''.join(str(s.get('text', '')) for s in segments(event, 'text')).casefold()
        if any(word.casefold() in text for word in policy.get('group_keywords', []) if word):
            return 'keyword'
    return ''


def quoted_message_id(event: dict, policy: dict) -> str:
    if 'reply' not in policy.get('group_triggers', ['mention']):
        return ''
    return reply_id(event)


def trigger_record(event: dict, matched: str) -> dict:
    sender = event.get('sender') or {}
    return {'type': 'group_mention' if matched == 'mention' else 'group_trigger',
            'matchedBy': matched, 'messageId': str(event.get('message_id', '')),
            'senderId': sender_id(event),
            'senderName': str(sender.get('card') or sender.get('nickname') or '')[:100],
            'mentions': mentions(event), 'replyToMessageId': reply_id(event)}


async def resolve_group_trigger(call, event: dict, account: str, policy: dict) -> dict | None:
    if sender_id(event) == account:
        return None
    matched = local_group_match(event, account, policy)
    if not matched:
        ident = quoted_message_id(event, policy)
        if not ident:
            return None
        try:
            result = await asyncio.wait_for(call('get_msg', {'message_id': int(ident)}), timeout=5)
        except (TimeoutError, ConnectionError, ValueError):
            return None
        quoted = result.get('data') or {}
        if (result.get('retcode') != 0 or not isinstance(quoted, dict)
                or str(quoted.get('message_id', '')) != ident
                or str(quoted.get('group_id', '')) != str(event.get('group_id', ''))
                or str(quoted.get('self_id', account)) != account
                or quoted.get('message_type', 'group') != 'group'
                or sender_id(quoted) != account):
            return None
        matched = 'reply'
    return trigger_record(event, matched)
