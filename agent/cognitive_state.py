"""Pure cognitive state staging and SQLite persistence.

The module deliberately contains no model calls and no natural-language
interpretation.  A caller supplies a model-produced directive and this module
only applies the small, audited patch vocabulary from the cognitive-state
architecture.  SQLite writes are left inside the caller's transaction.
"""

from __future__ import annotations

import copy
from agent.cognitive_answer_scope import apply_answer_scope
from agent.cognitive_context import MAX_RECALL_QUERY_LENGTH, normalize_recall_scope
from agent.fingerprints import runtime_fingerprint
from agent.cognitive_tools import normalized_tool_plan, selected_tool_step, tool_plan_identity, project_durable_tool_progress
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


MAX_TRACE_ROWS = 100
MAX_TRACE_FIELD_CHARS = 4096

SUPPORTED_OPERATIONS = frozenset(
    {
        # Context and references.
        "PREFER_CONTEXT",
        "SUPPRESS_CONTEXT",
        "EXPAND_RECALL_SCOPE",
        "REWRITE_RETRIEVAL_QUERY",
        "ADD_REFERENCE_BINDING",
        "REJECT_REFERENCE_BINDING",
        # Beliefs and epistemic state.
        "RETRACT_INFERENCE",
        "MARK_BELIEF_CHALLENGED",
        "SUPERSEDE_USER_BELIEF",
        "MARK_EXTERNAL_BELIEF_STALE",
        "REQUIRE_FACT_VERIFICATION",
        "UPDATE_BELIEF_FROM_EVIDENCE",
        "PROPOSE_MEMORY_CORRECTION",
        # State bootstrap operations.  These are intentionally narrow: user
        # beliefs require an exact sourceSpan and model inferences cannot be
        # presented as external facts.
        "ADD_USER_BELIEF",
        "ADD_INFERENCE",
        # Task and tool planning.
        "START_TASK",
        "REPLACE_TASK_GOAL",
        "ADD_TASK_CONSTRAINT",
        "REMOVE_TASK_CONSTRAINT",
        "CHANGE_TASK_TARGET",
        "REDEFINE_SUCCESS_CRITERIA",
        "INVALIDATE_TASK_STEP",
        "RESET_PLAN_FROM_STEP",
        "CANCEL_TASK",
        "FAIL_TASK",
        "PAUSE_TASK",
        "RESUME_TASK",
        "REPLACE_TOOL_PLAN",
        "REPLACE_TOOL_TARGET",
        "REPLACE_TOOL_ARGUMENTS",
        "REUSE_EVIDENCE",
        "REANALYZE_EVIDENCE",
        "REFRESH_EVIDENCE",
        "INVALIDATE_PENDING_ACTION",
        "PROPOSE_ROLLBACK",
        # Interaction and bounded adaptation.
        "ADJUST_PERSONA_STYLE",
        "RESET_EMOTION_CARRYOVER",
        "SET_SESSION_INTERACTION_PREFERENCE",
        "PROPOSE_USER_INTERACTION_PREFERENCE",
        "ADD_TURN_RULE",
        "ADD_SESSION_RULE",
        "PROPOSE_USER_EXPERIENCE",
        "PROPOSE_SYSTEM_HEURISTIC",
        "REPORT_ARCHITECTURE_ISSUE",
        # Safe extension required by the state lifecycle: revoke an experience
        # without deleting its audit history.
        "REVOKE_EXPERIENCE",
        "PROMOTE_USER_ADAPTATION",
        "REVOKE_USER_ADAPTATION",
    }
)

_TASK_DEFAULT = {
    "taskId": None,
    "goal": "",
    "target": None,
    "constraints": [],
    "plan": [],
    "successCriteria": [],
    "status": "idle",
}

_CONTEXT_DEFAULT = {
    "preferredIds": [],
    "suppressedIds": [],
    "recallQuery": "",
    "recallScope": "conversation",
}

_STATE_LIST_KEYS = ("beliefs", "references", "experiences", "taskHistory")
_STATE_CORE_KEYS = (
    "conversationId",
    "revision",
    "beliefs",
    "task",
    "taskHistory",
    "references",
    "context",
    "preferences",
    "experiences",
    "lastDecisionDigest",
)

_EXTERNAL_DOMAINS = frozenset({"external_fact", "external_world", "external"})
_TOOL_DOMAINS = frozenset({"tool_fact"})
_PERCEPTUAL_DOMAINS = frozenset({"perceptual_fact"})
_EVIDENCE_DOMAINS = _EXTERNAL_DOMAINS | _TOOL_DOMAINS | _PERCEPTUAL_DOMAINS
_USER_DOMAINS = frozenset(
    {
        "user",
        "user_fact",
        "user_preference",
        "user_intent",
        "user_statement",
    }
)
_INFERENCE_DOMAINS = frozenset({"assistant_inference", "inference", "model_inference"})
_EVIDENCE_SOURCE_TYPES = frozenset({"web", "tool", "visual"})
_EVIDENCE_OK_STATUSES = frozenset(
    {"", "ok", "success", "succeeded", "fetched", "completed", "complete"}
)
_FORBIDDEN_FIELD_PARTS = frozenset(
    {
        "receipt",
        "receipts",
        "permission",
        "permissions",
        "personakernel",
        "sourcecode",
        "filesystem",
        "sandbox",
        "audit",
    }
)
_READ_ONLY_REFERENCE_FIELDS = frozenset(
    {
        "receiptid",
        "receiptids",
        "receiptref",
        "receiptrefs",
        "reusedreceiptid",
        "reusedreceiptids",
        "permissionid",
        "permissionids",
        "permissionref",
        "permissionrefs",
    }
)
_TASK_INVALIDATING_OPERATIONS = frozenset(
    {"START_TASK", "REPLACE_TASK_GOAL", "CHANGE_TASK_TARGET", "CANCEL_TASK", "FAIL_TASK", "REPLACE_TOOL_TARGET", "REPLACE_TOOL_ARGUMENTS"}
)
_SOURCE_REQUIRED_OPERATIONS = frozenset(
    {
        "START_TASK",
        "REPLACE_TASK_GOAL",
        "ADD_TASK_CONSTRAINT",
        "REMOVE_TASK_CONSTRAINT",
        "CHANGE_TASK_TARGET",
        "REDEFINE_SUCCESS_CRITERIA",
        "CANCEL_TASK",
        "FAIL_TASK",
        "PAUSE_TASK",
        "RESUME_TASK",
        "SUPERSEDE_USER_BELIEF",
        "ADD_USER_BELIEF",
        "PROPOSE_USER_INTERACTION_PREFERENCE",
        "PROPOSE_USER_EXPERIENCE",
        "PROMOTE_USER_ADAPTATION",
        "REVOKE_USER_ADAPTATION",
    }
)


class CognitivePatchError(ValueError):
    """Structured, caller-visible error for a rejected cognitive patch."""

    def __init__(
        self,
        code: str,
        operation: str | None = None,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.operation = operation
        self.details = dict(details or {})
        self.message = message or self.code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "operation": self.operation,
            "message": self.message,
            "details": copy.deepcopy(self.details),
        }


def _error(
    code: str,
    operation: str | None = None,
    message: str | None = None,
    **details: Any,
) -> CognitivePatchError:
    return CognitivePatchError(code, operation, message, details)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any, *, code: str = "invalid_value", operation: str | None = None) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise _error(code, operation, "value is not JSON serializable", reason=str(exc)) from exc


def _clone_json(value: Any, *, code: str = "invalid_value", operation: str | None = None) -> Any:
    # Round-tripping is intentional: state and audit records must remain JSON
    # values and cannot smuggle executable/custom Python objects into storage.
    try:
        return json.loads(_json(value, code=code, operation=operation))
    except CognitivePatchError:
        raise


def _identifier(value: Any, name: str, operation: str | None = None) -> str:
    if isinstance(value, bool) or value is None or not str(value).strip():
        raise _error("invalid_identifier", operation, f"{name} must be non-empty", field=name)
    return str(value)


def _nonnegative_int(value: Any, name: str, operation: str | None = None) -> int:
    if isinstance(value, bool):
        raise _error("invalid_integer", operation, f"{name} must be an integer", field=name)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise _error("invalid_integer", operation, f"{name} must be an integer", field=name) from exc
    if result < 0:
        raise _error("invalid_integer", operation, f"{name} must be non-negative", field=name)
    return result


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _as_list(value: Any, name: str, operation: str | None = None) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _error("invalid_list", operation, f"{name} must be a list", field=name)
    return list(value)


def _ensure_mapping(value: Any, name: str, operation: str | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("invalid_mapping", operation, f"{name} must be an object", field=name)
    return value


def _canonical_field(key: Any) -> str:
    return str(key).replace("_", "").replace("-", "").lower()


def _scan_forbidden(value: Any, operation: str | None = None) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _canonical_field(key)
            if normalized in _READ_ONLY_REFERENCE_FIELDS:
                # Existing receipts/permissions may be named as evidence or
                # rollback references.  Their payloads and mutable objects
                # remain outside the cognitive-state write surface.
                if isinstance(nested, Mapping) or (
                    isinstance(nested, Sequence)
                    and not isinstance(nested, (str, bytes))
                    and any(isinstance(item, Mapping) for item in nested)
                ):
                    raise _error(
                        "forbidden_write",
                        operation,
                        "receipt/permission references must be scalar IDs or lists of IDs",
                        field=str(key),
                    )
                continue
            if normalized in _FORBIDDEN_FIELD_PARTS or any(
                part in normalized
                for part in ("receipt", "permission", "personakernel", "sourcecode", "filesystem", "sandbox", "audit")
            ):
                raise _error(
                    "forbidden_write",
                    operation,
                    "patch cannot write execution receipts, permissions, persona kernel, or audit fields",
                    field=str(key),
                )
            _scan_forbidden(nested, operation)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            _scan_forbidden(nested, operation)


def _normalise_state(state: Mapping[str, Any], conversation_id: str | None = None) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        raise _error("invalid_state", message="state must be an object")
    result = copy.deepcopy(dict(state))
    raw_conversation = result.get("conversationId", conversation_id)
    cid = _identifier(raw_conversation, "conversationId")
    if conversation_id is not None and cid != str(conversation_id):
        raise _error(
            "conversation_mismatch",
            message="state conversationId does not match requested conversation",
            expected=str(conversation_id),
            actual=cid,
        )
    result["conversationId"] = cid
    result["revision"] = _nonnegative_int(result.get("revision", 0), "revision")

    for key in _STATE_LIST_KEYS:
        value = result.get(key, [])
        if not isinstance(value, list):
            raise _error("invalid_state", message=f"{key} must be a list", field=key)
        result[key] = value

    task = result.get("task", {})
    if not isinstance(task, Mapping):
        raise _error("invalid_state", message="task must be an object", field="task")
    normal_task = copy.deepcopy(dict(_TASK_DEFAULT))
    normal_task.update(copy.deepcopy(dict(task)))
    for key in ("constraints", "plan", "successCriteria"):
        if not isinstance(normal_task.get(key), list):
            raise _error("invalid_state", message=f"task.{key} must be a list", field=f"task.{key}")
    result["task"] = normal_task
    _ensure_constraint_ids(normal_task)

    context = result.get("context", {})
    if not isinstance(context, Mapping):
        raise _error("invalid_state", message="context must be an object", field="context")
    normal_context = copy.deepcopy(dict(_CONTEXT_DEFAULT))
    normal_context.update(copy.deepcopy(dict(context)))
    for key in ("preferredIds", "suppressedIds"):
        if not isinstance(normal_context.get(key), list):
            raise _error("invalid_state", message=f"context.{key} must be a list", field=f"context.{key}")
    result["context"] = normal_context

    preferences = result.get("preferences", {})
    if not isinstance(preferences, Mapping):
        raise _error("invalid_state", message="preferences must be an object", field="preferences")
    result["preferences"] = copy.deepcopy(dict(preferences))
    result["lastDecisionDigest"] = copy.deepcopy(result.get("lastDecisionDigest", {}))
    _expire_experiences(result)
    return result


def empty_state(conversation_id: str) -> dict[str, Any]:
    """Return a new, JSON-safe state for ``conversation_id``."""

    cid = _identifier(conversation_id, "conversation_id")
    return {
        "conversationId": cid,
        "revision": 0,
        "beliefs": [],
        "task": copy.deepcopy(_TASK_DEFAULT),
        "taskHistory": [],
        "references": [],
        "context": copy.deepcopy(_CONTEXT_DEFAULT),
        "preferences": {"session": {}, "userProposals": []},
        "experiences": [],
        "lastDecisionDigest": {},
    }


def _expire_experiences(state: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    for experience in state.get("experiences", []):
        if not isinstance(experience, Mapping) or experience.get("status", "active") != "active":
            continue
        remaining = experience.get("remainingTurns")
        expired = False
        if remaining is not None:
            try:
                expired = int(remaining) <= 0
            except (TypeError, ValueError):
                expired = True
        expires_at = experience.get("expiresAt")
        if expires_at:
            try:
                parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                expired = expired or parsed <= now
            except ValueError:
                # An invalid expiry is safer as expired than as an indefinite rule.
                expired = True
        if expired:
            experience["status"] = "expired"


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create cognitive tables without committing the caller's transaction."""

    statements = (
        """
        CREATE TABLE IF NOT EXISTS cognitive_states (
            conversation_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cognitive_revisions (
            conversation_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            base_revision INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            root_snapshot_id TEXT NOT NULL,
            state_json TEXT NOT NULL,
            invalidated_pending_action_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (conversation_id, turn_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cognitive_digests (
            conversation_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            digest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (conversation_id, turn_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cognitive_traces (
            trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            root_snapshot_id TEXT,
            trace_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cognitive_patches (
            patch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            root_snapshot_id TEXT,
            ordinal INTEGER NOT NULL,
            patch_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (conversation_id, turn_id, ordinal)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_cognitive_traces_conversation_turn ON cognitive_traces(conversation_id, turn_id)",
        "CREATE INDEX IF NOT EXISTS idx_cognitive_patches_conversation_turn ON cognitive_patches(conversation_id, turn_id)",
    )
    # Do not use executescript: it may commit a transaction that the caller is
    # using to atomically insert the surrounding conversation turn.
    for statement in statements:
        connection.execute(statement)


def load_state(connection: sqlite3.Connection, conversation_id: str) -> dict[str, Any]:
    """Load one conversation's state, returning a fresh empty state if absent."""

    cid = _identifier(conversation_id, "conversation_id")
    ensure_schema(connection)
    row = connection.execute(
        "SELECT state_json FROM cognitive_states WHERE conversation_id = ?", (cid,)
    ).fetchone()
    if row is None:
        return empty_state(cid)
    try:
        decoded = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _error("state_corrupt", message="stored cognitive state is not valid JSON") from exc
    return project_durable_tool_progress(connection, _normalise_state(decoded, cid))


def _patch_payload(patch: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    value = _first(patch, *keys, default=None)
    if value is not None:
        return value
    nested = patch.get("value")
    if isinstance(nested, Mapping):
        value = _first(nested, *keys, default=None)
        if value is not None:
            return value
    if "value" in patch:
        return nested
    return default


def _patch_record_payload(patch: Mapping[str, Any], *aliases: str, default: Any = None) -> Any:
    """Keep a record's canonical value intact, including alias-named fields.

    Scalar selectors may look inside value for a named field. A record cannot:
    preference/rule/experience can be ordinary content beside its ID, condition
    and exceptions. Only top-level aliases are alternate record containers.
    """
    if "value" in patch:
        return patch["value"]
    return _first(patch, *aliases, default=default)


def _source_span(
    patch: Mapping[str, Any],
    user_message: str,
    operation: str,
    *,
    required: bool = False,
) -> str | None:
    value = _first(patch, "sourceSpan", "source_span", default=None)
    if value is None and isinstance(patch.get("value"), Mapping):
        value = _first(patch["value"], "sourceSpan", "source_span", default=None)
    if value is None:
        if required:
            raise _error(
                "source_span_required",
                operation,
                "user fact, intent, and constraint changes require sourceSpan",
            )
        return None
    if not isinstance(value, str) or not value or value not in user_message:
        raise _error(
            "invalid_source_span",
            operation,
            "sourceSpan must be an exact non-empty substring of current user_message",
            sourceSpan=value,
        )
    return value


def _record_source(record: dict[str, Any], source_span: str | None) -> None:
    if source_span is not None:
        record["sourceSpan"] = source_span


def _record_id(record: Mapping[str, Any], *keys: str) -> str | None:
    value = _first(record, *keys, default=None)
    if value is None:
        return None
    return str(value)


def declared_belief_patch_id(patch: Mapping[str, Any]) -> str | None:
    """Resolve an explicit belief identity using the accepted payload aliases."""
    value = _patch_payload(patch, "belief", "newBelief", default=None)
    nested_id = _record_id(value, "beliefId", "targetId", "id") if isinstance(value, Mapping) else None
    return nested_id or _record_id(patch, "beliefId", "targetId", "id")


def _find_record(items: list[Any], record_id: str, operation: str, name: str) -> tuple[int, dict[str, Any]]:
    for index, item in enumerate(items):
        if isinstance(item, Mapping) and _record_id(
            item, "id", "targetId", f"{name}Id", "beliefId", "referenceId", "experienceId"
        ) == record_id:
            return index, dict(item)
    raise _error("target_not_found", operation, f"{name} was not found", id=record_id)


def _upsert_record(items: list[Any], record: Mapping[str, Any], record_id: str, operation: str, name: str) -> None:
    for index, item in enumerate(items):
        if isinstance(item, Mapping) and _record_id(
            item, "id", "targetId", f"{name}Id", "beliefId", "referenceId", "experienceId"
        ) == record_id:
            items[index] = copy.deepcopy(dict(record))
            return
    items.append(copy.deepcopy(dict(record)))


def _ids_from_patch(
    patch: Mapping[str, Any],
    operation: str,
    *,
    fallback: Sequence[str] = (),
    keys: tuple[str, ...] = ("ids", "contextIds", "contextId", "pendingActionIds", "targetId", "id"),
) -> list[str]:
    value = _patch_payload(patch, *keys, default=None)
    if value is None:
        value = list(fallback)
    if isinstance(value, (str, bytes)):
        values = [value]
    elif isinstance(value, Sequence):
        values = list(value)
    else:
        raise _error("invalid_ids", operation, "ids must be a string or list")
    result: list[str] = []
    for item in values:
        ident = _identifier(item, "id", operation)
        if ident not in result:
            result.append(ident)
    if not result:
        raise _error("missing_ids", operation, "at least one id is required")
    return result


def _belief_domain(record: Mapping[str, Any]) -> str:
    return str(_first(record, "domain", "beliefDomain", default="")).strip().lower()


def _belief_id(patch: Mapping[str, Any], operation: str) -> str:
    value = _patch_payload(patch, "beliefId", "targetId", "id", default=None)
    return _identifier(value, "beliefId", operation)


def _reference_id(patch: Mapping[str, Any], operation: str) -> str:
    value = _patch_payload(patch, "referenceId", "targetId", "id", default=None)
    return _identifier(value, "referenceId", operation)


def _experience_id(patch: Mapping[str, Any], operation: str) -> str:
    value = _patch_payload(patch, "experienceId", "targetId", "id", default=None)
    return _identifier(value, "experienceId", operation)


def _constraint_key(value: Any) -> str:
    return _json(value)


def _ensure_constraint_ids(task: dict[str, Any]) -> None:
    constraints = task.get("constraints", [])
    for index, item in enumerate(constraints):
        constraint = dict(item) if isinstance(item, Mapping) else {"text": item}
        if not constraint.get("constraintId"):
            constraint["constraintId"] = str(constraint.get("id") or _proposal_id("constraint", str(task.get("taskId") or ""), constraint))
        constraints[index] = constraint


def _external_evidence_items(evidence: Any, operation: str) -> dict[str, Mapping[str, Any]]:
    if evidence is None:
        return {}
    raw: Any = evidence
    if isinstance(evidence, Mapping) and "items" in evidence:
        raw = evidence["items"]
    elif isinstance(evidence, Mapping) and "refs" in evidence and isinstance(evidence["refs"], Mapping):
        raw = evidence["refs"]

    items: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, Mapping):
        # A single result may be passed directly as {id, body, ...}.
        if "body" in raw or "ok" in raw or "sourceType" in raw:
            ident = _first(raw, "id", "refId", "evidenceId", default=None)
            if ident is None:
                raise _error("evidence_missing_id", operation, "evidence result needs an id")
            items[str(ident)] = raw
        else:
            for key, value in raw.items():
                if not isinstance(value, Mapping):
                    raise _error("invalid_evidence", operation, "evidence items must be objects", ref=str(key))
                items[str(key)] = value
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for value in raw:
            if not isinstance(value, Mapping):
                raise _error("invalid_evidence", operation, "evidence items must be objects")
            ident = _first(value, "id", "refId", "evidenceId", default=None)
            if ident is None:
                raise _error("evidence_missing_id", operation, "evidence result needs an id")
            items[str(ident)] = value
    else:
        raise _error("invalid_evidence", operation, "evidence must contain item objects")
    return items


def _evidence_refs(patch: Mapping[str, Any], evidence: Any, operation: str) -> list[str]:
    value = _patch_payload(patch, "evidenceRefs", "evidenceRefIds", "refs", default=None)
    if value is None and isinstance(evidence, Mapping):
        raw_refs = evidence.get("refs")
        if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, (str, bytes)):
            value = raw_refs
        elif isinstance(raw_refs, str):
            value = [raw_refs]
    if value is None:
        items = _external_evidence_items(evidence, operation)
        value = list(items)
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, Sequence):
        raise _error("invalid_evidence_refs", operation, "evidenceRefs must be a list")
    refs: list[str] = []
    for item in value:
        ident = _identifier(item, "evidenceRef", operation)
        if ident not in refs:
            refs.append(ident)
    if not refs:
        raise _error("evidence_required", operation, "at least one evidence reference is required")
    return refs


def belief_evidence_problem(item: Mapping[str, Any]) -> tuple[str, str] | None:
    """One eligibility rule for generation hints and belief-update validation.

    Eligibility only describes readable evidence, not whether it supports a
    particular claim. That interpretation remains the model's responsibility.
    """
    if item.get("ok") is not True:
        return "evidence_not_successful", "evidence must have ok=true"
    if not isinstance(item.get("body"), str) or not item["body"].strip():
        return "evidence_body_missing", "successful evidence needs readable body"
    if str(item.get("fetchStatus", "")).strip().lower() not in _EVIDENCE_OK_STATUSES:
        return "evidence_not_fetched", "evidence fetch did not succeed"
    if str(item.get("sourceType", "")).strip().lower() not in _EVIDENCE_SOURCE_TYPES:
        return "evidence_source_invalid", "evidence sourceType is not allowed"
    return None


def _validate_external_evidence(
    patch: Mapping[str, Any], evidence: Any, operation: str
) -> list[str]:
    items = _external_evidence_items(evidence, operation)
    refs = _evidence_refs(patch, evidence, operation)
    violations = []
    for ref in refs:
        item = items.get(ref)
        if item is None:
            violations.append({"code": "evidence_ref_missing", "ref": ref, "message": "evidence reference is not present"})
            continue
        problem = belief_evidence_problem(item)
        if problem:
            violations.append({"code": problem[0], "ref": ref, "message": problem[1],
                               "sourceType": item.get("sourceType"), "fetchStatus": item.get("fetchStatus"),
                               "bodyError": str(item.get("bodyError") or "")[:240]})
    if violations:
        first = violations[0]
        readable_candidates = []
        seen = set()
        for ref, item in reversed(list(items.items())):
            if belief_evidence_problem(item) is not None:
                continue
            stable_ref = str(item.get("sourceUid") or item.get("canonicalUrl") or item.get("url") or ref)
            if stable_ref in seen:
                continue
            seen.add(stable_ref)
            readable_candidates.append({
                "id": stable_ref if stable_ref in items else ref,
                "sourceType": item.get("sourceType"), "title": str(item.get("title") or item.get("name") or "")[:160],
                "bodyExcerpt": item["body"][:1200], "fetchStatus": item.get("fetchStatus"),
                "historical": bool(item.get("historical")), "observedAt": item.get("observedAt"),
            })
            if len(readable_candidates) >= 4:
                break
        raise _error(first["code"], operation, first["message"], ref=first["ref"], violations=violations,
                     readableCandidates=readable_candidates,
                     repair="这些引用不能验证新认识。逐项核对实际正文：仅在其他可读正文完整支持命题时移除无效引用，否则缩小命题或改查新来源。不要重复已有成功查询，也不要把摘要当正文。")
    return refs


def _proposal_id(operation: str, turn_id: str, payload: Any) -> str:
    material = f"{turn_id}\n{operation}\n{_json(payload)}".encode("utf-8")
    return f"{operation.lower()}-{runtime_fingerprint(material, length=16)}"


def _register_user_adaptation(preferences: dict[str, Any], *, adaptation_id: str,
                              kind: str, value: Any, source: str, turn_id: str) -> bool:
    """Validate identity and report whether this proposal created a new record."""
    if len(_json(value)) > 4000:
        raise _error("adaptation_too_large", message="keep an adaptation within 4000 characters")
    records = preferences.setdefault("userAdaptations", [])
    if not isinstance(records, list):
        raise _error("invalid_adaptations", message="userAdaptations must be a list")
    for record in records:
        if record.get("adaptationId") == adaptation_id:
            if record.get("kind") != kind or record.get("value") != value:
                raise _error("adaptation_id_conflict", message="use a new adaptation ID for changed content")
            return False
    if len(records) >= 128:
        raise _error("adaptation_capacity", message="user adaptation catalogue is full")
    records.append({"adaptationId": adaptation_id, "kind": kind, "scope": "user",
                    "status": "proposed", "value": copy.deepcopy(value),
                    "sourceSpan": source, "sourceTurnId": turn_id})
    return True


def _bind_user_belief_scope(belief: dict[str, Any], state: Mapping[str, Any], operation: str, *, prior: Mapping[str, Any] | None = None) -> None:
    scope = belief.get("scope", "user" if prior and prior.get("memoryId") else "session")
    if scope not in {"session", "user"}:
        raise _error("belief_scope_invalid", operation, "user belief scope must be session or user")
    if prior and prior.get("memoryId"):
        scope = "user"
    belief["scope"] = scope
    belief.pop("memoryId", None)
    if scope == "user":
        identity = f"{state['conversationId']}\n{belief['beliefId']}".encode("utf-8")
        belief["memoryId"] = "mem_cognitive_" + runtime_fingerprint(identity, length=24)


def _append_unique(items: list[Any], value: Any) -> None:
    key = _constraint_key(value)
    if all(_constraint_key(existing) != key for existing in items):
        items.append(copy.deepcopy(value))


def _task_source(patch: Mapping[str, Any], user_message: str, operation: str) -> str:
    value = _source_span(patch, user_message, operation, required=True)
    assert value is not None
    return value


_START_TASK_FIELDS = (
    "goal",
    "taskGoal",
    "target",
    "taskTarget",
    "constraints",
    "plan",
    "toolPlan",
    "successCriteria",
    "criteria",
    "userIntent",
    "targetObjects",
    "toolArguments",
)
_TASK_RESTRICTIONS = frozenset({"read_only", "no_web", "no_observation", "no_navigation", "no_retry", "none"})


def _start_task_payload(patch: Mapping[str, Any], operation: str) -> dict[str, Any]:
    """Extract the finite TaskFrame fields accepted by START_TASK.

    The model may place the payload directly on the patch or under ``value``.
    A scalar ``value`` is the shorthand for the new goal.  No fields from the
    previous task are copied, and a caller-supplied taskId is intentionally
    ignored because the backend owns task identity.
    """

    nested = patch.get("value")
    if isinstance(nested, Mapping):
        payload = copy.deepcopy(dict(nested))
    else:
        payload = {}
        if nested is not None:
            payload["goal"] = copy.deepcopy(nested)
    for key in _START_TASK_FIELDS:
        if key in patch:
            payload[key] = copy.deepcopy(patch[key])
    return payload


def _start_task_constraints(
    value: Any,
    *,
    source: str,
    user_message: str,
    operation: str,
) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _error("invalid_task_constraints", operation, "constraints must be a list")

    constraints: list[Any] = []
    for item in value:
        if isinstance(item, Mapping):
            constraint = copy.deepcopy(dict(item))
            restriction = constraint.get("restriction")
            if restriction is not None and restriction not in _TASK_RESTRICTIONS:
                raise _error(
                    "invalid_constraint_restriction",
                    operation,
                    "restriction is not an allowed machine constraint",
                )
            if "text" not in constraint:
                raise _error("invalid_constraint", operation, "constraint object needs text")
            if not isinstance(constraint["text"], str):
                raise _error("invalid_constraint", operation, "constraint.text must be a string")
            nested_source = _source_span(constraint, user_message, operation, required=False)
            constraint["sourceSpan"] = nested_source or source
        elif isinstance(item, str):
            constraint = {"text": item, "sourceSpan": source}
        else:
            raise _error("invalid_constraint", operation, "constraint must be text or an object")
        constraints.append(constraint)
    return constraints


def _start_task_list(value: Any, name: str, operation: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _error("invalid_task_list", operation, f"{name} must be a list")
    if name == "plan" and any(isinstance(item, Mapping) and (item.get("name") or item.get("tool")) for item in value):
        try:
            return normalized_tool_plan(list(value))
        except ValueError as error:
            raise _error("invalid_tool_plan", operation, str(error)) from error
    return copy.deepcopy(list(value))


def _task_has_history(task: Mapping[str, Any]) -> bool:
    if task.get("taskId"):
        return True
    if str(task.get("status", "idle")).strip().lower() not in {"", "idle"}:
        return True
    return any(
        task.get(key)
        for key in ("goal", "target", "constraints", "plan", "successCriteria", "toolArguments")
    )


def _new_task_id(state: Mapping[str, Any], turn_id: str, payload: Mapping[str, Any]) -> str:
    """Generate a backend-owned ID that cannot reuse an existing task ID."""

    task = state.get("task", {})
    history = state.get("taskHistory", [])
    prior_ids = {
        str(item.get("taskId"))
        for item in history
        if isinstance(item, Mapping) and item.get("taskId")
    }
    prior_id = str(task.get("taskId") or "") if isinstance(task, Mapping) else ""
    ordinal = len(history)
    suffix = 0
    while True:
        material = {
            "conversationId": state.get("conversationId"),
            "turnId": turn_id,
            "priorTaskId": prior_id,
            "historyLength": ordinal,
            "suffix": suffix,
            "payload": payload,
        }
        candidate = f"task-{runtime_fingerprint(_json(material))}"
        if candidate not in prior_ids and candidate != prior_id:
            return candidate
        suffix += 1


def _experience_payload(patch: Mapping[str, Any], operation: str) -> dict[str, Any]:
    value = _patch_record_payload(patch, "experience", "rule", default=None)
    if value is None:
        value = patch.get("value", patch)
    if not isinstance(value, Mapping):
        raise _error("invalid_experience", operation, "experience/rule must be an object")
    payload = copy.deepcopy(dict(value))
    # Do not permit proposals to smuggle execution or security state.
    _scan_forbidden(payload, operation)
    return payload


def task_start_is_additive(task: Mapping[str, Any], patch: Mapping[str, Any], turn_id: str) -> bool:
    """Recognize a same-turn declaration that cannot replace existing work.

    A tool-only first response can register the goal before the model names
    its target. Completing that declaration is not another task. This uses
    exact structured equality; it never decides that paraphrases are alike.
    """
    value = patch.get("value")
    return bool(
        task.get("taskId") and task.get("startedByTurnId") == turn_id
        and patch.get("operation") == "START_TASK" and not patch.get("replaceCurrentTaskId")
        and patch.get("targetId") in {None, task.get("taskId")}
        and isinstance(value, Mapping) and set(value) <= {"goal", "target"}
        and value.get("goal") == task.get("goal")
        and (value.get("target") is None or task.get("target") is None or value["target"] == task.get("target"))
    )


def _apply_patch(
    state: dict[str, Any],
    patch: Mapping[str, Any],
    *,
    operation: str,
    turn_id: str,
    user_message: str,
    evidence: Any,
    context_ids: Sequence[str],
    replayed_supersession: bool = False,
) -> None:
    """Apply one already validated patch to a private state copy."""

    task = state["task"]
    context = state["context"]
    preferences = state["preferences"]
    if task.get("terminalFailure") and operation in {
        "REPLACE_TOOL_PLAN", "REPLACE_TOOL_TARGET", "REPLACE_TOOL_ARGUMENTS", "RESET_PLAN_FROM_STEP",
    }:
        raise _error("task_terminal_failed", operation, "Explicitly resume the failed task in a later turn before replanning")

    if operation == "PREFER_CONTEXT":
        ids = _ids_from_patch(patch, operation, fallback=context_ids)
        if not set(ids).issubset(set(context_ids)):
            raise _error("context_id_missing", operation, "requested context is not in the frozen candidate set")
        for ident in ids:
            if ident not in context["preferredIds"]:
                context["preferredIds"].append(ident)
            context["suppressedIds"] = [x for x in context["suppressedIds"] if str(x) != ident]
        return

    if operation == "SUPPRESS_CONTEXT":
        ids = _ids_from_patch(patch, operation, fallback=context_ids)
        if not set(ids).issubset(set(context_ids)):
            raise _error("context_id_missing", operation, "requested context is not in the frozen candidate set")
        for ident in ids:
            if ident not in context["suppressedIds"]:
                context["suppressedIds"].append(ident)
            context["preferredIds"] = [x for x in context["preferredIds"] if str(x) != ident]
        return

    if operation == "EXPAND_RECALL_SCOPE":
        scope = _patch_payload(patch, "scope", "recallScope", default=None)
        if not isinstance(scope, str) or not scope.strip():
            raise _error("invalid_recall_scope", operation, "scope must be a non-empty string")
        try:
            context["recallScope"] = normalize_recall_scope(scope, default=None)
        except ValueError:
            raise _error("invalid_recall_scope", operation,
                         "scope must be one of conversation, archive, or long_term")
        return

    if operation == "REWRITE_RETRIEVAL_QUERY":
        query = _patch_payload(patch, "query", "recallQuery", default=None)
        if not isinstance(query, str):
            raise _error("invalid_retrieval_query", operation, "query must be a string")
        if len(query) > MAX_RECALL_QUERY_LENGTH:
            raise _error("invalid_retrieval_query", operation,
                         f"query must be at most {MAX_RECALL_QUERY_LENGTH} characters")
        context["recallQuery"] = query.strip()
        return

    if operation == "ADD_REFERENCE_BINDING":
        value = _patch_payload(patch, "reference", "binding", default=None)
        if value is None:
            value = patch
        if not isinstance(value, Mapping):
            raise _error("invalid_reference", operation, "reference binding must be an object")
        ref = copy.deepcopy(dict(value))
        rid = _record_id(ref, "referenceId", "targetId", "id")
        if rid is None:
            rid = _reference_id(patch, operation)
        ref["referenceId"] = rid
        ref.setdefault("status", "active")
        _upsert_record(state["references"], ref, rid, operation, "reference")
        return

    if operation == "REJECT_REFERENCE_BINDING":
        rid = _reference_id(patch, operation)
        try:
            index, ref = _find_record(state["references"], rid, operation, "reference")
        except CognitivePatchError as exc:
            if exc.code != "target_not_found":
                raise
            state["references"].append({"referenceId": rid, "status": "rejected"})
        else:
            ref["status"] = "rejected"
            state["references"][index] = ref
        return

    if operation in {"RETRACT_INFERENCE", "MARK_BELIEF_CHALLENGED", "MARK_EXTERNAL_BELIEF_STALE", "REQUIRE_FACT_VERIFICATION"}:
        bid = _belief_id(patch, operation)
        index, belief = _find_record(state["beliefs"], bid, operation, "belief")
        if operation == "RETRACT_INFERENCE":
            if _belief_domain(belief) not in _INFERENCE_DOMAINS:
                raise _error("belief_domain_invalid", operation, "only assistant inferences may be retracted", id=bid)
            belief["status"] = "retracted"
        elif operation == "MARK_BELIEF_CHALLENGED":
            belief["status"] = "challenged"
            belief["challengedByTurnId"] = turn_id
        elif operation == "MARK_EXTERNAL_BELIEF_STALE":
            if _belief_domain(belief) not in _EXTERNAL_DOMAINS:
                raise _error("belief_domain_invalid", operation, "only external beliefs may be marked stale", id=bid)
            belief["status"] = "stale"
        else:
            belief["verificationRequired"] = True
            belief["verificationRequestedByTurnId"] = turn_id
        state["beliefs"][index] = belief
        return

    if operation == "SUPERSEDE_USER_BELIEF":
        source = _source_span(patch, user_message, operation, required=True)
        old_id = _patch_payload(patch, "oldBeliefId", "beliefId", "targetId", "id", default=None)
        old_id = _identifier(old_id, "oldBeliefId", operation)
        index, old = _find_record(state["beliefs"], old_id, operation, "belief")
        if _belief_domain(old) not in _USER_DOMAINS and str(old.get("authority", "")).lower() != "user":
            raise _error("belief_domain_invalid", operation, "only user beliefs may be superseded", id=old_id)
        value = _patch_payload(patch, "newBelief", "belief", default=None)
        if value is None:
            value = patch.get("value")
        if value is None and any(key in patch for key in ("beliefId", "domain", "claim", "content")):
            value = {
                key: copy.deepcopy(value)
                for key, value in patch.items()
                if key not in {"operation", "op", "oldBeliefId", "sourceSpan", "source_span"}
            }
        if not isinstance(value, Mapping):
            raise _error("invalid_belief", operation, "newBelief must be an object")
        new_belief = copy.deepcopy(dict(value))
        if _belief_domain(new_belief) and _belief_domain(new_belief) not in _USER_DOMAINS:
            raise _error("belief_domain_invalid", operation, "user correction cannot create external or tool facts")
        new_belief["domain"] = _belief_domain(new_belief) or "user_fact"
        new_belief["authority"] = "user"
        new_id = _record_id(new_belief, "beliefId", "targetId", "id")
        if new_id is None:
            new_id = _proposal_id(operation, turn_id, new_belief)
        new_belief["beliefId"] = new_id
        new_belief["status"] = "active"
        new_belief["supersedes"] = old_id
        _record_source(new_belief, source)
        _bind_user_belief_scope(new_belief, state, operation, prior=old)
        existing = next((item for item in state["beliefs"]
                         if _record_id(item, "beliefId", "id") == new_id), None)
        if existing is not None:
            # Only a backend-held patch in this turn's pending ledger can be
            # replayed. A changed or withdrawn effect is still a conflict.
            if (replayed_supersession and old_id != new_id
                    and old.get("status") == "superseded" and existing == new_belief):
                return
            raise _error("belief_id_exists", operation, "a superseding belief needs a new version ID")
        if replayed_supersession:
            raise _error("belief_replay_state_conflict", operation, "accepted supersession no longer exists in pending state")
        old["status"] = "superseded"
        state["beliefs"][index] = old
        state["beliefs"].append(new_belief)
        return

    if operation == "UPDATE_BELIEF_FROM_EVIDENCE":
        refs = _validate_external_evidence(patch, evidence, operation)
        value = _patch_payload(patch, "belief", "newBelief", default=None)
        if value is None:
            value = patch.get("value")
        if value is None and any(key in patch for key in ("beliefId", "domain", "claim", "content")):
            value = {
                key: copy.deepcopy(value)
                for key, value in patch.items()
                if key not in {"operation", "op", "evidenceRefs", "evidenceRefIds", "refs"}
            }
        if not isinstance(value, Mapping):
            raise _error("invalid_belief", operation, "belief must be an object")
        belief = copy.deepcopy(dict(value))
        bid = _record_id(belief, "beliefId", "id")
        if bid is None:
            bid = _record_id(patch, "beliefId", "targetId", "id")
        if bid is None:
            raise _error("missing_belief_id", operation, "external belief needs beliefId")
        belief["beliefId"] = bid
        domain = _belief_domain(belief)
        if domain not in _EVIDENCE_DOMAINS:
            raise _error("belief_domain_invalid", operation, "evidence may update external, tool or perceptual beliefs only", id=bid)
        source_items = _external_evidence_items(evidence, operation)
        required_source = "visual" if domain in _PERCEPTUAL_DOMAINS else "tool" if domain in _TOOL_DOMAINS else None
        if required_source and any(source_items[ref].get("sourceType") != required_source for ref in refs):
            raise _error("evidence_domain_mismatch", operation, "evidence must match the belief domain", id=bid)
        belief["status"] = "active"
        belief["evidenceRefs"] = refs
        belief["verifiedByTurnId"] = turn_id
        prior_id = _record_id(patch, "targetId") or bid
        if prior_id != bid and any(_record_id(item, "beliefId", "id") == bid for item in state["beliefs"]):
            raise _error("belief_id_conflict", operation, "replacement beliefId already exists", id=bid)
        # Keep the challenged/old content available for later audit and
        # review.  Strip prior chains from each snapshot so repeated evidence
        # refreshes do not recursively duplicate the entire state.
        prior_index = next(
            (
                index
                for index, candidate in enumerate(state["beliefs"])
                if isinstance(candidate, Mapping)
                and _record_id(candidate, "id", "targetId", "beliefId", "referenceId", "experienceId") == prior_id
            ),
            None,
        )
        if _record_id(patch, "targetId") and prior_index is None:
            raise _error("target_not_found", operation, "target belief does not exist", id=prior_id)
        if prior_index is not None:
            prior = copy.deepcopy(dict(state["beliefs"][prior_index]))
            if _belief_domain(prior) in _USER_DOMAINS:
                raise _error("belief_domain_invalid", operation, "external evidence cannot overwrite a user-owned belief", id=bid)
            prior_history = prior.pop("history", None)
            prior_versions = prior.pop("versions", None)
            history = copy.deepcopy(belief.get("history", []))
            versions = copy.deepcopy(belief.get("versions", []))
            if isinstance(prior_history, list):
                history.extend(copy.deepcopy(prior_history))
            if isinstance(prior_versions, list):
                versions.extend(copy.deepcopy(prior_versions))
            prior["supersededByEvidenceTurnId"] = turn_id
            if prior_id != bid:
                retired = copy.deepcopy(dict(state["beliefs"][prior_index]))
                retired["status"] = "superseded"
                retired["supersededBy"] = bid
                retired["supersededByEvidenceTurnId"] = turn_id
                state["beliefs"][prior_index] = retired
                belief["supersedes"] = prior_id
            history.append(prior)
            versions.append(copy.deepcopy(prior))
            belief["history"] = history
            belief["versions"] = versions
        _upsert_record(state["beliefs"], belief, bid, operation, "belief")
        return

    if operation == "PROPOSE_MEMORY_CORRECTION":
        value = _patch_payload(patch, "proposal", "correction", default=None)
        if value is None:
            value = patch.get("value", patch)
        if not isinstance(value, Mapping):
            raise _error("invalid_memory_proposal", operation, "memory correction proposal must be an object")
        proposal = copy.deepcopy(dict(value))
        domain = str(proposal.get("domain", "")).lower()
        if domain in _USER_DOMAINS or proposal.get("userFact") is True:
            _source_span(patch, user_message, operation, required=True)
        proposal.setdefault("proposalId", _proposal_id(operation, turn_id, proposal))
        proposal["status"] = "proposed"
        state["context"].setdefault("memoryCorrectionProposals", []).append(proposal)
        return

    if operation in {"ADD_USER_BELIEF", "ADD_INFERENCE"}:
        value = _patch_payload(patch, "belief", "newBelief", default=None)
        if value is None:
            value = patch.get("value")
        if value is None and any(key in patch for key in ("beliefId", "targetId", "domain", "claim", "content")):
            value = {
                key: copy.deepcopy(item)
                for key, item in patch.items()
                if key not in {"operation", "op", "sourceSpan", "source_span"}
            }
        if not isinstance(value, Mapping):
            raise _error("invalid_belief", operation, "belief must be an object")
        belief = copy.deepcopy(dict(value))
        bid = declared_belief_patch_id(patch)
        if bid is None:
            bid = _proposal_id(operation, turn_id, belief)
        belief["beliefId"] = bid
        domain = _belief_domain(belief)
        if operation == "ADD_USER_BELIEF":
            if domain and domain not in _USER_DOMAINS:
                raise _error(
                    "belief_domain_invalid",
                    operation,
                    "ADD_USER_BELIEF may create user-domain beliefs only",
                    id=bid,
                )
            source = _source_span(patch, user_message, operation, required=True)
            belief["domain"] = domain or "user_fact"
            belief["authority"] = "user"
            belief["status"] = "active"
            _record_source(belief, source)
            _bind_user_belief_scope(belief, state, operation)
        else:
            if domain and domain not in _INFERENCE_DOMAINS:
                raise _error(
                    "belief_domain_invalid",
                    operation,
                    "ADD_INFERENCE cannot create user or external facts",
                    id=bid,
                )
            supplied_source = _source_span(patch, user_message, operation, required=False)
            belief["domain"] = domain or "assistant_inference"
            belief["authority"] = "assistant"
            belief["status"] = "active"
            _record_source(belief, supplied_source)
            if context_ids and "contextIds" not in belief:
                belief["contextIds"] = list(context_ids)
        for existing in state["beliefs"]:
            if _record_id(existing, "beliefId", "id") == bid:
                if all(existing.get(key) == value for key, value in belief.items()):
                    return
                raise _error("belief_id_exists", operation, "use a versioned correction to replace an existing belief")
        _upsert_record(state["beliefs"], belief, bid, operation, "belief")
        return

    if operation == "START_TASK":
        source = _task_source(patch, user_message, operation)
        if task_start_is_additive(task, patch, turn_id):
            target = patch["value"].get("target")
            if target is not None and task.get("target") is None:
                task["target"] = copy.deepcopy(target)
                task["targetSourceSpan"] = source
            return
        payload = _start_task_payload(patch, operation)
        goal = _first(payload, "goal", "taskGoal", default=None)
        if not isinstance(goal, str) or not goal.strip():
            raise _error("invalid_task_goal", operation, "START_TASK needs a non-empty goal")

        # Archive the complete prior TaskFrame before replacing it.  The
        # archived copy is immutable history; in particular, its constraints
        # and plan must never leak into the new active task.
        if _task_has_history(task):
            archived = copy.deepcopy(task)
            archived["endedByTurnId"] = turn_id
            archived["endReason"] = "START_TASK"
            state.setdefault("taskHistory", []).append(archived)

        new_task: dict[str, Any] = {
            "taskId": _new_task_id(state, turn_id, payload),
            "goal": goal,
            "goalSourceSpan": source,
            "target": copy.deepcopy(_first(payload, "target", "taskTarget", default=None)),
            "constraints": _start_task_constraints(
                _first(payload, "constraints", default=None),
                source=source,
                user_message=user_message,
                operation=operation,
            ),
            "plan": _start_task_list(_first(payload, "plan", "toolPlan", default=None), "plan", operation),
            "successCriteria": _start_task_list(
                _first(payload, "successCriteria", "criteria", default=None),
                "successCriteria",
                operation,
            ),
            "status": "active",
            "startedByTurnId": turn_id,
            "statusSourceSpan": source,
        }
        if new_task["target"] is not None:
            new_task["targetSourceSpan"] = source
        if new_task["plan"]:
            try:
                tool_plan_identity(new_task["plan"])
            except ValueError:
                # Keep the normalized plan for the normal execution validator,
                # but an unknown or invalid schema cannot establish a replay
                # marker for the current turn.
                pass
            else:
                new_task["planDeclaredByTurnId"] = turn_id
        for key in ("userIntent", "targetObjects", "toolArguments"):
            if key not in payload:
                continue
            value = payload[key]
            if key == "targetObjects":
                value = _start_task_list(value, key, operation)
            elif key == "toolArguments":
                if not isinstance(value, Mapping):
                    raise _error("invalid_tool_arguments", operation, "arguments must be an object")
                value = copy.deepcopy(dict(value))
            else:
                value = copy.deepcopy(value)
            new_task[key] = value

        task.clear()
        task.update(new_task)
        return

    if operation == "REPLACE_TASK_GOAL":
        source = _task_source(patch, user_message, operation)
        goal = _patch_payload(patch, "goal", "taskGoal", default=None)
        if not isinstance(goal, str) or not goal.strip():
            raise _error("invalid_task_goal", operation, "goal must be a non-empty string")
        if task.get("goal") != goal:
            task["planRevision"] = int(task.get("planRevision", 0)) + 1
            for step in task["plan"]:
                if isinstance(step, dict) and step.get("name"):
                    step["status"] = "invalidated"
                    step["invalidatedByTurnId"] = turn_id
                    step.pop("receiptId", None)
                    step.pop("receiptIds", None)
        task["goal"] = goal
        task["goalSourceSpan"] = source
        if task.get("status") == "idle" and not task.get("taskId"):
            # A model-supplied goal initializes the empty frame. It must not
            # leave a runnable task permanently idle merely because the model
            # used the update operation for its first explicit goal.
            task["taskId"] = "task-" + runtime_fingerprint(f"{state.get('conversationId')}:{turn_id}:goal")
            task["status"] = "active"
            task["startedByTurnId"] = turn_id
        return

    if operation == "ADD_TASK_CONSTRAINT":
        source = _task_source(patch, user_message, operation)
        value = _patch_payload(patch, "constraint", default=None)
        if value is None:
            value = patch.get("value")
        if isinstance(value, Mapping):
            constraint = copy.deepcopy(dict(value))
            restriction = constraint.get("restriction")
            if restriction is not None and restriction not in _TASK_RESTRICTIONS:
                raise _error("invalid_constraint_restriction", operation, "restriction is not an allowed machine constraint")
            if "text" not in constraint:
                raise _error("invalid_constraint", operation, "constraint object needs text")
            if not isinstance(constraint["text"], str):
                raise _error("invalid_constraint", operation, "constraint.text must be a string")
        elif isinstance(value, str):
            constraint = value
        else:
            raise _error("invalid_constraint", operation, "constraint must be text or an object")
        if isinstance(constraint, Mapping):
            constraint["sourceSpan"] = source
        else:
            constraint = {"text": constraint, "sourceSpan": source}
        if not constraint.get("constraintId"):
            constraint["constraintId"] = str(constraint.get("id") or _proposal_id("constraint", str(task.get("taskId") or ""), constraint))
        _append_unique(task["constraints"], constraint)
        return

    if operation == "REMOVE_TASK_CONSTRAINT":
        source = _task_source(patch, user_message, operation)
        value = _patch_payload(patch, "constraintId", "targetId", "constraint", "id", default=None)
        if value is None:
            raise _error("missing_constraint", operation, "constraint or constraintId is required")
        if isinstance(value, (str, int, float, bool)):
            key = str(value)
        else:
            key = _constraint_key(value)
        before = len(task["constraints"])
        kept = []
        removed = []
        for constraint in task["constraints"]:
            candidate = constraint.get("constraintId", constraint.get("id")) if isinstance(constraint, Mapping) else constraint
            if str(candidate) == key or _constraint_key(constraint) == key:
                removed.append(copy.deepcopy(constraint))
                continue
            kept.append(constraint)
        if len(kept) == before:
            if any(item.get("removedByTurnId") == turn_id and str((item.get("constraint") or {}).get("constraintId") or "") == key
                   for item in task.get("constraintHistory", []) if isinstance(item, Mapping)):
                # A continuation can replay an already accepted removal. The
                # deletion receipt, scoped to this turn and immutable ID,
                # distinguishes that replay from an invented missing target.
                return
            raise _error("target_not_found", operation, "constraint was not found", constraint=value)
        task["constraints"] = kept
        task["constraintHistory"] = [*task.get("constraintHistory", []),
            *[{"constraint": item, "removedByTurnId": turn_id, "sourceSpan": source} for item in removed]][-32:]
        task["lastConstraintChangeSourceSpan"] = source
        return

    if operation == "CHANGE_TASK_TARGET":
        source = _task_source(patch, user_message, operation)
        target = _patch_payload(patch, "target", "taskTarget", default=None)
        if target is None:
            raise _error("missing_task_target", operation, "target is required")
        if task.get("target") != target:
            task["planRevision"] = int(task.get("planRevision", 0)) + 1
            for step in task["plan"]:
                if isinstance(step, dict) and step.get("name"):
                    step["status"] = "invalidated"
                    step["invalidatedByTurnId"] = turn_id
                    step.pop("receiptId", None)
                    step.pop("receiptIds", None)
        task["target"] = copy.deepcopy(target)
        task["targetSourceSpan"] = source
        return

    if operation == "REDEFINE_SUCCESS_CRITERIA":
        source = _task_source(patch, user_message, operation)
        criteria = _patch_payload(patch, "successCriteria", "criteria", default=None)
        if isinstance(criteria, (str, bytes)) or not isinstance(criteria, Sequence):
            raise _error("invalid_success_criteria", operation, "criteria must be a list")
        task["successCriteria"] = copy.deepcopy(list(criteria))
        task["successCriteriaSourceSpan"] = source
        return

    if operation == "INVALIDATE_TASK_STEP":
        value = _patch_payload(patch, "stepId", "taskStepId", "stepIndex", "index", default=None)
        if value is None:
            raise _error("missing_task_step", operation, "stepId or stepIndex is required")
        index = None
        if isinstance(value, int) and not isinstance(value, bool):
            index = value
        else:
            for candidate_index, candidate in enumerate(task["plan"]):
                if isinstance(candidate, Mapping) and str(_first(candidate, "stepId", "id", default="")) == str(value):
                    index = candidate_index
                    break
        if index is None or index < 0 or index >= len(task["plan"]):
            raise _error("target_not_found", operation, "task step was not found", step=value)
        item = task["plan"][index]
        if not isinstance(item, Mapping):
            item = {"value": copy.deepcopy(item)}
        item = copy.deepcopy(dict(item))
        item["status"] = "invalidated"
        item["invalidatedByTurnId"] = turn_id
        item.pop("receiptId", None)
        item.pop("receiptIds", None)
        task["plan"][index] = item
        return

    if operation == "RESET_PLAN_FROM_STEP":
        value = _patch_payload(patch, "fromStep", "stepIndex", "stepId", "index", default=0)
        index = None
        if isinstance(value, int) and not isinstance(value, bool):
            index = value
        else:
            for candidate_index, candidate in enumerate(task["plan"]):
                if isinstance(candidate, Mapping) and str(_first(candidate, "stepId", "id", default="")) == str(value):
                    index = candidate_index
                    break
        if index is None or index < 0 or index >= max(1, len(task["plan"])):
            raise _error("target_not_found", operation, "reset step was not found", step=value)
        plan = []
        for candidate_index, candidate in enumerate(task["plan"]):
            if not isinstance(candidate, Mapping):
                candidate = {"value": copy.deepcopy(candidate)}
            item = copy.deepcopy(dict(candidate))
            if candidate_index >= index:
                item["status"] = "pending"
            plan.append(item)
        task["plan"] = plan
        task["planResetFromStep"] = index
        # Reset is the explicit request for a new attempt. Old execution
        # bindings must not immediately complete the reset steps again.
        task["planRevision"] = int(task.get("planRevision", 0)) + 1
        for item in task["plan"][index:]:
            item.pop("receiptId", None)
            item.pop("receiptIds", None)
            item.pop("invalidatedByTurnId", None)
        task["planDeclaredByTurnId"] = turn_id
        return

    if operation in {"CANCEL_TASK", "FAIL_TASK", "PAUSE_TASK", "RESUME_TASK"}:
        source = _task_source(patch, user_message, operation)
        if operation == "RESUME_TASK" and task.get("terminalFailureTurnId") == turn_id:
            raise _error("task_terminal_failed_this_turn", operation, "Cannot resume a task terminated in this turn")
        if operation == "FAIL_TASK" and (not task.get("taskId") or patch.get("targetId") != task["taskId"]):
            raise _error("task_identity_mismatch", operation, "FAIL_TASK requires the current taskId as targetId")
        previous_status = task.get("status")
        task["status"] = {"CANCEL_TASK": "cancelled", "FAIL_TASK": "failed", "PAUSE_TASK": "paused", "RESUME_TASK": "active"}[operation]
        task["statusSourceSpan"] = source
        if operation == "FAIL_TASK":
            task["terminalFailure"] = True
            task["terminalFailureTurnId"] = turn_id
        elif operation == "RESUME_TASK":
            task.pop("terminalFailure", None)
            task.pop("terminalFailureTurnId", None)
        if operation in {"CANCEL_TASK", "FAIL_TASK"}:
            for index, candidate in enumerate(task["plan"]):
                if isinstance(candidate, Mapping):
                    item = copy.deepcopy(dict(candidate))
                    if item.get("status") not in {"completed", "invalidated"}:
                        item["status"] = "cancelled"
                    task["plan"][index] = item
        elif operation == "RESUME_TASK" and previous_status in {"cancelled", "failed"}:
            # Explicit resumption restores remaining work; cancellation cannot
            # undo successful actions or make them run again.
            task["planRevision"] = int(task.get("planRevision", 0)) + 1
            task["planDeclaredByTurnId"] = turn_id
            for item in task["plan"]:
                if isinstance(item, dict) and item.get("status") == "cancelled":
                    item["status"] = "pending"
                    item.pop("receiptId", None)
                    item.pop("receiptIds", None)
        return

    if operation == "REPLACE_TOOL_PLAN":
        plan = _patch_payload(patch, "plan", "toolPlan", default=None)
        try:
            proposed = normalized_tool_plan(plan)
            try:
                current_declaration = normalized_tool_plan(task.get("plan", []))
            except ValueError:
                current_declaration = None  # A descriptive old plan can still be replaced.
            try:
                proposed_identity = tool_plan_identity(proposed)
                current_identity = (
                    tool_plan_identity(current_declaration)
                    if current_declaration is not None else None
                )
            except ValueError:
                # Unknown schemas and invalid arguments are never safe replay
                # identities, even when their raw dictionaries are equal.
                proposed_identity = current_identity = None
            # Pro/Flash continuations may restate the accepted plan. Within
            # one turn an identical declaration is not a fresh execution;
            # retain backend statuses and their exact receipt bindings.
            # A later turn, changed plan, or invalidated plan still replaces it.
            if (task.get("planDeclaredByTurnId") == turn_id
                    and current_identity is not None
                    and current_identity == proposed_identity
                    and not any(step.get("status") in {"invalidated", "cancelled"}
                                for step in task.get("plan", []))):
                return
            task["plan"] = proposed
            task["planRevision"] = int(task.get("planRevision", 0)) + 1
            task["planDeclaredByTurnId"] = turn_id
        except ValueError as error:
            raise _error("invalid_tool_plan", operation, str(error)) from error
        return

    if operation == "REPLACE_TOOL_TARGET":
        selector = patch.get("value") if isinstance(patch.get("value"), Mapping) else patch
        target = _patch_payload(patch, "target", "toolTarget", default=None)
        field = selector.get("field")
        if target is None or not isinstance(field, str) or not field:
            raise _error("missing_tool_target", operation, "value needs stepId, field (tool argument name), and target")
        try:
            step = selected_tool_step(task["plan"], selector)
        except ValueError as error:
            raise _error("tool_step_missing", operation, str(error)) from error
        step["args"][field] = copy.deepcopy(target)
        step["status"] = "pending"
        step.pop("receiptId", None)
        step.pop("receiptIds", None)
        step.pop("invalidatedByTurnId", None)
        task["planRevision"] = int(task.get("planRevision", 0)) + 1
        task["planDeclaredByTurnId"] = turn_id
        task["target"] = copy.deepcopy(target)
        return

    if operation == "REPLACE_TOOL_ARGUMENTS":
        selector = patch.get("value") if isinstance(patch.get("value"), Mapping) else patch
        arguments = _patch_payload(patch, "arguments", "toolArguments", default=None)
        if not isinstance(arguments, Mapping):
            raise _error("invalid_tool_arguments", operation, "arguments must be an object")
        try:
            step = selected_tool_step(task["plan"], selector)
        except ValueError as error:
            raise _error("tool_step_missing", operation, str(error)) from error
        step["args"] = copy.deepcopy(dict(arguments))
        step["status"] = "pending"
        step.pop("receiptId", None)
        step.pop("receiptIds", None)
        step.pop("invalidatedByTurnId", None)
        task["planRevision"] = int(task.get("planRevision", 0)) + 1
        task["planDeclaredByTurnId"] = turn_id
        return

    if operation in {"REUSE_EVIDENCE", "REANALYZE_EVIDENCE", "REFRESH_EVIDENCE"}:
        refs = _evidence_refs(patch, evidence, operation)
        context.setdefault("evidenceRefs", [])
        for ref in refs:
            if ref not in context["evidenceRefs"]:
                context["evidenceRefs"].append(ref)
        context["lastEvidenceAction"] = {"operation": operation, "refs": refs, "turnId": turn_id}
        return

    if operation == "INVALIDATE_PENDING_ACTION":
        # The overlay owns the authoritative invalidation list.  Keep only a
        # safe context marker here; execution receipts remain untouched.
        ids = _ids_from_patch(patch, operation)
        context.setdefault("invalidatedPendingActionIds", [])
        for ident in ids:
            if ident not in context["invalidatedPendingActionIds"]:
                context["invalidatedPendingActionIds"].append(ident)
        return

    if operation == "PROPOSE_ROLLBACK":
        value = _patch_payload(patch, "proposal", "rollback", default=None)
        if value is None:
            value = patch.get("value", {})
        proposal = copy.deepcopy(value)
        _scan_forbidden(proposal, operation)
        if not isinstance(proposal, Mapping):
            proposal = {"value": proposal}
        proposal = dict(proposal)
        proposal.setdefault("proposalId", _proposal_id(operation, turn_id, proposal))
        proposal["status"] = "proposed"
        context.setdefault("rollbackProposals", []).append(proposal)
        return

    if operation == "ADJUST_PERSONA_STYLE":
        value = _patch_payload(patch, "style", "personaStyle", default=None)
        if value is None:
            raise _error("missing_persona_style", operation, "style is required")
        _scan_forbidden(value, operation)
        preferences.setdefault("session", {})["personaStyle"] = copy.deepcopy(value)
        return

    if operation == "RESET_EMOTION_CARRYOVER":
        preferences.setdefault("session", {})["emotionCarryover"] = None
        preferences["session"]["emotionCarryoverResetByTurnId"] = turn_id
        return

    if operation == "SET_SESSION_INTERACTION_PREFERENCE":
        value = _patch_record_payload(patch, "preference", "preferences", default=None)
        if not isinstance(value, Mapping):
            raise _error("invalid_interaction_preference", operation, "preference must be an object")
        _scan_forbidden(value, operation)
        preferences.setdefault("session", {}).update(copy.deepcopy(dict(value)))
        return

    if operation == "PROPOSE_USER_INTERACTION_PREFERENCE":
        value = _patch_record_payload(patch, "preference", "proposal", default=None)
        if value is None:
            value = patch.get("value", {})
        source = _source_span(patch, user_message, operation, required=True)
        proposal = copy.deepcopy(value)
        if not isinstance(proposal, Mapping):
            proposal = {"value": proposal}
        proposal = dict(proposal)
        adaptation_value = copy.deepcopy(proposal)
        proposal_id = str(proposal.get("proposalId") or _proposal_id(operation, turn_id, proposal))
        proposal["proposalId"] = proposal_id
        proposal["sourceSpan"] = source
        proposal["status"] = "proposed"
        created = _register_user_adaptation(preferences, adaptation_id=proposal_id, kind="preference",
                                            value=adaptation_value, source=source, turn_id=turn_id)
        if created:
            preferences.setdefault("userProposals", []).append(proposal)
        return

    if operation in {"PROMOTE_USER_ADAPTATION", "REVOKE_USER_ADAPTATION"}:
        target = _identifier(_patch_payload(patch, "targetId", "adaptationId", "id"), "adaptationId", operation)
        records = preferences.setdefault("userAdaptations", [])
        index, record = _find_record(records, target, operation, "adaptation")
        source = _source_span(patch, user_message, operation, required=True)
        if operation == "PROMOTE_USER_ADAPTATION":
            if _patch_payload(patch, "authorization") != "explicit_user_request":
                raise _error("adaptation_authorization_required", operation, "promotion requires model-declared explicit user request and exact current sourceSpan")
            if record.get("status") in {"revoked", "superseded"}:
                raise _error("adaptation_inactive", operation, "propose a new version instead of reviving a revoked adaptation")
            record.update({"status": "active", "authorizedByTurnId": turn_id, "authorizationSpan": source})
        else:
            record.update({"status": "revoked", "revokedByTurnId": turn_id, "revocationSpan": source})
        records[index] = record
        for item in preferences.get("userProposals", []):
            if item.get("proposalId") == target:
                item["status"] = record["status"]
        return

    if operation in {"ADD_TURN_RULE", "ADD_SESSION_RULE", "PROPOSE_USER_EXPERIENCE", "PROPOSE_SYSTEM_HEURISTIC", "REPORT_ARCHITECTURE_ISSUE"}:
        payload = _experience_payload(patch, operation)
        requested_id = _record_id(payload, "experienceId", "id") or _record_id(patch, "experienceId", "id")
        experience_id = requested_id or _proposal_id(operation, turn_id, payload)
        payload["experienceId"] = experience_id
        if operation == "ADD_TURN_RULE":
            payload["scope"] = "turn"
            payload["status"] = "active"
            payload["remainingTurns"] = 1
        elif operation == "ADD_SESSION_RULE":
            payload["scope"] = "session"
            payload["status"] = "active"
            remaining = payload.get("remainingTurns", payload.get("ttlTurns", 1))
            remaining = _nonnegative_int(remaining, "remainingTurns", operation)
            if remaining == 0:
                raise _error("invalid_experience_ttl", operation, "session rule TTL must be positive")
            payload["remainingTurns"] = min(remaining, 1000)
        else:
            payload["status"] = "proposed"
            if operation == "PROPOSE_USER_EXPERIENCE":
                payload["scope"] = "user"
                source = _source_span(patch, user_message, operation, required=True)
                payload["sourceSpan"] = source
                created = _register_user_adaptation(preferences, adaptation_id=experience_id, kind="experience",
                                                     value={key: value for key, value in payload.items() if key not in {"status", "sourceSpan", "scope"}},
                                                     source=source, turn_id=turn_id)
                if not created:
                    return
        _upsert_record(state["experiences"], payload, experience_id, operation, "experience")
        return

    if operation == "REVOKE_EXPERIENCE":
        experience_id = _experience_id(patch, operation)
        user_record = next((item for item in preferences.get("userAdaptations", [])
                            if item.get("adaptationId") == experience_id and item.get("kind") == "experience"), None)
        if user_record is not None:
            source = _source_span(patch, user_message, operation, required=True)
            user_record.update({"status": "revoked", "revokedByTurnId": turn_id, "revocationSpan": source})
            for item in state["experiences"]:
                if isinstance(item, dict) and item.get("experienceId") == experience_id:
                    item.update({"status": "revoked", "revokedByTurnId": turn_id, "remainingTurns": 0})
            return
        index, experience = _find_record(state["experiences"], experience_id, operation, "experience")
        experience["status"] = "revoked"
        experience["revokedByTurnId"] = turn_id
        experience["remainingTurns"] = 0
        state["experiences"][index] = experience
        return

    raise _error("unsupported_operation", operation, "operation is not in the cognitive patch whitelist")


def _overlay_result(
    state: Mapping[str, Any],
    *,
    base_revision: int,
    turn_id: str,
    root_snapshot_id: str,
    patches: Sequence[Mapping[str, Any]],
    invalidated: Sequence[str],
    directive: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": copy.deepcopy(dict(state)),
        "baseRevision": base_revision,
        "turnId": turn_id,
        "rootSnapshotId": root_snapshot_id,
        "patches": copy.deepcopy(list(patches)),
        "invalidatedPendingActionIds": list(dict.fromkeys(str(x) for x in invalidated)),
    }
    if directive is not None:
        result["directive"] = copy.deepcopy(dict(directive))
        if "interactionAct" in directive:
            result["interactionAct"] = copy.deepcopy(directive["interactionAct"])
        if "understoodGoal" in directive:
            result["understoodGoal"] = copy.deepcopy(directive["understoodGoal"])
    return result


def stage_revision(
    state: Mapping[str, Any],
    directive: Mapping[str, Any],
    *,
    turn_id: str,
    root_snapshot_id: str,
    user_message: str,
    evidence: Any = None,
    pending_action_ids: Sequence[str] = (),
    context_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Apply a directive to a private overlay without changing revision.

    Passing a previous overlay stages more patches on the same revision.  All
    mutable inputs are copied before validation, so a rejected patch leaves the
    caller's state/overlay byte-for-byte unchanged.
    """

    turn = _identifier(turn_id, "turn_id")
    root = _identifier(root_snapshot_id, "root_snapshot_id")
    if not isinstance(user_message, str):
        raise _error("invalid_user_message", message="user_message must be a string")
    if not isinstance(directive, Mapping):
        raise _error("invalid_directive", message="directive must be an object")

    previous_overlay = isinstance(state, Mapping) and "state" in state and "baseRevision" in state and "patches" in state
    if previous_overlay:
        original_overlay = state
        previous_state = state.get("state")
        if not isinstance(previous_state, Mapping):
            raise _error("invalid_overlay", message="overlay.state must be an object")
        previous_turn = _identifier(state.get("turnId"), "overlay.turnId")
        previous_root = _identifier(state.get("rootSnapshotId"), "overlay.rootSnapshotId")
        if previous_turn != turn:
            raise _error("turn_mismatch", message="staged overlay may only accumulate the same turn")
        if previous_root != root:
            raise _error("snapshot_mismatch", message="staged overlay may only accumulate the same root snapshot")
        base_revision = _nonnegative_int(state.get("baseRevision"), "baseRevision")
        working = _normalise_state(previous_state)
        if working["revision"] != base_revision:
            raise _error("invalid_overlay", message="overlay state revision changed before commit")
        existing_patches = state.get("patches")
        if not isinstance(existing_patches, list):
            raise _error("invalid_overlay", message="overlay.patches must be a list")
        patches = copy.deepcopy(existing_patches)
        invalidated = list(state.get("invalidatedPendingActionIds", []))
        # Preserve arbitrary metadata from the previous overlay while replacing
        # the state and patch lists with their private copies.
        metadata = {
            key: copy.deepcopy(value)
            for key, value in state.items()
            if key not in {"state", "patches", "invalidatedPendingActionIds"}
        }
        prior_directive = metadata.get("directive") if isinstance(metadata.get("directive"), Mapping) else None
    else:
        working = _normalise_state(state)
        base_revision = working["revision"]
        patches = []
        invalidated = []
        metadata = {}
        prior_directive = None

    # Validate all patch objects and JSON safety before mutating the private
    # working copy.  This makes rejection atomic at the Python overlay level.
    raw_patches = directive.get("patches", [])
    if not isinstance(raw_patches, list):
        raise _error("invalid_patches", message="directive.patches must be a list")
    for raw_patch in raw_patches:
        if not isinstance(raw_patch, Mapping):
            raise _error("invalid_patch", message="each cognitive patch must be an object")
        _scan_forbidden(raw_patch)
        _json(raw_patch, code="invalid_patch")

    pending_ids: list[str] = []
    for item in pending_action_ids:
        pending_ids.append(_identifier(item, "pendingActionId"))
    ctx_ids: list[str] = []
    for item in context_ids:
        ctx_ids.append(_identifier(item, "contextId"))

    # Expiry is evaluated on each turn boundary.  It does not decrement a rule
    # automatically; callers may stage a replacement rule with a new TTL.
    _expire_experiences(working)
    for raw_patch in raw_patches:
        patch = copy.deepcopy(dict(raw_patch))
        operation_value = patch.get("operation", patch.get("op"))
        if not isinstance(operation_value, str) or not operation_value.strip():
            raise _error("unsupported_operation", message="patch operation is required")
        operation = operation_value.strip().upper()
        if operation not in SUPPORTED_OPERATIONS:
            raise _error("unsupported_operation", operation, "operation is not in the cognitive patch whitelist")
        if operation in _SOURCE_REQUIRED_OPERATIONS:
            _source_span(patch, user_message, operation, required=True)

        replayed_supersession = operation == "SUPERSEDE_USER_BELIEF" and patch in patches
        _apply_patch(
            working,
            patch,
            operation=operation,
            turn_id=turn,
            user_message=user_message,
            evidence=evidence,
            context_ids=ctx_ids,
            replayed_supersession=replayed_supersession,
        )
        _ensure_constraint_ids(working["task"])
        if not replayed_supersession:
            patches.append(patch)

        if operation in _TASK_INVALIDATING_OPERATIONS:
            for ident in pending_ids:
                if ident not in invalidated:
                    invalidated.append(ident)
        if operation == "INVALIDATE_PENDING_ACTION":
            for ident in _ids_from_patch(patch, operation):
                if ident not in invalidated:
                    invalidated.append(ident)

    try:
        apply_answer_scope(working['context'], directive.get('answerScope'), user_message=user_message, turn_id=turn,
            original_goal=str((working.get('lastDecisionDigest') or {}).get('userRequest')
                              or (working.get('task') or {}).get('goal') or user_message))
    except ValueError as exc:
        raise _error('invalid_answer_scope', message=str(exc)) from exc

    combined_directive: dict[str, Any] = {}
    if isinstance(prior_directive, Mapping):
        combined_directive.update(copy.deepcopy(dict(prior_directive)))
    for key in ("interactionAct", "understoodGoal", "questionRelation", "answerScope"):
        if key in directive:
            combined_directive[key] = copy.deepcopy(directive[key])
    metadata["directive"] = combined_directive
    return _overlay_result(
        working,
        base_revision=base_revision,
        turn_id=turn,
        root_snapshot_id=root,
        patches=patches,
        invalidated=invalidated,
        directive=combined_directive,
    )


def _decode_json(raw: Any, code: str, message: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _error(code, message=message) from exc


def _result(
    state: Mapping[str, Any],
    *,
    committed: bool,
    idempotent: bool,
    patches: Sequence[Mapping[str, Any]],
    invalidated: Sequence[str],
    base_revision: int | None = None,
    turn_id: str | None = None,
    root_snapshot_id: str | None = None,
) -> dict[str, Any]:
    normal = _normalise_state(state)
    result: dict[str, Any] = {
        "state": normal,
        "committed": committed,
        "idempotent": idempotent,
        "revision": normal["revision"],
        "patches": copy.deepcopy(list(patches)),
        "invalidatedPendingActionIds": list(dict.fromkeys(str(x) for x in invalidated)),
    }
    if base_revision is not None:
        result["baseRevision"] = base_revision
    if turn_id is not None:
        result["turnId"] = turn_id
    if root_snapshot_id is not None:
        result["rootSnapshotId"] = root_snapshot_id
    for key in _STATE_CORE_KEYS:
        result[key] = copy.deepcopy(normal[key])
    return result


def commit_revision(
    connection: sqlite3.Connection,
    overlay: Mapping[str, Any],
    *,
    digest: Any,
    trace: Any,
) -> dict[str, Any]:
    """Optimistically persist one overlay without committing the connection.

    The caller owns ``commit``/``rollback``.  State, digest, trace, and patch
    audit rows are written through the same connection transaction.
    """

    if not isinstance(overlay, Mapping):
        raise _error("invalid_overlay", message="overlay must be an object")
    ensure_schema(connection)
    raw_state = overlay.get("state")
    if not isinstance(raw_state, Mapping):
        raise _error("invalid_overlay", message="overlay.state must be an object")
    # Execution receipts are authoritative even when this overlay predates
    # the result, or the response that initiated the action never committed.
    state = project_durable_tool_progress(connection, _normalise_state(raw_state))
    cid = state["conversationId"]
    turn = _identifier(overlay.get("turnId"), "turnId")
    root = _identifier(overlay.get("rootSnapshotId"), "rootSnapshotId")
    base = _nonnegative_int(overlay.get("baseRevision"), "baseRevision")
    if state["revision"] != base:
        raise _error("invalid_overlay", message="overlay state revision must equal baseRevision")
    raw_patches = overlay.get("patches", [])
    if not isinstance(raw_patches, list) or any(not isinstance(item, Mapping) for item in raw_patches):
        raise _error("invalid_overlay", message="overlay.patches must be a list of objects")
    invalidated = []
    for item in overlay.get("invalidatedPendingActionIds", []):
        ident = _identifier(item, "invalidatedPendingActionId")
        if ident not in invalidated:
            invalidated.append(ident)
    digest_value = _clone_json(digest, code="invalid_digest")
    trace_value = _clone_json(trace, code="invalid_trace")
    state_json = _json(state)
    now = _now()

    # Turn IDs are idempotency keys.  Check them before the revision conflict,
    # so a retried request receives the original committed result.
    existing_revision = connection.execute(
        """
        SELECT base_revision, revision, root_snapshot_id, state_json,
               invalidated_pending_action_ids_json
        FROM cognitive_revisions
        WHERE conversation_id = ? AND turn_id = ?
        """,
        (cid, turn),
    ).fetchone()
    if existing_revision is not None:
        existing_state = _decode_json(existing_revision[3], "state_corrupt", "stored revision state is invalid")
        existing_invalidated = _decode_json(
            existing_revision[4], "state_corrupt", "stored invalidation list is invalid"
        )
        if not isinstance(existing_invalidated, list):
            raise _error("state_corrupt", message="stored invalidation list is not a list")
        patch_rows = connection.execute(
            """
            SELECT patch_json FROM cognitive_patches
            WHERE conversation_id = ? AND turn_id = ?
            ORDER BY ordinal
            """,
            (cid, turn),
        ).fetchall()
        existing_patches = []
        for row in patch_rows:
            decoded_patch = _decode_json(row[0], "state_corrupt", "stored patch is invalid")
            if not isinstance(decoded_patch, Mapping):
                raise _error("state_corrupt", message="stored patch is not an object")
            existing_patches.append(decoded_patch)
        return _result(
            existing_state,
            committed=True,
            idempotent=True,
            patches=existing_patches,
            invalidated=existing_invalidated,
            base_revision=int(existing_revision[0]),
            turn_id=turn,
            root_snapshot_id=str(existing_revision[2]),
        )

    current_row = connection.execute(
        "SELECT revision, state_json FROM cognitive_states WHERE conversation_id = ?", (cid,)
    ).fetchone()
    if current_row is None:
        current_revision = 0
    else:
        current_revision = _nonnegative_int(current_row[0], "revision")
        current_state = _decode_json(current_row[1], "state_corrupt", "stored state is invalid")
        _normalise_state(current_state, cid)
    if current_revision != base:
        raise _error(
            "revision_conflict",
            message="cognitive state revision changed before commit",
            expected=base,
            actual=current_revision,
        )

    committed_state = copy.deepcopy(state)
    committed_state["revision"] = base + 1
    committed_state["lastDecisionDigest"] = digest_value
    # Count committed turns, not model calls or retries. A two-turn session
    # rule applies to the creating turn and one following turn, then expires.
    for experience in committed_state.get("experiences", []):
        if isinstance(experience, dict) and experience.get("status") == "active" and experience.get("remainingTurns") is not None:
            experience["remainingTurns"] = max(0, int(experience["remainingTurns"]) - 1)
    _expire_experiences(committed_state)
    committed_json = _json(committed_state)
    try:
        if current_row is None:
            connection.execute(
                "INSERT INTO cognitive_states(conversation_id, revision, state_json, updated_at) VALUES (?, ?, ?, ?)",
                (cid, base + 1, committed_json, now),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE cognitive_states
                SET revision = ?, state_json = ?, updated_at = ?
                WHERE conversation_id = ? AND revision = ?
                """,
                (base + 1, committed_json, now, cid, base),
            )
            if cursor.rowcount != 1:
                raise _error(
                    "revision_conflict",
                    message="cognitive state revision changed during commit",
                    expected=base,
                )
        connection.execute(
            """
            INSERT INTO cognitive_revisions(
                conversation_id, turn_id, base_revision, revision, root_snapshot_id,
                state_json, invalidated_pending_action_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, turn, base, base + 1, root, committed_json, _json(invalidated), now),
        )
        connection.execute(
            """
            INSERT INTO cognitive_digests(conversation_id, turn_id, digest_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (cid, turn, _json(digest_value), now),
        )
        connection.execute(
            """
            INSERT INTO cognitive_traces(conversation_id, turn_id, root_snapshot_id, trace_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cid, turn, root, _json(trace_value), now),
        )
        for ordinal, patch in enumerate(raw_patches):
            _scan_forbidden(patch)
            connection.execute(
                """
                INSERT INTO cognitive_patches(
                    conversation_id, turn_id, root_snapshot_id, ordinal, patch_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cid, turn, root, ordinal, _json(dict(patch), code="invalid_patch"), now),
            )
    except sqlite3.IntegrityError as exc:
        # The caller can roll back the transaction.  Do not roll it back here,
        # because the surrounding conversation-turn write is atomic with us.
        raise _error("revision_conflict", message="cognitive state commit conflicted with another writer") from exc

    return _result(
        committed_state,
        committed=True,
        idempotent=False,
        patches=raw_patches,
        invalidated=invalidated,
        base_revision=base,
        turn_id=turn,
        root_snapshot_id=root,
    )


_TRACE_META_FIELDS = frozenset(
    {"traceId", "conversationId", "turnId", "rootSnapshotId", "createdAt"}
)
# Trace payload projection is explicit.  Returning a requested payload field
# does not expose the whole trace JSON, which keeps diagnostics bounded and
# prevents a trace from becoming an accidental second state store.
_TRACE_PAYLOAD_FIELDS = frozenset(
    {
        "toolReceipts",
        "contextSelection",
        "taskInterpretation",
        "toolPlan",
        "evidenceUsed",
        "finalAnswer",
        "speechText",
        "memoryChanges",
        "patches",
        "invalidatedPendingActionIds",
        "digest",
        "outcome",
        "error",
        "camera",
        "screen",
        "visualResult",
    }
)
_TRACE_FIELDS = _TRACE_META_FIELDS | _TRACE_PAYLOAD_FIELDS | {"trace"}


def _bounded_trace_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value) if not isinstance(value, str) else value
        if len(text) <= MAX_TRACE_FIELD_CHARS:
            return value
        return {"truncated": True, "preview": text[:MAX_TRACE_FIELD_CHARS]}
    encoded = _json(value)
    if len(encoded) <= MAX_TRACE_FIELD_CHARS:
        return copy.deepcopy(value)
    return {"truncated": True, "preview": encoded[:MAX_TRACE_FIELD_CHARS]}


def read_traces(
    connection: sqlite3.Connection,
    conversation_id: str,
    turn_ids: Sequence[str],
    fields: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Read bounded, whitelisted trace columns for one conversation only."""

    cid = _identifier(conversation_id, "conversation_id")
    ensure_schema(connection)
    if isinstance(turn_ids, (str, bytes)) or not isinstance(turn_ids, Sequence):
        raise _error("invalid_turn_ids", message="turn_ids must be a list")
    normalized_turn_ids: list[str] = []
    for item in turn_ids:
        ident = _identifier(item, "turnId")
        if ident not in normalized_turn_ids:
            normalized_turn_ids.append(ident)
    if not normalized_turn_ids:
        return []
    if len(normalized_turn_ids) > MAX_TRACE_ROWS:
        raise _error("trace_limit_exceeded", message="too many turn IDs requested")

    if fields is None or (isinstance(fields, Sequence) and not isinstance(fields, (str, bytes)) and not fields):
        # An empty projection is a bounded metadata response.  In particular,
        # it must not return an unbounded copy of trace_json.
        requested_fields = ["traceId", "conversationId", "turnId", "rootSnapshotId", "createdAt"]
    elif isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
        raise _error("invalid_trace_fields", message="fields must be a list")
    else:
        requested_fields = list(fields)
    if len(requested_fields) > len(_TRACE_FIELDS):
        raise _error("trace_limit_exceeded", message="too many trace fields requested")
    for field in requested_fields:
        if field not in _TRACE_FIELDS:
            raise _error("invalid_trace_field", message="trace field is not whitelisted", field=field)

    placeholders = ",".join("?" for _ in normalized_turn_ids)
    rows = connection.execute(
        f"""
        SELECT trace_id, conversation_id, turn_id, root_snapshot_id, trace_json, created_at
        FROM cognitive_traces
        WHERE conversation_id = ? AND turn_id IN ({placeholders})
        ORDER BY trace_id
        LIMIT ?
        """,
        (cid, *normalized_turn_ids, MAX_TRACE_ROWS),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        trace_value = _decode_json(row[4], "trace_corrupt", "stored trace is invalid")
        full = {
            "traceId": row[0],
            "conversationId": row[1],
            "turnId": row[2],
            "rootSnapshotId": row[3],
            "trace": trace_value,
            "createdAt": row[5],
        }
        result = {}
        for field in requested_fields:
            if field in full:
                value = full[field]
            elif isinstance(trace_value, Mapping):
                value = trace_value.get(field)
            else:
                value = None
            result[field] = _bounded_trace_value(value)
        results.append(result)
    return results


def load_trace_receipts(
    connection: sqlite3.Connection,
    conversation_id: str,
    turn_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Load complete backend receipts; never send this result directly to a model.

    Public trace projections deliberately truncate large fields. Evidence
    hydration must instead use the stored receipt, still scoped to the same
    conversation and bounded set of explicitly selected turns.
    """
    conversation_id = _identifier(conversation_id, "conversation_id")
    metadata = read_traces(connection, conversation_id, turn_ids, ["traceId", "turnId", "createdAt"])
    results = []
    for item in metadata:
        row = connection.execute(
            "SELECT trace_json FROM cognitive_traces WHERE conversation_id = ? AND trace_id = ?",
            (conversation_id, item["traceId"]),
        ).fetchone()
        if row is None:
            continue
        trace = _decode_json(row[0], "trace_corrupt", "stored trace is invalid")
        receipts = trace.get("toolReceipts") if isinstance(trace, Mapping) else None
        if isinstance(receipts, list):
            results.append({**item, "toolReceipts": receipts})
    return results


__all__ = [
    "CognitivePatchError",
    "SUPPORTED_OPERATIONS",
    "empty_state",
    "ensure_schema",
    "load_state",
    "stage_revision",
    "commit_revision",
    "read_traces",
    "load_trace_receipts",
]
