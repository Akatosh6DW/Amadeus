"""Read bounded QQ history through the gateway's existing OneBot connection.

Returned messages are untrusted conversation data, never instructions. The
caller owns authentication and verification of the currently connected account.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Awaitable, Callable

from .media import image_segments, segment_description
from .identity import mention_text, mentions, replace_cq_mentions, display
from .references import reply_id, reply_text, replace_cq_replies


DEFAULT_LIMIT = 20
MAX_LIMIT = 30
MAX_MESSAGE_TEXT = 2_000
MAX_TOTAL_TEXT = 18_000
_NUMERIC = re.compile(r"^[0-9]{1,20}$")
_MESSAGE_ID = re.compile(r"^-?[0-9]{1,32}$")
_CQ = re.compile(r"\[CQ:([a-zA-Z0-9_]{1,32})(?:,[^\]]*)?\]")
_TYPE = re.compile(r"^[a-zA-Z0-9_]{1,32}$")


class _HistoryError(ValueError):
    pass


def _number(value: Any, pattern: re.Pattern = _NUMERIC) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return ""
    text = str(value).strip()
    return text if pattern.fullmatch(text) else ""


def _validate(policy: dict, args: dict) -> tuple[str, str, str, int]:
    if not isinstance(policy, dict) or not isinstance(args, dict):
        raise _HistoryError("QQ 历史读取参数无效。")
    if set(args) - {"targetType", "targetId", "limit", "cursor"}:
        raise _HistoryError("QQ 历史读取包含不支持的参数。")
    account = _number(policy.get("account_id"))
    kind = args.get("targetType")
    target = _number(args.get("targetId"))
    if not account or kind not in {"private", "group"} or not target:
        raise _HistoryError("请指定已配置 QQ 账号下的好友或群会话。")
    whitelist = policy.get(kind + "_ids", [])
    if not isinstance(whitelist, list) or target not in {_number(item) for item in whitelist}:
        raise _HistoryError("该 QQ 会话不在允许读取的白名单中。")
    limit_text = _number(args.get("limit", DEFAULT_LIMIT))
    if not limit_text or int(limit_text) < 1:
        raise _HistoryError("历史消息数量必须为正整数，最多返回 30 条。")
    return account, kind, target, min(int(limit_text), MAX_LIMIT)


def _decode_cursor(value: Any, account: str, kind: str, target: str) -> dict | None:
    if value in (None, ""):
        return None
    try:
        if not isinstance(value, str) or len(value) > 2_048:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = json.loads(raw.decode("utf-8"))
        if not isinstance(cursor, dict) or cursor.get("v") != 1:
            raise ValueError
        if (cursor.get("account"), cursor.get("targetType"), cursor.get("targetId")) != (account, kind, target):
            raise ValueError
        if not _number(cursor.get("messageId"), _MESSAGE_ID) or not _number(cursor.get("messageSeq"), _MESSAGE_ID):
            raise ValueError
        if cursor["messageSeq"] == "0" or not isinstance(cursor.get("time"), int) or isinstance(cursor["time"], bool):
            raise ValueError
        if cursor.get("realSeq") and not _number(cursor["realSeq"], _MESSAGE_ID):
            raise ValueError
        return cursor
    except (ValueError, TypeError, UnicodeError):
        raise _HistoryError("历史游标无效或不属于当前账号和会话，请重新读取最近消息。") from None


def _encode_cursor(account: str, kind: str, target: str, item: dict) -> str | None:
    if item["messageSeq"] == "0":
        return None
    value = {"v": 1, "account": account, "targetType": kind, "targetId": target,
             "messageId": item["messageId"], "messageSeq": item["messageSeq"],
             "realSeq": item["_realSeq"], "time": item["time"]}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _text(raw: dict) -> tuple[str, bool]:
    message = raw.get("message", raw.get("raw_message", ""))
    if isinstance(message, str):
        text = _CQ.sub(lambda match: "[" + match.group(1) + "]", replace_cq_replies(replace_cq_mentions(message)))
        return text[:MAX_MESSAGE_TEXT], len(text) > MAX_MESSAGE_TEXT
    if not isinstance(message, list):
        return "[message]", False
    parts, length, truncated = [], 0, False
    for segment in message:
        if not isinstance(segment, dict):
            part = "[message]"
        elif segment.get("type") == "text":
            data = segment.get("data")
            part = data.get("text", "") if isinstance(data, dict) else ""
            part = part if isinstance(part, str) else "[text]"
        elif segment.get('type') == 'reply':
            part = reply_text({'message': [segment]})
        elif segment.get('type') == 'at':
            part = mention_text(segment)
        elif segment.get('type') == 'face':
            part = segment_description(segment)
        else:
            kind = segment.get("type")
            part = "[" + (kind if isinstance(kind, str) and _TYPE.fullmatch(kind) else "attachment") + "]"
        available = MAX_MESSAGE_TEXT - length
        parts.append(part[:available])
        length += min(len(part), available)
        if len(part) > available:
            truncated = True
        if length == MAX_MESSAGE_TEXT:
            # Continue checking lengths without retaining attachment data or URLs.
            continue
    return "".join(parts), truncated


def _message(raw: Any, account: str, kind: str, target: str, index: int) -> dict:
    if not isinstance(raw, dict):
        raise _HistoryError("QQ 返回的历史消息格式无法识别。")
    if raw.get("self_id") is not None and _number(raw["self_id"]) != account:
        raise _HistoryError("QQ 返回的历史账号不匹配，已停止读取。")
    if raw.get("message_type") is not None and raw["message_type"] != kind:
        raise _HistoryError("QQ 返回的历史会话不匹配，已停止读取。")
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    sender_id = _number(sender.get("user_id")) or _number(raw.get("user_id"))
    if (kind == "group" and raw.get("group_id") is not None and _number(raw["group_id"]) != target
            or kind == "private" and sender_id and sender_id not in {account, target}
            or kind == "private" and raw.get('user_id') is not None and _number(raw['user_id']) not in {account, target}):
        raise _HistoryError("QQ 返回的历史会话不匹配，已停止读取。")
    message_id = _number(raw.get("message_id"), _MESSAGE_ID)
    message_seq = _number(raw.get("message_seq"), _MESSAGE_ID) or message_id
    message_id = message_id or message_seq
    timestamp = _number(raw.get("time"))
    if not message_id or not message_seq or not timestamp:
        raise _HistoryError("QQ 返回的历史消息缺少有效标识或时间。")
    text, truncated = _text(raw)
    name = sender.get("card") or sender.get("nickname") or ""
    return {"messageId": message_id, "messageSeq": message_seq, "senderId": sender_id,
            "speakerRole": "account" if sender_id == account else "contact" if sender_id else "unknown",
            "senderName": name[:100] if isinstance(name, str) else "", "time": int(timestamp),
            "text": text, "textTruncated": truncated, "mentions": mentions(raw),
            "replyTo": {'messageId': reply_id(raw)} if reply_id(raw) else None,
            "senderNickname": display(sender.get('nickname')), "senderCard": display(sender.get('card')),
            "imageRefs": [{"index": n, "description": segment_description(segment)}
                          for n, segment in enumerate(image_segments(raw)[:2])],
            "_realSeq": _number(raw.get("real_seq"), _MESSAGE_ID), "_index": index}


def _sort_key(item: dict) -> tuple[int, int, int]:
    # NapCat's message_seq is a short message ID, not the chronological sequence.
    return item["time"], int(item["_realSeq"] or 0), item["_index"]


async def read_history(call: Callable[[str, dict], Awaitable[dict]], policy: dict, args: dict) -> dict:
    """Read one bounded page; paused sending does not disable authorized reads.

    ``hasMore`` is True only with observed older records, False for an empty
    page, and None when NapCat supplies no evidence either way. NapCat exposes
    no terminal-history flag, so a short page must not be called complete.
    """
    try:
        account, kind, target, limit = _validate(policy, args)
        cursor = _decode_cursor(args.get("cursor"), account, kind, target)
        params = {"user_id" if kind == "private" else "group_id": target,
                  "count": limit + 1 + int(cursor is not None), "reverse_order": cursor is not None,
                  "disable_get_url": True, "parse_mult_msg": False, "quick_reply": False}
        if cursor:
            # Despite its name, NapCat resolves message_seq as a short message ID.
            # getMsgsIncludeSelf needs reverse_order=True to move toward older records.
            params["message_seq"] = cursor["messageSeq"]
        try:
            response = await call("get_friend_msg_history" if kind == "private" else "get_group_msg_history", params)
        except Exception:
            raise _HistoryError("QQ 历史暂时无法读取，请检查连接后重试；未确认该会话的历史是否完整。") from None
        if (not isinstance(response, dict) or response.get("status") != "ok"
                or response.get("retcode") != 0 or not isinstance(response.get("data"), dict)
                or not isinstance(response["data"].get("messages"), list)):
            raise _HistoryError("QQ 未返回可用的历史记录；可能是记录不可用或连接异常，不能据此判断历史为空。")
        raw_messages = response["data"]["messages"]
        items, seen = [], set()
        for index, raw in enumerate(raw_messages):
            item = _message(raw, account, kind, target, index)
            if item['speakerRole'] == 'contact' and item['senderId'] in policy.get('owner_ids', []):
                item['speakerRole'] = 'owner'
            if cursor:
                if item["messageId"] == cursor["messageId"] or item["messageSeq"] == cursor["messageSeq"]:
                    continue
                if item["time"] > cursor["time"]:
                    continue
                if (item["time"] == cursor["time"] and item["_realSeq"] and cursor.get("realSeq")
                        and int(item["_realSeq"]) >= int(cursor["realSeq"])):
                    continue
            if item["messageId"] not in seen:
                seen.add(item["messageId"])
                items.append(item)
        items.sort(key=_sort_key)
        candidates = items[-limit:]
        selected, budget = [], MAX_TOTAL_TEXT
        for item in reversed(candidates):
            if budget == 0 and item["text"]:
                break
            if len(item["text"]) > budget:
                item = {**item, "text": item["text"][:budget], "textTruncated": True}
            selected.append(item)
            budget -= len(item["text"])
        selected.reverse()
        has_more = True if len(items) > len(selected) else None if selected else False
        next_cursor = _encode_cursor(account, kind, target, selected[0]) if selected else None
        if has_more and not next_cursor:
            has_more = None
        truncated = len(candidates) > len(selected) or any(item["textTruncated"] for item in selected)
        message = ("已返回最近一页 QQ 历史；可用游标继续读取更早记录。" if has_more
                   else "本页未发现可确认的更早记录，QQ 未提供历史结束标记，历史完整性未知。" if selected
                   else "本次未返回更早消息；这不代表该会话的完整历史已被读取。")
        if truncated:
            message += " 部分正文因长度限制已截断。"
            if len(candidates) > len(selected):
                message += " 较早的未返回消息可继续分页。"
        return {"ok": True, "account": account, "targetType": kind, "targetId": target,
                "messages": [{key: value for key, value in item.items() if not key.startswith("_")} for item in selected],
                "nextCursor": next_cursor, "hasMore": has_more, "truncated": truncated, "message": message}
    except _HistoryError as exc:
        return {"ok": False, "messages": [], "nextCursor": None, "hasMore": None, "message": str(exc)}
    except Exception:
        return {"ok": False, "messages": [], "nextCursor": None, "hasMore": None,
                "message": "QQ 历史读取未完成，请检查账号、会话和游标后重试。"}
