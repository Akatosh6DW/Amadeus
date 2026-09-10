"""Durable cognitive turn commits and retryable non-SQLite projections.

The state reducer is deliberately injected (the cognitive state module owns
its schema and validation).  This module owns the transaction boundary around
that reducer, the conversation turn row, pending-action invalidations and the
outbox used by projections which cannot participate in the SQLite transaction.

Nothing in this module commits an injected reducer's connection.  Callers may
use it inside the existing ``state_database`` context manager, which commits
the whole unit or rolls it back as one SQLite transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from agent.fingerprints import runtime_fingerprint
from agent.cognitive_json_store import ensure_json_schema, store_json, load_json
import importlib
import json
import sqlite3
import time
from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping, Sequence


COMMIT_SCHEMA_VERSION = 2
OUTBOX_LEASE_SECONDS = 60.0
# Logical-document headroom; physical records split automatically at 128 KB.
MAX_INTERNAL_JSON_BYTES = 2_000_000
MAX_PROJECTION_JSON_BYTES = 2_000_000


class CognitiveCommitError(RuntimeError):
    """Base error for a cognitive commit which must not be reported as success."""


class CognitiveStateUnavailable(CognitiveCommitError):
    """The validated overlay has no available state reducer."""


class CognitiveCommitConflict(CognitiveCommitError):
    """A turn id was already committed with a different overlay."""


class CognitiveProjectionError(CognitiveCommitError):
    """One or more post-commit projections need retry."""


@dataclass(frozen=True)
class CognitiveCommitResult:
    commit_id: str
    conversation_id: str
    turn_id: str
    state_revision: int
    state: Mapping[str, Any]
    outbox_ids: tuple[str, ...]
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "commitId": self.commit_id,
            "conversationId": self.conversation_id,
            "turnId": self.turn_id,
            "stateRevision": self.state_revision,
            "outboxIds": list(self.outbox_ids),
            "idempotent": self.idempotent,
        }


@dataclass(frozen=True)
class ProjectionFailure:
    outbox_id: str
    kind: str
    error: str


@dataclass(frozen=True)
class ProjectionReport:
    applied: tuple[str, ...]
    failed: tuple[ProjectionFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any, *, maximum: int = MAX_INTERNAL_JSON_BYTES, payload_name: str = "internal") -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    size = len(encoded.encode("utf-8"))
    if size > maximum:
        sections = sorted(((str(key), len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
                           for key, item in value.items()), key=lambda item: item[1], reverse=True)[:5] if isinstance(value, Mapping) else []
        raise CognitiveCommitError(
            f"cognitive payload exceeds bounded storage size: payload={payload_name}, bytes={size}, "
            f"limit={maximum}, sectionBytes={json.dumps(sections, ensure_ascii=False)}")
    return encoded


def _stored_json(connection: sqlite3.Connection, value: Any, *, payload_name: str,
                 maximum: int = MAX_INTERNAL_JSON_BYTES) -> str:
    encoded = _canonical(value, maximum=maximum, payload_name=payload_name)
    return store_json(connection, value, encoded=encoded)


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _public_projection(value: Any, *, depth: int = 0) -> Any:
    """Remove private/raw model fields before archive or outbox projection.

    Cognitive trace/state remains in its private SQLite table.  The outbox
    carries only the small data a concrete projector needs; private keys are
    never copied into the conversation archive or client-facing result.
    """

    if depth > 8:
        return "<depth-limited>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key.startswith("_"):
                continue
            result[key[:120]] = _public_projection(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_projection(item, depth=depth + 1) for item in list(value)[:128]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 8_000:
            return value[:8_000]
        return value
    return str(value)[:8_000]


def _overlay_for_storage(overlay: Mapping[str, Any]) -> dict[str, Any]:
    value = _public_projection(overlay)
    if not isinstance(value, dict):
        raise CognitiveCommitError("cognitive overlay must be an object")
    patches = value.get("patches")
    if not isinstance(patches, list):
        raise CognitiveCommitError("cognitive overlay patches must be a list")
    if any(not isinstance(item, Mapping) for item in patches):
        raise CognitiveCommitError("cognitive overlay patches must contain objects")
    value["patches"] = [dict(item) for item in patches[:64]]
    invalidated = value.get("invalidatedPendingActionIds")
    if invalidated is None:
        invalidated = []
    if not isinstance(invalidated, list):
        raise CognitiveCommitError("invalidatedPendingActionIds must be a list")
    value["invalidatedPendingActionIds"] = list(dict.fromkeys(
        str(item).strip()[:160] for item in invalidated if str(item).strip()
    ))[:64]
    turn_id = str(value.get("turnId") or "").strip()
    if not turn_id:
        raise CognitiveCommitError("cognitive overlay requires turnId")
    value["turnId"] = turn_id[:180]
    base_revision = _safe_int(value.get("baseRevision"), -1)
    if base_revision < 0:
        raise CognitiveCommitError("cognitive overlay requires nonnegative baseRevision")
    value["baseRevision"] = base_revision
    value["rootSnapshotId"] = str(value.get("rootSnapshotId") or "").strip()[:180]
    raw_state = value.get("state")
    if isinstance(raw_state, Mapping):
        value["state"] = _public_projection(raw_state)
    elif raw_state is not None:
        value["state"] = str(raw_state).strip()[:80]
    return value


def _turn_id(turn: Mapping[str, Any]) -> str:
    value = str(turn.get("turnId") or "").strip()
    if not value:
        raise CognitiveCommitError("conversation turn requires turnId")
    return value[:180]


def _conversation_id(turn: Mapping[str, Any], overlay: Mapping[str, Any]) -> str:
    value = str(
        turn.get("conversationId")
        or overlay.get("conversationId")
        or turn.get("sessionId")
        or ""
    ).strip()
    if not value:
        raise CognitiveCommitError("cognitive turn requires conversationId")
    return value[:180]


def has_revision_overlay(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    patches = value.get("patches")
    return isinstance(patches, list) and any(isinstance(item, Mapping) for item in patches)


def has_cognitive_overlay(value: Any) -> bool:
    """Return whether a cognitive envelope exists, including ``patches=[]``."""

    return isinstance(value, Mapping) and isinstance(value.get("patches"), list)


def _commit_id(conversation_id: str, turn_id: str, overlay: Mapping[str, Any]) -> str:
    material = "\0".join((conversation_id, turn_id, _canonical(overlay)))
    return "cog_" + runtime_fingerprint(material, length=40)


def ensure_commit_schema(connection: sqlite3.Connection) -> None:
    """Create commit/outbox tables without committing the caller's work.

    ``executescript`` is intentionally avoided: sqlite3 may implicitly commit
    an open transaction before executing a script.  Each DDL statement is
    issued separately so the caller can start its transaction afterwards.
    """

    statements = (
        """
        CREATE TABLE IF NOT EXISTS cognitive_turn_commits (
            commit_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            root_snapshot_id TEXT NOT NULL DEFAULT '',
            base_revision INTEGER NOT NULL,
            state_revision INTEGER NOT NULL,
            overlay_json TEXT NOT NULL,
            digest_json TEXT NOT NULL,
            trace_json TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, turn_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cognitive_pending_invalidations (
            commit_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            prior_payload_json TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(commit_id, action_id),
            FOREIGN KEY(commit_id) REFERENCES cognitive_turn_commits(commit_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cognitive_outbox (
            outbox_id TEXT PRIMARY KEY,
            commit_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            projection_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(commit_id, kind, projection_key),
            FOREIGN KEY(commit_id) REFERENCES cognitive_turn_commits(commit_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_cognitive_outbox_pending
        ON cognitive_outbox(status, lease_until, created_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_turns (
            turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            user_text TEXT NOT NULL,
            assistant_text TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pending_actions (
            action_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS action_receipts (
            action_id TEXT PRIMARY KEY,
            canonical_target TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)
    ensure_json_schema(connection)


def _state_backend(state_backend: Any | None) -> Any:
    if state_backend is not None:
        return state_backend
    try:
        return importlib.import_module("agent.cognitive_state")
    except (ImportError, ModuleNotFoundError) as error:
        raise CognitiveStateUnavailable(
            "validated cognitive overlay has no agent.cognitive_state backend"
        ) from error


def _state_dict(state: Any) -> dict[str, Any]:
    if isinstance(state, Mapping):
        nested = state.get("state")
        if isinstance(nested, Mapping):
            return _public_projection(nested)
        return _public_projection(state)
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return _public_projection(value)
    raise CognitiveCommitError("cognitive state reducer returned a non-object")


def _insert_turn_row(connection: sqlite3.Connection, turn: Mapping[str, Any]) -> None:
    metadata = {
        key: _public_projection(value)
        for key, value in turn.items()
        if key not in {"user", "assistant"} and not str(key).startswith("_")
    }
    existing = connection.execute(
        "SELECT metadata_json FROM conversation_turns WHERE turn_id = ?",
        (_turn_id(turn),),
    ).fetchone()
    if existing is not None:
        try:
            existing_metadata = json.loads(str(existing[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise CognitiveCommitError("stored conversation turn metadata is invalid") from error
        existing_commit_id = (
            str(existing_metadata.get("cognitiveCommitId") or "")
            if isinstance(existing_metadata, Mapping) else ""
        )
        requested_commit_id = str(turn.get("cognitiveCommitId") or "")
        if existing_commit_id != requested_commit_id:
            raise CognitiveCommitConflict(
                f"conversation turn {turn.get('turnId')} already exists without this cognitive commit"
            )
        return
    connection.execute(
        """
        INSERT OR IGNORE INTO conversation_turns(
            turn_id, session_id, created_at, user_text, assistant_text, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _turn_id(turn),
            str(turn.get("sessionId") or "default")[:180],
            str(turn.get("at") or _now_iso())[:80],
            str(turn.get("user") or ""),
            str(turn.get("assistant") or ""),
            _canonical(metadata, maximum=MAX_PROJECTION_JSON_BYTES, payload_name="turn_metadata"),
        ),
    )


def _pending_ids(overlay: Mapping[str, Any], extra: Iterable[str] | None) -> tuple[str, ...]:
    values = [
        *(overlay.get("invalidatedPendingActionIds") or []),
        *(extra or ()),
    ]
    return tuple(dict.fromkeys(
        str(item).strip()[:160] for item in values if str(item).strip()
    ))[:64]


def _invalidate_pending_actions(
    connection: sqlite3.Connection,
    *,
    commit_id: str,
    action_ids: Sequence[str],
    created_at: str,
) -> None:
    for action_id in action_ids:
        row = connection.execute(
            "SELECT payload_json FROM pending_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        prior_payload = str(row[0]) if row is not None else ""
        connection.execute(
            """
            INSERT OR IGNORE INTO cognitive_pending_invalidations(
                commit_id, action_id, prior_payload_json, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (commit_id, action_id, prior_payload, created_at),
        )
        # Delete the executable pending record.  The action receipt table is
        # intentionally untouched: a completed side effect remains an audit
        # fact and cannot be rewritten by a later cognitive correction.
        connection.execute(
            "DELETE FROM pending_actions WHERE action_id = ?",
            (action_id,),
        )


def _projection_payloads(
    commit_id: str,
    *,
    turn: Mapping[str, Any],
    requested: Mapping[str, Any] | None,
) -> list[tuple[str, str, dict[str, Any]]]:
    values: list[tuple[str, str, dict[str, Any]]] = [(
        "conversation_archive",
        "turn",
        {"turn": _public_projection(turn)},
    )]
    aliases = {
        "archive": "conversation_archive",
        "conversation_archive": "conversation_archive",
        "context": "conversation_context",
        "conversation_context": "conversation_context",
        "memory": "memory_records",
        "memory_records": "memory_records",
        "relationship": "relationship_state",
        "relationship_state": "relationship_state",
    }
    for raw_kind, raw_payload in (requested or {}).items():
        kind = aliases.get(str(raw_kind).strip().lower())
        if not kind or kind == "conversation_archive" or not isinstance(raw_payload, Mapping):
            continue
        payload = _public_projection(raw_payload)
        if not isinstance(payload, dict):
            continue
        projection_key = str(payload.pop("projectionKey", "") or kind).strip()[:160] or kind
        values.append((kind, projection_key, payload))
    return values


def enqueue_projection(
    connection: sqlite3.Connection,
    *,
    commit_id: str,
    kind: str,
    payload: Mapping[str, Any],
    projection_key: str | None = None,
) -> str:
    """Append one idempotent projection intent to an existing commit."""

    normalized_kind = str(kind or "").strip().lower()
    if not normalized_kind:
        raise CognitiveCommitError("projection kind is required")
    key = str(projection_key or normalized_kind).strip()[:160] or normalized_kind
    safe_payload = _public_projection(payload)
    if not isinstance(safe_payload, dict):
        raise CognitiveCommitError("projection payload must be an object")
    payload_json = _canonical(
        safe_payload,
        maximum=MAX_PROJECTION_JSON_BYTES,
        payload_name="projection:" + normalized_kind,
    )
    existing = connection.execute(
        """
        SELECT outbox_id, payload_json, status
        FROM cognitive_outbox
        WHERE commit_id = ? AND kind = ? AND projection_key = ?
        """,
        (commit_id, normalized_kind, key),
    ).fetchone()
    if existing is not None:
        if _canonical(load_json(connection, str(existing[1])), maximum=MAX_PROJECTION_JSON_BYTES,
                      payload_name="stored_projection:" + normalized_kind) != payload_json:
            raise CognitiveCommitConflict(
                f"projection payload changed for {normalized_kind}:{key}"
            )
        return str(existing[0])
    outbox_id = "out_" + runtime_fingerprint("\0".join((commit_id, normalized_kind, key)), length=40)
    now = _now_iso()
    payload_json = store_json(connection, safe_payload, encoded=payload_json)
    connection.execute(
        """
        INSERT INTO cognitive_outbox(
            outbox_id, commit_id, kind, projection_key, payload_json,
            status, attempts, lease_until, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, '', ?, ?)
        """,
        (outbox_id, commit_id, normalized_kind, key, payload_json, now, now),
    )
    return outbox_id


def _existing_commit(
    connection: sqlite3.Connection,
    *,
    conversation_id: str,
    turn_id: str,
    overlay_json: str,
) -> CognitiveCommitResult | None:
    row = connection.execute(
        """
        SELECT commit_id, state_revision, state_json, overlay_json
        FROM cognitive_turn_commits
        WHERE conversation_id = ? AND turn_id = ?
        """,
        (conversation_id, turn_id),
    ).fetchone()
    if row is None:
        return None
    if _canonical(load_json(connection, str(row[3])), payload_name="stored_overlay") != overlay_json:
        raise CognitiveCommitConflict(
            f"turn {conversation_id}/{turn_id} already has a different overlay"
        )
    try:
        state = load_json(connection, str(row[2]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise CognitiveCommitError("stored cognitive state is invalid") from error
    outbox_rows = connection.execute(
        "SELECT outbox_id FROM cognitive_outbox WHERE commit_id = ? ORDER BY outbox_id",
        (str(row[0]),),
    ).fetchall()
    return CognitiveCommitResult(
        commit_id=str(row[0]),
        conversation_id=conversation_id,
        turn_id=turn_id,
        state_revision=_safe_int(row[1]),
        state=state if isinstance(state, Mapping) else {},
        outbox_ids=tuple(str(item[0]) for item in outbox_rows),
        idempotent=True,
    )


def commit_turn(
    connection: sqlite3.Connection,
    *,
    turn: Mapping[str, Any],
    overlay: Mapping[str, Any],
    digest: Mapping[str, Any] | None = None,
    trace: Mapping[str, Any] | None = None,
    pending_action_ids: Iterable[str] | None = None,
    projection_payloads: Mapping[str, Any] | None = None,
    user_adaptation_snapshot: Mapping[str, Any] | None = None,
    state_backend: Any | None = None,
    turn_writer: Callable[[Mapping[str, Any], sqlite3.Connection], Any] | None = None,
) -> CognitiveCommitResult:
    """Commit a validated revision and all SQLite-authoritative turn data.

    The caller owns the transaction.  This function deliberately contains no
    ``commit`` call: a reducer failure, turn-row failure, or pending-action
    failure therefore rolls back together with every prior write in the
    caller's ``state_database`` context.
    """

    ensure_commit_schema(connection)
    stored_overlay = _overlay_for_storage(overlay)
    if user_adaptation_snapshot is not None:
        preferences = (stored_overlay.get("state") or {}).get("preferences") or {}
        expected_adaptations = {"scopeId": preferences.get("userAdaptationScopeId"),
            "memoryGeneration": preferences.get("userAdaptationGeneration"),
            "revision": preferences.get("userAdaptationRevision"),
            "records": preferences.get("userAdaptations", [])}
        if dict(user_adaptation_snapshot) != expected_adaptations:
            raise CognitiveCommitError("user adaptation commit differs from the validated overlay")
    turn_id = _turn_id(turn)
    if str(stored_overlay.get("turnId") or "") != turn_id:
        raise CognitiveCommitError("overlay turnId does not match conversation turn")
    conversation_id = _conversation_id(turn, stored_overlay)
    overlay_json = _canonical(stored_overlay, payload_name="overlay")
    commit_id = _commit_id(conversation_id, turn_id, stored_overlay)
    duplicate = _existing_commit(
        connection,
        conversation_id=conversation_id,
        turn_id=turn_id,
        overlay_json=overlay_json,
    )
    if duplicate is not None:
        return duplicate

    if user_adaptation_snapshot is not None:
        from agent.cognitive_adaptations import commit_user_adaptations
        adaptation_state = commit_user_adaptations(
            connection,
            scope_id=str(user_adaptation_snapshot["scopeId"]),
            memory_generation=int(user_adaptation_snapshot["memoryGeneration"]),
            base_revision=int(user_adaptation_snapshot["revision"]),
            commit_id=commit_id,
            records=deepcopy(user_adaptation_snapshot["records"]),
        )
        stored_overlay["state"]["preferences"]["userAdaptationRevision"] = adaptation_state["revision"]

    backend = _state_backend(state_backend)
    ensure_state_schema = getattr(backend, "ensure_schema", None)
    if callable(ensure_state_schema):
        ensure_state_schema(connection)
    reducer = getattr(backend, "commit_revision", None)
    if not callable(reducer):
        raise CognitiveStateUnavailable("cognitive state backend has no commit_revision")
    state = _state_dict(reducer(
        connection,
        deepcopy(stored_overlay),
        digest=dict(digest or {}),
        trace=dict(trace or {}),
    ))
    state_revision = _safe_int(
        state.get("revision", state.get("stateRevision", stored_overlay.get("baseRevision", 0) + 1)),
        _safe_int(stored_overlay.get("baseRevision")) + 1,
    )
    if state_revision < 0:
        raise CognitiveCommitError("cognitive state revision must be nonnegative")
    persisted_turn = _public_projection(turn)
    if not isinstance(persisted_turn, dict):
        raise CognitiveCommitError("conversation turn must be an object")
    persisted_turn.update({
        "cognitiveCommitId": commit_id,
        "cognitiveStateRevision": state_revision,
        "cognitiveDigestRef": commit_id,
        "cognitiveTraceRef": commit_id if trace else "",
    })
    created_at = str(persisted_turn.get("at") or _now_iso())[:80]
    digest_json = _stored_json(connection, _public_projection(dict(digest or {})), payload_name="digest")
    trace_json = _stored_json(connection, _public_projection(dict(trace or {})), payload_name="trace")
    state_json = _stored_json(connection, state, payload_name="state")
    # Use the pre-reducer overlay for replay identity, as in the inline format.
    overlay_json = store_json(connection, json.loads(overlay_json), encoded=overlay_json)
    connection.execute(
        """
        INSERT INTO cognitive_turn_commits(
            commit_id, conversation_id, turn_id, root_snapshot_id,
            base_revision, state_revision, overlay_json, digest_json,
            trace_json, state_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            commit_id,
            conversation_id,
            turn_id,
            str(stored_overlay.get("rootSnapshotId") or "")[:180],
            _safe_int(stored_overlay.get("baseRevision")),
            state_revision,
            overlay_json,
            digest_json,
            trace_json,
            state_json,
            created_at,
        ),
    )
    if turn_writer is None:
        _insert_turn_row(connection, persisted_turn)
    else:
        turn_writer(persisted_turn, connection)
    _invalidate_pending_actions(
        connection,
        commit_id=commit_id,
        action_ids=_pending_ids(stored_overlay, pending_action_ids),
        created_at=created_at,
    )
    outbox_ids: list[str] = []
    for kind, key, payload in _projection_payloads(
        commit_id,
        turn=persisted_turn,
        requested=projection_payloads,
    ):
        outbox_ids.append(enqueue_projection(
            connection,
            commit_id=commit_id,
            kind=kind,
            projection_key=key,
            payload=payload,
        ))
    return CognitiveCommitResult(
        commit_id=commit_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        state_revision=state_revision,
        state=state,
        outbox_ids=tuple(outbox_ids),
        idempotent=False,
    )


def build_memory_projection_payload(
    overlay: Mapping[str, Any],
    updates: Sequence[Any] = (),
    *,
    source_turn_id: str = "",
) -> dict[str, Any]:
    """Extract bounded memory operations without copying the full model result."""

    def patch_value(patch: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in ("newBelief", "belief", "proposal", "correction", "value"):
            value = patch.get(key)
            if isinstance(value, Mapping):
                return value
        return {}

    def belief_candidates() -> list[Mapping[str, Any]]:
        state = overlay.get("state") if isinstance(overlay, Mapping) else None
        raw_beliefs = (
            state.get("beliefs")
            if isinstance(state, Mapping) else overlay.get("beliefs")
            if isinstance(overlay, Mapping) else None
        )
        return [item for item in (raw_beliefs or ()) if isinstance(item, Mapping)]

    def belief_for_id(belief_id: str) -> Mapping[str, Any] | None:
        if not belief_id:
            return None
        for belief in belief_candidates():
            for key in ("beliefId", "id", "targetId"):
                if str(belief.get(key) or "").strip() == belief_id:
                    return belief
        return None

    def text_from(value: Mapping[str, Any] | None, *keys: str) -> str:
        if not isinstance(value, Mapping):
            return ""
        for key in keys:
            candidate = value.get(key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()[:600]
        return ""

    def explicitly_authorized(patch: Mapping[str, Any], value: Mapping[str, Any]) -> bool:
        authorization = patch.get("authorization", value.get("authorization"))
        if isinstance(authorization, bool):
            return authorization
        token = str(authorization or "").strip().casefold()
        if token in {"explicit", "authorized", "user_authorized", "user_confirmed", "confirmed"}:
            return True
        return any(
            bool(patch.get(key, value.get(key)))
            for key in ("explicitlyAuthorized", "userAuthorized")
        )

    operations: list[dict[str, Any]] = []
    allowed = {
        "ADD_USER_BELIEF",
        "SUPERSEDE_USER_BELIEF",
        "PROPOSE_MEMORY_CORRECTION",
    }
    for raw_patch in overlay.get("patches", []) if isinstance(overlay, Mapping) else []:
        if not isinstance(raw_patch, Mapping):
            continue
        operation = str(raw_patch.get("operation") or raw_patch.get("op") or "").strip().upper()
        if operation not in allowed:
            continue
        value = patch_value(raw_patch)
        if not value and raw_patch.get("value") is not None:
            value = {"content": raw_patch.get("value")}
        target_belief_id = "" if operation == "ADD_USER_BELIEF" else str(
            raw_patch.get("oldBeliefId")
            or raw_patch.get("targetId")
            or raw_patch.get("beliefId")
            or ""
        ).strip()[:180]
        target_belief = belief_for_id(target_belief_id)
        target_memory_id = text_from(target_belief, "memoryId", "memory_id")
        # ``targetId`` identifies the cognitive ledger belief.  Only the
        # injected belief's explicit memoryId may cross into MemoryRecord.
        new_belief_id = text_from(value, "beliefId", "id", "targetId") or text_from(
            raw_patch,
            "newBeliefId",
            "beliefId" if operation == "ADD_USER_BELIEF" else "newBeliefId",
            "targetId" if operation == "ADD_USER_BELIEF" else "newBeliefId",
        )
        content = text_from(value, "claim", "content", "newContent", "object", "value")
        new_belief = belief_for_id(new_belief_id) or value
        if not content:
            content = text_from(raw_patch, "claim", "content", "newContent")
        source_span = text_from(raw_patch, "sourceSpan", "source_span") or text_from(
            value, "sourceSpan", "source_span"
        )
        operation_payload = {
            "operation": operation,
            "targetBeliefId": target_belief_id,
            "targetMemoryId": target_memory_id,
            "beliefId": new_belief_id,
            "memoryId": text_from(new_belief, "memoryId"),
            "scope": text_from(new_belief, "scope") or "session",
            "content": content,
            "domain": text_from(value, "domain", "beliefDomain") or text_from(raw_patch, "domain"),
            "sourceSpan": source_span,
            "sourceTurnId": str(source_turn_id or "")[:180],
            "subject": text_from(value, "subject"),
            "predicate": text_from(value, "predicate"),
            "object": text_from(value, "object", "objectValue"),
            "polarity": text_from(value, "polarity"),
            "temporal": text_from(value, "temporal"),
        }
        if operation == "PROPOSE_MEMORY_CORRECTION":
            # The state ledger keeps this as a proposal.  The external memory
            # projector may act only when an explicit authorization accompanies
            # the proposal; sourceSpan alone is provenance, not authorization.
            operation_payload["status"] = "proposed"
            operation_payload["authorized"] = explicitly_authorized(raw_patch, value)
        operations.append(operation_payload)
    return {
        "projectionVersion": 1,
        "sourceTurnId": str(source_turn_id or "")[:180],
        "operations": operations[:32],
        "updates": _public_projection(list(updates)[:32]),
    }


def _connection_scope(connection_factory: Callable[[], sqlite3.Connection]):
    connection = connection_factory()
    try:
        ensure_commit_schema(connection)
        return connection
    except Exception:
        connection.close()
        raise


def _claim_outbox_row(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    commit_id: str | None,
    kinds: set[str] | None,
    now: float,
    lease_seconds: float,
) -> dict[str, Any] | None:
    connection = _connection_scope(connection_factory)
    try:
        clauses = [
            "(status = 'pending' OR (status = 'running' AND lease_until <= ?))",
        ]
        params: list[Any] = [now]
        if commit_id:
            clauses.append("commit_id = ?")
            params.append(commit_id)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(sorted(kinds))
        where = " AND ".join(clauses)
        with connection:
            row = connection.execute(
                f"""
                SELECT outbox_id, commit_id, kind, projection_key, payload_json, attempts
                FROM cognitive_outbox
                WHERE {where}
                ORDER BY created_at, outbox_id
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            outbox_id = str(row[0])
            updated = connection.execute(
                """
                UPDATE cognitive_outbox
                SET status='running', attempts=attempts+1,
                    lease_until=?, updated_at=?, last_error=''
                WHERE outbox_id=?
                  AND (status='pending' OR (status='running' AND lease_until <= ?))
                """,
                (now + max(1.0, lease_seconds), _now_iso(), outbox_id, now),
            )
            if updated.rowcount != 1:
                return None
            try:
                payload = load_json(connection, str(row[4]))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise CognitiveCommitError("invalid cognitive outbox payload") from error
            return {
                "outboxId": outbox_id,
                "commitId": str(row[1]),
                "kind": str(row[2]),
                "projectionKey": str(row[3]),
                "payload": payload if isinstance(payload, Mapping) else {},
            }
    finally:
        connection.close()


def _finish_outbox_row(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    outbox_id: str,
    ok: bool,
    error: str = "",
) -> None:
    connection = _connection_scope(connection_factory)
    try:
        with connection:
            if ok:
                connection.execute(
                    """
                    UPDATE cognitive_outbox
                    SET status='applied', lease_until=0, last_error='', updated_at=?
                    WHERE outbox_id=?
                    """,
                    (_now_iso(), outbox_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE cognitive_outbox
                    SET status='pending', lease_until=0, last_error=?, updated_at=?
                    WHERE outbox_id=?
                    """,
                    (str(error)[:1_000], _now_iso(), outbox_id),
                )
    finally:
        connection.close()


def project_pending(
    connection_factory: Callable[[], sqlite3.Connection],
    projectors: Mapping[str, Callable[[Mapping[str, Any]], Any]],
    *,
    commit_id: str | None = None,
    kinds: Iterable[str] | None = None,
    limit: int = 32,
    lease_seconds: float = OUTBOX_LEASE_SECONDS,
    raise_on_error: bool = False,
) -> ProjectionReport:
    """Run at-least-once projections and leave failures retryable.

    Each concrete projector must be idempotent by ``commitId`` and
    ``projectionKey``.  The outbox status is changed only after the projector
    returns successfully; a crash leaves a running lease that can be claimed
    after expiry.
    """

    normalized_kinds = {
        str(item).strip().lower() for item in (kinds or ()) if str(item).strip()
    }
    applied: list[str] = []
    failed: list[ProjectionFailure] = []
    for _ in range(max(0, int(limit))):
        row = _claim_outbox_row(
            connection_factory,
            commit_id=commit_id,
            kinds=normalized_kinds or None,
            now=time.time(),
            lease_seconds=lease_seconds,
        )
        if row is None:
            break
        outbox_id = str(row["outboxId"])
        kind = str(row["kind"])
        projector = projectors.get(kind)
        try:
            if projector is None:
                raise CognitiveProjectionError(f"no projector registered for {kind}")
            payload = dict(row["payload"])
            payload.setdefault("commitId", str(row["commitId"]))
            payload.setdefault("projectionKey", str(row["projectionKey"]))
            projector(payload)
        except Exception as error:
            failure = ProjectionFailure(outbox_id, kind, str(error)[:1_000])
            failed.append(failure)
            _finish_outbox_row(
                connection_factory,
                outbox_id=outbox_id,
                ok=False,
                error=failure.error,
            )
            # Leave the row pending for a later worker/request.  Retrying the
            # same failed projection in this invocation would create a hot
            # loop and could repeatedly invoke a non-idempotent destination.
            break
        _finish_outbox_row(connection_factory, outbox_id=outbox_id, ok=True)
        applied.append(outbox_id)
    report = ProjectionReport(tuple(applied), tuple(failed))
    if raise_on_error and failed:
        details = "; ".join(f"{item.kind}:{item.error}" for item in failed)
        raise CognitiveProjectionError("cognitive projection failed; retryable outbox remains: " + details)
    return report


def pending_projection_rows(
    connection: sqlite3.Connection,
    *,
    commit_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_commit_schema(connection)
    if commit_id:
        rows = connection.execute(
            "SELECT outbox_id, commit_id, kind, projection_key, status, attempts, last_error FROM cognitive_outbox WHERE commit_id = ? ORDER BY outbox_id",
            (commit_id,),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT outbox_id, commit_id, kind, projection_key, status, attempts, last_error FROM cognitive_outbox ORDER BY outbox_id"
        ).fetchall()
    return [
        {
            "outboxId": str(row[0]),
            "commitId": str(row[1]),
            "kind": str(row[2]),
            "projectionKey": str(row[3]),
            "status": str(row[4]),
            "attempts": _safe_int(row[5]),
            "lastError": str(row[6] or ""),
        }
        for row in rows
    ]


def pending_projection_payloads(
    connection: sqlite3.Connection, *, kind: str,
    conversation_id: str = "", latest_only: bool = False,
) -> list[dict[str, Any]]:
    """Read committed data whose file projection is still pending; no writes."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_outbox'").fetchone() is None:
        return []
    clauses = ["o.kind=?", "o.status IN ('pending', 'running')"]
    parameters: list[Any] = [kind]
    if conversation_id:
        clauses.append("c.conversation_id=?")
        parameters.append(conversation_id)
    order = "DESC LIMIT 1" if latest_only else "ASC"
    rows = connection.execute(
        "SELECT o.payload_json FROM cognitive_outbox o JOIN cognitive_turn_commits c ON c.commit_id=o.commit_id "
        "JOIN conversation_turns t ON t.turn_id=c.turn_id "
        + "WHERE " + " AND ".join(clauses) + " ORDER BY c.rowid " + order, parameters,
    ).fetchall()
    return [load_json(connection, str(row[0])) for row in rows]
