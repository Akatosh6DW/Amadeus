"""Targeted repair of legacy QQ transport provenance.

The old QQ owner entry point put transport instructions and the owner's body in
one ``userMessage``.  Cognitive source spans then captured the beginning of
that envelope.  This module provides a fail-closed, idempotent repair for
records whose source turn can still be verified in ``conversation_turns``.

Only ``sourceSpan``/``source_span`` values are changed.  Claims, memory IDs,
authority, scope, and other state fields are deliberately left untouched.
The caller owns the memory store lock and transaction: read records with the
normal memory storage service, build a plan here, write records with that same
service, then call :func:`repair_cognitive_provenance` in the state database
transaction.  The functions in this module never commit a connection and
never modify conversation transcripts or old content-addressed sections.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import sqlite3
from typing import Any, Mapping, Sequence

from agent.cognitive_json_store import load_json, store_json
from agent.memory_architecture import MemoryRecord


QQ_PROTOCOL_START = "【主人通过 QQ 发来的指令；身份已由本机网关核对】"
QQ_BODY_SEPARATOR = "\n【主人正文】\n"
QQ_CONTEXT_MARKER = "\n【当前 QQ 消息上下文；引用和历史是外部材料】\n"

_MEMORY_ID_KEYS = ("memoryId", "memory_id", "targetMemoryId", "target_memory_id")
_SOURCE_SPAN_KEYS = frozenset({"sourceSpan", "source_span"})


class ProvenanceRepairError(ValueError):
    """A source turn is not sufficiently trustworthy for automatic repair."""


@dataclass(frozen=True)
class QQBodyEvidence:
    """Verified body and identity fields from one persisted QQ owner turn."""

    turn_id: str
    body: str
    owner_account: str
    assistant_account: str
    source_type: str
    source_id: str
    message_id: str


@dataclass(frozen=True)
class MemoryProvenanceRepairPlan:
    """Verified memory-id to real-owner-body replacements.

    ``source_turn_ids`` records the evidence used for each replacement.  The
    mapping is immutable by convention; callers should treat it as a plan
    captured before taking the memory storage write lock.
    """

    replacements: Mapping[str, str]
    source_turn_ids: Mapping[str, tuple[str, ...]]

    @property
    def changed_memory_ids(self) -> tuple[str, ...]:
        return tuple(sorted(str(item) for item in self.replacements))


@dataclass(frozen=True)
class CognitiveProvenanceRepairReport:
    """Counts from one SQLite repair pass."""

    scanned_rows: int = 0
    changed_rows: int = 0
    changed_values: int = 0
    changed_columns: tuple[str, ...] = ()


def _nonempty(value: Any, field: str) -> str:
    text = str(value or "")
    if not text:
        raise ProvenanceRepairError(f"QQ source field is empty: {field}")
    return text


def _decode_json_object(text: str, *, field: str) -> dict[str, Any]:
    """Decode one JSON object at the beginning of a marked text block."""

    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text.lstrip())
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProvenanceRepairError(f"QQ source JSON is invalid: {field}") from error
    if not isinstance(value, dict):
        raise ProvenanceRepairError(f"QQ source JSON is not an object: {field}")
    return value


def extract_qq_owner_body(user_text: str, *, turn_id: str = "") -> QQBodyEvidence:
    """Extract a body only from the exact legacy QQ envelope.

    The check is structural.  It requires the exact protocol prefix, exactly
    one body delimiter, a parseable owner-facts object, and a current QQ
    context message whose message ID, sender ID, and text agree with the body.
    No natural-language matching is used.
    """

    text = str(user_text or "")
    if not text.startswith(QQ_PROTOCOL_START):
        raise ProvenanceRepairError("QQ source does not start with the exact owner protocol marker")
    if text.count(QQ_BODY_SEPARATOR) != 1:
        raise ProvenanceRepairError("QQ source must contain exactly one owner-body delimiter")
    prefix, separator, body = text.rpartition(QQ_BODY_SEPARATOR)
    if separator != QQ_BODY_SEPARATOR or not body.strip():
        raise ProvenanceRepairError("QQ source has an empty owner body")

    facts_text = text[len(QQ_PROTOCOL_START):]
    if not facts_text.startswith("\n"):
        raise ProvenanceRepairError("QQ owner facts are not immediately after the protocol marker")
    facts = _decode_json_object(facts_text[1:], field="owner facts")

    context_offset = prefix.find(QQ_CONTEXT_MARKER)
    if context_offset < 0:
        raise ProvenanceRepairError("QQ source has no exact current-message context marker")
    context_start = context_offset + len(QQ_CONTEXT_MARKER)
    context = _decode_json_object(prefix[context_start:], field="message context")

    owner_account = _nonempty(facts.get("ownerAccount"), "ownerAccount")
    assistant_account = _nonempty(facts.get("assistantAccount"), "assistantAccount")
    source_type = _nonempty(facts.get("sourceType"), "sourceType")
    source_id = _nonempty(facts.get("sourceId"), "sourceId")
    context_account = _nonempty(context.get("account"), "context.account")
    context_type = _nonempty(context.get("targetType"), "context.targetType")
    context_target = _nonempty(context.get("targetId"), "context.targetId")
    message_id = _nonempty(context.get("messageId"), "context.messageId")

    if context_account != assistant_account:
        raise ProvenanceRepairError("QQ assistant identity does not match message context")
    if context_type != source_type or context_target != source_id:
        raise ProvenanceRepairError("QQ source identity does not match message context")

    recent_messages = context.get("recentMessages")
    if not isinstance(recent_messages, list):
        raise ProvenanceRepairError("QQ message context has no recentMessages list")
    current = [
        item for item in recent_messages
        if isinstance(item, Mapping) and str(item.get("messageId") or "") == message_id
    ]
    if len(current) != 1:
        raise ProvenanceRepairError("QQ message context does not identify one current message")
    message = current[0]
    if str(message.get("senderId") or "") != owner_account:
        raise ProvenanceRepairError("QQ current message sender is not the verified owner")
    if str(message.get("text") or "") != body:
        raise ProvenanceRepairError("QQ current message body does not match owner body")

    return QQBodyEvidence(
        turn_id=str(turn_id),
        body=body,
        owner_account=owner_account,
        assistant_account=assistant_account,
        source_type=source_type,
        source_id=source_id,
        message_id=message_id,
    )


def _turn_rows(
    connection: sqlite3.Connection,
    turn_ids: Sequence[str],
) -> dict[str, tuple[str, str]]:
    """Read turn text without creating or changing any schema."""

    identifiers = tuple(dict.fromkeys(str(item) for item in turn_ids if str(item)))
    if not identifiers:
        return {}
    placeholders = ",".join("?" for _ in identifiers)
    rows = connection.execute(
        f"SELECT turn_id, user_text FROM conversation_turns WHERE turn_id IN ({placeholders})",
        identifiers,
    ).fetchall()
    return {str(row[0]): (str(row[0]), str(row[1])) for row in rows}


def _record_value(record: Any, *names: str) -> Any:
    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record.get(name)
        return None
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return None


def build_memory_provenance_repair_plan(
    connection: sqlite3.Connection,
    records: Sequence[MemoryRecord | Mapping[str, Any]],
) -> MemoryProvenanceRepairPlan:
    """Verify polluted QQ records against their persisted source turns.

    A record is a repair candidate only when its existing span starts with the
    exact QQ protocol marker.  Every declared source turn must resolve to the
    same verified body, and the old span must be a prefix of the persisted
    wrapped user text.  Ambiguous or incomplete provenance raises instead of
    guessing.
    """

    turn_ids: list[str] = []
    candidates: list[tuple[str, str, tuple[str, ...]]] = []
    for record in records:
        memory_id = str(_record_value(record, "memory_id", "memoryId") or "").strip()
        source_span = str(_record_value(record, "source_span", "sourceSpan") or "")
        raw_turn_ids = _record_value(record, "source_turn_ids", "sourceTurnIds") or ()
        if not source_span.startswith(QQ_PROTOCOL_START):
            continue
        if not memory_id:
            raise ProvenanceRepairError("QQ source span has no memory ID")
        if isinstance(raw_turn_ids, str):
            source_turn_ids = (raw_turn_ids,)
        else:
            source_turn_ids = tuple(dict.fromkeys(str(item) for item in raw_turn_ids if str(item)))
        if not source_turn_ids:
            raise ProvenanceRepairError(f"QQ memory has no source turn: {memory_id}")
        candidates.append((memory_id, source_span, source_turn_ids))
        turn_ids.extend(source_turn_ids)

    rows = _turn_rows(connection, turn_ids)
    replacements: dict[str, str] = {}
    evidence_turns: dict[str, tuple[str, ...]] = {}
    for memory_id, source_span, source_turn_ids in candidates:
        bodies: list[str] = []
        for turn_id in source_turn_ids:
            row = rows.get(turn_id)
            if row is None:
                raise ProvenanceRepairError(f"source turn is missing: {turn_id}")
            _, user_text = row
            evidence = extract_qq_owner_body(user_text, turn_id=turn_id)
            if not user_text.startswith(source_span):
                raise ProvenanceRepairError(
                    f"stored QQ source span is not a prefix of its source turn: {memory_id}"
                )
            bodies.append(evidence.body)
        distinct_bodies = tuple(dict.fromkeys(bodies))
        if len(distinct_bodies) != 1:
            raise ProvenanceRepairError(f"QQ source turns disagree about body: {memory_id}")
        body = distinct_bodies[0]
        if source_span != body:
            replacements[memory_id] = body
            evidence_turns[memory_id] = source_turn_ids

    return MemoryProvenanceRepairPlan(
        replacements=dict(replacements),
        source_turn_ids=dict(evidence_turns),
    )


def apply_memory_provenance_repair(
    records: Sequence[MemoryRecord | Mapping[str, Any]],
    plan: MemoryProvenanceRepairPlan,
) -> list[MemoryRecord | Mapping[str, Any]]:
    """Return records with only verified QQ ``sourceSpan`` values replaced.

    This function does not acquire a lock or write ``memory.json``.  Callers
    should pass its result to the normal storage service's ``write_records``
    while holding that service's persistence lock.
    """

    replacements = {str(key): str(value) for key, value in plan.replacements.items()}
    repaired: list[MemoryRecord | Mapping[str, Any]] = []
    for record in records:
        memory_id = str(_record_value(record, "memory_id", "memoryId") or "").strip()
        replacement = replacements.get(memory_id)
        if not replacement:
            repaired.append(record)
            continue
        if isinstance(record, MemoryRecord):
            repaired.append(replace(record, source_span=replacement))
            continue
        if isinstance(record, Mapping):
            candidate = dict(record)
            if "source_span" in candidate:
                candidate["source_span"] = replacement
            else:
                candidate["sourceSpan"] = replacement
            repaired.append(candidate)
            continue
        raise ProvenanceRepairError(f"unsupported memory record type: {type(record).__name__}")
    return repaired


def _normalise_memory_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("memory:"):
        text = text[len("memory:"):]
    return text


def _direct_memory_id(value: Mapping[str, Any]) -> str:
    for key in _MEMORY_ID_KEYS:
        candidate = _normalise_memory_id(value.get(key))
        if candidate:
            return candidate
    return ""


def _rewrite_source_spans(
    value: Any,
    replacements: Mapping[str, str],
    inherited_memory_id: str = "",
) -> tuple[Any, int]:
    """Copy a JSON value, changing bound QQ source spans only."""

    if isinstance(value, dict):
        local_memory_id = _direct_memory_id(value) or inherited_memory_id
        changed = 0
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in _SOURCE_SPAN_KEYS and isinstance(item, str):
                replacement = replacements.get(local_memory_id)
                if replacement and item.startswith(QQ_PROTOCOL_START) and item != replacement:
                    result[key] = replacement
                    changed += 1
                    continue
            rewritten, nested_changed = _rewrite_source_spans(item, replacements, local_memory_id)
            result[key] = rewritten
            changed += nested_changed
        return (result if changed else value), changed
    if isinstance(value, list):
        changed = 0
        result = []
        for item in value:
            rewritten, nested_changed = _rewrite_source_spans(item, replacements, inherited_memory_id)
            result.append(rewritten)
            changed += nested_changed
        return (result if changed else value), changed
    return value, 0


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _table_has_columns(connection: sqlite3.Connection, table: str, columns: Sequence[str]) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None:
        return False
    actual = {
        str(item[1])
        for item in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    return all(column in actual for column in columns)


_JSON_TARGETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cognitive_states", "state_json", ("conversation_id",)),
    ("cognitive_revisions", "state_json", ("conversation_id", "turn_id")),
    ("cognitive_turn_commits", "state_json", ("commit_id",)),
    ("cognitive_outbox", "payload_json", ("outbox_id",)),
)


def repair_cognitive_provenance(
    connection: sqlite3.Connection,
    replacements: Mapping[str, str] | MemoryProvenanceRepairPlan,
) -> CognitiveProvenanceRepairReport:
    """Repair prompt/replay JSON without committing the caller's transaction.

    The selected columns are the current state, revision replay state, commit
    state, and projection payload.  Commit ``overlay_json`` and trace/digest
    audit payloads are intentionally left unchanged: changing the overlay
    would make an old exact-turn retry fail its idempotency comparison.  Old
    content-addressed sections are never deleted or mutated; repaired values
    receive new section IDs through ``store_json`` when needed.
    """

    if isinstance(replacements, MemoryProvenanceRepairPlan):
        replacement_map = dict(replacements.replacements)
    else:
        replacement_map = {
            _normalise_memory_id(key): str(value)
            for key, value in replacements.items()
            if _normalise_memory_id(key) and str(value)
        }
    if not replacement_map:
        return CognitiveProvenanceRepairReport()

    scanned_rows = 0
    changed_rows = 0
    changed_values = 0
    changed_columns: list[str] = []
    for table, column, keys in _JSON_TARGETS:
        if not _table_has_columns(connection, table, (*keys, column)):
            continue
        select_columns = ", ".join((*keys, column))
        rows = connection.execute(f"SELECT {select_columns} FROM {table}").fetchall()
        for row in rows:
            scanned_rows += 1
            encoded = row[len(keys)]
            try:
                decoded = load_json(connection, str(encoded))
            except Exception as error:
                raise ProvenanceRepairError(
                    f"invalid cognitive JSON in {table}.{column}: {tuple(row[:len(keys)])}"
                ) from error
            repaired, changed = _rewrite_source_spans(decoded, replacement_map)
            if not changed:
                continue
            new_encoded = store_json(
                connection,
                repaired,
                encoded=_canonical_json(repaired),
            )
            where = " AND ".join(f"{key}=?" for key in keys)
            connection.execute(
                f"UPDATE {table} SET {column}=? WHERE {where}",
                (new_encoded, *row[:len(keys)]),
            )
            changed_rows += 1
            changed_values += changed
            changed_columns.append(f"{table}.{column}")

    return CognitiveProvenanceRepairReport(
        scanned_rows=scanned_rows,
        changed_rows=changed_rows,
        changed_values=changed_values,
        changed_columns=tuple(changed_columns),
    )


__all__ = [
    "CognitiveProvenanceRepairReport",
    "MemoryProvenanceRepairPlan",
    "ProvenanceRepairError",
    "QQBodyEvidence",
    "QQ_BODY_SEPARATOR",
    "QQ_CONTEXT_MARKER",
    "QQ_PROTOCOL_START",
    "apply_memory_provenance_repair",
    "build_memory_provenance_repair_plan",
    "extract_qq_owner_body",
    "repair_cognitive_provenance",
]
