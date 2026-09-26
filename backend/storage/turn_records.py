"""请求规划、外部动作收据和会话 turn 的 SQLite 记录规则。

所有数据库和时间依赖都由入口显式传入。带有 ``connection`` 参数的 writer
必须沿用外层事务连接；只有没有外传连接时才通过 ``database_context``
开启独立事务，避免 todo/bookmark/cognitive 提交出现半提交。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from agent.fingerprints import legacy_persistent_fingerprint
from agent.research_architecture import ResearchLoopState


DatabaseContext = Callable[[], AbstractContextManager[sqlite3.Connection]]
Cutoff = Callable[[int], str]
NowIso = Callable[[], str]
LegacyContextReader = Callable[[], Mapping[str, Any]]
TurnEpochReader = Callable[[dict[str, Any]], int]


def state_record_cutoff(seconds: int, *, now: Callable[[], datetime] | None = None) -> str:
    """按 UTC 时钟生成瞬态记录截止时间，TTL 下限保持原有 1 秒保护。"""

    clock = datetime.now if now is None else now
    current = clock(timezone.utc) if now is None else clock()
    return (current - timedelta(seconds=max(1, seconds))).isoformat(timespec="seconds")


def prune_transient_state_records(
    connection: sqlite3.Connection,
    *,
    cutoff: Cutoff,
    prepared_ttl_seconds: int,
    research_ttl_seconds: int,
) -> None:
    """回收请求规划状态，不触碰持久化审计记录。"""

    connection.execute(
        "DELETE FROM prepared_turns WHERE datetime(updated_at) < datetime(?)",
        (cutoff(prepared_ttl_seconds),),
    )
    connection.execute(
        "DELETE FROM research_states WHERE datetime(updated_at) < datetime(?)",
        (cutoff(research_ttl_seconds),),
    )


def clear_transient_turn_records(*, database_context: DatabaseContext) -> None:
    with database_context() as connection:
        connection.execute("DELETE FROM prepared_turns")
        connection.execute("DELETE FROM research_states")
        connection.execute("DELETE FROM pending_actions")


def write_prepared_turn_record(
    payload: dict[str, Any],
    *,
    database_context: DatabaseContext,
    now_iso: NowIso,
    prune: Callable[[sqlite3.Connection], None],
) -> None:
    request_id = str(payload.get("requestId") or "")
    if not request_id:
        return
    timestamp = now_iso()
    with database_context() as connection:
        prune(connection)
        connection.execute(
            """
            INSERT INTO prepared_turns(request_id, route, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                route=excluded.route,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                request_id,
                str(payload.get("route") or "fast_chat"),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                timestamp,
                timestamp,
            ),
        )


def read_prepared_turn_record(
    request_id: str,
    *,
    database_context: DatabaseContext,
    prune: Callable[[sqlite3.Connection], None],
) -> dict[str, Any]:
    normalized = str(request_id or "").strip()[:120]
    if not normalized:
        return {}
    with database_context() as connection:
        prune(connection)
        row = connection.execute(
            "SELECT payload_json FROM prepared_turns WHERE request_id = ?",
            (normalized,),
        ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_research_state_record(
    state: ResearchLoopState,
    *,
    database_context: DatabaseContext,
    now_iso: NowIso,
    prune: Callable[[sqlite3.Connection], None],
) -> dict[str, Any]:
    payload = {"stateVersion": 1, **state.to_dict()}
    request_id = str(state.request_id or "").strip()[:120]
    if not request_id:
        return payload
    timestamp = now_iso()
    with database_context() as connection:
        prune(connection)
        connection.execute(
            """
            INSERT INTO research_states(request_id, payload_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(request_id) DO UPDATE SET
                payload_json=excluded.payload_json,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                request_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                str(state.status or "planned"),
                timestamp,
                timestamp,
            ),
        )
    return payload


def read_research_state_record(
    request_id: str,
    *,
    database_context: DatabaseContext,
    prune: Callable[[sqlite3.Connection], None],
) -> ResearchLoopState | None:
    normalized = str(request_id or "").strip()[:120]
    if not normalized:
        return None
    with database_context() as connection:
        prune(connection)
        row = connection.execute(
            "SELECT payload_json FROM research_states WHERE request_id = ?",
            (normalized,),
        ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return ResearchLoopState.from_dict(value) if isinstance(value, dict) else None


def read_pending_action_record(*, database_context: DatabaseContext) -> dict[str, Any]:
    with database_context() as connection:
        row = connection.execute(
            "SELECT payload_json FROM pending_actions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_pending_action_record(
    payload: dict[str, Any],
    *,
    database_context: DatabaseContext,
    now_iso: NowIso,
) -> dict[str, Any]:
    action_id = str(payload.get("actionId") or "")
    if not action_id:
        return {}
    timestamp = now_iso()
    with database_context() as connection:
        connection.execute("DELETE FROM pending_actions WHERE action_id <> ?", (action_id,))
        connection.execute(
            """
            INSERT INTO pending_actions(action_id, payload_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(action_id) DO UPDATE SET
                payload_json=excluded.payload_json,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                action_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                str(payload.get("status") or ""),
                str(payload.get("createdAt") or timestamp),
                timestamp,
            ),
        )
    return payload


def delete_pending_action_record(
    action_id: str = "",
    *,
    database_context: DatabaseContext,
) -> None:
    with database_context() as connection:
        if action_id:
            connection.execute("DELETE FROM pending_actions WHERE action_id = ?", (action_id,))
        else:
            connection.execute("DELETE FROM pending_actions")


def write_action_receipt_record(
    receipt: dict[str, Any],
    *,
    connection: sqlite3.Connection | None = None,
    database_context: DatabaseContext,
    now_iso: NowIso,
) -> dict[str, Any]:
    action_id = str(receipt.get("actionId") or "")
    if not action_id:
        return {}
    timestamp = now_iso()
    canonical = str(receipt.get("resolvedTarget") or receipt.get("canonicalUrl") or "")

    def write(active_connection: sqlite3.Connection) -> None:
        active_connection.execute(
            """
            INSERT INTO action_receipts(action_id, canonical_target, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(action_id) DO UPDATE SET
                canonical_target=excluded.canonical_target,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                action_id,
                canonical,
                json.dumps(receipt, ensure_ascii=False, separators=(",", ":")),
                timestamp,
                timestamp,
            ),
        )

    # 外层连接属于 todo/bookmark/cognitive 的同一事务，不能在这里重新开库。
    if connection is not None:
        write(connection)
    else:
        with database_context() as active_connection:
            write(active_connection)
    return receipt


def _decode_dict(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        value = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_action_receipt_records(
    limit: int,
    *,
    database_context: DatabaseContext,
    read_legacy_context: LegacyContextReader,
) -> list[dict[str, Any]]:
    with database_context() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM action_receipts ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in reversed(rows):
        value = _decode_dict(row)
        if value is not None:
            result.append(value)
    legacy_context = read_legacy_context()
    legacy = (
        legacy_context.get("actionReceipts")
        if isinstance(legacy_context.get("actionReceipts"), list)
        else []
    )
    merged: dict[str, dict[str, Any]] = {}
    for item in [*legacy, *result]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("actionId") or item.get("resolvedTarget") or "")
        if key:
            merged[key] = dict(item)
    return list(merged.values())[-max(1, int(limit)):]


def find_action_receipt_by_idempotency_key(
    idempotency_key: str,
    *,
    database_context: DatabaseContext,
) -> dict[str, Any] | None:
    """按幂等键寻找已完成动作，恢复路径不依赖易变 action id。"""

    key = str(idempotency_key or "").strip()
    if not key:
        return None
    with database_context() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM action_receipts ORDER BY updated_at DESC"
        ).fetchall()
    for row in rows:
        value = _decode_dict(row)
        if (
            value is not None
            and str(value.get("idempotencyKey") or "") == key
            and str(value.get("status") or "") == "accepted"
        ):
            return value
    return None


def write_conversation_turn_record(
    turn: dict[str, Any],
    *,
    connection: sqlite3.Connection | None = None,
    database_context: DatabaseContext,
    now_iso: NowIso,
) -> None:
    timestamp = str(turn.get("at") or now_iso())
    stable_material = "\0".join([
        timestamp,
        str(turn.get("user") or ""),
        str(turn.get("assistant") or ""),
        str(turn.get("proactiveEventId") or ""),
    ])
    turn_id = str(
        turn.get("turnId")
        or "turn_" + legacy_persistent_fingerprint(stable_material, length=24)
    )
    metadata = {
        key: value
        for key, value in turn.items()
        if key not in {"user", "assistant"} and not str(key).startswith("_")
    }

    def write(active_connection: sqlite3.Connection) -> None:
        active_connection.execute(
            """
            INSERT OR IGNORE INTO conversation_turns(
                turn_id, session_id, created_at, user_text, assistant_text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id,
                str(turn.get("sessionId") or "default"),
                timestamp,
                str(turn.get("user") or ""),
                str(turn.get("assistant") or ""),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    # 外层连接属于会话提交事务，失败时由调用方统一回滚所有记录。
    if connection is not None:
        write(connection)
    else:
        with database_context() as active_connection:
            write(active_connection)


def read_conversation_turn_records(
    limit: int,
    *,
    epoch: int | None,
    path_provider: Callable[[], Path],
    database_context: DatabaseContext,
    turn_epoch: TurnEpochReader,
) -> list[dict[str, Any]]:
    path = path_provider()
    if not path.exists():
        return []
    with database_context() as connection:
        rows = connection.execute(
            """
            SELECT turn_id, created_at, user_text, assistant_text, metadata_json
            FROM conversation_turns
            WHERE session_id = 'default'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    turns: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        turns.append({
            **(metadata if isinstance(metadata, dict) else {}),
            "turnId": str(row["turn_id"]),
            "at": str(row["created_at"]),
            "user": str(row["user_text"]),
            "assistant": str(row["assistant_text"]),
        })
    if epoch is not None:
        turns = [item for item in turns if turn_epoch(item) == max(0, int(epoch))]
    return turns[-max(1, int(limit)):]


def clear_conversation_turn_records(*, database_context: DatabaseContext) -> None:
    with database_context() as connection:
        connection.execute("DELETE FROM conversation_turns WHERE session_id = 'default'")


def conversation_turn_record_count(
    *,
    path_provider: Callable[[], Path],
    database_context: DatabaseContext,
) -> int:
    path = path_provider()
    if not path.exists():
        return 0
    with database_context() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM conversation_turns WHERE session_id = 'default'"
        ).fetchone()
    return int(row["count"] if row is not None else 0)


__all__ = [
    "conversation_turn_record_count",
    "clear_conversation_turn_records",
    "clear_transient_turn_records",
    "delete_pending_action_record",
    "find_action_receipt_by_idempotency_key",
    "prune_transient_state_records",
    "read_action_receipt_records",
    "read_conversation_turn_records",
    "read_pending_action_record",
    "read_prepared_turn_record",
    "read_research_state_record",
    "state_record_cutoff",
    "write_action_receipt_record",
    "write_conversation_turn_record",
    "write_pending_action_record",
    "write_prepared_turn_record",
    "write_research_state_record",
]
