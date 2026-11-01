"""Local OneBot permission gateway between NapCat, AstrBot and Amadeus.

NapCat is a forward WS server. This process connects to it and presents itself
as a reverse WS client to AstrBot. Only the gateway possesses NapCat's token.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import hashlib
import hmac
import inspect
import random
import re
import secrets
import time
import uuid
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from .policy import (Ledger, allowed, control_allowed, conversation, normalize_policy,
                     outgoing, format_reply, policy_revision, conversation_policy)
from .account_profiles import account_summaries, remember_account, select_account, validate_account_id
from .amadeus_runtime import AmadeusRunner, message_text_length, reply_trigger, reply_request_id, MAX_QQ_TEXT_CHARS
from .triggers import resolve_group_trigger, sender_id
from .policy import reply_delay, voice_enabled
from .reply_segments import (
    DEFAULT_REPLY_BUBBLE_CHARS,
    MAX_OWNER_CONTROL_BUBBLES,
    MAX_REPLY_BUBBLES,
    split_reply,
)
from .history import read_history
from .media import collect_images, image_segments
from .contacts import clean_contacts, read_contacts, write_contacts
from .identity_gateway import QQIdentities
from .references import resolve_reply, message_context
from .context_reset import (ContextResetStore, archive_profiles, restore_profiles,
                             scope_key as reset_scope_key, validate_scope)
from .owner_control import (OwnerControlClient, OwnerControlUnavailable, OwnerControlBackendFailure,
                             OwnerControlOutcomeUnknown, OwnerReceiptStore, owner_receipt_key,
                             event_sender_id, exact_account_mention,
                             owner_message_text)
from .owner_commands import developer_command, persona_command
from .qzone import QzoneService
from .qzone_schedule import QzoneSchedule


_MEDIA_MAX_FILE_BYTES = 10 * 1024 * 1024
_MEDIA_MAX_BASE64_LENGTH = ((_MEDIA_MAX_FILE_BYTES + 2) * 4 // 3) + 8


class _DeliveryStopped(Exception):
    """A bubble was safely prevented before its OneBot side effect."""

    def __init__(self, detail: str, *, part_index: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.part_index = part_index


def _delivery_parts(message: list[dict], max_chars: int, *, compact: bool = True,
                    max_parts: int = MAX_REPLY_BUBBLES) -> list[list[dict]]:
    """Split one plain text draft into immutable OneBot message payloads."""
    if not isinstance(message, list) or not message:
        return [message]
    result = []
    text_buffer = []

    def flush_text():
        if not text_buffer:
            return
        text = ''.join(text_buffer)
        # Keep each short message a valid OneBot message (a list of segments)
        # and apply the same compact-reply rendering at the actual bubble
        # boundary.  Applying it here preserves complete sentence boundaries
        # while removing the model's trailing punctuation/newlines per bubble.
        stripped = text.strip()
        parts = ([stripped] if not compact and
                 (max_chars == 0 or len(stripped) <= max_chars)
                 else split_reply(text, max_chars, max_parts=max_parts))
        for part in parts:
            rendered = format_reply(part, compact)
            if rendered:
                result.append([{'type': 'text', 'data': {'text': rendered}}])
        text_buffer.clear()

    for segment in message:
        if not isinstance(segment, dict):
            return [message]
        if segment.get('type') == 'text' and isinstance(segment.get('data', {}).get('text'), str):
            text_buffer.append(segment['data']['text'])
            continue
        if segment.get('type') in {'image', 'qq_image'}:
            flush_text()
            result.append([segment])
            continue
        return [message]
    flush_text()
    return result or [message]


class Gateway:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = remember_account(json.loads(config_path.read_text(encoding="utf-8")))
        self.policy = normalize_policy(self.config["policy"])
        self.identities = QQIdentities(self)
        self.ledger = Ledger(config_path.parent / "ledger.sqlite3")
        self.napcat = None
        self.astrbot = None
        self.pending: dict[str, asyncio.Future] = {}
        self.account_verified = False
        self.generation = 0
        self.last_error = "尚未连接"
        self.send_lock = asyncio.Lock()
        self.delegate_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.owner_tasks: set[asyncio.Task] = set()
        self.amadeus = AmadeusRunner(config_path.parent, python=self.config.get("amadeus_python"), frozen=self.config.get('amadeus_frozen', False))
        self.amadeus_tasks: set[asyncio.Task] = set()
        self.owners: dict[str, float] = {}
        self.lease_seconds = 25
        self.started_at = time.monotonic()
        self.stop_requested = False
        self.stop_event = asyncio.Event()
        self._reset_store = ContextResetStore(config_path.parent / "context-resets.json")
        self._reset_state_lock = asyncio.Lock()
        self._resetting_scopes: set[str] = set()
        self._scope_tasks: dict[str, set[asyncio.Task]] = {}
        # NapCat can deliver a message while a target is being reset.  Keep
        # those events out of ``fresh_incoming`` until the new epoch exists;
        # otherwise the deduplication ledger would consume them before the
        # reset task could schedule a reply.
        self._deferred_events: dict[str, list[dict]] = {}
        self._deferred_message_ids: dict[str, set[str]] = {}
        self._contacts_path = config_path.parent / 'contacts-cache.json'
        self._contacts: list[dict] = read_contacts(self._contacts_path, self.policy['account_id'])
        self._contacts_at = 0.0
        self._contacts_account = self.policy['account_id']
        self._contacts_complete = False
        self._contacts_refresh = None
        # A bounded pause lets split messages enter QQ history before reading it.
        # No subsequent message is required to wake the model.
        self.group_context_settle_seconds = 3.5
        # Owner control uses the already-running desktop HTTP backend.  Keep
        # its client separate from the ordinary reply agents and remember
        # accepted message IDs so a duplicated OneBot event cannot run or
        # deliver the owner command twice.
        self.owner_control = OwnerControlClient(config_path.parent)
        self.owner_receipts = OwnerReceiptStore(config_path.parent / 'owner-receipts.sqlite3')
        self._owner_control_seen: set[tuple[str, str, str, str, str]] = set()
        self.qzone = QzoneService(config_path.parent)
        self.qzone_lock = asyncio.Lock()
        self.qzone_schedule = QzoneSchedule(config_path.parent)

    async def execute_qzone(self, data: dict) -> dict:
        async with self.qzone_lock:
            policy = dict(self.policy)
            if not self.lifecycle_status()['active']:
                raise ValueError('QQ 后台未启用，空间操作已停止')
            if data.get('expectedAccount') != policy['account_id']:
                raise ValueError('QQ 账号已变化，请重新读取空间状态')
            args = data.get('args')
            if not isinstance(args, dict):
                raise ValueError('空间操作参数无效')
            if args.get('action') == 'publish' and not policy['qzone_allow_publish']:
                return {'ok': False, 'error': 'qzone_publish_disabled', 'message': '发说说权限尚未开启'}
            call = await self.require_account(policy['account_id'])
            result = await self.qzone.execute(args, policy, call)
            # Account-scoped calls reject stale transports. Keep receipts small;
            # never include cookies, private content or feed bodies in activity.
            self.record_event('qzone_action', {'account': policy['account_id'],
                'action': args.get('action'), 'ok': result.get('ok') is True,
                'state': result.get('state', result.get('error', 'completed'))})
            return result

    async def qzone_visit(self):
        policy = dict(self.policy)
        if (not self.lifecycle_status()['active'] or not self.account_verified
                or not policy['qzone_enabled'] or not policy['qzone_autonomous']):
            return
        account = policy['account_id']
        run_id = self.qzone_schedule.claim(account, policy['qzone_interval_minutes'])
        if not run_id:
            return
        summary = ''
        try:
            from .qzone_runtime import QzoneRunner
            model = json.loads(self.amadeus.model_path.read_text(encoding='utf-8'))
            runner = QzoneRunner(self.config_path.parent, python=self.config.get('amadeus_python'),
                                 frozen=self.config.get('amadeus_frozen', False))
            result = await runner.run(account=account, settings=model,
                control_url=f"http://127.0.0.1:{self.config['control_port']}",
                control_token=self.config['control_token'], instructions=policy['qzone_instructions'],
                allow_write=policy['qzone_allow_write'], allow_publish=policy['qzone_allow_publish'])
            state = 'completed' if result.get('ok') else 'failed'
            summary = str(result.get('reply') or '')[:500]
        except asyncio.CancelledError:
            self.qzone_schedule.finish(account, run_id, 'unknown')
            raise
        except Exception:
            state = 'failed'
        self.qzone_schedule.finish(account, run_id, state)
        self.record_event('qzone_visit', {'account': account, 'state': state, 'runId': run_id,
            'reason': summary if state == 'completed' else '本次空间自主检查未完成，将按设置间隔再检查'})

    async def watch_qzone(self):
        while not self.stop_requested:
            task = self.spawn(self.qzone_visit())
            try:
                await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                pass  # A failed visit is retried only at its saved next interval.
            await asyncio.sleep(10)

    def lifecycle_status(self):
        now = time.monotonic()
        self.owners = {key: seen for key, seen in self.owners.items() if now - seen < self.lease_seconds}
        background = self.policy['background_enabled']
        return {'desktopOnline': bool(self.owners), 'backgroundEnabled': background,
                'active': not self.stop_requested and (background or bool(self.owners))}

    def record_event(self, kind: str, payload: dict):
        self.ledger.event(kind, {'account': self.policy['account_id'], **payload})

    def persist_policy(self, policy: dict):
        normalized = normalize_policy(policy)
        account_changed = normalized["account_id"] != self.policy["account_id"]
        if account_changed:
            # A policy carrying another account must never carry this account's
            # allowlist or per-conversation preferences with it.
            updated = select_account({**self.config, "policy": self.policy},
                                     normalized["account_id"])
            normalized = updated["policy"]
        else:
            updated = {**self.config, 'policy': normalized}
        if (not account_changed and normalized['mode'] == 'paused'
                and self.policy['mode'] != 'paused'):
            updated['resume_mode'] = self.policy['mode']
        updated = remember_account(updated)
        temporary = self.config_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.config_path)
        self.config, self.policy = updated, updated['policy']

    def request_stop(self, reason: str, *, explicit: bool = False):
        if self.stop_requested:
            return
        if explicit:
            self.persist_policy({**self.policy, 'mode': 'paused', 'background_enabled': False})
        self.stop_requested = True
        self.generation += 1
        self.revoke_tasks()
        self.record_event('background_stopped', {'reason': reason, 'explicit': explicit})
        asyncio.get_running_loop().call_later(.2, self.stop_event.set)

    async def watch_owner(self):
        while not self.stop_requested:
            await asyncio.sleep(1)
            if not self.lifecycle_status()['active'] and time.monotonic() - self.started_at > self.lease_seconds:
                self.request_stop('桌面租约已过期，未授权后台接管')

    async def contacts(self, *, force=False):
        account, generation = self.policy['account_id'], self.generation
        if self._contacts_account != account:
            self._contacts, self._contacts_at, self._contacts_account = [], 0.0, account
            self._contacts_complete = False
        if self.account_verified and (force or time.monotonic() - self._contacts_at > (60 if self._contacts_complete else 5)):
            if not self._contacts_refresh or self._contacts_refresh.done():
                self._contacts_refresh = self.spawn(self.refresh_contacts(account, generation))
            if force:
                try:
                    await asyncio.shield(self._contacts_refresh)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    # Changing reply settings cancels generation-bound tasks.
                    # Keep cached names and let the next read retry the refresh.
                    self._contacts_complete = False
                    self._contacts_at = 0.0
        aliases = self.identities.directory.aliases(account)
        return [{**row, **({'aliases': aliases[row['id']]} if row['kind'] == 'private' and row['id'] in aliases else {})}
                for row in self._contacts]

    async def refresh_contacts(self, account, generation):
        try:
            call = await self.require_account(account)
        except (ValueError, ConnectionError, TimeoutError):
            self._contacts_complete = False
            self._contacts_at = 0.0
            return
        async def fetch(kind, action, key, name):
            try:
                result = await asyncio.wait_for(call(action), 3)
                rows = result.get('data')
                if result.get('retcode') == 0 and isinstance(rows, list):
                    return kind, clean_contacts([{'kind':kind,'id':row.get(key), 'name':row.get(name), 'remark':row.get('remark')}
                        for row in rows[:5000] if isinstance(row, dict)])
            except (ValueError, ConnectionError, TimeoutError):
                pass
            return kind, None
        results = await asyncio.gather(fetch('private','get_friend_list','user_id','nickname'),
                                       fetch('group','get_group_list','group_id','group_name'))
        if account == self.policy['account_id'] and generation == self.generation:
            contacts = list(self._contacts)
            for kind, rows in results:
                if rows is not None:
                    contacts = [row for row in contacts if row['kind'] != kind] + rows
            self._contacts, self._contacts_at = contacts, time.monotonic()
            self._contacts_complete = all(rows is not None for _, rows in results)
            if any(rows is not None for _, rows in results):
                try:
                    write_contacts(self._contacts_path, account, contacts)
                except OSError:
                    pass  # A display-name cache failure must not disrupt QQ transport.

    def annotate_history(self, context: dict):
        ids = [str(item['messageId']) for item in context.get('messages', [])]
        delivered = set()
        if ids:
            with self.ledger.lock:
                delivered = {str(row[0]) for row in self.ledger.db.execute(
                    "SELECT message_id FROM sends WHERE state='sent' AND account=? AND kind=? AND target=? AND message_id IN (" + ','.join('?' for _ in ids) + ')',
                    [self.policy['account_id'], context.get('targetType'), context.get('targetId'), *ids])}
        for item in context.get('messages', []):
            if item['senderId'] == self.policy['account_id'] and item['messageId'] in delivered:
                item['speakerRole'] = 'assistant'
            for ref in item.get('imageRefs', []):
                ref['token'] = self.image_token(context['account'], context['targetType'], context['targetId'], item['messageId'], ref['index'])
        context['identityContext'] = self.identities.directory.context(
            self.policy['account_id'], context.get('targetType'), context.get('targetId'),
            self._contacts, history=context)
        return context

    async def message_material(self, call, event, account, kind, target, history=None):
        """Keep the reply edge and its pixels through either owner or child routing."""
        reply, quoted = await resolve_reply(call, event, account, kind, target, history)
        images, notes = [], []
        for relation, source in [('reply', quoted), ('current', event)]:
            if source is None:
                continue
            parts = image_segments(source)
            remaining = 2 - len(images)
            if not parts or remaining <= 0:
                if parts:
                    notes.append('本次已读取两张图片，其余图片尚未读取')
                continue
            loaded, failed = await collect_images(call, {'message': parts[:remaining]})
            ident = str(source.get('message_id', ''))
            images.extend({**item, 'sourceMessageId': ident, 'sourceRelation': relation} for item in loaded)
            label = '引用消息' if relation == 'reply' else '当前消息'
            notes.extend(f'{label} {ident}：{note}' for note in failed)
            if len(parts) > remaining:
                notes.append(f'{label} {ident} 只读取前 {remaining} 张图片，其余图片尚未读取')
        if reply.get('status') == 'unavailable':
            notes.append(reply['message'])
        if quoted is not None and history is not None:
            from .history import _message
            row = _message(quoted, account, kind, target, 0)
            row.pop('_realSeq', None)
            row.pop('_index', None)
            row['sourceRelation'] = 'reply'
            projected = self.annotate_history({'ok': True, 'account': account, 'targetType': kind,
                                               'targetId': target, 'messages': [row]})
            history['referencedMessages'] = projected['messages']
        if reply:
            self.record_event('reply_context_read', {'account': account, 'kind': kind, 'target': target,
                'message_id': event.get('message_id'), 'reply_message_id': reply['messageId'],
                'status': reply['status'], 'imageCount': sum(i['sourceRelation'] == 'reply' for i in images)})
        return message_context(event, account, kind, target, reply, history), images, notes

    def image_token(self, account, kind, target, message_id, index):
        value = json.dumps([account, kind, target, str(message_id), index], separators=(',', ':'))
        return hmac.new(self.config['control_token'].encode(), value.encode(), hashlib.sha256).hexdigest()

    async def read_message_image(self, args):
        account, kind, target = str(args.get('expectedAccount', '')), args.get('targetType'), str(args.get('targetId', ''))
        message_id, index = str(args.get('messageId', '')), args.get('index')
        if (not self.account_verified or account != self.policy['account_id'] or kind not in {'private', 'group'}
                or target not in self.policy[kind + '_ids']):
            raise ValueError('图片不属于当前已授权 QQ 会话')
        if (type(index) is not int or not 0 <= index < 2 or not message_id.lstrip('-').isdigit()
                or len(message_id) > 32 or not hmac.compare_digest(str(args.get('token', '')),
                    self.image_token(account, kind, target, message_id, index))):
            raise ValueError('请先读取当前会话历史，再选择其中真实的图片引用')
        generation = self.generation
        call = await self.require_account(account)
        response = await call('get_msg', {'message_id': int(message_id)})
        event = response.get('data') or {}
        if response.get('retcode') != 0 or not isinstance(event, dict) or str(event.get('message_id')) != message_id:
            raise ValueError('这条图片消息已不可读取')
        # The signed reference was issued only from scope-checked history.
        # Recheck returned routing fields too, before touching a media URL.
        from .history import _message
        _message(event, account, kind, target, 0)
        segments = image_segments(event)
        images, notes = await collect_images(call, {'message': segments[index:index+1]})
        if generation != self.generation or not self.account_verified:
            raise ValueError('读取期间 QQ 账号或授权已改变')
        self.record_event('image_read', {'account': account, 'kind': kind, 'target': target,
            'message_id': message_id, 'imageCount': len(images), 'notes': notes, 'source': 'history'})
        return {'ok': bool(images), 'images': images, 'notes': notes, 'messageId': message_id}

    @property
    def reply_agent(self):
        return self.config.get("reply_agent", "astrbot")

    @property
    def resume_mode(self):
        mode = self.config.get("resume_mode", "review")
        return mode if mode in {"auto", "review"} else "review"

    def spawn(self, coroutine, *, scope: tuple[str, str, str] | None = None, owner_control=False):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        if owner_control:
            self.owner_tasks.add(task)
        scope_id = None
        if scope is not None:
            scope_id = self._scope_id(*scope)
            self._scope_tasks.setdefault(scope_id, set()).add(task)
        def done(completed):
            self.tasks.discard(completed)
            self.owner_tasks.discard(completed)
            if scope_id is not None:
                tasks = self._scope_tasks.get(scope_id)
                if tasks is not None:
                    tasks.discard(completed)
                    if not tasks:
                        self._scope_tasks.pop(scope_id, None)
        task.add_done_callback(done)
        return task

    def _owner_control_enabled(self) -> bool:
        return self.policy.get("owner_control_enabled",
                               self.config.get("owner_control_enabled", False)) is True

    def _owner_control_in_groups(self) -> bool:
        return self.policy.get("owner_control_in_groups",
                               self.config.get("owner_control_in_groups", False)) is True

    def _owner_control_details(self, event: dict, *, check_seen: bool = True):
        """Validate one owner event before ordinary policy/mode filtering.

        This is intentionally independent of ``allowed`` and ``conversation``
        policy.  Owner control has its own explicit configuration and still
        requires a verified live account and desktop lease.
        """

        if (not isinstance(event, dict) or event.get("post_type") != "message"
                or not self._owner_control_enabled()):
            return None
        if not self.account_verified or not self.lifecycle_status()["active"]:
            return None
        account = str(self.policy.get("account_id", ""))
        if not account.isascii() or not account.isdigit() or str(event.get("self_id", "")) != account:
            return None
        sender = event_sender_id(event)
        owners = {str(value) for value in self.policy.get("owner_ids", [])
                  if isinstance(value, (str, int)) and not isinstance(value, bool)}
        if sender is None or sender not in owners or sender == account:
            return None
        message_id = event.get("message_id")
        if (message_id is None or isinstance(message_id, bool)
                or not str(message_id)):
            return None

        kind = event.get("message_type")
        if kind == "private":
            target = str(event.get("user_id", ""))
            if target != sender:
                return None
        elif kind == "group":
            if not self._owner_control_in_groups():
                return None
            target = str(event.get("group_id", ""))
            if target not in {str(value) for value in self.policy.get("group_ids", [])}:
                return None
            # The current managed account must be named by a real OneBot at
            # segment.  Text keywords, ``@all``, quotes, and nicknames do not
            # satisfy this check.
            if not exact_account_mention(event, account):
                return None
        else:
            return None
        # Match the desktop backend's request-id key so a reused OneBot
        # message number in another channel cannot suppress a distinct event.
        marker = (account, sender, kind, target, str(message_id))
        if check_seen and marker in self._owner_control_seen:
            return None
        text = owner_message_text(event)
        if not text:
            return None
        return {
            "account": account,
            "sender": sender,
            "kind": kind,
            "target": target,
            "message_id": str(message_id),
            "text": text,
            "marker": marker,
        }

    def _try_owner_control_event(self, event: dict) -> bool:
        details = self._owner_control_details(event, check_seen=False)
        if details is None:
            return False
        if not self.owner_receipts.claim(*details['marker']):
            return True  # Consume duplicate owner events; never proxy them.
        self._owner_control_seen.add(details["marker"])
        # Keep the local replay guard bounded during a long-running gateway.
        if len(self._owner_control_seen) > 4096:
            self._owner_control_seen = set(list(self._owner_control_seen)[-2048:])
        self.record_event("owner_control_received", {
            "account": details["account"], "kind": details["kind"],
            "target": details["target"], "sender": details["sender"],
            "message_id": details["message_id"],
        })
        self.spawn(self._owner_control_reply(details, event, self.generation, self.napcat), owner_control=True)
        return True

    def _record_owner_control_result(self, details: dict, *, ok: bool,
                                      request_id: str = "", status: str = "",
                                      message_ids: list[str] | None = None) -> None:
        # Keep a small audit receipt without persisting the owner's private
        # text or model/tool result in the QQ transport ledger.
        with contextlib.suppress(Exception):
            payload = {
                "account": details["account"], "kind": details["kind"],
                "target": details["target"], "sender": details["sender"],
                "message_id": details["message_id"], "ok": ok,
                "request_id": request_id, "status": status,
            }
            if message_ids is not None:
                # Message IDs are delivery receipts, not owner content.  Keep
                # them so a successful multi-bubble result is auditable while
                # leaving the private reply text out of the event ledger.
                payload["message_ids"] = [str(value) for value in message_ids]
            self.record_event("owner_control_result", payload)

    async def _owner_request(self, details, event, napcat):
        account, sender = details['account'], details['sender']
        # An event can wait in the task queue while the account/owner settings
        # change. Revalidate before any direct command writes policy, not only
        # later when delivering its acknowledgement.
        current = self._owner_control_details(event, check_seen=False)
        if (current is None or current['marker'] != details['marker']
                or current['account'] != account or self.napcat is not napcat):
            return {'ok': False, 'error': 'qq_scope_changed'}
        # This entry is reached only after gateway owner/account/scene checks.
        # Save through the same policy/profile store used by the settings UI.
        style = persona_command(details['text'], account)
        if style is not None:
            key = f"{details['kind']}:{details['target']}"
            overrides = {name: dict(value) for name, value in self.policy['conversation_overrides'].items()}
            patch = overrides.setdefault(key, {})
            if style == 'auto':
                patch.pop('reply_style', None)
                if not patch:
                    overrides.pop(key)
            else:
                patch['reply_style'] = style
            self.persist_policy({**self.policy, 'conversation_overrides': overrides})
            # A response already being generated under the old mode must not
            # arrive after the switch. Other conversations keep running.
            scope = self._scope_id(account, details['kind'], details['target'])
            for task in list(self._scope_tasks.get(scope, set())):
                if task is not asyncio.current_task() and task not in self.owner_tasks:
                    task.cancel()
            self.record_event('policy_changed', {'actor': '主人', 'reason': '主人通过 QQ 切换本会话人格',
                'kind': details['kind'], 'target': details['target'], 'reply_style': style})
            effective = conversation_policy(self.policy, details['kind'], details['target'])['reply_style']
            reply = '这个会话已切换为' + {'tieba': '贴吧人格 · 孙吧语料',
                'kangba': '最强攻击性语料', 'game': '红莉栖人格'}[effective]
            return {'ok': True, 'reply': reply,
                    'route': 'group_control' if details['kind'] == 'group' else 'owner_task',
                    'controlAck': ['configured']}
        command = developer_command(details['text'], account) if self.policy.get('owner_developer_mode') else None
        words = command.lower().split() if command is not None else []
        if words and words[0] in {'pause', 'resume'} and (len(words) == 1 or words[1:] == ['all']):
            action = words[0]
            reason = '主人通过 QQ 开发者指令' + ('暂停回复' if action == 'pause' else '恢复回复')
            if len(words) == 2:
                await self.set_reply_mode(action, reason)
                reply = '已暂停所有自动回复' if action == 'pause' else '已恢复自动回复，单独暂停的会话仍保持暂停'
            else:
                self.set_conversation_reply(details['kind'], details['target'], action, reason)
                reply = '这个会话已暂停自动回复' if action == 'pause' else '这个会话已恢复回复'
                if action == 'resume' and self.policy['mode'] == 'paused':
                    reply += '，全局还在暂停，可用 /dev resume all 恢复'
            return {'ok': True, 'reply': reply, 'route': 'owner_task'}
        call = await self.require_account(account)
        read_policy = self.policy
        if details['kind'] == 'private' and details['target'] not in read_policy['private_ids']:
            read_policy = {**read_policy, 'private_ids': [*read_policy['private_ids'], details['target']]}
        try:
            history = await asyncio.wait_for(read_history(call, read_policy,
                {'targetType': details['kind'], 'targetId': details['target'], 'limit': 10}), 5)
        except TimeoutError:
            history = {'ok': False, 'messages': []}
        linked_message, images, media_notes = await self.message_material(call, event, account, details['kind'], details['target'], history)
        identity_context = await self.identities.context(account, details['kind'], details['target'], event,
            history=history, call=call)
        if self.napcat is not napcat or account != self.policy['account_id']:
            return
        return await self.owner_control.chat(
            control_token=self.config.get("control_token", ""),
            account=account,
            sender=sender,
            kind=details["kind"],
            target=details["target"],
            message_id=details["message_id"],
            text=details["text"],
            identity_context=identity_context,
            message_context=linked_message, images=images, media_notes=media_notes,
        )

    async def _owner_control_reply(self, details: dict, event: dict,
                                   generation: int, napcat) -> None:
        account = details["account"]
        sender = details["sender"]
        receipt_key = owner_receipt_key(*details['marker'])
        group_chat = False
        group_control = False
        request_id = ""
        result_ok = False
        try:
            result = await self._owner_request(details, event, napcat)
            result_ok = isinstance(result, dict) and result.get("ok") is True
            request_id = result.get("requestId", "") if isinstance(result, dict) else ""
            group_chat = result_ok and result.get('route') == 'group_chat' and details['kind'] == 'group'
            if result_ok and result.get('route') == 'group_control' and details['kind'] == 'group':
                from .owner_delivery import control_ack_text
                public_ack = control_ack_text(result.get('controlAck'))
                group_control = bool(public_ack)
            if isinstance(result, dict) and result.get('outcome'):
                self.record_event('owner_control_outcome', {key: result[key] for key in (
                    'requestId', 'outcome', 'errorCode', 'errorStage', 'deliveryState', 'messageId') if key in result})
            self.owner_receipts.set_state(receipt_key, 'accepted' if result_ok else 'failed', request_id=request_id)
            self._record_owner_control_result(
                details, ok=result_ok,
                request_id=request_id if isinstance(request_id, str) else "",
                status="accepted" if result_ok else "rejected",
            )
            if not result_ok:
                reply = "主人指令暂时未完成，请稍后再试。"
            else:
                reply = public_ack if group_control else result.get("reply")
                if not isinstance(reply, str) or not reply.strip():
                    reply = "主人指令已收到，但暂时没有可发送的结果。"
                else:
                    # Keep the complete backend result.  QQ presentation is
                    # handled below by sentence-aware delivery bubbles; this
                    # boundary must never silently truncate a useful result.
                    reply = reply.strip()
        except asyncio.CancelledError:
            raise
        except (OwnerControlBackendFailure, OwnerControlOutcomeUnknown) as exc:
            request_id = exc.request_id
            self.record_event('owner_control_error', exc.to_public())
            self.owner_receipts.set_state(receipt_key, 'unknown' if exc.outcome in {'unknown', 'side_effect_unknown'} else 'failed',
                                          request_id=request_id)
            reply = ('指令已交给桌面，但没拿到确定结果，先别重复发送' if exc.outcome in {'unknown', 'side_effect_unknown'}
                     else '这次指令执行失败，QQ 连接没有断开')
        except OwnerControlUnavailable:
            self._record_owner_control_result(details, ok=False, status="unavailable")
            reply = "没连上 Amadeus 桌面，确认她已启动后再试"
        except Exception as exc:
            # The QQ side receives only a fixed safe failure message.  In
            # particular, never expose a token, URL, or local exception.
            self._record_owner_control_result(details, ok=False, status="processing_failed")
            self.record_event('owner_control_error', {'code': type(exc).__name__, 'stage': 'gateway_processing'})
            reply = "这次指令处理失败了，执行结果还没确认"

        # Re-run the complete identity and lease checks after the desktop call:
        # a switch or reconnect while the HTTP request was in flight must not
        # send its result through the new account.
        current = self._owner_control_details(event, check_seen=False)
        if (current is None or current["marker"] != details["marker"]
                or current["account"] != account
                or self.napcat is not napcat):
            return
        if group_chat:
            # No main-chat text enters this branch. Generate a fresh ordinary
            # reply using only this group's history and trusted owner mapping.
            if self.policy['mode'] != 'paused' and allowed(self.policy, 'group', details['target']):
                await self.amadeus_reply(event, self.generation)
            return

        # Owner-control uses the same sentence-aware QQ renderer as ordinary
        # text, with a smaller bubble ceiling so a verbose desktop result does
        # not become a long burst.  ``split_reply`` coalesces the tail and
        # preserves all text; it is not a character truncation mechanism.
        reply_kind = 'group' if group_control else 'private'
        reply_target = details['target'] if group_control else sender
        owner_policy = conversation_policy(self.policy, reply_kind, reply_target)
        message_ids: list[str] = []

        def finish_delivery(state: str, status: str) -> None:
            receipt_ids = ""
            if message_ids:
                receipt_ids = (message_ids[0] if len(message_ids) == 1 else
                               json.dumps(message_ids, ensure_ascii=True, separators=(",", ":")))
            self.owner_receipts.set_state(
                receipt_key,
                state,
                request_id=request_id,
                send_message_id=receipt_ids if receipt_ids else None,
            )
            self._record_owner_control_result(
                details,
                ok=state == "sent",
                request_id=request_id,
                status=status,
                message_ids=message_ids,
            )

        try:
            attempt_started = False
            # Configuration tools run inside the owner request. Read the
            # resulting policy now so "reply to me in voice/text" affects
            # this response as well as future ones. Ordinary reply pause and
            # allowlists do not disable the separately authenticated owner.
            request_key = 'owner-result:' + receipt_key
            selected_format = (self.ledger.select_reply_format(
                self.policy, reply_kind, reply_target, request_key) if result_ok else 'text')
            if selected_format == 'voice':
                language = self.delivery_language(reply_kind, reply_target, request_key)
                self.record_event('owner_voice_preparing', {'account': account,
                    'sender': sender, 'requestId': request_id, 'speech_language': language})
                try:
                    message = await self.synthesize_voice_message(
                        account=account, kind=reply_kind, target=reply_target,
                        reply_identity=owner_policy['reply_identity'],
                        draft_id=receipt_key[:32], text=reply, speech={}, speech_language=language)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.record_event('owner_voice_failed', {'account': account,
                        'sender': sender, 'requestId': request_id})
                    finish_delivery('failed', 'voice_failed')
                    return
                delivery_parts = [message]
            else:
                delivery_parts = _delivery_parts(
                    [{'type': 'text', 'data': {'text': reply}}],
                    owner_policy.get('reply_bubble_chars', DEFAULT_REPLY_BUBBLE_CHARS),
                    compact=owner_policy.get('compact_reply', True),
                    max_parts=MAX_OWNER_CONTROL_BUBBLES,
                )
            if result_ok and not group_control and str(result.get('galleryAccount') or '') == account:
                ids = result.get('imageIds') or []
                if isinstance(ids, list) and 1 <= len(ids) <= 2:
                    found = self._media_library(account).get(ids, reusable_only=True)
                    if found.get('ok') and not found.get('missing'):
                        delivery_parts.extend([[{'type': 'qq_image', 'data': {'assetId': item['id']}}]
                                              for item in found.get('items', [])])
            async with self.send_lock:
                for part_index, part in enumerate(delivery_parts):
                    # Re-check identity, owner permission, lifecycle and account
                    # immediately before every bubble.  A reconnect or lease
                    # change stops the remainder without moving it to a new
                    # account.
                    current = self._owner_control_details(event, check_seen=False)
                    if (current is None or current["marker"] != details["marker"]
                            or current["account"] != account
                            or self.napcat is not napcat):
                        finish_delivery("unknown" if message_ids else "failed", "delivery_stopped")
                        return

                    if selected_format == 'voice' and not voice_enabled(
                            conversation_policy(self.policy, reply_kind, reply_target)):
                        finish_delivery('failed', 'voice_disabled')
                        return
                    part_text = ''.join(
                        segment.get('data', {}).get('text', '')
                        for segment in part
                        if isinstance(segment, dict) and segment.get('type') == 'text'
                    ).strip()
                    if not part_text and selected_format != 'voice' and not any(p.get('type') == 'qq_image' for p in part):
                        continue
                    part = await self._resolve_media_message(part, account)
                    part_call = await self.require_account(account)
                    # ``require_account`` can yield while account selection or the
                    # desktop lease changes, so check again before the side effect.
                    current = self._owner_control_details(event, check_seen=False)
                    if (current is None or current["marker"] != details["marker"]
                            or current["account"] != account
                            or self.napcat is not napcat):
                        finish_delivery("unknown" if message_ids else "failed", "delivery_stopped")
                        return
                    if selected_format == 'voice' and not voice_enabled(
                            conversation_policy(self.policy, reply_kind, reply_target)):
                        finish_delivery('failed', 'voice_disabled')
                        return
                    attempt_started = True
                    result = await part_call(
                        'send_group_msg' if group_control else 'send_private_msg',
                        {'group_id' if group_control else 'user_id': int(reply_target),
                         'message': part if selected_format == 'voice' or any(
                             segment.get('type') != 'text' for segment in part) else part_text},
                    )
                    attempt_started = False
                    data = result.get('data') if isinstance(result, dict) else None
                    message_id = data.get('message_id') if isinstance(data, dict) else None
                    sent = (isinstance(result, dict) and result.get('retcode') == 0
                            and message_id is not None and str(message_id).strip())
                    if not sent:
                        # A failed/partial OneBot call has an uncertain side effect.
                        # Persist unknown and stop; the claim store prevents an
                        # automatic replay of this owner command.
                        finish_delivery("unknown", "delivery_unknown")
                        return
                    message_id = str(message_id)
                    message_ids.append(message_id)
                    with contextlib.suppress(Exception):
                        self.record_event("owner_control_bubble_sent", {
                            "account": account, "kind": details["kind"],
                            "target": details["target"], "sender": sender,
                            "message_id": details["message_id"],
                            "part_index": part_index, "send_message_id": message_id,
                        })
                finish_delivery("sent", "sent")
        except asyncio.CancelledError:
            finish_delivery("unknown" if (message_ids or attempt_started) else "failed",
                            "delivery_cancelled")
            raise
        except (ValueError, ConnectionError, TimeoutError, TypeError):
            finish_delivery("unknown" if (message_ids or attempt_started) else "failed",
                            "delivery_unknown")
            return


    def _scope_id(self, account: str, kind: str, target: str) -> str:
        return reset_scope_key(account, kind, target)

    def _scope_epoch_now(self, account: str, kind: str, target: str) -> int:
        return int(self._reset_store.get(account, kind, target).get("epoch", 0))

    def _scope_cutoff_now(self, account: str, kind: str, target: str) -> float:
        return float(self._reset_store.get(account, kind, target).get("cutoff", 0.0))

    async def _enter_scope_task(self, account: str, kind: str, target: str,
                                *, skip_when_resetting: bool = False) -> int | None:
        scope = self._scope_id(account, kind, target)
        async with self._reset_state_lock:
            if scope in self._resetting_scopes:
                if skip_when_resetting:
                    return None
                raise ValueError("该 QQ 会话正在重置，请稍候重试")
            epoch = self._scope_epoch_now(account, kind, target)
            task = asyncio.current_task()
            if task is not None:
                self._scope_tasks.setdefault(scope, set()).add(task)
            return epoch

    async def _leave_scope_task(self, account: str, kind: str, target: str) -> None:
        scope = self._scope_id(account, kind, target)
        task = asyncio.current_task()
        async with self._reset_state_lock:
            tasks = self._scope_tasks.get(scope)
            if tasks is not None and task is not None:
                tasks.discard(task)
                if not tasks:
                    self._scope_tasks.pop(scope, None)

    async def _scope_is_current(self, account: str, kind: str, target: str,
                                epoch: int) -> bool:
        scope = self._scope_id(account, kind, target)
        async with self._reset_state_lock:
            return (scope not in self._resetting_scopes
                    and self._scope_epoch_now(account, kind, target) == epoch)

    def _scope_is_current_now(self, account: str, kind: str, target: str,
                              epoch: int) -> bool:
        scope = self._scope_id(account, kind, target)
        return (scope not in self._resetting_scopes
                and self._scope_epoch_now(account, kind, target) == epoch)

    async def _automatic_history(self, account: str, kind: str, target: str,
                                 args: dict, epoch: int,
                                 *, owner_authorized: bool = False) -> dict:
        context = await self.scoped_history(args, owner_authorized=owner_authorized)
        if not self._scope_is_current_now(account, kind, target, epoch):
            raise ValueError("QQ 会话上下文已重置，本次自动回复已取消")
        cutoff = self._scope_cutoff_now(account, kind, target)
        if not cutoff or not context.get("ok"):
            return context
        messages = [item for item in context.get("messages", [])
                    if isinstance(item, dict) and isinstance(item.get("time"), (int, float))
                    and float(item["time"]) > cutoff]
        if len(messages) == len(context.get("messages", [])):
            return context
        # Automatic context is a fresh-start boundary.  Keep explicit history
        # reads untouched and do not give the model a cursor that points into
        # the pre-reset page.
        return {**context, "messages": messages, "nextCursor": None,
                "hasMore": False,
                "message": "QQ 会话已重置，只向自动回复提供重置后的新历史。"}

    def _delivery_history(self, account: str, kind: str, target: str,
                          cutoff: float) -> list[dict]:
        rows = []
        for item in self.ledger.snapshot()["sends"]:
            if item.get("account") != account or item.get("kind") != kind or item.get("target") != target:
                continue
            # ``cancel_unsent`` updates ``updated`` while closing old drafts.
            # The immutable creation time is the reset boundary, so an old
            # draft must not reappear merely because reset touched its audit
            # timestamp.
            if cutoff and float(item.get("created") or 0.0) <= cutoff:
                continue
            rows.append(item)
            if len(rows) >= 5:
                break
        return rows

    def _defer_event(self, scope: str, event: dict) -> None:
        """Queue one NapCat event until a target reset leaves its gate.

        The reader has already checked account, sender and trigger policy when
        this is called.  Message ids are deduplicated here instead of through
        the persistent incoming ledger, so a successful reset can still admit
        this event into the fresh epoch.
        """
        message_id = event.get("message_id")
        ids = self._deferred_message_ids.setdefault(scope, set())
        marker = str(message_id) if message_id is not None else None
        if marker is not None and marker in ids:
            return
        queue = self._deferred_events.setdefault(scope, [])
        if len(queue) >= 128:
            self.record_event("amadeus_busy", {
                "kind": str(event.get("message_type", "")),
                "target": scope,
                "reason": "会话重置期间待处理消息已满，本条未交给模型",
            })
            return
        queue.append(event)
        if marker is not None:
            ids.add(marker)

    def _take_deferred_events(self, scope: str) -> list[dict]:
        events = self._deferred_events.pop(scope, [])
        self._deferred_message_ids.pop(scope, None)
        return events

    def _schedule_deferred_events(self, scope: str, events: list[dict]) -> None:
        """Admit deferred events after the reset gate is closed."""
        for event in events:
            account = str(event.get("self_id", ""))
            try:
                kind, target = conversation(event)
            except (ValueError, TypeError, KeyError):
                continue
            if (account != str(self.policy.get("account_id", ""))
                    or self._scope_id(account, kind, target) != scope
                    or not allowed(self.policy, kind, target)):
                continue
            if not self.ledger.fresh_incoming(account, event.get("message_id")):
                continue
            self.record_event("received", {
                "account": account, "kind": kind, "target": target,
                "sender": str(event.get("user_id", "")),
                "message_id": event.get("message_id"),
                "text": str(event.get("raw_message", ""))[:6000],
            })
            task = self.spawn(self.amadeus_reply(event, self.generation),
                              scope=(account, kind, target))
            self.amadeus_tasks.add(task)
            task.add_done_callback(self.amadeus_tasks.discard)

    async def clear_context(self, data: dict) -> dict:
        """Reset one configured target after cancelling its in-flight work."""
        if not isinstance(data, dict) or set(data) != {
                "targetType", "targetId", "expectedAccount", "confirm"}:
            raise ValueError("上下文重置需要目标、当前账号和 confirm=true")
        account, kind, target = validate_scope(
            data.get("expectedAccount"), data.get("targetType"), data.get("targetId"))
        if account != str(self.policy.get("account_id", "")):
            raise ValueError("QQ 登录账号已改变，不能重置旧账号会话")
        if target not in self.policy.get(kind + "_ids", []):
            raise ValueError("只能重置当前已授权的 QQ 会话")
        if data.get("confirm") is not True:
            raise ValueError("上下文重置必须明确 confirm=true")

        scope = self._scope_id(account, kind, target)
        cutoff = time.time()
        async with self._reset_state_lock:
            if scope in self._resetting_scopes:
                raise ValueError("该 QQ 会话正在重置，请稍候重试")
            previous = self._reset_store.get(account, kind, target)
            epoch = int(previous.get("epoch", 0)) + 1
            self._resetting_scopes.add(scope)
            pending = [task for task in self._scope_tasks.get(scope, set())
                       if task is not asyncio.current_task() and not task.done()]
            for task in pending:
                task.cancel()
        reset_gate_failed = False
        try:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            cancelled = self.ledger.cancel_unsent(account, kind, target)
            archived = archive_profiles(self.amadeus.runtime, account, kind, target,
                                        epoch=epoch, cutoff=cutoff)
            try:
                marker = self._reset_store.save(account, kind, target, epoch=epoch,
                                                cutoff=cutoff, archived=archived)
            except Exception:
                # Moving the profiles is reversible.  Do not leave an
                # apparently fresh marker absent while the live runtime has
                # already lost the old profiles; restore the old scope before
                # allowing automatic replies again.  If restoration itself
                # fails, retain the reset gate so stale state cannot run.
                if archived:
                    try:
                        restore_profiles(self.amadeus.runtime, archived)
                    except Exception:
                        reset_gate_failed = True
                if reset_gate_failed:
                    raise RuntimeError(
                        "QQ 会话归档已完成但重置标记未保存；当前会话保持清理中，"
                        "请检查本机归档和存储后恢复归档，再重启 QQ 网关"
                    ) from None
                raise
            self.record_event("context_reset", {
                "account": account, "kind": kind, "target": target,
                "epoch": epoch, "cutoff": cutoff,
                "cancelledDrafts": cancelled, "archived": bool(archived),
            })
            return {
                "ok": True,
                "message": "已开始新的 QQ 会话；旧的 Amadeus 对话已归档，QQ 历史未删除。",
                "cleared": {"account": account, "targetType": kind, "targetId": target,
                            "epoch": marker["epoch"], "cancelledDrafts": cancelled,
                            "archived": bool(archived)},
            }
        finally:
            async with self._reset_state_lock:
                if not reset_gate_failed:
                    self._resetting_scopes.discard(scope)
                    deferred = self._take_deferred_events(scope)
                else:
                    deferred = []
            if not reset_gate_failed and deferred:
                self._schedule_deferred_events(scope, deferred)

    def revoke_tasks(self, *, exclude=None, preserve_owner=False):
        for task in list(self.tasks):
            if task is not exclude and not (preserve_owner and task in self.owner_tasks):
                task.cancel()

    def set_conversation_reply(self, kind: str, target: str, action: str, reason: str):
        if kind not in {'private', 'group'} or (target not in self.policy[kind + '_ids'] and not (
                action in {'resume', 'unmute'} and target in self.policy['muted_' + kind + '_ids'])):
            raise ValueError('只能控制已经授权的会话')
        if action not in {'pause', 'resume', 'mute', 'unmute'}:
            raise ValueError('未知会话控制动作')
        muted = action in {'pause', 'mute'}
        key = 'muted_' + kind + '_ids'
        ids = [item for item in self.policy[key] if item != target]
        if muted:
            ids.append(target)
        self.persist_policy({**self.policy, key: ids})
        # Only cancel this conversation; unrelated chats and the owner command
        # remain live. The send boundary also reads the current mute switch.
        if muted:
            pending = list(self._scope_tasks.get(self._scope_id(self.policy['account_id'], kind, target), set()))
            asyncio.get_running_loop().call_later(.1, lambda: [task.cancel() for task in pending if not task.done()])
        self.record_event('conversation_control', {'action': 'mute' if muted else 'unmute',
            'kind': kind, 'target': target, 'reason': reason[:500]})

    async def set_reply_mode(self, action: str, reason: str):
        policy = dict(self.policy)
        if action == 'pause':
            policy['mode'] = 'paused'
        elif action == 'resume' and policy['mode'] == 'paused':
            policy['mode'] = self.resume_mode
        self.persist_policy(policy)
        self.generation += 1
        self.revoke_tasks(preserve_owner=True)
        # AstrBot's external request stream is tied to a connection generation.
        # Its old responses cannot safely be resumed on a new policy revision.
        if self.reply_agent != 'amadeus' and self.napcat and not self.napcat.closed:
            self.account_verified = False
            await self.napcat.close()
        self.record_event('amadeus_control', {'action': action, 'reason': reason[:1000], 'policy': policy})

    def invalidate_account(self, *, revoke_drafts: bool = True):
        old_account = self.policy.get('account_id', '')
        self.account_verified = False
        self.generation += 1
        self.revoke_tasks(exclude=asyncio.current_task())
        self._deferred_events.clear()
        self._deferred_message_ids.clear()
        if revoke_drafts and old_account:
            self.ledger.cancel_account_drafts(old_account)
        self._contacts, self._contacts_at, self._contacts_complete = [], 0.0, False
        self.last_error = 'QQ 登录账号已改变，旧账号代管已停止；请在设置中使用新账号'
        if self.napcat and not self.napcat.closed:
            self.spawn(self.napcat.close())

    async def require_account(self, expected: str | None = None):
        expected = self.policy['account_id'] if expected is None else expected
        if not self.account_verified or expected != self.policy['account_id']:
            raise ValueError('QQ 账号尚未验证或已改变')
        generation, ws = self.generation, self.napcat
        async def scoped_call(action, params=None):
            if generation != self.generation or ws is not self.napcat or not self.account_verified:
                raise ValueError('QQ 账号或连接已改变，旧请求已取消')
            result = await (self.call(action) if params is None else self.call(action, params))
            if generation != self.generation or ws is not self.napcat or not self.account_verified:
                raise ValueError('QQ 账号或连接已改变，旧结果已丢弃')
            return result
        login = await scoped_call('get_login_info')
        if login.get('retcode') != 0 or str((login.get('data') or {}).get('user_id', '')) != expected:
            self.invalidate_account()
            raise ValueError('QQ 登录账号已改变，本次操作已取消')
        return scoped_call

    async def scoped_history(self, args: dict, *, owner_authorized: bool = False) -> dict:
        generation, policy = self.generation, self.policy
        call = await self.require_account(policy['account_id'])
        history_policy = policy
        kind = args.get('targetType') if isinstance(args, dict) else None
        target = str(args.get('targetId', '')) if isinstance(args, dict) else ''
        if (owner_authorized and kind == 'private'
                and control_allowed(policy, kind, target)):
            # Owner-control history is an authenticated explicit lookup. Keep
            # the ordinary automatic-reply allowlist unchanged and widen only
            # this copied validation policy for the configured owner target.
            history_policy = {
                **policy,
                'private_ids': list(dict.fromkeys([
                    *policy.get('private_ids', []), target]))}
        context = await read_history(call, history_policy, args)
        if generation != self.generation or not self.account_verified:
            raise ValueError('读取期间 QQ 账号或授权已改变')
        return self.annotate_history(context)

    async def call(self, action: str, params: dict | None = None):
        if self.napcat is None or self.napcat.closed:
            raise ConnectionError("NapCat 未连接")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.napcat.send_json({"action": action, "params": params or {}, "echo": echo})
            return await asyncio.wait_for(future, 15)
        finally:
            self.pending.pop(echo, None)

    async def napcat_reader(self, ws):
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
                if self.account_verified and data.get('self_id') is not None and str(data['self_id']) != self.policy['account_id']:
                    self.invalidate_account()
                    continue
                if "echo" in data:
                    future = self.pending.get(str(data["echo"]))
                    if future and not future.done():
                        future.set_result(data)
                    continue
                if not self.account_verified or str(data.get("self_id", "")) != self.policy["account_id"]:
                    continue
                if data.get("post_type") != "message":
                    continue
                # Owner control is a separate, explicit route.  It runs
                # before ordinary allowlists and paused-mode filtering, while
                # still requiring the verified account and active desktop
                # lease inside _owner_control_details.
                if self._try_owner_control_event(data):
                    continue
                if self.policy["mode"] == "paused" or not self.lifecycle_status()['active']:
                    continue
                kind, target = conversation(data)
                if not allowed(self.policy, kind, target) or sender_id(data) == self.policy["account_id"]:
                    continue
                # ``reply_trigger`` intentionally avoids materializing group
                # text.  Check the shared model-input limit here first so an
                # @-mention with an overlong body is observable instead of
                # disappearing through the broad reader exception handler.
                text_length = message_text_length(data)
                if text_length > MAX_QQ_TEXT_CHARS:
                    self.record_event("received_too_long", {
                        "kind": kind, "target": target,
                        "sender": str(data.get("user_id", "")),
                        "message_id": data.get("message_id"),
                        "length": text_length,
                        "maxLength": MAX_QQ_TEXT_CHARS,
                    })
                    continue
                scope = self._scope_id(self.policy["account_id"], kind, target)
                if self.reply_agent == "amadeus":
                    if not reply_trigger(data, self.policy["account_id"], conversation_policy(self.policy, kind, target)):
                        continue
                    if scope in self._resetting_scopes:
                        self._defer_event(scope, data)
                        continue
                    if len(self.amadeus_tasks) >= 16:
                        self.record_event("amadeus_busy", {"kind": kind, "target": target,
                            "reason": "待处理消息已满，本条未交给模型"})
                        continue
                elif not self.astrbot or self.astrbot.closed:
                    continue
                if not self.ledger.fresh_incoming(self.policy["account_id"], data.get("message_id")):
                    continue
                self.record_event("received", {"account": self.policy["account_id"], "kind": kind, "target": target,
                    "sender": str(data.get("user_id", "")), "message_id": data.get("message_id"),
                    "text": str(data.get("raw_message", ""))[:6000]})
                if self.reply_agent == "amadeus":
                    task = self.spawn(self.amadeus_reply(data, self.generation),
                                      scope=(self.policy["account_id"], kind, target))
                    self.amadeus_tasks.add(task)
                    task.add_done_callback(self.amadeus_tasks.discard)
                else:
                    await self.astrbot.send_json(data)
            except (ValueError, TypeError):
                continue
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("NapCat 连接中断"))

    def delivery_language(self, kind: str, target: str, request_key: str, override=None) -> str:
        from backend.chat.presentation import speech_language_profile
        try:
            model = json.loads((self.config_path.parent / 'amadeus-model.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            model = {}
        return self.ledger.select_speech_language(self.policy, kind, target, request_key,
            desktop_language=speech_language_profile(model)['speechLanguage'], override=override)

    async def synthesize_voice_message(self, *, account: str, kind: str, target: str,
                                       reply_identity: str, draft_id: str, text: str,
                                       speech: dict, speech_language: str) -> list[dict]:
        """Use the same complete-audio adapter for owner and contact replies."""
        from .voice import audio_path
        from .dialogue import profile_path
        from .amadeus_runtime import scope_key
        style = conversation_policy(self.policy, kind, target).get('reply_style', 'game')
        audio = await self.amadeus.synthesize(account=account, kind=kind, target=target,
            reply_identity=reply_identity, draft_id=draft_id, text=text,
            speech=speech, speech_language=speech_language, reply_style=style)
        expected = audio_path(profile_path(self.amadeus.runtime,
            scope_key(account, kind, target), reply_identity, style), draft_id).resolve()
        if not audio.get('ok') or Path(audio.get('audio_path', '')).resolve() != expected or not expected.is_file():
            raise ValueError('没有取得当前回复的语音文件')
        return [{'type': 'record', 'data': {'file': expected.as_uri()}}]

    async def amadeus_reply(self, event: dict, generation: int):
        account = str(event["self_id"])
        kind, target = conversation(event)
        scope = self._scope_id(account, kind, target)
        epoch = await self._enter_scope_task(account, kind, target,
                                             skip_when_resetting=True)
        if epoch is None:
            self._defer_event(scope, event)
            return
        try:
            await self._amadeus_reply(event, generation, epoch)
        finally:
            await self._leave_scope_task(account, kind, target)

    async def _amadeus_reply(self, event: dict, generation: int, epoch: int):
        account = str(event["self_id"])
        kind, target = conversation(event)
        try:
            if (generation != self.generation or not self._scope_is_current_now(account, kind, target, epoch)
                    or self.policy["mode"] == "paused" or not allowed(self.policy, kind, target)
                    or not self.lifecycle_status()['active']):
                return
            call = await self.require_account(account)
            trigger = None
            if kind == 'group':
                # Never await a OneBot response in napcat_reader: it must keep
                # consuming the same socket to complete get_msg's future.
                trigger = await resolve_group_trigger(call, event, account, conversation_policy(self.policy, kind, target))
                if (not trigger or generation != self.generation
                        or not self._scope_is_current_now(account, kind, target, epoch)
                        or not self.lifecycle_status()['active']):
                    return
            trigger_label = {'mention': '@ 我', 'keyword': '命中关键词', 'reply': '引用回复我的消息', 'all': '任何新消息'}.get((trigger or {}).get('matchedBy'), '')
            self.record_event("amadeus_thinking", {"account": account, "kind": kind, "target": target,
                "message_id": event.get("message_id"), "sender": str(event.get('user_id', '')),
                "trigger": trigger,
                "reason": f"群聊触发：{trigger_label}，读取本群上下文后组织回复" if kind == 'group' else "在授权范围内，使用此 QQ 会话独立的历史和记忆生成回复"})
            if kind == 'group':
                await asyncio.sleep(self.group_context_settle_seconds)
                if (generation != self.generation
                        or not self._scope_is_current_now(account, kind, target, epoch)
                        or self.policy['mode'] == 'paused' or not allowed(self.policy, kind, target)
                        or not self.lifecycle_status()['active']):
                    return
            cutoff = self._scope_cutoff_now(account, kind, target)
            history = [{"state": row["state"], "message_id": row["message_id"],
                        "text": "".join(x.get("data", {}).get("text", "") for x in row["message"]
                                          if isinstance(x, dict))[:1500]}
                       for row in self.ledger.snapshot()["sends"]
                       if row["account"] == account and row["kind"] == kind and row["target"] == target
                       and (not cutoff or float(row.get("created") or 0.0) > cutoff)][:5]
            try:
                reply_policy = conversation_policy(self.policy, kind, target)
                history_limit = reply_policy['history_messages'] or (10 if kind == 'group' else 0)
                context = await asyncio.wait_for(self._automatic_history(
                    account, kind, target,
                    {"targetType": kind, "targetId": target, "limit": history_limit}, epoch),
                    timeout=5) if history_limit else {'ok': True, 'messages': []}
            except (TimeoutError, ConnectionError):
                context = {"ok": False, "message": "本次未取得 QQ 历史，只能依据当前来信和已保存的工作记录。"}
            if kind == 'group':
                self.record_event('history_read', {'account': account, 'kind': kind, 'target': target,
                    'message_id': event.get('message_id'), 'source': (trigger or {}).get('type'), 'trigger': trigger, 'ok': bool(context.get('ok')),
                    'count': len(context.get('messages', [])), 'reason': f'{trigger_label}后主动读取当前群聊上下文'})
            linked_message, images, media_notes = await self.message_material(call, event, account, kind, target, context)
            if (generation != self.generation or not self.account_verified
                    or not self._scope_is_current_now(account, kind, target, epoch)):
                return
            if image_segments(event):
                self.record_event('image_read', {'account': account, 'kind': kind, 'target': target,
                    'message_id': event.get('message_id'), 'imageCount': len(images), 'notes': media_notes, 'source': 'incoming'})
            reply_policy = conversation_policy(self.policy, kind, target)
            request_key = ('amadeus-auto:e' + str(epoch) + ':'
                           + reply_request_id(account, kind, target, event['message_id']))
            selected_format = self.ledger.select_reply_format(self.policy, kind, target, request_key)
            speech_language = self.delivery_language(kind, target, request_key)
            identity_context = await self.identities.context(account, kind, target, event, context, call)
            if generation != self.generation or not self._scope_is_current_now(account, kind, target, epoch):
                return
            try:
                identity_scope = self.identities.scope(
                    account, kind, target, event_sender_id(event), identity_context,
                    event=event)
            except TypeError as error:
                # Keep this checkout compatible with the pre-KB identity
                # helper while the event-aware signature rolls through the
                # parent integration.  Never hide unrelated TypeErrors.
                if "unexpected keyword argument 'event'" not in str(error):
                    raise
                identity_scope = self.identities.scope(
                    account, kind, target, event_sender_id(event), identity_context)
            result = await self.amadeus.reply(account=account, kind=kind, target=target,
                                              event=event, delivery_history=history, conversation_context=context,
                                              reply_instructions=reply_policy.get("reply_instructions", ""),
                                              reply_style=reply_policy.get('reply_style', 'game'),
                                               owner_ids=reply_policy.get('owner_ids', []),
                                               account_role=reply_policy['account_role'], identity_context=identity_context,
                                               message_context=linked_message,
                                                identity_scope=identity_scope,
                                              images=images, media_notes=media_notes, compact_reply=reply_policy['compact_reply'],
                                              reply_identity=reply_policy['reply_identity'], reply_format=selected_format,
                                              speech_language=speech_language,
                                              trigger=trigger,
                                              owner_tasks=self.ledger.owner_briefs(account, kind, target,
                                                                                   since=cutoff))
            # A completed model call never grants permission to use a new policy/connection.
            if (generation != self.generation or not self._scope_is_current_now(account, kind, target, epoch)
                    or self.reply_agent != "amadeus" or self.policy["mode"] == "paused"
                    or not allowed(self.policy, kind, target)):
                self.record_event("amadeus_discarded", {"account": account, "kind": kind, "target": target,
                    "reason": "生成期间连接或授权已改变，没有发送"})
                return
            if result.get('muted'):
                return
            if not result.get("reply") and not result.get("imageIds"):
                return
            _, _, segments = outgoing("send_msg", {"message_type": kind,
                "group_id" if kind == "group" else "user_id": target,
                "message": format_reply(result.get("reply") or "", reply_policy['compact_reply']),
                "imageIds": result.get("imageIds", [])})
            draft = self.ledger.draft(self.policy, kind, target, segments, actor="Amadeus",
                reason=result["reason"], request_key=request_key, speech=result.get("speech"))
            if reply_policy["mode"] == "auto":
                await self.send_draft(draft["id"], generation=generation, _scope_epoch=epoch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.record_event("amadeus_error", {"account": account, "kind": kind, "target": target,
                "error": type(exc).__name__, "reason": "本条未确认发送，请检查模型配置和该会话的本机日志"})

    async def delegate(self, data: dict, *, owner_authorized: bool = False) -> dict:
        kind = data.get('targetType') if isinstance(data, dict) else None
        target = str(data.get('targetId', '')) if isinstance(data, dict) else ''
        account = str(self.policy.get('account_id', ''))
        scope = None
        epoch = None
        if kind in {'private', 'group'} and target.isascii() and target.isdigit() and account.isascii() and account.isdigit():
            scope = self._scope_id(account, kind, target)
            epoch = await self._enter_scope_task(account, kind, target,
                                                 skip_when_resetting=False)
        try:
            return await self._delegate(data, scope, epoch,
                                        owner_authorized=owner_authorized)
        finally:
            if scope is not None:
                await self._leave_scope_task(account, kind, target)

    async def _delegate(self, data: dict, scope: str | None, epoch: int | None,
                        *, owner_authorized: bool = False) -> dict:
        """Owner task -> this target's history/worker -> normal draft/send gate."""
        async with self.delegate_lock:
            if data.get('expected_policy') != self.policy:
                raise ValueError('QQ 配置已变化，请重新读取状态')
            kind, target = data.get('targetType'), str(data.get('targetId', ''))
            target_allowed = (control_allowed if owner_authorized else allowed)(
                self.policy, kind, target)
            if kind not in {'private', 'group'} or not target_allowed:
                raise ValueError('目标不在允许会话中')
            if self.policy['mode'] == 'paused' or not self.lifecycle_status()['active']:
                raise ValueError('QQ 代管已暂停，需要主人恢复后才能执行委托')
            if self.reply_agent != 'amadeus' or not self.account_verified:
                raise ValueError('请先连接并启用 Amadeus QQ 代管')
            instruction = data.get('instruction')
            request_id, reason = data.get('request_id'), data.get('reason')
            if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 4000:
                raise ValueError('委托内容必须为 1–4000 字')
            if not isinstance(request_id, str) or not request_id or not isinstance(reason, str) or not reason.strip():
                raise ValueError('需要本轮请求标识和委托原因')
            if 'speechLanguage' in data and (not isinstance(data['speechLanguage'], str) or data['speechLanguage'] not in {'zh-CN', 'ja-JP'}):
                raise ValueError('本次语音语言必须为 zh-CN / ja-JP')
            generation, account = self.generation, self.policy['account_id']
            if scope is None:
                scope = self._scope_id(account, kind, target)
                epoch = await self._enter_scope_task(account, kind, target,
                                                     skip_when_resetting=False)
            if epoch is None or not self._scope_is_current_now(account, kind, target, epoch):
                raise ValueError('该 QQ 会话正在重置，本次委托已取消')
            await self.require_account(account)
            # One owner turn has one delegated task per destination. Rewording
            # an instruction on a tool retry must not create another message.
            digest = hashlib.sha256(json.dumps([request_id, self.policy['account_id'], kind, target], ensure_ascii=False).encode()).hexdigest()
            request_key = 'amadeus-delegate:e' + str(epoch) + ':' + digest
            previous = self.ledger.by_request(request_key)
            if previous:
                self.delivery_language(kind, target, request_key, data.get('speechLanguage'))
                metadata = self.ledger.delegation(request_key)
                if not metadata or metadata['instruction'] != instruction:
                    raise ValueError('本轮已经委托过该联系人，不能改写任务后重复发送；请读取原回执')
                if previous['state'] == 'draft' and metadata['policy_revision'] != policy_revision(self.policy):
                    raise ValueError('原委托已生成草稿，但回复设置已变化；请检查原草稿，或在新一轮重新委托')
                return {'draft': previous, 'explanation': previous['reason'], 'replayed': True,
                        'replyIdentity': metadata['reply_identity']}
            reply_policy = conversation_policy(self.policy, kind, target)
            generation, identity = self.generation, reply_policy['reply_identity']
            try:
                context = await asyncio.wait_for(self._automatic_history(
                    account, kind, target,
                    {'targetType': kind, 'targetId': target,
                     'limit': reply_policy['history_messages'] or 10}, epoch,
                    owner_authorized=owner_authorized), timeout=10)
            except TimeoutError:
                raise ValueError('读取目标会话超时，委托未发送') from None
            if not context.get('ok'):
                raise ValueError('未取得目标会话上下文，委托未发送；请稍后重试')
            if (generation != self.generation or not self.account_verified
                    or account != self.policy['account_id']
                    or not self._scope_is_current_now(account, kind, target, epoch)):
                raise ValueError('读取期间账号或授权已变化，委托未发送')
            self.record_event('owner_task', {'kind': kind, 'target': target, 'request_id': digest,
                'historyCount': len(context.get('messages', [])), 'replyIdentity': identity,
                'reason': reason[:1000]})
            cutoff = self._scope_cutoff_now(account, kind, target)
            delivery_history = self._delivery_history(account, kind, target, cutoff)
            selected_format = self.ledger.select_reply_format(self.policy, kind, target, request_key)
            speech_language = self.delivery_language(kind, target, request_key, data.get('speechLanguage'))
            identity_context = await self.identities.context(account, kind, target, history=context)
            if generation != self.generation or not self._scope_is_current_now(account, kind, target, epoch):
                raise ValueError('读取身份期间账号或授权已改变，委托未发送')
            result = await self.amadeus.reply(account=self.policy['account_id'], kind=kind, target=target,
                event={'message_id': 'owner-task:' + digest, 'user_id': target},
                conversation_context=context, owner_instruction=instruction,
                delivery_history=delivery_history,
                owner_tasks=self.ledger.owner_briefs(self.policy['account_id'], kind, target,
                                                     since=cutoff),
                reply_instructions=reply_policy['reply_instructions'], reply_identity=identity,
                reply_style=reply_policy.get('reply_style', 'game'),
                owner_ids=reply_policy.get('owner_ids', []),
                account_role=reply_policy['account_role'], identity_context=identity_context,
                identity_scope=self.identities.scope(account, kind, target, '', identity_context),
                compact_reply=reply_policy['compact_reply'], reply_format=selected_format, speech_language=speech_language)
            if (generation != self.generation or not self.lifecycle_status()['active']
                    or not self._scope_is_current_now(account, kind, target, epoch)):
                raise ValueError('生成期间授权或连接已变化，委托未发送')
            if result.get('muted'):
                raise ValueError('该会话已停止或没有生成可发送的正文')
            if not result.get('reply') and not result.get('imageIds'):
                raise ValueError('该会话已停止或没有生成可发送的正文')
            _, _, segments = outgoing('send_msg', {'message_type': kind,
                'group_id' if kind == 'group' else 'user_id': target,
                'message': format_reply(result.get('reply') or '', reply_policy['compact_reply']),
                'imageIds': result.get('imageIds', [])})
            row = self.ledger.draft(self.policy, kind, target, segments, actor='Amadeus', reason=reason,
                                    request_key=request_key, speech=result.get("speech"),
                                    creation_request_id=request_id)
            self.ledger.save_delegation(request_key, instruction, self.policy, reply_identity=identity)
            if reply_policy['mode'] == 'auto':
                await self.send_draft(row['id'], generation=generation, idempotent=True,
                                       _scope_epoch=epoch,
                                       owner_authorized=owner_authorized)
                row = self.ledger.get(row['id'])
            return {'draft': row, 'explanation': reason, 'historyCount': len(context.get('messages', [])),
                    'replyIdentity': identity, 'requiresApproval': row['state'] == 'draft'}

    def _media_library(self, account: str):
        try:
            from .media_library import MediaLibrary
        except ImportError:
            raise ValueError('QQ 图库组件尚未加载，请重启 QQ 后台') from None
        return MediaLibrary(self.config_path.parent, account)

    @staticmethod
    async def _media_call(library, name: str, *args, **kwargs):
        value = getattr(library, name)(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value

    async def _resolve_media_message(self, message: list[dict], account: str) -> list[dict]:
        ids = [str(part.get('data', {}).get('assetId', '')) for part in message
               if isinstance(part, dict) and part.get('type') == 'qq_image']
        if not ids:
            return message
        if len(ids) > 2 or any(not re.fullmatch(r'[0-9a-fA-F]{64}', asset_id) for asset_id in ids):
            raise ValueError('QQ 图片附件标识无效')
        segments = await self._media_call(self._media_library(account), 'segments', ids)
        if not isinstance(segments, list) or len(segments) != len(ids):
            raise ValueError('QQ 图片附件不存在或已被移除')
        if any(not isinstance(segment, dict) or segment.get('type') != 'image'
               or not isinstance(segment.get('data'), dict)
               or not isinstance(segment['data'].get('file'), str)
               or not segment['data']['file'] for segment in segments):
            raise ValueError('QQ 图库返回了无效图片')
        resolved = []
        index = 0
        for part in message:
            if isinstance(part, dict) and part.get('type') == 'qq_image':
                resolved.append(segments[index])
                index += 1
            else:
                resolved.append(part)
        return resolved

    async def send_draft(self, ident: str, *, approved: bool = False, generation: int | None = None,
                         idempotent: bool = False, _scope_epoch: int | None = None,
                         owner_authorized: bool = False) -> dict:
        previous = self.ledger.get(ident)
        account, kind, target = previous['account'], previous['kind'], previous['target']
        epoch = await self._enter_scope_task(account, kind, target, skip_when_resetting=False)
        if epoch is None:
            raise ValueError('该 QQ 会话正在重置，请稍候重试')
        if _scope_epoch is not None and epoch != _scope_epoch:
            await self._leave_scope_task(account, kind, target)
            raise ValueError('该 QQ 会话上下文已重置，旧草稿不能发送')
        generation = self.generation if generation is None else generation
        try:
            async with self.send_lock:
                previous = self.ledger.get(ident)
                if (previous['account'] != self.policy['account_id'] or generation != self.generation
                        or not self._scope_is_current_now(account, kind, target, epoch)):
                    raise ValueError('账号、授权或 QQ 会话上下文已改变，不能使用旧账号的草稿或回执')
                if idempotent and previous["state"] != "draft":
                    sent = previous["state"] == "sent" and bool(previous["message_id"])
                    return {"status": "ok" if sent else "failed", "retcode": 0 if sent else 1403,
                            "data": {"message_id": previous["message_id"]}, "replayed": True}
                send_policy = conversation_policy(self.policy, previous['kind'], previous['target'])
                if not approved and send_policy["mode"] == "auto":
                    content = ''.join(part.get('data', {}).get('text', '') for part in json.loads(previous['message']))
                    delay = reply_delay(send_policy, content)
                    if delay:
                        wait = random.uniform(delay * 0.7, delay * 1.3)
                        if send_policy.get('reply_delay_mode') == 'length':
                            wait = min(30, wait)
                        await asyncio.sleep(wait)
                if (generation is not None and generation != self.generation
                        or not self._scope_is_current_now(account, kind, target, epoch)):
                    raise ValueError("连接或授权已变更，旧动作已取消")
                if not self.lifecycle_status()['active']:
                    raise ValueError('Amadeus 已退出且没有后台接管授权，不能发送')
                if not self.account_verified:
                    raise ValueError("QQ 账号尚未验证，不能发送")
                await self.require_account(previous['account'])
                self.ledger.validate_send(ident, self.policy, approved,
                                          owner_authorized=owner_authorized)
                message = json.loads(previous['message'])
                images = [part for part in message if part.get('type') == 'qq_image']
                mixed_voice = previous.get('reply_format') == 'voice' and bool(images)
                saved_parts = self.ledger.send_parts(ident) if mixed_voice else []
                if previous.get('reply_format') == 'voice' and not saved_parts:
                    content = ''.join(part.get('data', {}).get('text', '') for part in message)
                    speech_language = self.delivery_language(previous['kind'], previous['target'], previous['request_key'])
                    self.record_event('voice_preparing', {'draft_id': ident, 'account': previous['account'],
                        'kind': previous['kind'], 'target': previous['target'], 'reason': '按已开启的语音模式合成回复'})
                    try:
                        message = await self.synthesize_voice_message(account=previous['account'], kind=previous['kind'],
                            target=previous['target'], reply_identity=send_policy['reply_identity'], draft_id=ident,
                            text=content, speech=json.loads(previous['speech_json']), speech_language=speech_language)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.record_event('voice_failed', {'draft_id': ident, 'account': previous['account'],
                            'kind': previous['kind'], 'target': previous['target'], 'reason': '语音合成失败，未发送，也未改发文字'})
                        raise ValueError('语音合成失败，未发送；请检查 Amadeus 语音设置和该会话日志') from None
                    if (generation != self.generation or not self.lifecycle_status()['active']
                            or not self.account_verified
                            or not self._scope_is_current_now(account, kind, target, epoch)):
                        raise ValueError('语音合成期间账号、授权或连接已改变，未发送')
                    await self.require_account(previous['account'])
                if not self._scope_is_current_now(account, kind, target, epoch):
                    raise ValueError('该 QQ 会话上下文已重置，旧草稿不能发送')
                # Text is split only after the final reply form is selected;
                # voice remains one audio payload.  The text plan is frozen in
                # the ledger before the first side effect so retries cannot
                # move boundaries or resend an already delivered part.
                if mixed_voice:
                    if not saved_parts:
                        self.ledger.ensure_send_parts(ident, [message, *[[part] for part in images]])
                    delivery_parts = self.ledger.send_parts(ident)
                elif previous.get('reply_format') == 'voice':
                    delivery_parts = None
                else:
                    candidate_parts = _delivery_parts(
                        message, int(send_policy.get('reply_bubble_chars', 60)),
                        compact=send_policy.get('compact_reply', True))
                    self.ledger.ensure_send_parts(ident, candidate_parts)
                    delivery_parts = self.ledger.send_parts(ident)
                draft = self.ledger.claim(ident, self.policy, approved,
                                          owner_authorized=owner_authorized)
                # Commit the explanation BEFORE invoking the side effect.
                self.record_event("sending", {"draft_id": ident, "account": draft["account"],
                    "kind": draft["kind"], "target": draft["target"], "actor": draft["actor"],
                    "reason": draft["reason"], "approved": approved})
                key = "group_id" if draft["kind"] == "group" else "user_id"
                if delivery_parts is None:
                    delivery_parts = [{"part_index": 0, "message": message, "state": "pending"}]
                part_results = []
                is_split = previous.get('reply_format') != 'voice' or mixed_voice
                attempt_started = False
                sent_any = False
                try:
                    for part in delivery_parts:
                        if part.get('state') == 'sent':
                            sent_any = True
                            continue
                        part_index = int(part['part_index'])
                        # Every bubble gets its own delay and fresh account,
                        # lifecycle, scope and quota checks.  The conversation
                        # cooldown is intentionally not reused between parts;
                        # the per-bubble reply delay is the pacing mechanism.
                        current_policy = conversation_policy(self.policy, kind, target)
                        if sent_any:
                            text = ''.join(segment.get('data', {}).get('text', '')
                                           for segment in part.get('message', [])
                                           if isinstance(segment, dict)
                                           and segment.get('type') == 'text')
                            delay = reply_delay(current_policy, text)
                            if delay:
                                await asyncio.sleep(delay)
                            current_policy = conversation_policy(self.policy, kind, target)
                        if (generation != self.generation
                                or self.policy.get('account_id') != account
                                or not self._scope_is_current_now(account, kind, target, epoch)):
                            raise _DeliveryStopped('连接、账号或 QQ 会话上下文已改变，旧回复已停止',
                                                   part_index=part_index)
                        if current_policy['mode'] == 'paused':
                            raise _DeliveryStopped('QQ 代管已暂停，剩余回复已保留为草稿',
                                                   part_index=part_index)
                        if not self.lifecycle_status()['active']:
                            raise _DeliveryStopped('Amadeus 已退出且没有后台接管授权，剩余回复已保留为草稿',
                                                   part_index=part_index)
                        if not self.account_verified:
                            raise _DeliveryStopped('QQ 账号尚未验证，剩余回复已保留为草稿',
                                                   part_index=part_index)
                        try:
                            if is_split:
                                self.ledger.validate_part_quota(ident, self.policy)
                            part_call = await self.require_account(account)
                            resolved_message = await self._resolve_media_message(
                                part['message'], account)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            raise _DeliveryStopped(str(exc) or 'QQ 回复发送前检查未通过',
                                                   part_index=part_index) from None
                        if (generation != self.generation
                                or self.policy.get('account_id') != account
                                or not self.account_verified
                                or not self._scope_is_current_now(account, kind, target, epoch)):
                            raise _DeliveryStopped('发送前账号、授权或 QQ 会话上下文已改变，剩余回复已保留为草稿',
                                                   part_index=part_index)
                        if is_split:
                            try:
                                self.ledger.claim_send_part(ident, part_index)
                            except Exception as exc:
                                raise _DeliveryStopped(str(exc) or 'QQ 分条回执不可发送',
                                                       part_index=part_index) from None
                        # Set this immediately before the side effect.  If the
                        # task is cancelled earlier, the claimed part is safe
                        # to retry; after this point the result is unknown.
                        attempt_started = True
                        result = await part_call("send_"+draft["kind"]+"_msg", {
                            key: int(draft["target"]), "message": resolved_message})
                        attempt_started = False
                        msg_id = (result.get("data") or {}).get("message_id")
                        if result.get("status") == "ok" and result.get("retcode") == 0 and msg_id is not None:
                            if is_split:
                                self.ledger.finish_send_part(ident, part_index, 'sent', str(msg_id))
                            part_results.append({"index": part_index, "result": result})
                            sent_any = True
                            continue
                        if result.get("status") == "failed":
                            if is_split:
                                self.ledger.finish_send_part(ident, part_index, 'failed', detail="QQ 接口明确返回失败")
                            self.ledger.finish(ident, "draft" if len(delivery_parts) > 1 else "failed",
                                detail="第 {} 条回复发送失败，可重试；已成功的部分不会重发".format(part_index + 1))
                            return self._split_result(result, ident, part_results)
                        if is_split:
                            self.ledger.finish_send_part(ident, part_index, 'unknown', detail="没有获得可确认的消息回执，请在 QQ 核对")
                        self.ledger.finish(ident, "unknown", detail="没有获得可确认的消息回执，请在 QQ 核对")
                        return self._split_result(result, ident, part_results)
                    message_ids = [
                        str(item['message_id']) for item in self.ledger.send_parts(ident)
                        if item.get('state') == 'sent' and item.get('message_id') is not None
                    ] if is_split else [
                        str(item['result'].get('data', {}).get('message_id'))
                        for item in part_results if item['result'].get('data', {}).get('message_id') is not None
                    ]
                    last_id = message_ids[-1] if message_ids else ""
                    self.ledger.finish(ident, "sent", last_id)
                    result = part_results[-1]['result'] if part_results else {
                        "status": "ok", "retcode": 0, "data": {"message_id": last_id}}
                    return self._split_result(result, ident, part_results)
                except _DeliveryStopped as exc:
                    if is_split and exc.part_index is not None:
                        current = next((item for item in self.ledger.send_parts(ident)
                                        if int(item['part_index']) == exc.part_index
                                        and item.get('state') == 'sending'), None)
                        if current is not None:
                            self.ledger.finish_send_part(ident, exc.part_index, 'failed',
                                                         detail=exc.detail)
                    self.ledger.finish(ident, 'draft', detail=exc.detail)
                    return self._split_result(
                        {'status': 'failed', 'retcode': 1403,
                         'message': exc.detail, 'data': None}, ident, part_results)
                except asyncio.CancelledError:
                    if is_split:
                        current_parts = self.ledger.send_parts(ident)
                        current = next((item for item in current_parts if item.get('state') == 'sending'), None)
                        if current is not None:
                            state = 'unknown' if attempt_started else 'failed'
                            detail = ("发送中断，请在 QQ 核对；不会自动重试"
                                      if attempt_started else "发送前已中止，可在恢复 QQ 后重试")
                            self.ledger.finish_send_part(ident, int(current['part_index']), state, detail=detail)
                    self.ledger.finish(ident, "unknown" if attempt_started else "draft",
                                       detail=("发送中断，请在 QQ 核对；不会自动重试"
                                               if attempt_started else "发送前已中止，可在恢复 QQ 后重试"))
                    raise
                except Exception:
                    if is_split:
                        current_parts = self.ledger.send_parts(ident)
                        current = next((item for item in current_parts if item.get('state') == 'sending'), None)
                        if current is not None:
                            self.ledger.finish_send_part(ident, int(current['part_index']), 'unknown', detail="连接中断或超时，请在 QQ 核对；不会自动重试")
                    self.ledger.finish(ident, "unknown", detail="连接中断或超时，请在 QQ 核对；不会自动重试")
                    return {"status": "failed", "retcode": 1504, "message": "发送结果未知，不会重试", "data": None}
        finally:
            await self._leave_scope_task(account, kind, target)

    def _split_result(self, result: dict, ident: str, part_results: list[dict]) -> dict:
        """Attach safe per-bubble receipts without changing one-message replies."""
        parts = self.ledger.send_parts(ident)
        if len(parts) <= 1:
            return result
        data = dict(result.get('data') or {})
        data['parts'] = [{
            'index': int(part['part_index']),
            'state': part['state'],
            'message_id': part.get('message_id'),
            'detail': part.get('detail', ''),
        } for part in parts]
        return {**result, 'data': data}

    async def astrbot_action(self, action: dict, generation: int | None = None, source_ws=None):
        generation = self.generation if generation is None else generation
        source_ws = self.astrbot if source_ws is None else source_ws
        echo = action.get("echo")
        try:
            if generation != self.generation or source_ws is not self.astrbot:
                raise ValueError("旧连接的动作已取消")
            name, params = action.get("action", ""), action.get("params", {})
            if name in {"get_login_info", "get_status", "get_version_info"}:
                result = await self.call(name)
            else:
                kind, target, segments = outgoing(name, params)
                if self.policy["mode"] == "paused" or not allowed(self.policy, kind, target):
                    raise ValueError("QQ 代管已暂停或目标不在授权范围")
                draft = self.ledger.draft(self.policy, kind, target, segments, actor="AstrBot",
                    reason="独立智能体根据授权会话生成回复", request_key="astrbot:"+uuid.uuid4().hex)
                if conversation_policy(self.policy, kind, target)["mode"] == "auto":
                    result = await self.send_draft(draft["id"], generation=generation)
                else:
                    result = {"status": "failed", "retcode": 1403, "message": "草稿已保存，等待主人确认", "data": {"draft_id": draft["id"]}}
        except (ValueError, ConnectionError, TimeoutError, TypeError):
            result = {"status": "failed", "retcode": 1403, "message": "权限、频率或连接检查未通过，请查看代管面板", "data": None}
        result["echo"] = echo
        if source_ws and not source_ws.closed:
            await source_ws.send_json(result)

    async def bridge(self):
        async with ClientSession(timeout=ClientTimeout(total=20)) as session:
            while True:
                reader = None
                try:
                    if not self.policy["account_id"] or not self.config.get("napcat_token"):
                        self.last_error = "请填写 QQ 账号和 NapCat OneBot Token"
                        await asyncio.sleep(3)
                        continue
                    async with session.ws_connect(self.config["napcat_ws"], headers={"Authorization": "Bearer "+self.config["napcat_token"]}, heartbeat=30) as napcat:
                        self.generation += 1
                        generation = self.generation
                        self.napcat = napcat
                        reader = asyncio.create_task(self.napcat_reader(napcat))
                        login = await self.call("get_login_info")
                        actual = str((login.get("data") or {}).get("user_id", ""))
                        if login.get("retcode") != 0 or actual != self.policy["account_id"]:
                            raise ValueError("登录账号与配置不符，已阻止代管")
                        self.account_verified = True
                        self._contacts_at, self._contacts_complete = 0.0, False
                        if self.reply_agent == "amadeus":
                            self.last_error = "" if self.amadeus.configured() else "Amadeus 模型尚未配置"
                            await reader
                            raise ConnectionError("NapCat 连接结束")
                        headers = {"Authorization": "Bearer "+self.config["astrbot_token"], "X-Self-ID": actual, "X-Client-Role": "Universal"}
                        async with session.ws_connect(self.config["astrbot_ws"], headers=headers, heartbeat=30) as astrbot:
                            self.astrbot = astrbot
                            self.last_error = ""
                            await astrbot.send_json({"time": int(__import__('time').time()), "self_id": int(actual), "post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect"})
                            async def receive_actions():
                                async for msg in astrbot:
                                    if msg.type == WSMsgType.TEXT:
                                        try:
                                            data = json.loads(msg.data)
                                            if isinstance(data, dict) and "action" in data:
                                                self.spawn(self.astrbot_action(data, generation, astrbot))
                                        except ValueError:
                                            pass
                            action_reader = asyncio.create_task(receive_actions())
                            try:
                                await asyncio.wait([reader, action_reader], return_when=asyncio.FIRST_COMPLETED)
                            finally:
                                action_reader.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await action_reader
                except asyncio.CancelledError:
                    raise
                except ValueError as exc:
                    self.last_error = str(exc)
                except Exception as exc:
                    # URLs/exceptions may contain tokens. Report type only.
                    self.last_error = "连接未就绪："+type(exc).__name__
                finally:
                    self.account_verified = False
                    self.revoke_tasks()
                    self.napcat = self.astrbot = None
                    if reader:
                        reader.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await reader
                await asyncio.sleep(3)

    @staticmethod
    def _media_metadata(value):
        """Remove pixel payloads from every media API response."""
        if isinstance(value, dict):
            return {key: Gateway._media_metadata(item) for key, item in value.items()
                    if str(key).lower() not in {'base64', 'database64', 'data_base64'}}
        if isinstance(value, list):
            return [Gateway._media_metadata(item) for item in value]
        return value

    @staticmethod
    def _media_ids(value, *, name='imageIds'):
        if not isinstance(value, list) or not 1 <= len(value) <= 20:
            raise ValueError(f'{name} 需要 1–20 个')
        if any(not isinstance(asset_id, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', asset_id)
               for asset_id in value):
            raise ValueError('QQ 图库标识无效')
        return list(dict.fromkeys(value))

    async def media_action(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise ValueError('QQ 图库请求格式不正确')
        expected = data.get('expectedAccount')
        account = str(self.policy.get('account_id', ''))
        if (not isinstance(expected, str) or expected != account
                or not re.fullmatch(r'[0-9]{1,20}', account)):
            raise ValueError('QQ 账号已改变，请刷新图库后重试')
        action = data.get('action')
        library = self._media_library(account)
        if action == 'search':
            query = data.get('query', data.get('q', ''))
            limit = data.get('limit', 20)
            if not isinstance(query, str) or len(query) > 200:
                raise ValueError('图库搜索文字不能超过 200 字')
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
                raise ValueError('图库搜索数量必须为 1–20')
            result = await self._media_call(library, 'search', query, limit)
        elif action == 'get':
            ids = data.get('imageIds', data.get('assetIds'))
            result = await self._media_call(library, 'get', self._media_ids(ids), reusable_only=True)
        elif action == 'delete':
            ids = data.get('imageIds', data.get('assetIds'))
            result = await self._media_call(library, 'delete', self._media_ids(ids))
        elif action == 'import':
            items = data.get('items')
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                raise ValueError('一次请导入 1–20 张图片')
            safe_items = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError('图库导入项格式不正确')
                name, encoded, kind = item.get('name'), item.get('base64'), item.get('kind')
                if not isinstance(name, str) or not name.strip() or len(name) > 200:
                    raise ValueError('图库名称不能为空且不能超过 200 字')
                if (not isinstance(encoded, str) or not encoded
                        or len(encoded) > _MEDIA_MAX_BASE64_LENGTH):
                    raise ValueError('图库图片数据无效或超过 10 MiB')
                if kind not in {'image', 'meme'}:
                    raise ValueError('图库类型只能是 image 或 meme')
                tags = item.get('tags', [])
                if not isinstance(tags, list) or len(tags) > 20 or any(
                        not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 80 for tag in tags):
                    raise ValueError('图库标签最多 20 个，每个不能超过 80 字')
                description = item.get('description', '')
                if not isinstance(description, str) or len(description) > 500:
                    raise ValueError('图库说明不能超过 500 字')
                safe_items.append({'name': name.strip(), 'base64': encoded, 'kind': kind,
                                   'tags': list(dict.fromkeys(tag.strip() for tag in tags)),
                                   'description': description.strip()})
            reusable = data.get('reusable', True)
            if not isinstance(reusable, bool):
                raise ValueError('图库 reusable 必须为布尔值')
            result = await self._media_call(library, 'import_items', safe_items, reusable=reusable)
        else:
            raise ValueError('图库操作无效')
        cleaned = self._media_metadata(result)
        if isinstance(cleaned, dict):
            return {'ok': True, **cleaned}
        return {'ok': True, 'items': cleaned if isinstance(cleaned, list) else []}

    def make_app(self):
        @web.middleware
        async def auth(request, handler):
            bearer = secrets.compare_digest(request.headers.get("Authorization", ""), "Bearer "+self.config["control_token"])
            cookie_value = hmac.new(self.config['control_token'].encode(), b'qq-panel-session-v1', hashlib.sha256).hexdigest()
            cookie = secrets.compare_digest(request.cookies.get('qq_panel', ''), cookie_value)
            if cookie and not bearer and request.method != 'GET' and request.headers.get('Origin') != f'{request.scheme}://{request.host}':
                return web.json_response({'error': '请在同源本机面板操作'}, status=403)
            if request.path != "/" and not bearer and not cookie:
                return web.json_response({"error": "需要本机管理密钥"}, status=401)
            try:
                return await handler(request)
            except (ValueError, TypeError, KeyError) as exc:
                return web.json_response({"error": str(exc)}, status=400)

        # Local gallery imports carry base64 payloads. Keep the request bound
        # high enough for one bounded image while the UI uploads files one by
        # one; the media endpoint still validates each encoded item.
        app = web.Application(middlewares=[auth], client_max_size=32 * 1024 * 1024)
        async def home(request):
            return web.FileResponse(Path(__file__).with_name("panel.html"))
        async def session(request):
            response = web.json_response({'ok': True})
            value = hmac.new(self.config['control_token'].encode(), b'qq-panel-session-v1', hashlib.sha256).hexdigest()
            response.set_cookie('qq_panel', value, httponly=True, samesite='Strict', max_age=30*86400, path='/')
            return response
        async def status(request):
            contacts = await self.contacts()
            directory = {(row['kind'], row['id']): row for row in contacts}
            activity = self.ledger.snapshot(self.policy['account_id'])
            for row in activity['sends']:
                contact = directory.get((row['kind'], row['target']), {}) if row['account'] == self.policy['account_id'] else {}
                row.update(targetName=contact.get('name', ''), targetRemark=contact.get('remark', ''))
            return web.json_response({"policy": self.policy,
                "resume_mode": self.resume_mode,
                "accountProfiles": account_summaries({**self.config, "policy": self.policy}),
                'contacts': [row for row in contacts if row['id'] in self.policy[row['kind'] + '_ids']],
                'contactsComplete': self.account_verified and self._contacts_complete,
                'lifecycle': self.lifecycle_status(),
                "reply_agent": self.reply_agent, "amadeus_configured": self.amadeus.configured(),
                "connected": self.account_verified and (self.reply_agent == "amadeus" or self.astrbot is not None),
                "error": self.last_error, "napcat_ws": self.config["napcat_ws"], **activity})
        async def history(request):
            if not self.account_verified:
                return web.json_response({"ok": False, "error": "qq_not_connected", "message": "QQ 账号尚未验证，不能读取历史。"}, status=409)
            generation = self.generation
            args = await request.json()
            expected_account = args.pop("expectedAccount", self.policy["account_id"])
            if str(expected_account) != self.policy["account_id"]:
                return web.json_response({"ok": False, "error": "qq_account_changed", "message": "当前 QQ 账号已变化，旧会话不能读取。"}, status=409)
            result = await self.scoped_history(args, owner_authorized=True)
            if generation != self.generation or not self.account_verified:
                return web.json_response({"ok": False, "error": "qq_scope_changed", "message": "读取期间账号或授权发生变化，请重新查询。"}, status=409)
            if result.get("ok"):
                contact = next((row for row in self._contacts
                                if row['kind'] == result.get('targetType') and row['id'] == result.get('targetId')), {})
                result.update(targetName=contact.get('name', ''), targetRemark=contact.get('remark', ''))
                self.record_event("history_read", {"account": self.policy["account_id"], "kind": result.get("targetType"),
                    "target": result.get("targetId"), "count": len(result.get("messages", [])), "reason": "按请求读取授权会话的一段历史"})
            return web.json_response(result, status=200 if result.get("ok") else 400)
        async def qzone(request):
            data = await request.json()
            task = self.spawn(self.execute_qzone(data))
            try:
                return web.json_response(await task)
            except asyncio.CancelledError:
                return web.json_response({'ok': False, 'error': 'qq_scope_changed'}, status=409)
        async def media(request):
            return web.json_response(await self.media_action(await request.json()))
        async def contacts(request):
            rows = await self.contacts(force=True)
            return web.json_response({'ok': True, 'contacts': rows,
                'complete': self.account_verified and self._contacts_complete})
        async def identity(request):
            return web.json_response(await self.identities.execute(
                await request.json(), owner_authorized=True))
        async def message_image(request):
            return web.json_response(await self.read_message_image(await request.json()))
        async def presence(request):
            data = await request.json()
            ident = str(data.get('clientId') or '')
            if not ident or len(ident) > 150 or type(data.get('active')) is not bool:
                raise ValueError('桌面租约无效')
            if data['active']:
                self.owners[ident] = time.monotonic()
            else:
                self.owners.pop(ident, None)
                if not self.lifecycle_status()['active']:
                    self.request_stop('Amadeus 桌面已退出，结束 QQ 接管')
            return web.json_response({'ok': True, 'lifecycle': self.lifecycle_status()})
        async def mute(request):
            data = await request.json()
            kind, target = data.get('targetType'), str(data.get('targetId') or '')
            if kind not in {'private', 'group'} or (target not in self.policy[kind + '_ids'] and not (
                    data.get('action') == 'unmute' and target in self.policy['muted_' + kind + '_ids'])):
                raise ValueError('只能控制已经授权的会话')
            if str(data.get('expectedAccount', self.policy['account_id'])) != self.policy['account_id']:
                raise ValueError('账号已经变更')
            action = data.get('action')
            if action not in {'mute', 'unmute'} or not str(data.get('reason') or '').strip():
                raise ValueError('需要明确的会话控制动作及原因')
            self.set_conversation_reply(kind, target, action, str(data['reason']))
            return web.json_response({'ok': True, 'policy': self.policy, 'targetType': kind, 'targetId': target,
                                      'muted': action == 'mute', 'sent': False})
        async def clear_context(request):
            data = await request.json()
            return web.json_response(await self.clear_context(data))
        async def configure(request):
            data = await request.json()
            if "expected_policy" in data and data["expected_policy"] != self.policy:
                raise ValueError("QQ 配置已变化，请重新读取状态后再修改")
            saved_mode = self.policy["mode"] if self.policy["mode"] != "paused" else self.resume_mode
            if "expected_resume_mode" in data and data["expected_resume_mode"] != saved_mode:
                raise ValueError("QQ 回复方式已变化，请重新读取状态后再修改")
            if "resume_mode" in data and data["resume_mode"] not in {"auto", "review"}:
                raise ValueError("回复方式必须为 auto / review")
            requested_policy = normalize_policy(data["policy"])
            requested_account = validate_account_id(requested_policy["account_id"])
            account_changed = requested_account != self.policy["account_id"]
            target_has_profile = requested_account in self.config.get("account_profiles", {})
            if account_changed:
                # The posted policy can come from the old account's form.  A
                # switch restores the gateway's own profile and deliberately
                # ignores its lists, overrides, identity and mode.
                updated = select_account({**self.config, "policy": self.policy}, requested_account)
                policy = updated["policy"]
            else:
                policy = requested_policy
                updated = {**self.config, "policy": policy}
            if "reply_agent" in data:
                if data["reply_agent"] not in {"astrbot", "amadeus"}:
                    raise ValueError("请选择 AstrBot 或 Amadeus")
                # A saved target profile wins during a switch.  For a new
                # account the explicit setup choice is safe to apply.
                if not account_changed or not target_has_profile:
                    updated["reply_agent"] = data["reply_agent"]
            if updated.get("reply_agent", "astrbot") == "amadeus" and policy["mode"] != "paused" and not self.amadeus.configured():
                raise ValueError("请先填写 Amadeus QQ 模型配置，再开启代管")
            if not account_changed:
                if policy["mode"] != "paused":
                    updated["resume_mode"] = policy["mode"]
                elif self.policy["mode"] != "paused":
                    updated["resume_mode"] = self.policy["mode"]
                if "resume_mode" in data:
                    updated["resume_mode"] = data["resume_mode"]
            elif not target_has_profile and "resume_mode" in data:
                updated["resume_mode"] = data["resume_mode"]
            for key in ("napcat_token",):
                if data.get(key):
                    updated[key] = str(data[key])
            if data.get("napcat_ws"):
                from urllib.parse import urlparse
                url = str(data["napcat_ws"])
                parsed = urlparse(url)
                if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password or parsed.query:
                    raise ValueError("第一版只连接本机 ws://127.0.0.1:端口/，鉴权请单独填写")
                updated["napcat_ws"] = url
            updated = remember_account(updated)
            temp = self.config_path.with_suffix(".tmp")
            temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.config_path)
            # Reply policy updates invalidate automatic work, not the owner's
            # command which requested the update or the live QQ transport.
            old_napcat = self.napcat
            transport_changed = account_changed or self.reply_agent != 'amadeus' or any(
                updated.get(key) != self.config.get(key)
                for key in ('napcat_ws', 'napcat_token', 'reply_agent'))
            if account_changed:
                self.invalidate_account(revoke_drafts=True)
            else:
                self.generation += 1
                self.revoke_tasks(preserve_owner=not transport_changed)
                if transport_changed:
                    self.account_verified = False
            self.policy = policy
            self.config = updated
            if account_changed:
                self._contacts, self._contacts_at, self._contacts_complete = [], 0.0, False
                self._contacts_account = policy["account_id"]
            if transport_changed and old_napcat and not old_napcat.closed:
                await old_napcat.close()
            self.record_event("policy_changed", {**policy,
                "actor": "Amadeus" if data.get("actor") == "Amadeus" else "主人",
                "reason": str(data.get("reason") or "主人在设置中更新 QQ 代管范围和模式")[:1000]})
            return web.json_response({"ok": True, "policy": policy,
                                      "resume_mode": self.resume_mode,
                                      "accountProfiles": account_summaries(updated)})
        async def approve(request):
            data = await request.json()
            if "expected_policy" in data and data["expected_policy"] != self.policy:
                raise ValueError("QQ 配置已变化，请重新读取状态；本次没有发送")
            if "approval_request_id" in data:
                approval_request_id = data["approval_request_id"]
                if not isinstance(approval_request_id, str) or not approval_request_id.strip():
                    raise ValueError("确认请求缺少本轮标识，本次没有发送")
                draft_row = self.ledger.get(str(data["id"]))
                if draft_row.get("creation_request_id") == approval_request_id:
                    raise ValueError("草稿创建回合不能自行批准，请等待主人后续确认")
            if "expected" in data:
                row = self.ledger.get(str(data["id"]))
                expected = data["expected"]
                actual = {"targetType": row["kind"], "targetId": row["target"],
                          "text": "".join(segment.get("data", {}).get("text", "")
                                             for segment in json.loads(row["message"])
                                             if isinstance(segment, dict))}
                image_ids = [segment.get("data", {}).get("assetId")
                             for segment in json.loads(row["message"])
                             if isinstance(segment, dict) and segment.get("type") == "qq_image"]
                # New callers always include imageIds.  Keep old plain-text
                # callers compatible by normalizing an omitted empty list;
                # drafts with gallery assets must explicitly acknowledge their
                # exact asset IDs before approval.
                if not isinstance(expected, dict):
                    raise ValueError("确认内容格式不正确，本次没有发送")
                expected = dict(expected)
                if "imageIds" not in expected:
                    if image_ids:
                        raise ValueError("确认内容缺少图片附件，本次没有发送")
                    expected["imageIds"] = []
                actual["imageIds"] = image_ids
                if expected != actual:
                    raise ValueError("确认的收件人或正文与草稿不一致，本次没有发送")
            result = await self.send_draft(str(data["id"]), approved=True,
                                           owner_authorized=True)
            return web.json_response({"result": result, "draft": self.ledger.get(str(data["id"]))})
        async def cancel(request):
            data = await request.json()
            row = self.ledger.get(str(data["id"]))
            if row['account'] != self.policy['account_id']:
                raise ValueError('这条草稿属于其他 QQ 账号')
            if row["state"] != "draft":
                raise ValueError("仅能取消尚未处理的草稿")
            self.ledger.finish(row["id"], "cancelled")
            return web.json_response({"ok": True})
        def owner_draft(data, *, sending=False):
            kind, target, message = outgoing("send_msg", data)
            if not control_allowed(self.policy, kind, target):
                raise ValueError("目标不在允许会话中")
            account = self.policy['account_id']
            scope = self._scope_id(account, kind, target)
            if scope in self._resetting_scopes:
                raise ValueError("该 QQ 会话正在重置，请稍候重试")
            reason = str(data.get("reason", "")).strip()
            if not reason:
                raise ValueError("必须说明发送原因")
            if not isinstance(data.get("request_id"), str) or not data["request_id"].strip():
                raise ValueError("必须提供消息请求标识，避免重复发送")
            creation_request_id = data.get("creation_request_id", "")
            if not isinstance(creation_request_id, str) or len(creation_request_id) > 200:
                raise ValueError("草稿创建回合标识格式不正确")
            creation_request_id = creation_request_id.strip()
            if 'speechLanguage' in data and (not isinstance(data['speechLanguage'], str) or data['speechLanguage'] not in {'zh-CN', 'ja-JP'}):
                raise ValueError('本次语音语言必须为 zh-CN / ja-JP')
            epoch = self._scope_epoch_now(account, kind, target)
            request_key = (("amadeus-send:" if sending else "amadeus:") + account + ':'
                           + data['request_id'] + ':e' + str(epoch))
            self.delivery_language(kind, target, request_key, data.get('speechLanguage'))
            row = self.ledger.draft(self.policy, kind, target, message, actor="Amadeus", reason=reason,
                request_key=request_key, creation_request_id=creation_request_id)
            return row, "通过 QQ 向 "+kind+":"+target+" 发送消息。原因："+reason
        async def draft(request):
            row, explanation = owner_draft(await request.json())
            return web.json_response({"draft": row, "explanation": "拟"+explanation})
        async def send(request):
            data = await request.json()
            if data.get("expected_policy") != self.policy:
                raise ValueError("QQ 配置已变化，请重新读取状态；本次没有发送")
            if self.policy["mode"] == "paused":
                raise ValueError("QQ 代管已暂停，本次没有发送；需要主人明确恢复后才能发送")
            kind, target, _message = outgoing('send_msg', data)
            reply_mode = conversation_policy(self.policy, kind, target)['mode']
            if reply_mode == "auto" and not self.account_verified:
                raise ValueError("QQ 账号尚未验证，本次没有发送")
            generation = self.generation
            row, explanation = owner_draft(data, sending=True)
            if reply_mode == "auto":
                await self.send_draft(row["id"], generation=generation,
                                      idempotent=True, owner_authorized=True)
                row = self.ledger.get(row["id"])
            return web.json_response({"draft": row, "explanation": explanation,
                                      "requiresApproval": row["state"] == "draft"})
        async def control(request):
            data = await request.json()
            if data.get("action") not in {"pause", "resume", "stop"} or not str(data.get("reason", "")).strip():
                raise ValueError("需要有效操作和相应说明")
            if data['action'] == 'stop':
                self.request_stop(str(data['reason']), explicit=True)
                return web.json_response({'ok': True, 'policy': self.policy, 'exiting': True, 'explanation': 'QQ 接管已暂停，后台正在退出'})
            await self.set_reply_mode(data['action'], str(data['reason']))
            policy = self.policy
            return web.json_response({"policy":policy, "resume_mode":self.resume_mode,
                                      "accountProfiles":account_summaries(self.config),
                                      "explanation":"Amadeus 已"+("暂停" if policy["mode"]=="paused" else "恢复")+" QQ 代管。原因："+str(data["reason"])[:1000]})
        async def delegate(request):
            data = await request.json()
            scope = None
            if isinstance(data, dict):
                kind = data.get('targetType')
                target = str(data.get('targetId', ''))
                account = str(self.policy.get('account_id', ''))
                if (kind in {'private', 'group'} and target.isascii() and target.isdigit()
                        and account.isascii() and account.isdigit()):
                    scope = (account, kind, target)
            task = self.spawn(self.delegate(data, owner_authorized=True), scope=scope)
            try:
                return web.json_response(await task)
            except asyncio.CancelledError:
                raise ValueError('QQ 授权或连接已变化，本次委托已中止；请查询回执确认状态') from None
        app.router.add_get("/", home)
        app.router.add_get("/api/status", status)
        app.router.add_post('/api/session', session)
        app.router.add_post('/api/presence', presence)
        app.router.add_get('/api/contacts', contacts)
        app.router.add_post('/api/identity', identity)
        app.router.add_post('/api/message-image', message_image)
        app.router.add_post('/api/conversation', mute)
        app.router.add_post('/api/conversation/clear', clear_context)
        app.router.add_post("/api/history", history)
        app.router.add_post('/api/qzone', qzone)
        app.router.add_post('/api/media', media)
        app.router.add_post("/api/config", configure)
        app.router.add_post("/api/approve", approve)
        app.router.add_post("/api/cancel", cancel)
        app.router.add_post("/api/draft", draft)
        app.router.add_post("/api/send", send)
        app.router.add_post("/api/delegate", delegate)
        app.router.add_post("/api/control", control)
        async def lifecycle(app):
            task = asyncio.create_task(self.bridge())
            watcher = asyncio.create_task(self.watch_owner())
            space_watcher = asyncio.create_task(self.watch_qzone())
            yield
            space_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await space_watcher
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            for child in list(self.tasks):
                child.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.ledger.db.close()
            self.owner_receipts.close()
        app.cleanup_ctx.append(lifecycle)
        return app


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    gateway = Gateway(args.config)
    async def serve():
        runner = web.AppRunner(gateway.make_app(), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, '127.0.0.1', int(gateway.config['control_port'])).start()
            await gateway.stop_event.wait()
        finally:
            await runner.cleanup()
    try:
        asyncio.run(serve())
    finally:
        # Stop only the portable components this project manages, never normal QQ.
        import os
        import subprocess
        root = Path(__file__).resolve().parents[2]
        if gateway.stop_requested and os.name == 'nt':
            subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                str(root / 'tools/qq_agent.ps1'), '-AppRoot', str(root), '-RuntimeRoot', str(gateway.config_path.resolve().parent),
                '-Action', 'stop', '-HelpersOnly'], cwd=root,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    main()
