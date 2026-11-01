"""QQ person metadata joined by account and numeric ID, never by display name."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .person_memory import PersonMemoryStore

_ID = re.compile(r"[0-9]{1,20}\Z")
_CQ_AT = re.compile(r"\[CQ:at,qq=([0-9]{1,20}|all)(?:,[^\]]*)?\]")


def qq_id(value) -> str:
    value = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return value if _ID.fullmatch(value) else ""


def event_sender(event: dict) -> str:
    sender = event.get('sender') or {}
    values = [v for v in (event.get('user_id'), sender.get('user_id') if isinstance(sender, dict) else None) if v is not None]
    ids = [qq_id(value) for value in values]
    return ids[0] if ids and all(ids) and len(set(ids)) == 1 else ''


def mention_text(segment: dict) -> str:
    data = segment.get('data') or {}
    ident = data.get('qq') if isinstance(data, dict) else None
    if ident == 'all':
        return '@全体成员'
    ident = qq_id(ident)
    return f'@<QQ:{ident}>' if ident else '[at]'


def mentions(event: dict) -> list[dict]:
    message = event.get('message', event.get('raw_message', ''))
    result = []
    if isinstance(message, str):
        return [{'userId': m.group(1), 'offset': m.start()}
                for m in list(_CQ_AT.finditer(message))[:50] if qq_id(m.group(1))]
    for index, segment in enumerate(message if isinstance(message, list) else []):
        if isinstance(segment, dict) and segment.get('type') == 'at':
            data = segment.get('data') or {}
            ident = qq_id(data.get('qq')) if isinstance(data, dict) else ''
            if ident:
                result.append({'userId': ident, 'segmentIndex': index})
    return result[:50]


def replace_cq_mentions(text: str) -> str:
    return _CQ_AT.sub(lambda m: mention_text({'data': {'qq': m.group(1)}}), text)


def display(value) -> str:
    return str(value or '').replace('\x00', '')[:100]


class IdentityDirectory:
    def __init__(self, runtime: Path):
        self.root = runtime / 'identities'
        self.person_memory = PersonMemoryStore(runtime / 'person-memory')

    def aliases(self, account: str) -> dict:
        if not qq_id(account):
            raise ValueError('无效 QQ 账号')
        try:
            raw = json.loads((self.root / (account + '.json')).read_text(encoding='utf-8'))
            return {ident: [display(a) for a in values if isinstance(a, str) and a.strip()][:20]
                    for ident, values in raw.items() if qq_id(ident) and isinstance(values, list)}
        except (OSError, ValueError, AttributeError):
            return {}

    def remember(self, account: str, user_id: str, alias: str) -> dict:
        if not qq_id(user_id) or not isinstance(alias, str) or not alias.strip() or len(alias) > 80:
            raise ValueError('请提供真实 QQ 号和不超过 80 字的称呼')
        alias = alias.strip()
        rows = self.aliases(account)
        values = rows.setdefault(user_id, [])
        if alias not in values:
            if len(values) >= 20:
                raise ValueError('该联系人的称呼已达 20 个，请先整理')
            values.append(alias)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (account + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
        return {'ok': True, 'account': account, 'userId': user_id, 'alias': alias,
                'remembered': True, 'sent': False}

    def context(self, account: str, kind: str, target: str, contacts: list,
                event: dict | None = None, history: dict | None = None) -> dict:
        rows = {}

        def person(ident, nickname='', card=''):
            ident = qq_id(ident)
            if not ident:
                return
            row = rows.setdefault(ident, {'userId': ident, 'nickname': '', 'groupCard': '',
                                         'friendRemark': '', 'isFriend': False, 'aliases': []})
            if nickname:
                row['nickname'] = display(nickname)
            if card and kind == 'group':
                row['groupCard'] = display(card)

        event = event or {}
        sender = event.get('sender') or {}
        person(event_sender(event), sender.get('nickname'), sender.get('card'))
        for mention in mentions(event):
            person(mention['userId'])
        for item in (history or {}).get('messages', []):
            person(item.get('senderId'), item.get('senderNickname'), item.get('senderCard'))
            for mention in item.get('mentions', []):
                person(mention.get('userId'))
        # Current display names win over potentially older history cards.
        person(event_sender(event), sender.get('nickname'), sender.get('card'))
        for mention in mentions(event):
            person(mention['userId'])
        if kind == 'private':
            person(target)
        aliases = self.aliases(account)
        friends = {c['id']: c for c in contacts if c.get('kind') == 'private'}
        for ident, row in rows.items():
            friend = friends.get(ident)
            if friend:
                row.update(isFriend=True, friendRemark=display(friend.get('remark')))
                row['nickname'] = display(friend.get('name')) or row['nickname']
            row['aliases'] = aliases.get(ident, [])
        memories = self.person_memory.by_people(account, kind, target, list(rows))
        for ident, values in memories.items():
            if values:
                rows[ident]['memories'] = values
        return {'accountId': account, 'groupId': target if kind == 'group' else '',
                'people': list(rows.values())[:80]}
