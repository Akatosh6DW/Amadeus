"""QQ policy and durable send ledger, independent of any model/framework."""
from __future__ import annotations

import json
import hashlib
import random
import sqlite3
import threading
import time
import uuid
import re
from pathlib import Path
from typing import Any


DEFAULT_REPLY_INSTRUCTIONS = "QQ 日常回复默认用 1～2 句简短自然的话，先回答重点，不堆砌解释或连续长段；对方明确要求详细说明时再展开。"
CONVERSATION_FIELDS = frozenset({'mode', 'reply_identity', 'reply_style', 'reply_instructions', 'reply_delay_seconds',
                                 'minimum_interval_seconds', 'history_messages', 'compact_reply',
                                 'group_triggers', 'group_keywords', 'reply_delay_mode', 'reply_format', 'voice_reply_percent', 'speech_language',
                                 'reply_bubble_chars'})

DEFAULT_POLICY = {
    "mode": "paused",
    "account_id": "",
    # The managed QQ account can be Amadeus's assistant account or the human
    # owner's own account.  This is separate from reply_identity, which only
    # controls the voice used for a reply.
    "account_role": "assistant",
    "owner_ids": [],
    "owner_control_enabled": False,
    "owner_control_in_groups": False,
    "owner_developer_mode": False,
    "qzone_enabled": False,
    "qzone_allow_write": False,
    "qzone_allow_publish": False,
    "qzone_autonomous": False,
    "qzone_interval_minutes": 60,
    "qzone_instructions": "",
    "private_ids": [],
    "group_ids": [],
    "muted_private_ids": [],
    "muted_group_ids": [],
    "background_enabled": False,
    "history_messages": 10,
    "compact_reply": True,
    "reply_identity": "assistant",
    "reply_style": "auto",
    "reply_format": "text",
    "reply_bubble_chars": 60,
    "voice_reply_percent": 100,
    "speech_language": "inherit",
    "conversation_overrides": {},
    "group_triggers": ["mention"],
    "group_keywords": [],
    "reply_instructions": DEFAULT_REPLY_INSTRUCTIONS,
    "reply_delay_seconds": 3,
    "reply_delay_mode": "fixed",
    "minimum_interval_seconds": 10,
    "max_sends_per_hour": 0,
}

_ASSET_ID = re.compile(r'^[0-9a-fA-F]{64}$')


def normalize_group_fields(raw: dict) -> dict:
    result = {}
    if raw.get('group_triggers') is not None:
        modes = raw['group_triggers']
        if not isinstance(modes, list) or not modes or any(not isinstance(x, str) or x not in {'mention', 'keyword', 'reply', 'all'} for x in modes):
            raise ValueError('群聊触发条件请选择 @我、关键词、引用回复或任何新消息')
        result['group_triggers'] = ['all'] if 'all' in modes else list(dict.fromkeys(modes))
    if raw.get('group_keywords') is not None:
        words = raw['group_keywords']
        if not isinstance(words, list) or len(words) > 30 or any(not isinstance(x, str) or not x.strip() or len(x.strip()) > 80 for x in words):
            raise ValueError('关键词最多 30 个，每个为 1–80 字的文字')
        result['group_keywords'] = list(dict.fromkeys(x.strip() for x in words))
    return result


def policy_revision(policy: dict) -> str:
    """Opaque compare-and-swap version; no copying contact lists through the model."""
    return hashlib.sha256(json.dumps(policy, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def normalize_policy(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("QQ 设置必须为对象")
    has_account_role = "account_role" in raw
    result = {**DEFAULT_POLICY, **{k: v for k, v in raw.items() if k in DEFAULT_POLICY}}
    if result['group_triggers'] is None or result['group_keywords'] is None:
        raise ValueError('全局群聊触发设置不能为空')
    result.update(normalize_group_fields(result))
    if 'keyword' in result['group_triggers'] and not result['group_keywords']:
        raise ValueError('启用关键词触发前请填写关键词')
    if result["mode"] not in {"paused", "review", "auto"}:
        raise ValueError("模式必须为 paused / review / auto")
    if result["reply_identity"] not in {"assistant", "owner"}:
        raise ValueError("QQ 回复身份必须为 assistant / owner")
    if not isinstance(result['reply_style'], str) or result['reply_style'] not in {'auto', 'game', 'tieba', 'kangba', 'acg'}:
        raise ValueError('默认聊天风格必须为 auto / game / tieba / kangba / acg')
    # The old ACG option had no corpus. Retain old profiles by migrating its
    # value, not by dropping their other per-contact preferences.
    if result['reply_style'] == 'acg':
        result['reply_style'] = 'tieba'
    if not isinstance(result['reply_format'], str) or result['reply_format'] not in {'text', 'voice'}:
        raise ValueError('回复形式必须为 text / voice；语音需要主人明确开启')
    if not isinstance(result['speech_language'], str) or result['speech_language'] not in {'inherit', 'zh-CN', 'ja-JP'}:
        raise ValueError('QQ 语音语言必须为 inherit / zh-CN / ja-JP')
    bubble_chars = result['reply_bubble_chars']
    if isinstance(bubble_chars, bool) or not isinstance(bubble_chars, int) or not 0 <= bubble_chars <= 240:
        raise ValueError('QQ 气泡字数必须为 0–240 的整数')
    percent = result['voice_reply_percent']
    if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
        raise ValueError('语音回复频率必须为 0–100 的整数百分比')
    if result['reply_delay_mode'] not in {'fixed', 'length'}:
        raise ValueError('回复等待模式必须为 fixed / length')
    if not isinstance(result["reply_instructions"], str) or len(result["reply_instructions"]) > 2000:
        raise ValueError("QQ 回复要求必须为不超过 2000 字的文字")
    result["reply_instructions"] = result["reply_instructions"].strip()
    for key in ("background_enabled", "compact_reply", "owner_control_enabled", "owner_control_in_groups", "owner_developer_mode",
                "qzone_enabled", "qzone_allow_write", "qzone_allow_publish", "qzone_autonomous"):
        if not isinstance(result[key], bool):
            raise ValueError(key + " 必须为布尔值")
    interval = result['qzone_interval_minutes']
    if isinstance(interval, bool) or not isinstance(interval, int) or not 5 <= interval <= 10080:
        raise ValueError('QQ 空间自主查看间隔必须为 5–10080 分钟的整数')
    if not isinstance(result['qzone_instructions'], str) or len(result['qzone_instructions']) > 2000:
        raise ValueError('QQ 空间要求必须为不超过 2000 字的文字')
    result['qzone_instructions'] = result['qzone_instructions'].strip()
    if isinstance(result["history_messages"], bool) or not isinstance(result["history_messages"], int) or not 0 <= result["history_messages"] <= 30:
        raise ValueError("自动读取上下文条数必须为 0–30")
    delay = result["reply_delay_seconds"]
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
        raise ValueError("QQ 回复停顿必须为 0–30 秒")
    result["account_id"] = str(result["account_id"]).strip()
    if result["account_id"] and not result["account_id"].isdigit():
        raise ValueError("QQ 账号必须为数字")
    owners = result['owner_ids']
    if (not isinstance(owners, list) or any(isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).strip().isascii() or not str(value).strip().isdigit()
            or not 5 <= len(str(value).strip()) <= 20 for value in owners)):
        raise ValueError("本人 QQ 号必须为 5–20 位数字的数组")
    result['owner_ids'] = list(dict.fromkeys(str(value).strip() for value in owners))
    if has_account_role:
        if result["account_role"] not in {"assistant", "owner"}:
            raise ValueError("QQ 账号归属必须为 assistant / owner")
    else:
        # Older profiles had no account_role.  Preserve their intended owner
        # account only when the selected account itself was explicitly listed
        # among trusted owner IDs; all other legacy accounts are assistant
        # accounts by default.
        result["account_role"] = (
            "owner" if result["account_id"] and result["account_id"] in result["owner_ids"]
            else "assistant"
        )
    for key in ("private_ids", "group_ids", "muted_private_ids", "muted_group_ids"):
        if not isinstance(result[key], list):
            raise ValueError("会话白名单必须为数组")
        result[key] = list(dict.fromkeys(str(x).strip() for x in result[key]))
        if any(not x.isdigit() for x in result[key]):
            raise ValueError("联系人和群号必须为数字")
    result["minimum_interval_seconds"] = max(1, min(3600, int(result["minimum_interval_seconds"])))
    quota = result['max_sends_per_hour']
    if isinstance(quota, bool) or not isinstance(quota, int) or quota < 0:
        raise ValueError('每小时发送上限必须为非负整数，0 表示不限')
    overrides = result["conversation_overrides"]
    if not isinstance(overrides, dict):
        raise ValueError("会话回复设置必须为对象")
    result["conversation_overrides"] = {}
    for key, patch in overrides.items():
        if not isinstance(key, str) or ':' not in key or not isinstance(patch, dict):
            raise ValueError("会话设置需要 private:QQ号 或 group:群号")
        kind, target = key.split(':', 1)
        if kind not in {'private', 'group'} or not target.isascii() or not target.isdigit():
            raise ValueError("无效会话设置目标")
        if set(patch) - CONVERSATION_FIELDS:
            raise ValueError("会话只能单独设置回复方式、身份、要求、间隔、上下文、排版和群聊触发条件")
        value = {}
        if kind != 'group' and any(field in patch for field in ('group_triggers', 'group_keywords')):
            raise ValueError('群聊触发条件只能用于群聊')
        value.update(normalize_group_fields(patch))
        if 'keyword' in value.get('group_triggers', result['group_triggers']) and not value.get('group_keywords', result['group_keywords']):
            raise ValueError('启用关键词触发前请填写关键词')
        for field, allowed_values in [('mode', {'auto', 'review'}), ('reply_identity', {'assistant', 'owner'}), ('reply_style', {'game', 'tieba', 'kangba', 'acg'}), ('reply_delay_mode', {'fixed', 'length'}), ('reply_format', {'text', 'voice'}), ('speech_language', {'inherit', 'zh-CN', 'ja-JP'})]:
            if field in patch and patch[field] is not None:
                if not isinstance(patch[field], str) or patch[field] not in allowed_values:
                    raise ValueError('无效的会话 ' + field)
                value[field] = 'tieba' if field == 'reply_style' and patch[field] == 'acg' else patch[field]
        if patch.get('reply_instructions') is not None:
            instructions = patch['reply_instructions']
            if not isinstance(instructions, str) or len(instructions) > 2000:
                raise ValueError('会话回复要求必须为不超过 2000 字的文字')
            if instructions.strip():
                value['reply_instructions'] = instructions.strip()
        for field, minimum, maximum, integer in [('reply_delay_seconds', 0, 30, False),
                ('minimum_interval_seconds', 1, 3600, True), ('history_messages', 0, 30, True),
                ('voice_reply_percent', 0, 100, True), ('reply_bubble_chars', 0, 240, True)]:
            if patch.get(field) is not None:
                number = patch[field]
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not minimum <= number <= maximum or (integer and not isinstance(number, int)):
                    raise ValueError(f'会话 {field} 必须为 {minimum}–{maximum}' + (' 的整数' if integer else ' 的数字'))
                value[field] = number
        if patch.get('compact_reply') is not None:
            if not isinstance(patch['compact_reply'], bool):
                raise ValueError('会话 compact_reply 必须为布尔值')
            value['compact_reply'] = patch['compact_reply']
        # Owner control has its own list; it must retain delivery preferences
        # even when the owner is not an ordinary auto-reply contact.
        saved_target = target in result[kind + '_ids'] or (kind == 'private' and target in result['owner_ids'])
        if value and saved_target:
            result['conversation_overrides'][key] = value
    if result["mode"] != "paused" and (not result["account_id"] or not (result["private_ids"] or result["group_ids"])):
        raise ValueError("开启前请填写自己的 QQ 账号和允许的联系人或群")
    return result


def conversation_policy(policy: dict, kind: str, target: str) -> dict:
    effective = {**policy, **policy.get('conversation_overrides', {}).get(f'{kind}:{target}', {})}
    if effective.get('reply_style', 'auto') == 'auto':
        effective['reply_style'] = 'tieba' if kind == 'group' else 'game'
    elif effective.get('reply_style') == 'acg':
        effective['reply_style'] = 'tieba'
    if policy['mode'] == 'paused':
        effective['mode'] = 'paused'
    return effective


def voice_enabled(policy: dict) -> bool:
    return policy.get('reply_format', 'text') == 'voice' and policy.get('voice_reply_percent', 100) > 0


def choose_reply_format(policy: dict) -> str:
    """Only owner-enabled voice is eligible; endpoints never need randomness."""
    if not voice_enabled(policy):
        return 'text'
    percent = policy.get('voice_reply_percent', 100)
    return 'voice' if percent == 100 or random.random() * 100 < percent else 'text'


def conversation(event: dict) -> tuple[str, str]:
    kind = event.get("message_type")
    if kind == "group":
        return "group", str(event.get("group_id", ""))
    if kind == "private":
        return "private", str(event.get("user_id", ""))
    raise ValueError("只支持群聊和私聊")


def allowed(policy: dict, kind: str, target: str) -> bool:
    return bool(policy["account_id"] and target in policy.get(kind + "_ids", [])
                and target not in policy.get("muted_" + kind + "_ids", []))


def control_allowed(policy: dict, kind: str, target: str) -> bool:
    """Allow an authenticated control action to address the configured owner.

    ``allowed`` deliberately remains the ordinary automatic-reply boundary.
    The owner list is a separate role list: it may authorize an explicit,
    authenticated control send or lookup to a private owner target without
    adding that target to the ordinary incoming-reply allowlist.  Mute state
    still applies to both paths, while paused/review handling remains at the
    caller and send-draft boundaries.
    """
    if not policy.get("account_id") or kind not in {"private", "group"}:
        return False
    if target in policy.get("muted_" + kind + "_ids", []):
        return False
    return target in policy.get(kind + "_ids", []) or (
        kind == "private" and target in policy.get("owner_ids", [])
    )


def format_reply(text: str, compact: bool = True) -> str:
    """Apply the QQ-only display preference before a draft gets its identity."""
    if not compact:
        return text.strip()
    return " ".join(line.strip().rstrip("。.") for line in text.splitlines() if line.strip()).strip()


def reply_delay(policy: dict, text: str) -> float:
    """Post-generation waiting; length mode adds roughly six characters per second."""
    base = float(policy['reply_delay_seconds'])
    if policy.get('reply_delay_mode', 'fixed') == 'length':
        return min(30.0, base + sum(not char.isspace() for char in text) / 6)
    return base


def outgoing(action: str, params: dict) -> tuple[str, str, list[dict]]:
    if action not in {"send_msg", "send_private_msg", "send_group_msg"}:
        raise ValueError("此接口未开放")
    kind = action.removeprefix("send_").removesuffix("_msg") if action != "send_msg" else params.get("message_type")
    if kind is None:
        kind = "group" if params.get("group_id") else "private"
    if kind not in {"group", "private"}:
        raise ValueError("消息目标类型不正确")
    target = str(params.get("group_id" if kind == "group" else "user_id", ""))
    segments = params.get("message")
    image_ids = params.get("imageIds")
    if image_ids is not None:
        if not isinstance(image_ids, list) or len(image_ids) > 2:
            raise ValueError("QQ 图片附件最多选择 2 张")
        if any(not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id) for asset_id in image_ids):
            raise ValueError("QQ 图片附件标识无效")
        if segments is None:
            segments = []
        elif isinstance(segments, str):
            segments = [{"type": "text", "data": {"text": segments}}]
        if not isinstance(segments, list):
            raise ValueError("QQ 消息格式不正确")
        segments = [*segments, *[
            {"type": "qq_image", "data": {"assetId": asset_id}}
            for asset_id in image_ids
        ]]
    if isinstance(segments, str):
        # Treat strings as plain text, never parse attacker-controlled CQ actions.
        segments = [{"type": "text", "data": {"text": segments}}]
    if not isinstance(segments, list) or not segments:
        raise ValueError("消息不能为空")
    normalized = []
    image_count = 0
    text_length = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("当前代发只支持文字和图库图片")
        segment_type = segment.get("type")
        data = segment.get("data")
        if segment_type == "text":
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                raise ValueError("QQ 文字消息格式不正确")
            text = data["text"]
            text_length += len(text)
            if text:
                normalized.append({"type": "text", "data": {"text": text}})
        elif segment_type == "qq_image":
            if (not isinstance(data, dict) or not isinstance(data.get("assetId"), str)
                    or not _ASSET_ID.fullmatch(data["assetId"])):
                raise ValueError("QQ 图片附件标识无效")
            image_count += 1
            if image_count > 2:
                raise ValueError("QQ 图片附件最多选择 2 张")
            normalized.append({"type": "qq_image", "data": {"assetId": data["assetId"]}})
        else:
            raise ValueError("当前代发只支持文字和图库图片")
    if not normalized or (not text_length and not image_count):
        raise ValueError("消息至少需要文字或一张 QQ 图片")
    if text_length > 6000:
        raise ValueError("文字消息最多 6000 字")
    return kind, target, normalized


class Ledger:
    """Persist drafts before sending. Unknown outcomes are never retried blindly."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sends (
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                creation_request_id TEXT NOT NULL DEFAULT '',
                account TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
                message TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                message_id TEXT, detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS incoming (key TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS delegations (
                request_key TEXT PRIMARY KEY, instruction TEXT NOT NULL,
                reply_identity TEXT NOT NULL, policy_revision TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reply_choices (
                request_key TEXT PRIMARY KEY, account TEXT NOT NULL,
                kind TEXT NOT NULL, target TEXT NOT NULL, reply_format TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS send_parts (
                draft_id TEXT NOT NULL, part_index INTEGER NOT NULL,
                message TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL,
                message_id TEXT, detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (draft_id, part_index)
            );
        """)
        with self.db:
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(sends)')}
            if 'creation_request_id' not in columns:
                self.db.execute("ALTER TABLE sends ADD COLUMN creation_request_id TEXT NOT NULL DEFAULT ''")
            if 'reply_format' not in columns:
                self.db.execute("ALTER TABLE sends ADD COLUMN reply_format TEXT NOT NULL DEFAULT 'text'")
            if 'speech_json' not in columns:
                self.db.execute("ALTER TABLE sends ADD COLUMN speech_json TEXT NOT NULL DEFAULT '{}'")
            if 'speech_language' not in columns:
                self.db.execute("ALTER TABLE sends ADD COLUMN speech_language TEXT NOT NULL DEFAULT 'inherit'")
            choice_columns = {row[1] for row in self.db.execute('PRAGMA table_info(reply_choices)')}
            if 'speech_language' not in choice_columns:
                self.db.execute("ALTER TABLE reply_choices ADD COLUMN speech_language TEXT NOT NULL DEFAULT 'inherit'")
            self.db.execute("UPDATE sends SET state='unknown', detail='网关重启，发送结果待核对' WHERE state='sending'")

    def event(self, kind: str, payload: dict):
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(kind,payload,created) VALUES(?,?,?)", (kind, json.dumps(payload, ensure_ascii=False), time.time()))
            self.db.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-1000 FROM events)")

    def fresh_incoming(self, account: str, message_id: Any) -> bool:
        if message_id is None:
            return False
        with self.lock, self.db:
            self.db.execute("DELETE FROM incoming WHERE created < ?", (time.time()-86400*7,))
            return bool(self.db.execute("INSERT OR IGNORE INTO incoming VALUES(?,?)", (account+":"+str(message_id), time.time())).rowcount)

    def draft(self, policy: dict, kind: str, target: str, message: list, *, actor: str,
              reason: str, request_key: str, speech: dict | None = None,
              creation_request_id: str = "") -> dict:
        creation_request_id = str(creation_request_id or "").strip()[:200]
        now = time.time()
        serialized = json.dumps(message, ensure_ascii=False, sort_keys=True)
        with self.lock, self.db:
            existing = self.db.execute("SELECT * FROM sends WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if (existing["account"], existing["kind"], existing["target"], existing["message"]) != (policy["account_id"], kind, target, serialized):
                    raise ValueError("同一请求标识对应了不同消息")
                return dict(existing)
            ident = uuid.uuid4().hex
            reply_format = self.select_reply_format(policy, kind, target, request_key)
            if not any(part.get('type') == 'text' and part.get('data', {}).get('text', '').strip()
                       for part in message):
                # An image-only reply has no text to synthesize. Mixed replies
                # retain their selected voice mode and send images separately.
                reply_format = 'text'
                self.db.execute('UPDATE reply_choices SET reply_format=? WHERE request_key=?',
                                ('text', request_key))
            language = self.select_speech_language(policy, kind, target, request_key,
                desktop_language=(speech or {}).get('speechLanguage') or 'ja-JP')
            self.db.execute("INSERT INTO sends (id,request_key,creation_request_id,account,kind,target,message,reason,actor,state,created,updated,message_id,detail,reply_format,speech_json,speech_language) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ident, request_key, creation_request_id, policy["account_id"], kind, target, serialized, reason, actor, "draft", now, now, None, "", reply_format, json.dumps(speech or {}, ensure_ascii=False), language))
            return self.get(ident)

    def select_reply_format(self, policy: dict, kind: str, target: str, request_key: str) -> str:
        """Reserve once before generation, so retries/restarts keep the same plan."""
        with self.lock, self.db:
            existing = self.db.execute('SELECT account,kind,target,reply_format FROM sends WHERE request_key=?', (request_key,)).fetchone()
            if existing is None:
                existing = self.db.execute('SELECT account,kind,target,reply_format FROM reply_choices WHERE request_key=?', (request_key,)).fetchone()
            effective = conversation_policy(policy, kind, target)
            if existing is not None:
                if (existing['account'], existing['kind'], existing['target']) != (policy['account_id'], kind, target):
                    raise ValueError('同一请求标识对应了不同会话')
                if existing['reply_format'] == 'voice' and not voice_enabled(effective):
                    raise ValueError('该会话已关闭语音或将语音频率设为 0，旧语音请求不能继续')
                return existing['reply_format']
            selected = choose_reply_format(effective)
            self.db.execute('INSERT INTO reply_choices (request_key,account,kind,target,reply_format) VALUES(?,?,?,?,?)',
                            (request_key, policy['account_id'], kind, target, selected))
            return selected

    def select_speech_language(self, policy: dict, kind: str, target: str, request_key: str,
                               *, desktop_language: str, override: str | None = None) -> str:
        """Freeze the effective language alongside the delivery choice, before generation."""
        if override is not None and (not isinstance(override, str) or override not in {'zh-CN', 'ja-JP'}):
            raise ValueError('本次语音语言必须为 zh-CN / ja-JP')
        if desktop_language not in {'zh-CN', 'ja-JP'}:
            raise ValueError('桌面语音语言无效')
        with self.lock, self.db:
            row = self.db.execute('SELECT account,kind,target,speech_language FROM sends WHERE request_key=?', (request_key,)).fetchone()
            if row is None:
                self.select_reply_format(policy, kind, target, request_key)
                row = self.db.execute('SELECT account,kind,target,speech_language FROM reply_choices WHERE request_key=?', (request_key,)).fetchone()
            if (row['account'], row['kind'], row['target']) != (policy['account_id'], kind, target):
                raise ValueError('同一请求标识对应了不同会话')
            if row['speech_language'] != 'inherit':
                if override is not None and override != row['speech_language']:
                    raise ValueError('同一请求不能改写语音语言后重复发送，请查看原草稿或回执')
                return row['speech_language']
            language = override or conversation_policy(policy, kind, target).get('speech_language', 'inherit')
            if language == 'inherit':
                language = desktop_language
            self.db.execute('UPDATE reply_choices SET speech_language=? WHERE request_key=?', (language, request_key))
            self.db.execute('UPDATE sends SET speech_language=? WHERE request_key=?', (language, request_key))
            return language

    def get(self, ident: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM sends WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ValueError("草稿不存在")
            result = dict(row)
            result.update(self._part_counts_locked(ident))
            return result

    def _part_counts_locked(self, ident: str) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN state='sent' THEN 1 ELSE 0 END) AS sent "
            "FROM send_parts WHERE draft_id=?", (ident,)
        ).fetchone()
        total = int(row['total'] or 0)
        sent = int(row['sent'] or 0)
        return {
            'totalParts': total,
            'sentPartCount': sent,
            'remainingPartCount': max(0, total - sent),
        }

    def send_parts(self, ident: str) -> list[dict]:
        """Return durable per-bubble delivery receipts for one logical draft."""
        with self.lock:
            rows = self.db.execute(
                'SELECT * FROM send_parts WHERE draft_id=? ORDER BY part_index', (ident,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['message'] = json.loads(item['message'])
            result.append(item)
        return result

    def ensure_send_parts(self, ident: str, messages: list[list[dict]]) -> list[dict]:
        """Freeze a text draft's split plan before the first side effect.

        A later retry always uses this stored plan, even if the user changes
        the bubble preference between attempts.
        """
        serialized = [json.dumps(message, ensure_ascii=False, sort_keys=True) for message in messages]
        with self.lock, self.db:
            rows = self.db.execute(
                'SELECT * FROM send_parts WHERE draft_id=? ORDER BY part_index', (ident,)
            ).fetchall()
            if rows:
                # The first attempt freezes the delivery plan.  A retry may
                # observe a different current preference, but must keep the
                # original part boundaries and never duplicate sent parts.
                return [dict(row) for row in rows]
            now = time.time()
            self.db.executemany(
                'INSERT INTO send_parts(draft_id,part_index,message,state,created,updated,message_id,detail) VALUES(?,?,?,?,?,?,?,?)',
                [(ident, index, message, 'pending', now, now, None, '')
                 for index, message in enumerate(serialized)]
            )
            return [dict(row) for row in self.db.execute(
                'SELECT * FROM send_parts WHERE draft_id=? ORDER BY part_index', (ident,)
            ).fetchall()]

    def claim_send_part(self, ident: str, index: int) -> dict:
        with self.lock, self.db:
            row = self.db.execute(
                'SELECT * FROM send_parts WHERE draft_id=? AND part_index=?', (ident, index)
            ).fetchone()
            if row is None:
                raise ValueError('QQ 回复分条回执不存在')
            if row['state'] == 'sent':
                return dict(row)
            if row['state'] == 'unknown':
                raise ValueError('这条 QQ 回复已有未知分条回执，请先在 QQ 核对')
            self.db.execute(
                "UPDATE send_parts SET state='sending',updated=?,detail='' WHERE draft_id=? AND part_index=?",
                (time.time(), ident, index)
            )
            return dict(self.db.execute(
                'SELECT * FROM send_parts WHERE draft_id=? AND part_index=?', (ident, index)
            ).fetchone())

    def finish_send_part(self, ident: str, index: int, state: str,
                         message_id: str = '', detail: str = ''):
        if state not in {'sent', 'failed', 'unknown'}:
            raise ValueError('无效的 QQ 分条回执状态')
        with self.lock, self.db:
            self.db.execute(
                'UPDATE send_parts SET state=?,updated=?,message_id=?,detail=? WHERE draft_id=? AND part_index=?',
                (state, time.time(), message_id or None, detail, ident, index)
            )

    def by_request(self, request_key: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM sends WHERE request_key=?", (request_key,)).fetchone()
            return dict(row) if row else None

    def delegation(self, request_key: str) -> dict | None:
        with self.lock:
            row = self.db.execute('SELECT * FROM delegations WHERE request_key=?', (request_key,)).fetchone()
            return dict(row) if row else None

    def save_delegation(self, request_key: str, instruction: str, policy: dict, *, reply_identity: str | None = None):
        with self.lock, self.db:
            self.db.execute('INSERT INTO delegations VALUES(?,?,?,?)',
                            (request_key, instruction, reply_identity or policy['reply_identity'], policy_revision(policy)))

    def owner_briefs(self, account: str, kind: str, target: str,
                     *, since: float | None = None) -> list[dict]:
        # Only delivered tasks supply continuing facts. Draft/failed/cancelled
        # tasks must not silently become standing instructions for auto replies.
        with self.lock:
            query = '''SELECT d.instruction, d.reply_identity AS replyIdentity,
                              s.message_id AS messageId, s.updated AS sentAt
                       FROM delegations d JOIN sends s USING(request_key)
                       WHERE s.account=? AND s.kind=? AND s.target=? AND s.state='sent' '''
            params: list[Any] = [account, kind, target]
            if since is not None:
                # A reset updates old drafts while cancelling them.  The
                # creation boundary, rather than the mutable audit timestamp,
                # keeps those old rows out of a fresh automatic dialogue.
                query += 'AND s.created > ? '
                params.append(float(since))
            query += 'ORDER BY s.updated DESC LIMIT 12'
            rows = self.db.execute(query, params).fetchall()
            return [dict(row) for row in reversed(rows)]

    def cancel_unsent(self, account: str, kind: str, target: str,
                      *, detail: str = "QQ 会话上下文已重置") -> int:
        """Cancel only drafts in one scope; in-flight sends finish as unknown."""
        with self.lock, self.db:
            now = time.time()
            result = self.db.execute(
                "UPDATE sends SET state='cancelled',updated=?,detail=? "
                "WHERE account=? AND kind=? AND target=? AND state='draft'",
                (now, detail, account, kind, target),
            )
            self.db.execute(
                "UPDATE send_parts SET state='failed',updated=?,detail=? "
                "WHERE draft_id IN (SELECT id FROM sends WHERE account=? AND kind=? AND target=? AND state='cancelled') "
                "AND state IN ('pending','failed')",
                (now, detail, account, kind, target),
            )
            return int(result.rowcount or 0)

    def cancel_account_drafts(self, account: str,
                              *, detail: str = "QQ 账号已切换") -> int:
        """Revoke every pending draft for an account before it can be selected again.

        Account switching can later return to the old QQ id.  Leaving its
        drafts in ``draft`` would otherwise make an old recipient/message
        claimable after the account has been disconnected and reselected.
        In-flight sends are handled by the gateway's generation cancellation
        and remain auditable as unknown when their side effect was attempted.
        """
        with self.lock, self.db:
            now = time.time()
            result = self.db.execute(
                "UPDATE sends SET state='cancelled',updated=?,detail=? "
                "WHERE account=? AND state='draft'",
                (now, detail, account),
            )
            self.db.execute(
                "UPDATE send_parts SET state='failed',updated=?,detail=? "
                "WHERE draft_id IN (SELECT id FROM sends WHERE account=? AND state='cancelled') "
                "AND state IN ('pending','failed')",
                (now, detail, account),
            )
            return int(result.rowcount or 0)

    def cancel_account_unsent(self, account: str,
                              *, detail: str = "QQ 账号已切换") -> int:
        """Compatibility name for callers that revoke an account offline."""
        return self.cancel_account_drafts(account, detail=detail)

    def claim(self, ident: str, policy: dict, approved: bool,
              *, owner_authorized: bool = False) -> dict:
        with self.lock, self.db:
            row = self.validate_send(ident, policy, approved,
                                     owner_authorized=owner_authorized)
            self.db.execute("UPDATE sends SET state='sending',updated=? WHERE id=?", (time.time(), ident))
            return row

    def validate_send(self, ident: str, policy: dict, approved: bool,
                      *, owner_authorized: bool = False) -> dict:
        with self.lock, self.db:
            row = self.get(ident)
            policy = conversation_policy(policy, row['kind'], row['target'])
            if row["state"] != "draft":
                raise ValueError("这条消息已处理，不会重复发送")
            if policy["mode"] == "paused":
                raise ValueError("QQ 代管已暂停")
            target_allowed = (control_allowed if owner_authorized else allowed)(
                policy, row["kind"], row["target"])
            if row["account"] != policy["account_id"] or not target_allowed:
                raise ValueError("账号或允许会话已更改")
            if policy["mode"] != "auto" and not approved:
                raise ValueError("等待你确认具体收件人和内容")
            now = time.time()
            if now - row["created"] > 900:
                raise ValueError("草稿已超过 15 分钟，请重新生成")
            recent = self.db.execute("SELECT COUNT(*) AS n,MAX(updated) AS last FROM sends WHERE account=? AND state IN ('sending','sent','unknown') AND updated>?", (row["account"], now-3600)).fetchone()
            target_last = self.db.execute("SELECT MAX(updated) FROM sends WHERE account=? AND kind=? AND target=? AND state IN ('sending','sent','unknown')", (row['account'], row['kind'], row['target'])).fetchone()[0]
            parts_last = self.db.execute(
                "SELECT MAX(p.updated) FROM send_parts p JOIN sends s ON s.id=p.draft_id "
                "WHERE s.account=? AND s.kind=? AND s.target=? AND p.state='sent'",
                (row['account'], row['kind'], row['target']),
            ).fetchone()[0]
            if parts_last and (not target_last or parts_last > target_last):
                target_last = parts_last
            if policy['max_sends_per_hour'] > 0 and recent['n'] >= policy['max_sends_per_hour']:
                raise ValueError(f"已达到本机每小时 {policy['max_sends_per_hour']} 条发送上限，可在 QQ 设置中设为 0 取消")
            if target_last and now-target_last < policy['minimum_interval_seconds']:
                raise ValueError('距离上次回复太近，请等待此会话的最短回复间隔')
            if row['reply_format'] == 'voice' and not voice_enabled(policy):
                raise ValueError('该会话已关闭语音模式或将语音频率设为 0，旧语音草稿不能发送')
        return row

    def validate_part_quota(self, ident: str, policy: dict) -> dict:
        """Check the hourly limit immediately before one real bubble send.

        A split reply is one logical draft but several OneBot sends.  The
        parent draft is deliberately excluded once it has durable part rows;
        each sent/unknown part is counted instead.  Unknown outcomes remain
        counted because the side effect may already have reached QQ.
        """
        with self.lock, self.db:
            row = self.get(ident)
            limit = int(policy.get('max_sends_per_hour', 0) or 0)
            if limit <= 0:
                return row
            cutoff = time.time() - 3600
            parts = self.db.execute(
                "SELECT COUNT(*) AS n FROM send_parts p JOIN sends s ON s.id=p.draft_id "
                "WHERE s.account=? AND p.state IN ('sent','unknown') AND p.updated>?",
                (row['account'], cutoff),
            ).fetchone()['n']
            legacy = self.db.execute(
                "SELECT COUNT(*) AS n FROM sends s "
                "WHERE s.account=? AND s.state IN ('sending','sent','unknown') "
                "AND s.updated>? AND NOT EXISTS "
                "(SELECT 1 FROM send_parts p WHERE p.draft_id=s.id)",
                (row['account'], cutoff),
            ).fetchone()['n']
            if int(parts or 0) + int(legacy or 0) >= limit:
                raise ValueError(f'已达到本机每小时 {limit} 条发送上限，可在 QQ 设置中设为 0 取消')
            return row

    def finish(self, ident: str, state: str, message_id: str = "", detail: str = ""):
        with self.lock, self.db:
            self.db.execute("UPDATE sends SET state=?,updated=?,message_id=?,detail=? WHERE id=?", (state, time.time(), message_id, detail, ident))

    def snapshot(self, account: str | None = None) -> dict:
        with self.lock:
            sends = [dict(r) for r in self.db.execute("SELECT * FROM sends" + (" WHERE account=?" if account is not None else "") + " ORDER BY created DESC LIMIT 100", (account,) if account is not None else ())]
            events = [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (1000 if account is not None else 100,))]
            for row in sends:
                row.update(self._part_counts_locked(row['id']))
        for r in sends:
            r["message"] = json.loads(r["message"])
            r.pop("speech_json", None)
        for r in events:
            r["payload"] = json.loads(r["payload"])
        if account is not None:
            events = [r for r in events if str(r['payload'].get('account', r['payload'].get('account_id', ''))) == account][:100]
        return {"sends": sends, "events": events}
