"""状态数据库的连接与 schema 初始化边界。

调用方传入已经解析好的数据库路径；本模块不读取 server 的路径或状态。
schema 初始化继续直接使用各领域模块的函数，保证事务连接和 schema 版本
由同一个数据库入口持有。
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Callable, Iterator
from pathlib import Path
import sqlite3

from agent.context_compatibility import ContextCompatibilityError
from agent.cognitive_state import ensure_schema as ensure_cognitive_schema
from bookmark_store import ensure_bookmark_schema
from schedule_service import ensure_schedule_schema
from todo_store import ensure_todo_schema
from web_destination_store import ensure_web_destination_schema


def open_state_database(path: Path) -> sqlite3.Connection:
    """打开状态库并初始化现有 schema；未来版本只读拒绝且不改原库。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    database_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if database_version > 2:
        connection.close()
        raise ContextCompatibilityError(
            f"Unsupported AMADEUS state database version: {database_version}；请使用更新版本打开，原数据库未修改。",
            code="context_version_too_new",
        )
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS prepared_turns (
            request_id TEXT PRIMARY KEY,
            route TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS research_states (
            request_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_actions (
            action_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS action_receipts (
            action_id TEXT PRIMARY KEY,
            canonical_target TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_turns (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            user_text TEXT NOT NULL,
            assistant_text TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_session_time
        ON conversation_turns(session_id, created_at);
        """
    )
    ensure_todo_schema(connection)
    ensure_schedule_schema(connection)
    ensure_bookmark_schema(connection)
    ensure_web_destination_schema(connection)
    ensure_cognitive_schema(connection)
    if database_version < 2:
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    return connection


@contextmanager
def state_database(
    open_connection: Callable[[], sqlite3.Connection],
) -> Iterator[sqlite3.Connection]:
    """用入口提供的连接工厂管理 commit/rollback 与关闭顺序。"""

    connection = open_connection()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


__all__ = ["open_state_database", "state_database"]
