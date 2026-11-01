"""Small, account-isolated memory records about QQ participants.

These records are deliberately weaker than identity or authorization facts.  A
record is scoped to one managed QQ account, conversation and numeric user ID;
the source role and source identity remain visible so a caller can distinguish
an owner's statement from a participant's attributed claim or the assistant's
own inference.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any


_ID = re.compile(r"[0-9]{1,20}\Z")
_MESSAGE_ID = re.compile(r"[-A-Za-z0-9_:]{1,80}\Z")
_ROLES = {"owner_explicit", "person_statement", "assistant_inference"}
_CERTAINTIES = {"explicit", "attributed", "inferred"}
_KINDS = {"private", "group"}

MAX_VALUE = 240
MAX_TOPIC = 64
MAX_PER_PERSON = 12
MAX_TOTAL = 1200


def _id(value: Any) -> str:
    value = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    value = value.strip()
    return value if _ID.fullmatch(value) else ""


def _message_id(value: Any) -> str:
    value = str(value or "").strip()
    return value if _MESSAGE_ID.fullmatch(value) else ""


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def _topic_key(value: Any) -> str:
    return _clean(value, MAX_TOPIC).casefold()


def _kind(value: Any) -> str:
    value = str(value or "")
    return value if value in _KINDS else ""


def _scope_key(kind: str, target: str) -> tuple[str, str]:
    return (_kind(kind), _id(target))


def _role(value: Any) -> str:
    value = str(value or "").strip()
    return value if value in _ROLES else ""


def _certainty(source_role: str, value: Any) -> str:
    supplied = str(value or "").strip()
    if supplied in _CERTAINTIES:
        return supplied
    return {"owner_explicit": "explicit", "person_statement": "attributed",
            "assistant_inference": "inferred"}[source_role]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class PersonMemoryStore:
    """Atomic JSON storage for bounded, low-priority participant memories."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()

    def _path(self, account: str) -> Path:
        account = _id(account)
        if not account:
            raise ValueError("无效 QQ 账号")
        return self.root / (account + ".json")

    def _read(self, account: str) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._path(account).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        rows = raw.get("entries") if isinstance(raw, Mapping) else raw
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, Mapping) and self._valid_row(row)]

    @staticmethod
    def _valid_row(row: Mapping[str, Any]) -> bool:
        return bool(_id(row.get("userId")) and _kind(row.get("targetType"))
                    and _id(row.get("targetId")) and _topic_key(row.get("topic"))
                    and _clean(row.get("value"), MAX_VALUE)
                    and _role(row.get("sourceRole")))

    def _write(self, account: str, rows: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(account)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "entries": rows},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _memory_id(kind: str, target: str, user_id: str, topic: str) -> str:
        value = json.dumps([kind, target, user_id, _topic_key(topic)],
                           ensure_ascii=False, separators=(",", ":"))
        return "qq-person-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        fields = ("memoryId", "targetType", "targetId", "userId", "topic", "value",
                  "sourceRole", "certainty", "sourceSenderId", "sourceMessageId",
                  "sourceTime", "shared", "updatedAt")
        return {key: row[key] for key in fields if key in row}

    def _scope_match(self, row: Mapping[str, Any], kind: str | None,
                     target: str | None, include_shared: bool) -> bool:
        if kind is None and target is None:
            return bool(include_shared or not row.get("shared"))
        if _kind(kind) != row.get("targetType") or _id(target) != row.get("targetId"):
            return False
        return True

    def list(self, account: str, *, kind: str | None = None, target: str | None = None,
             user_id: str | None = None, include_shared: bool = False) -> list[dict[str, Any]]:
        wanted_user = _id(user_id) if user_id else ""
        with self._lock:
            rows = self._read(account)
        selected = [row for row in rows
                    if self._scope_match(row, kind, target, include_shared)
                    and (not wanted_user or row.get("userId") == wanted_user)]
        selected.sort(key=lambda row: (str(row.get("updatedAt") or ""), str(row.get("memoryId") or "")),
                     reverse=True)
        return [self._public(row) for row in selected]

    def by_people(self, account: str, kind: str, target: str, user_ids: list[str],
                  *, include_shared: bool = False, limit: int = 8) -> dict[str, list[dict[str, Any]]]:
        ids = {_id(value) for value in user_ids if _id(value)}
        result: dict[str, list[dict[str, Any]]] = {ident: [] for ident in ids}
        if not ids:
            return result
        rows = self.list(account, kind=kind, target=target, include_shared=include_shared)
        for row in rows:
            ident = row.get("userId")
            if ident in result and len(result[ident]) < max(1, limit):
                result[ident].append(row)
        return {ident: values for ident, values in result.items() if values}

    def remember(self, account: str, *, kind: str, target: str, user_id: str,
                 topic: str, value: str, source_role: str, source_sender_id: str = "",
                 source_message_id: str = "", source_time: Any = "", shared: bool = False,
                 certainty: str = "") -> dict[str, Any]:
        account = _id(account)
        kind, target, user_id = _kind(kind), _id(target), _id(user_id)
        topic, value, source_role = _clean(topic, MAX_TOPIC), _clean(value, MAX_VALUE), _role(source_role)
        source_sender_id, source_message_id = _id(source_sender_id), _message_id(source_message_id)
        if not account or not kind or not target or not user_id or not topic or not value or not source_role:
            raise ValueError("人物认知需要有效会话、userId、topic、value 和 sourceRole")
        if not isinstance(shared, bool):
            raise ValueError("shared 必须是布尔值")
        certainty = _certainty(source_role, certainty)
        if source_time in (None, ""):
            source_time = _now()
        with self._lock:
            rows = self._read(account)
            memory_id = self._memory_id(kind, target, user_id, topic)
            existing = next((row for row in rows if row.get("memoryId") == memory_id), None)
            if existing and existing.get("sourceRole") == "owner_explicit" and source_role != "owner_explicit":
                raise ValueError("主人明确记录受保护，不能被较低可信度认知覆盖")
            if not existing:
                count = sum(row.get("targetType") == kind and row.get("targetId") == target
                            and row.get("userId") == user_id for row in rows)
                if count >= MAX_PER_PERSON:
                    raise ValueError("该对象的人物认知已达上限，请先整理")
                if len(rows) >= MAX_TOTAL:
                    raise ValueError("人物认知总量已达上限，请先整理")
            row = {
                "memoryId": memory_id, "targetType": kind, "targetId": target, "userId": user_id,
                "topic": topic, "value": value, "sourceRole": source_role, "certainty": certainty,
                "sourceSenderId": source_sender_id, "sourceMessageId": source_message_id,
                "sourceTime": _clean(source_time, 40) if isinstance(source_time, str) else source_time,
                "shared": shared, "updatedAt": _now(),
            }
            if existing:
                rows[rows.index(existing)] = row
            else:
                rows.append(row)
            self._write(account, rows)
        return {"ok": True, "account": account, "targetType": kind, "targetId": target,
                "userId": user_id, "memory": self._public(row), "remembered": True, "sent": False}

    def forget(self, account: str, *, kind: str, target: str, user_id: str,
               topic: str = "", memory_id: str = "", source_sender_id: str = "",
               owner: bool = False) -> dict[str, Any]:
        account = _id(account)
        kind, target, user_id = _kind(kind), _id(target), _id(user_id)
        topic_key, source_sender_id = _topic_key(topic), _id(source_sender_id)
        memory_id = _clean(memory_id, 80)
        if not account or not kind or not target or not user_id or (not topic_key and not memory_id):
            raise ValueError("删除人物认知需要有效会话、userId 以及 topic 或 memoryId")
        with self._lock:
            rows = self._read(account)
            matched = [row for row in rows if row.get("targetType") == kind and row.get("targetId") == target
                       and row.get("userId") == user_id
                       and (row.get("memoryId") == memory_id if memory_id else _topic_key(row.get("topic")) == topic_key)]
            if not matched:
                return {"ok": True, "account": account, "targetType": kind, "targetId": target,
                        "userId": user_id, "forgotten": False, "sent": False}
            if not owner:
                if not source_sender_id or source_sender_id != user_id:
                    raise ValueError("普通 QQ 会话只能删除当前发言者自己的低可信度认知")
                if any(row.get("sourceRole") == "owner_explicit" for row in matched):
                    raise ValueError("主人明确记录只能由主人纠正或删除")
            rows = [row for row in rows if row not in matched]
            self._write(account, rows)
        return {"ok": True, "account": account, "targetType": kind, "targetId": target,
                "userId": user_id, "forgotten": True, "sent": False}
