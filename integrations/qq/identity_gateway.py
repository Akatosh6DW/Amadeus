"""Gateway-backed identity lookup and owner-authorized alias persistence."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time

from .identity import IdentityDirectory, display, mentions, qq_id
from .policy import control_allowed


class QQIdentities:
    def __init__(self, gateway):
        self.gateway = gateway
        self.directory = IdentityDirectory(gateway.config_path.parent)
        self.instance = secrets.token_hex(16)
        self.members = {}

    def _signature(self, claims):
        wire = json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()
        return hmac.new(self.gateway.config['control_token'].encode(), wire, hashlib.sha256).hexdigest()

    def scope(self, account, kind, target, sender, context, event=None):
        claims = {'account': account, 'kind': kind, 'target': target, 'sender': sender,
                  'people': [p['userId'] for p in context.get('people', [])],
                  'generation': self.gateway.generation, 'instance': self.instance,
                  'expires': time.time() + 900}
        # Bind source provenance when the caller has the real incoming event.
        # The event is never copied wholesale into the signed scope.
        if isinstance(event, dict):
            message_id = str(event.get('message_id') or '').strip()
            if message_id and len(message_id) <= 80 and all(ch.isalnum() or ch in '-_:' for ch in message_id):
                claims['sourceMessageId'] = message_id
            source_time = event.get('time')
            if isinstance(source_time, (int, float)) and not isinstance(source_time, bool):
                claims['sourceTime'] = int(source_time)
        return {'claims': claims, 'signature': self._signature(claims)}

    async def member(self, call, account, group, ident):
        key = (account, group, ident)
        cached = self.members.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        result = await asyncio.wait_for(call('get_group_member_info', {'group_id': group, 'user_id': ident, 'no_cache': False}), 2)
        data = result.get('data') or {}
        if result.get('retcode') != 0 or qq_id(data.get('user_id')) != ident:
            return None
        row = {'userId': ident, 'nickname': display(data.get('nickname')), 'groupCard': display(data.get('card'))}
        self.members[key] = (time.monotonic(), row)
        return row

    async def context(self, account, kind, target, event=None, history=None, call=None):
        gateway = self.gateway
        contacts = await gateway.contacts()
        if not contacts and gateway.account_verified:
            contacts = await gateway.contacts(force=True)
        context = self.directory.context(account, kind, target, contacts, event, history)
        if kind == 'group':
            for person in context['people']:
                if person['groupCard']:
                    self.members[(account, target, person['userId'])] = (time.monotonic(),
                        {key: person[key] for key in ('userId', 'nickname', 'groupCard')})
        if kind == 'group' and call is not None:
            # Existing history already supplies its senders. Resolve current @ targets
            # without loading every member or anyone's private conversation.
            ids = list(dict.fromkeys(m['userId'] for m in mentions(event or {}) if m['userId'] != account))[:8]
            async def fetch(ident):
                try:
                    return await self.member(call, account, target, ident)
                except (TimeoutError, ConnectionError, ValueError):
                    return None
            results = await asyncio.gather(*(fetch(ident) for ident in ids))
            by_id = {p['userId']: p for p in context['people']}
            for row in results:
                if row and row['userId'] in by_id:
                    person = by_id[row['userId']]
                    person['groupCard'] = row['groupCard']
                    person['nickname'] = person['nickname'] or row['nickname']
        return context

    async def execute(self, args, *, owner_authorized: bool = False):
        gateway = self.gateway
        account = str(args.get('expectedAccount') or '')
        if account != gateway.policy['account_id']:
            raise ValueError('QQ 账号已变化，请重新读取身份信息')
        generation = gateway.generation
        action = args.get('action', 'lookup')
        if action not in {'lookup', 'remember_alias', 'memory_lookup', 'memory_remember', 'memory_forget'}:
            raise ValueError('未知身份操作')
        kind, target = args.get('targetType'), str(args.get('targetId') or '')
        scope = args.get('scope')
        claims = None
        if scope is not None:
            if not isinstance(scope, dict) or not isinstance(scope.get('claims'), dict):
                raise ValueError('无效身份查询范围')
            claims = scope['claims']
            if (not hmac.compare_digest(str(scope.get('signature', '')), self._signature(claims))
                    or claims.get('account') != account or claims.get('generation') != generation
                    or claims.get('instance') != self.instance or claims.get('expires', 0) < time.time()
                    or (kind, target) != (claims.get('kind'), claims.get('target'))):
                raise ValueError('QQ 身份查询范围已失效')
            if action == 'remember_alias' and claims.get('sender') not in gateway.policy['owner_ids']:
                raise ValueError('只有主人本人明确介绍的称呼可以保存')
            if action.startswith('memory_') and action != 'memory_lookup' and not claims.get('sender'):
                raise ValueError('人物认知写入需要当前真实 QQ 发言者')
        if kind is not None:
            target_allowed = (control_allowed(gateway.policy, kind, target)
                              if owner_authorized else
                              kind in {'private', 'group'}
                              and target in gateway.policy[kind + '_ids'])
            if not target_allowed:
                raise ValueError('该会话未授权')
        call = await gateway.require_account(account)
        contacts = await gateway.contacts()
        def ensure_current() -> None:
            if (generation != gateway.generation or account != gateway.policy['account_id']
                    or not gateway.account_verified):
                raise ValueError('QQ 账号或授权已改变，不能保存旧会话人物认知')
        user_id = qq_id(args.get('userId'))
        query = str(args.get('query') or '').strip().casefold()[:100]
        ids = set(claims.get('people', [])) if claims else {c['id'] for c in contacts if c['kind'] == 'private'}
        if claims and user_id and user_id not in ids:
            raise ValueError('只能核对当前消息和已提供历史中出现的对象')
        if kind == 'private':
            ids = {target, account} if claims else ids | {target}
        aliases = self.directory.aliases(account)
        if claims is None:
            ids.update(aliases)

        if action.startswith('memory_'):
            store = self.directory.person_memory
            if action == 'memory_lookup':
                ensure_current()
                if claims is not None and kind not in {'private', 'group'}:
                    raise ValueError('QQ 身份查询范围缺少会话')
                if claims is not None and user_id and user_id not in ids:
                    raise ValueError('只能查询当前消息和已提供历史中出现的对象')
                rows = store.list(account,
                                  kind=kind if claims is not None else (kind if kind in {'private', 'group'} else None),
                                  target=target if kind in {'private', 'group'} else None,
                                  user_id=user_id or None,
                                  include_shared=claims is None and bool(args.get('includeShared')))
                return {'ok': True, 'account': account,
                        'targetType': kind if kind in {'private', 'group'} else '',
                        'targetId': target if kind in {'private', 'group'} else '',
                        'userId': user_id, 'memories': rows[:80],
                        'ambiguous': False, 'truncated': len(rows) > 80}

            if kind not in {'private', 'group'} or not target:
                raise ValueError('人物认知写入需要明确 private/group 会话')
            if not user_id:
                raise ValueError('人物认知写入需要有效 userId')
            if claims is not None and user_id not in ids:
                raise ValueError('只能写入当前消息和已提供历史中出现的对象')
            sender = qq_id(claims.get('sender')) if claims else qq_id(args.get('sourceSenderId'))
            owner = claims is None or sender in {qq_id(value) for value in gateway.policy.get('owner_ids', [])}
            if action == 'memory_remember':
                ensure_current()
                source_role = str(args.get('sourceRole') or '').strip()
                if source_role not in {'owner_explicit', 'person_statement', 'assistant_inference'}:
                    raise ValueError('sourceRole 必须区分 owner_explicit、person_statement 或 assistant_inference')
                if not owner and source_role == 'owner_explicit':
                    raise ValueError('普通 QQ 发言者不能伪造主人明确认知')
                if not owner and user_id != sender and source_role != 'person_statement':
                    raise ValueError('对第三人的普通陈述只能保存为 person_statement 归因材料')
                shared = args.get('shared', False)
                if shared and (not owner or source_role != 'owner_explicit'):
                    raise ValueError('只有主人明确认知可以标记 shared')
                source_message_id = str((claims or {}).get('sourceMessageId') or args.get('sourceMessageId') or '')
                source_time = (claims or {}).get('sourceTime') or args.get('sourceTime') or ''
                return store.remember(
                    account, kind=kind, target=target, user_id=user_id,
                    topic=args.get('topic'), value=args.get('value'), source_role=source_role,
                    source_sender_id=sender, source_message_id=source_message_id,
                    source_time=source_time, shared=shared, certainty=args.get('certainty', ''),
                )
            if action == 'memory_forget':
                ensure_current()
                return store.forget(account, kind=kind, target=target, user_id=user_id,
                                    topic=args.get('topic', ''), memory_id=args.get('memoryId', ''),
                                    source_sender_id=sender, owner=owner)
        member_rows = {}
        if kind == 'group' and user_id:
            row = await self.member(call, account, target, user_id)
            if row:
                ids.add(user_id)
                member_rows[user_id] = row
        elif kind == 'group' and query and claims is None:
            result = await asyncio.wait_for(call('get_group_member_list', {'group_id': target}), 5)
            for row in result.get('data', []) if result.get('retcode') == 0 and isinstance(result.get('data'), list) else []:
                ident = qq_id(row.get('user_id')) if isinstance(row, dict) else ''
                if ident and any(query in str(value).casefold() for value in (ident, row.get('nickname', ''), row.get('card', ''))):
                    ids.add(ident)
                    member_rows[ident] = {'userId': ident, 'nickname': display(row.get('nickname')), 'groupCard': display(row.get('card'))}
                    if len(member_rows) >= 50:
                        break
        if user_id:
            ids.intersection_update({user_id})
        elif query:
            friend_rows = {c['id']: c for c in contacts if c['kind'] == 'private'}
            ids = {ident for ident in ids if any(query in str(v).casefold() for v in [ident,
                friend_rows.get(ident, {}).get('name', ''), friend_rows.get(ident, {}).get('remark', ''),
                member_rows.get(ident, {}).get('nickname', ''), member_rows.get(ident, {}).get('groupCard', ''),
                self.members.get((account, target, ident), (0, {}))[1].get('groupCard', ''), *aliases.get(ident, [])])}
        context = self.directory.context(account, kind or 'private', target, contacts,
            history={'messages': [{'senderId': ident} for ident in sorted(ids)]})
        for person in context['people']:
            row = member_rows.get(person['userId'])
            if row:
                person.update(groupCard=row['groupCard'])
                person['nickname'] = person['nickname'] or row['nickname']
            elif kind == 'group':
                cached = self.members.get((account, target, person['userId']))
                if cached:
                    person['groupCard'] = cached[1]['groupCard']
        people = [p for p in context['people'] if (not user_id or p['userId'] == user_id) and (not query or any(
            query in str(v).casefold() for v in [p['userId'], p['nickname'], p['friendRemark'], p['groupCard'], *p['aliases']]))]
        if generation != gateway.generation or account != gateway.policy['account_id'] or not gateway.account_verified:
            raise ValueError('读取期间 QQ 账号或授权已改变')
        if action == 'remember_alias':
            if not user_id or len(people) != 1 or (claims and user_id not in claims.get('people', [])):
                raise ValueError('需先明确当前消息或历史中真实出现的 QQ 对象')
            return self.directory.remember(account, user_id, args.get('alias'))
        return {'ok': True, 'account': account, 'groupId': target if kind == 'group' else '',
                'people': people[:30], 'ambiguous': len(people) > 1, 'truncated': len(people) > 30}
