"""书签动作的应用编排。

书签写入、版本检查、幂等回放、目的地学习和回执写入在同一 SQLite
连接上完成。安全 URL 解析、时间、数据库和回执由入口端口注入；模块不
反向依赖 server，也不承担书签列表等只读协议。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import json
import sqlite3
import urllib.parse
import uuid

from agent.fingerprints import legacy_persistent_fingerprint
from bookmark_store import (
    BookmarkAliasConflict,
    BookmarkVersionConflict,
    create_bookmark,
    delete_bookmark,
    read_bookmark,
    update_bookmark,
)
from tools.safe_web import SafeWebError


@dataclass(frozen=True)
class BookmarkActionPorts:
    """书签动作所需的窄端口；状态和事务仍归 bookmark_store/database。"""

    database_context: Callable[[], Any]
    validate_public_url: Callable[[str], Any]
    canonicalize_public_url: Callable[[str], str]
    learn_web_destination: Callable[..., dict[str, Any]]
    write_action_receipt: Callable[..., dict[str, Any]]
    now_iso: Callable[[], str]


class BookmarkActionService:
    """执行 create/update/delete 并返回原有公开协议。"""

    def __init__(self, ports: BookmarkActionPorts) -> None:
        self.ports = ports

    def manage(
            self,
            settings: dict[str, Any],
            args: dict[str, Any] | None = None,
            *,
            source_turn_id: str = "",
        ) -> dict[str, Any]:
        """Apply one schema-validated bookmark mutation with an action receipt."""

        del settings
        raw = dict(args) if isinstance(args, dict) else {}
        action = str(raw.get("action") or "create").strip().lower()
        if action == "save":
            action = "create"
        if action not in {"create", "update", "delete"}:
            return {"name": "bookmark_manage", "ok": False, "error": "bookmark_invalid_action"}
        bookmark_id = str(raw.get("bookmarkId") or raw.get("bookmark_id") or "").strip()
        name = " ".join(str(raw.get("name") or raw.get("alias") or "").split()).strip()
        if len(name) > 120:
            return {"name": "bookmark_manage", "ok": False, "error": "bookmark_name_too_long"}
        try:
            expected_version = max(0, int(raw.get("expectedVersion") or 0))
        except (TypeError, ValueError):
            return {"name": "bookmark_manage", "ok": False, "error": "bookmark_invalid_version"}
        if action == "create" and not name:
            return {"name": "bookmark_manage", "ok": False, "error": "bookmark_name_required"}
        if action in {"update", "delete"} and not bookmark_id:
            return {"name": "bookmark_manage", "ok": False, "error": "bookmark_id_required"}

        raw_url = str(raw.get("url") or "").strip()
        canonical_url = ""
        if action == "create" or raw_url:
            if not raw_url:
                return {"name": "bookmark_manage", "ok": False, "error": "bookmark_url_required"}
            try:
                parsed, _ = self.ports.validate_public_url(raw_url)
                canonical_url = self.ports.canonicalize_public_url(urllib.parse.urlunsplit(parsed))
            except SafeWebError as error:
                return {
                    "name": "bookmark_manage",
                    "ok": False,
                    "error": "bookmark_unsafe_url",
                    "message": str(error)[:300],
                }
        command = {
            "action": action,
            "bookmarkId": bookmark_id,
            "name": name,
            "url": canonical_url,
            "expectedVersion": expected_version,
        }
        material = json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        action_id = (
            "bookmark-action-" + legacy_persistent_fingerprint(
                f"{source_turn_id}\0{material}", length=32
            )
            if source_turn_id else f"bookmark-action-{uuid.uuid4().hex}"
        )
        bookmark: dict[str, Any] = {}
        before: dict[str, Any] = {}
        destination_knowledge: dict[str, Any] = {}
        try:
            with self.ports.database_context() as connection:
                replay_row = connection.execute(
                    "SELECT payload_json FROM action_receipts WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                if replay_row is not None:
                    replay = json.loads(str(replay_row["payload_json"] or "{}"))
                    if isinstance(replay, dict) and replay.get("status") == "accepted":
                        replay_bookmark = read_bookmark(
                            connection, str(replay.get("bookmarkId") or "")
                        )
                        return {
                            "name": "bookmark_manage", "ok": True,
                            "action": str(replay.get("action") or action),
                            "bookmark": replay_bookmark,
                            "actionReceipt": replay,
                            "replayed": True,
                            "source": "local_sqlite",
                        }
                if action == "create":
                    bookmark = create_bookmark(
                        connection,
                        name=name,
                        canonical_url=canonical_url,
                        source_turn_id=source_turn_id,
                    )
                else:
                    before = read_bookmark(connection, bookmark_id)
                    if not before or before.get("deletedAt"):
                        return {
                            "name": "bookmark_manage", "ok": False,
                            "error": "bookmark_not_found",
                        }
                    if action == "update":
                        changes: dict[str, Any] = {}
                        if "name" in raw or "alias" in raw:
                            changes["name"] = name
                        if "url" in raw:
                            changes["url"] = canonical_url
                        if not changes:
                            return {
                                "name": "bookmark_manage", "ok": False,
                                "error": "bookmark_no_changes",
                            }
                        bookmark = update_bookmark(
                            connection, bookmark_id, changes,
                            expected_version=expected_version,
                        )
                    else:
                        bookmark = delete_bookmark(
                            connection, bookmark_id,
                            expected_version=expected_version,
                        )
                if action in {"create", "update"}:
                    learned_name = str(bookmark.get("name") or name).strip()
                    learned_url = str(bookmark.get("url") or canonical_url).strip()
                    if learned_name and learned_url:
                        destination_knowledge = self.ports.learn_web_destination(
                            connection,
                            canonical_name=learned_name,
                            canonical_url=learned_url,
                            aliases=[learned_name],
                            source_kind="bookmark",
                            source_turn_id=source_turn_id,
                        )
                receipt = {
                    "actionId": action_id,
                    "tool": "bookmark_manage",
                    "status": "accepted",
                    "action": action,
                    "bookmarkId": str(bookmark.get("bookmarkId") or ""),
                    "name": str(bookmark.get("name") or name),
                    "resolvedTarget": str(bookmark.get("url") or canonical_url),
                    "versionBefore": int(before.get("version") or 0),
                    "versionAfter": int(bookmark.get("version") or 0),
                    "requestId": str(source_turn_id or "")[:160],
                    "timestamp": self.ports.now_iso(),
                }
                self.ports.write_action_receipt(receipt, connection=connection)
        except BookmarkAliasConflict as conflict:
            return {
                "name": "bookmark_manage", "ok": False,
                "error": "bookmark_alias_conflict",
                "currentBookmark": conflict.bookmark,
                "requiresRefresh": True,
            }
        except BookmarkVersionConflict as conflict:
            return {
                "name": "bookmark_manage", "ok": False,
                "error": "bookmark_version_conflict",
                "expectedVersion": conflict.expected_version,
                "currentBookmark": conflict.bookmark,
                "requiresRefresh": True,
            }
        except (sqlite3.Error, ValueError, json.JSONDecodeError) as error:
            return {
                "name": "bookmark_manage", "ok": False,
                "error": str(error) if isinstance(error, ValueError) else "bookmark_transaction_failed",
            }
        return {
            "name": "bookmark_manage",
            "ok": True,
            "action": action,
            "bookmark": bookmark,
            "destinationKnowledge": destination_knowledge,
            "actionReceipt": receipt,
            "source": "local_sqlite",
        }
