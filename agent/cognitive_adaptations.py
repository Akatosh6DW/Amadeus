"""Transactional SQLite storage for user cognitive adaptations.

This module stores complete adaptation snapshots.  It deliberately leaves the
meaning of a record (and whether a proposed record may become active) to the
caller.  The storage layer only validates the shape needed to make a snapshot
safe to serialize and address by ``adaptationId``.

The caller owns the SQLite transaction.  None of the public functions commit,
roll back, or close the supplied connection, so a snapshot and its audit row
participate in the caller's transaction as one unit.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import sqlite3
from typing import Any


CURRENT_TABLE = "cognitive_user_adaptations"
AUDIT_TABLE = "cognitive_user_adaptation_audit"

# These bounds keep a malformed/model-produced payload from turning a small
# SQLite database into an unbounded JSON store.  They are intentionally shape
# bounds; semantic validation belongs to the parent cognitive pipeline.
MAX_IDENTIFIER_LENGTH = 256
MAX_RECORDS = 512
MAX_JSON_BYTES = 1_000_000
MAX_JSON_DEPTH = 32
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


class UserAdaptationError(ValueError):
    """Base class for validation, conflict, or storage errors."""


class UserAdaptationValidationError(UserAdaptationError):
    """The requested scope, revision, or snapshot has an invalid shape."""


class UserAdaptationStorageError(UserAdaptationError):
    """The database contains a malformed snapshot written outside this API."""


class UserAdaptationConflict(UserAdaptationError):
    """The requested commit cannot be applied without changing its identity."""


class UserAdaptationRevisionConflict(UserAdaptationConflict):
    """The caller attempted to write from a stale scope/generation revision."""

    def __init__(
        self,
        *,
        scope_id: str,
        memory_generation: int,
        expected_revision: int,
        actual_revision: int,
    ) -> None:
        self.scope_id = scope_id
        self.memory_generation = memory_generation
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        # These aliases make the conflict useful to callers that use the
        # terminology from the surrounding cognitive commit protocol.
        self.base_revision = expected_revision
        self.current_revision = actual_revision
        super().__init__("user_adaptation_revision_conflict")


class UserAdaptationCommitConflict(UserAdaptationConflict):
    """A commit id was reused with another scope, generation, or snapshot."""

    def __init__(
        self,
        *,
        commit_id: str,
        scope_id: str,
        memory_generation: int,
        reason: str = "content",
    ) -> None:
        self.commit_id = commit_id
        self.scope_id = scope_id
        self.memory_generation = memory_generation
        self.reason = reason
        super().__init__("user_adaptation_commit_conflict")


# Concise aliases keep the module easy to adopt from code that names the
# protocol "cognitive adaptations" rather than "user adaptations".
CognitiveAdaptationError = UserAdaptationError
CognitiveAdaptationValidationError = UserAdaptationValidationError
CognitiveAdaptationStorageError = UserAdaptationStorageError
CognitiveAdaptationConflict = UserAdaptationConflict
CognitiveAdaptationRevisionConflict = UserAdaptationRevisionConflict
CognitiveAdaptationCommitConflict = UserAdaptationCommitConflict


_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS {CURRENT_TABLE} (
        scope_id TEXT NOT NULL,
        memory_generation INTEGER NOT NULL CHECK(memory_generation >= 0),
        revision INTEGER NOT NULL CHECK(revision >= 0),
        records_json TEXT NOT NULL,
        updated_commit_id TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(scope_id, memory_generation)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope_id TEXT NOT NULL,
        memory_generation INTEGER NOT NULL CHECK(memory_generation >= 0),
        revision INTEGER NOT NULL CHECK(revision >= 0),
        base_revision INTEGER NOT NULL CHECK(base_revision >= 0),
        commit_id TEXT NOT NULL UNIQUE,
        records_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(scope_id, memory_generation, revision)
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_{AUDIT_TABLE}_scope_revision
    ON {AUDIT_TABLE}(scope_id, memory_generation, revision)
    """,
)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the current-snapshot and audit tables without owning a commit.

    Each DDL statement is executed independently.  In particular, this does
    not use ``executescript`` because that API may commit an open transaction
    before running a script.
    """

    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)


def _validation(message: str) -> UserAdaptationValidationError:
    return UserAdaptationValidationError(message)


def _text_identifier(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise _validation(f"{name}_must_be_text")
    if not value or not value.strip():
        raise _validation(f"{name}_required")
    if "\x00" in value:
        raise _validation(f"{name}_contains_nul")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise _validation(f"{name}_too_long")
    return value


def _nonnegative_int(value: Any, *, name: str, allow_max: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _validation(f"{name}_must_be_integer")
    maximum = MAX_SQLITE_INTEGER if allow_max else MAX_SQLITE_INTEGER - 1
    if value < 0 or value > maximum:
        raise _validation(f"{name}_out_of_range")
    return value


def _clone_json(value: Any, *, depth: int = 0) -> Any:
    """Return a detached JSON-shaped value or reject it.

    Doing this before serialization means nested mutable objects supplied by a
    caller are never retained by the store and unsupported Python values do
    not get stringified into a misleading snapshot.
    """

    if depth > MAX_JSON_DEPTH:
        raise _validation("adaptation_payload_too_deep")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and "\x00" in value:
            raise _validation("adaptation_payload_contains_nul")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _validation("adaptation_payload_non_finite_number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _validation("adaptation_payload_keys_must_be_text")
            if "\x00" in key:
                raise _validation("adaptation_payload_key_contains_nul")
            result[key] = _clone_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_RECORDS * 4:
            raise _validation("adaptation_payload_list_too_large")
        return [_clone_json(item, depth=depth + 1) for item in value]
    raise _validation("adaptation_payload_must_be_json")


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise _validation("adaptation_payload_must_be_json") from error
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise _validation("adaptation_payload_too_large")
    return encoded


def _normalize_records(records: Any) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(records, list):
        raise _validation("adaptation_records_must_be_list")
    if len(records) > MAX_RECORDS:
        raise _validation("adaptation_records_too_many")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(records):
        value = _clone_json(record)
        if not isinstance(value, dict):
            raise _validation(f"adaptation_record_{index}_must_be_object")
        adaptation_id = value.get("adaptationId")
        if not isinstance(adaptation_id, str) or not adaptation_id.strip():
            raise _validation(f"adaptation_record_{index}_id_required")
        if len(adaptation_id) > MAX_IDENTIFIER_LENGTH:
            raise _validation(f"adaptation_record_{index}_id_too_long")
        if adaptation_id in seen_ids:
            raise _validation("adaptation_ids_must_be_unique")
        seen_ids.add(adaptation_id)
        # ``kind`` and ``status`` are intentionally not restricted to a
        # vocabulary here.  The parent chain owns semantic and authorization
        # checks; storage only prevents values that cannot be represented as
        # normal JSON strings.
        for field in ("kind", "status"):
            if field in value and not isinstance(value[field], str):
                raise _validation(f"adaptation_record_{index}_{field}_must_be_text")
        normalized.append(value)

    return normalized, _canonical_json(normalized)


def _decode_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, str):
        raise UserAdaptationStorageError("stored_adaptation_snapshot_not_text")
    try:
        decoded = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise UserAdaptationStorageError("stored_adaptation_snapshot_invalid_json") from error
    try:
        normalized, canonical = _normalize_records(decoded)
    except UserAdaptationValidationError as error:
        raise UserAdaptationStorageError("stored_adaptation_snapshot_invalid") from error
    if canonical != payload:
        raise UserAdaptationStorageError("stored_adaptation_snapshot_not_canonical")
    return normalized


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _current_row(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    memory_generation: int,
) -> tuple[int, str, str] | None:
    row = connection.execute(
        f"SELECT revision, records_json, updated_commit_id FROM {CURRENT_TABLE} "
        "WHERE scope_id = ? AND memory_generation = ?",
        (scope_id, memory_generation),
    ).fetchone()
    if row is None:
        return None
    try:
        revision = int(row[0])
        records_json = str(row[1])
        updated_commit_id = str(row[2])
    except (TypeError, ValueError, IndexError) as error:
        raise UserAdaptationStorageError("stored_adaptation_current_row_invalid") from error
    if revision < 0 or revision > MAX_SQLITE_INTEGER:
        raise UserAdaptationStorageError("stored_adaptation_revision_invalid")
    # Validate the current payload while it is being used as a CAS source.
    _decode_records(records_json)
    return revision, records_json, updated_commit_id


def _audit_row(
    connection: sqlite3.Connection,
    *,
    commit_id: str,
) -> tuple[str, int, int, str] | None:
    row = connection.execute(
        f"SELECT scope_id, memory_generation, revision, records_json "
        f"FROM {AUDIT_TABLE} WHERE commit_id = ?",
        (commit_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        scope_id = str(row[0])
        generation = int(row[1])
        revision = int(row[2])
        records_json = str(row[3])
    except (TypeError, ValueError, IndexError) as error:
        raise UserAdaptationStorageError("stored_adaptation_audit_row_invalid") from error
    if generation < 0 or revision < 0:
        raise UserAdaptationStorageError("stored_adaptation_audit_revision_invalid")
    _decode_records(records_json)
    return scope_id, generation, revision, records_json


def load_user_adaptations(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    memory_generation: int,
) -> dict[str, Any]:
    """Load the current complete snapshot for one scope and memory generation."""

    normalized_scope = _text_identifier(scope_id, name="scope_id")
    normalized_generation = _nonnegative_int(memory_generation, name="memory_generation")
    ensure_schema(connection)
    current = _current_row(
        connection,
        scope_id=normalized_scope,
        memory_generation=normalized_generation,
    )
    if current is None:
        return {"revision": 0, "records": []}
    revision, records_json, _ = current
    return {"revision": revision, "records": _decode_records(records_json)}


def commit_user_adaptations(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    memory_generation: int,
    base_revision: int,
    commit_id: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """CAS-commit a complete snapshot and append its immutable audit row.

    A previously seen ``commit_id`` is checked before the base revision.  This
    is what makes an exact replay idempotent even when a later commit has
    advanced the current revision.
    """

    normalized_scope = _text_identifier(scope_id, name="scope_id")
    normalized_generation = _nonnegative_int(memory_generation, name="memory_generation")
    normalized_base = _nonnegative_int(
        base_revision,
        name="base_revision",
        allow_max=False,
    )
    normalized_commit = _text_identifier(commit_id, name="commit_id")
    normalized_records, records_json = _normalize_records(records)
    # Keep the local normalized value alive through the transaction.  It also
    # documents that caller-owned ``records`` is never altered by this call.
    del normalized_records

    ensure_schema(connection)

    prior_commit = _audit_row(connection, commit_id=normalized_commit)
    if prior_commit is not None:
        prior_scope, prior_generation, prior_revision, prior_records_json = prior_commit
        if (
            prior_scope != normalized_scope
            or prior_generation != normalized_generation
        ):
            raise UserAdaptationCommitConflict(
                commit_id=normalized_commit,
                scope_id=normalized_scope,
                memory_generation=normalized_generation,
                reason="scope_or_generation",
            )
        if prior_records_json != records_json:
            raise UserAdaptationCommitConflict(
                commit_id=normalized_commit,
                scope_id=normalized_scope,
                memory_generation=normalized_generation,
                reason="content",
            )
        return {"status": "idempotent", "revision": prior_revision}

    current = _current_row(
        connection,
        scope_id=normalized_scope,
        memory_generation=normalized_generation,
    )
    actual_revision = current[0] if current is not None else 0
    if actual_revision != normalized_base:
        raise UserAdaptationRevisionConflict(
            scope_id=normalized_scope,
            memory_generation=normalized_generation,
            expected_revision=normalized_base,
            actual_revision=actual_revision,
        )

    next_revision = normalized_base + 1
    timestamp = _now_iso()
    if current is None:
        try:
            connection.execute(
                f"""INSERT INTO {CURRENT_TABLE}(
                    scope_id, memory_generation, revision, records_json,
                    updated_commit_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    normalized_scope,
                    normalized_generation,
                    next_revision,
                    records_json,
                    normalized_commit,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as error:
            # A concurrent writer may have materialized this scope between
            # the read and the insert.  Re-read it and report a normal CAS
            # conflict rather than leaking an implementation-level error.
            after_insert = _current_row(
                connection,
                scope_id=normalized_scope,
                memory_generation=normalized_generation,
            )
            if after_insert is None:
                raise UserAdaptationStorageError("adaptation_snapshot_insert_failed") from error
            raise UserAdaptationRevisionConflict(
                scope_id=normalized_scope,
                memory_generation=normalized_generation,
                expected_revision=normalized_base,
                actual_revision=after_insert[0],
            ) from error
    else:
        updated = connection.execute(
            f"UPDATE {CURRENT_TABLE} SET revision = ?, records_json = ?, "
            "updated_commit_id = ?, updated_at = ? "
            "WHERE scope_id = ? AND memory_generation = ? AND revision = ?",
            (
                next_revision,
                records_json,
                normalized_commit,
                timestamp,
                normalized_scope,
                normalized_generation,
                normalized_base,
            ),
        )
        if updated.rowcount != 1:
            after_update = _current_row(
                connection,
                scope_id=normalized_scope,
                memory_generation=normalized_generation,
            )
            actual_after_update = after_update[0] if after_update is not None else 0
            raise UserAdaptationRevisionConflict(
                scope_id=normalized_scope,
                memory_generation=normalized_generation,
                expected_revision=normalized_base,
                actual_revision=actual_after_update,
            )

    try:
        connection.execute(
            f"""INSERT INTO {AUDIT_TABLE}(
                scope_id, memory_generation, revision, base_revision,
                commit_id, records_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized_scope,
                normalized_generation,
                next_revision,
                normalized_base,
                normalized_commit,
                records_json,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError as error:
        # The caller owns the surrounding transaction and can roll it back;
        # surface this as a protocol conflict instead of hiding a partial
        # transaction behind a generic sqlite exception.
        raise UserAdaptationCommitConflict(
            commit_id=normalized_commit,
            scope_id=normalized_scope,
            memory_generation=normalized_generation,
            reason="audit_identity",
        ) from error

    return {"status": "committed", "revision": next_revision}


__all__ = [
    "AUDIT_TABLE",
    "CURRENT_TABLE",
    "CognitiveAdaptationCommitConflict",
    "CognitiveAdaptationConflict",
    "CognitiveAdaptationError",
    "CognitiveAdaptationRevisionConflict",
    "CognitiveAdaptationStorageError",
    "CognitiveAdaptationValidationError",
    "UserAdaptationCommitConflict",
    "UserAdaptationConflict",
    "UserAdaptationError",
    "UserAdaptationRevisionConflict",
    "UserAdaptationStorageError",
    "UserAdaptationValidationError",
    "commit_user_adaptations",
    "ensure_schema",
    "load_user_adaptations",
]
