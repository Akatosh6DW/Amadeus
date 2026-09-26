"""Compact projections and objective execution checks for cognitive turns."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence
from urllib.parse import urldefrag, urlsplit, quote

from agent.visual_grounding import build_visual_evidence_packet
from agent.cognitive_tools import tool_plan_violations
from agent.cognitive_outcome import claim_contract_for_evidence, success_criteria_catalog
from agent.cognitive_state import belief_evidence_problem


def evidence_url_key(value: Any) -> str:
    # Encode literal Unicode without decoding reserved URL separators.
    return quote(urldefrag(str(value or ""))[0], safe=":/?@!$&'()*+,;=%[]~-")


def readable_evidence_urls(evidence: Mapping[str, Any]) -> set[str]:
    """URLs observed in backend results are available for follow-up reading."""
    urls: set[str] = set()
    for ref, item in evidence.items():
        if not isinstance(item, Mapping) or (not item.get("ok") and item.get("sourceType") != "web"):
            continue
        candidates = [ref, item.get("url"), item.get("canonicalUrl")]
        for link in item.get("links") or []:
            candidates.append(link.get("url") or link.get("href") if isinstance(link, Mapping) else link)
        for candidate in candidates:
            value = str(candidate or "")
            parsed = urlsplit(value)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                urls.add(evidence_url_key(value))
    return urls


_STRUCTURED_TOOL_FACT_FIELDS = {
    "get_system_time": ("iso", "display", "year", "month", "day", "hour", "minute", "second"),
    "local_compute": ("operation", "result", "count", "length", "target", "unit", "caseSensitive", "normalization",
                      "positions", "positionsTruncated", "expected", "satisfied", "difference", "exact",
                      "numerator", "denominator", "decimalApproximation"),
    "todo_search": ("targetRef", "resolution", "candidates", "count"),
    "todo_list": ("scope", "items", "count", "activeCount", "revision", "unseenCount"),
    "todo_manage": ("timeFacts", "task"),
    "bookmark_list": ("query", "items", "count"),
    "bookmark_manage": ("bookmark", "destinationKnowledge"),
    "list_directory": ("path", "entries", "truncated"),
    "read_text_file": ("path", "size"),
}


def tool_result_body(result: Mapping[str, Any]) -> str:
    """Expose returned facts; transport metadata and write receipts are not text."""
    text = result.get("content") or result.get("text")
    if text:
        return str(text)
    if result.get("name") == "read_file":
        return str(result.get("contentPreview") or "")
    fields = _STRUCTURED_TOOL_FACT_FIELDS.get(str(result.get("name")), ())
    facts = {key: result[key] for key in fields if key in result}
    return json.dumps(facts, ensure_ascii=False, separators=(",", ":")) if facts and result.get("ok") else ""


def collect_evidence(results: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build references from executed results, never from model-supplied IDs."""
    evidence: dict[str, dict[str, Any]] = {}
    for index, result in enumerate(results):
        if not isinstance(result, Mapping):
            continue
        for source in result.get("sources", []):
            if not isinstance(source, Mapping):
                continue
            ref = str(source.get("sourceUid") or source.get("canonicalUrl") or source.get("url") or "")
            if not ref:
                continue
            entry = {**deepcopy(dict(source)), "ok": bool(result.get("ok")), "sourceType": "web"}
            entry["body"] = str(source.get("body") or "")
            evidence[ref] = entry
            # Qualified IDs remain stable when a later search reuses S1/S2.
            short_id = str(source.get("id") or "")
            if short_id:
                evidence[f"result:{index}:{short_id}"] = entry
            # S1/S2 denote the most recent tool result, matching tool_context_prompt.
            # Cross-result or historical references use the qualified result ID.
            if short_id:
                evidence[short_id] = entry
        receipt_id = str(result.get("toolCallId") or result.get("callId") or f"result:{index}")
        evidence[receipt_id] = {
            "ok": bool(result.get("ok")), "sourceType": "tool",
            "name": str(result.get("name") or ""),
            "body": tool_result_body(result),
            "executionStatus": ("not_attempted" if result.get("attempted") is False or result.get("error") == "cognitive_task_constraint"
                                else "success" if result.get("ok") else "failed"),
            "receipt": deepcopy(dict(result)),
        }
        if result.get("name") == "web_search":
            evidence[receipt_id]["searchDiagnostics"] = {
                key: deepcopy(result[key]) for key in
                ("query", "provider", "requestedProvider", "providerAttempts", "providerErrors") if key in result
            }
        if result.get("name") in {"capture_screen", "capture_camera", "read_image"}:
            packet = build_visual_evidence_packet([result])
            observations = packet.get("observations") or []
            understood = any(item.get("sceneEvidence") or item.get("textEvidence")
                             or (item.get("imageInput") or {}).get("available") for item in observations)
            evidence[receipt_id].update({
                "sourceType": "visual",
                "body": json.dumps(packet, ensure_ascii=False) if understood else "",
                "observedAt": result.get("capturedAt") or result.get("observedAt"),
                "understandingAvailable": understood,
            })
            artifact_id = str((result.get("imageArtifact") or {}).get("artifactId") or "")
            if artifact_id:
                evidence["image:" + artifact_id] = evidence[receipt_id]
    return evidence


def adaptation_evidence_ref(preferences: Mapping[str, Any], record: Mapping[str, Any]) -> str:
    identity = json.dumps([
        preferences.get("userAdaptationScopeId"), preferences.get("userAdaptationGeneration"),
        preferences.get("userAdaptationRevision"), record.get("adaptationId"),
    ], ensure_ascii=False, separators=(",", ":"))
    return "adaptation:" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def collect_current_user_evidence(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the frozen user statement, not a proposed memory or external fact."""
    message = snapshot.get("currentUserMessage") or {}
    text = str(message.get("text") or "") if isinstance(message, Mapping) else ""
    turn_id = str(snapshot.get("currentTurnId") or "")
    if not text or not turn_id:
        return {}
    return {"user-turn:" + turn_id: {
        "ok": True, "sourceType": "user_message", "authority": "USER_STATEMENT",
        "sourceTurnId": turn_id, "historical": False, "body": text,
    }}


def collect_user_adaptation_evidence(preferences: Mapping[str, Any]) -> dict[str, Any]:
    """Register authoritative stored records, including their withdrawn state.

    Called at snapshot loading, never to turn uncommitted proposals into
    evidence that a write already happened.
    """
    evidence = {}
    for record in preferences.get("userAdaptations") or []:
        if not isinstance(record, Mapping) or not all(record.get(key) for key in ("adaptationId", "sourceSpan", "sourceTurnId")):
            continue
        evidence[adaptation_evidence_ref(preferences, record)] = {
            "ok": True, "sourceType": "user_adaptation", "authority": "USER_ADAPTATION_RECORD",
            "name": str(record["adaptationId"]), "historical": False,
            "sourceTurnId": record.get("revokedByTurnId") or record.get("sourceTurnId"),
            "body": json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")),
        }
    return evidence


def compact_packet(
    state: Mapping[str, Any], snapshot: Mapping[str, Any], *,
    evidence: Mapping[str, Any] | None = None,
    selected_adaptation_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Keep current corrections visible while bounding ordinary-turn overhead."""
    beliefs = state.get("beliefs") or {}
    beliefs = list(beliefs.values()) if isinstance(beliefs, Mapping) else list(beliefs)
    active = [item for item in beliefs if isinstance(item, Mapping) and item.get("status") == "active"]
    disputed = [item for item in beliefs if isinstance(item, Mapping) and item.get("status") in {
        "challenged", "stale", "retracted", "superseded",
    }]
    experiences = state.get("experiences") or []
    if isinstance(experiences, Mapping):
        experiences = list(experiences.values())
    active_experiences = [item for item in experiences if isinstance(item, Mapping)
                          and item.get("status") == "active" and item.get("scope") in {"turn", "session"}]
    preferences = deepcopy(state.get("preferences") or {})
    adaptation_source = deepcopy(preferences)
    adaptations = preferences.pop("userAdaptations", [])
    records = []
    for item in adaptations:
        if not isinstance(item, Mapping):
            continue
        record = dict(item)
        ref = adaptation_evidence_ref(adaptation_source, item)
        entry = (evidence or {}).get(ref) or {}
        try:
            stored = json.loads(str(entry.get("body") or ""))
        except (TypeError, ValueError):
            stored = None
        # A same-turn change is staged, not proof that the stored version
        # already changed. Only an exact snapshot can supply this reference.
        if entry.get("sourceType") == "user_adaptation" and stored == item:
            record["evidenceRef"] = ref
        records.append(record)
    adaptations = records
    for field in ("userAdaptationRevision", "userAdaptationGeneration", "userAdaptationScopeId"):
        preferences.pop(field, None)
    # Every stored record remains discoverable. Previews are retrieval hints,
    # never a substitute for reading its full conditions and exceptions.
    catalog = []
    for item in adaptations:
        entry = {key: item.get(key) for key in ("adaptationId", "kind", "status")}
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= 600:
            # Small records are already cheaper to read than a separate model
            # continuation. Include their exact identity, conditions and source.
            entry.update(detailsComplete=True, record=deepcopy(item))
        else:
            entry.update(detailsComplete=False,
                valuePreview=json.dumps(item.get("value"), ensure_ascii=False, separators=(",", ":"))[:360],
                sourcePreview=str(item.get("sourceSpan") or "")[:120])
        catalog.append(entry)
    preferences["userAdaptationCatalog"] = catalog
    selected = set(selected_adaptation_ids) if selected_adaptation_ids is not None else None
    active_adaptations = [item for item in adaptations if item.get("status") == "active"]
    proposed_adaptations = [item for item in adaptations if item.get("status") == "proposed"]
    preferences["activeUserAdaptations"] = ([item for item in active_adaptations if item.get("adaptationId") in selected]
                                           if selected is not None else active_adaptations[-3:])
    preferences["userAdaptationCandidates"] = ([item for item in proposed_adaptations if item.get("adaptationId") in selected]
                                              if selected is not None else proposed_adaptations[-3:])
    withdrawn = [item for item in adaptations
                 if isinstance(item, Mapping) and item.get("status") in {"revoked", "superseded"}]
    recent_withdrawn = withdrawn[-3:]
    if selected is None:
        visible_withdrawn = recent_withdrawn
    else:
        # Keep the normal three-record summary and add any explicitly selected
        # older record so a recall remains grounded after a status change.
        visible_withdrawn = [item for item in withdrawn
                             if item in recent_withdrawn or item.get("adaptationId") in selected]
    withdrawn_projection = []
    for item in visible_withdrawn:
        ident = item.get("adaptationId")
        if selected is not None and ident in selected:
            # Metadata belongs to this packet entry; do not write it into the
            # stored record or turn detailsComplete into a user adaptation
            # field that could be persisted by a later patch.
            withdrawn_projection.append({
                "adaptationId": ident,
                "status": item.get("status"),
                **({"evidenceRef": item["evidenceRef"]} if "evidenceRef" in item else {}),
                "detailsComplete": True,
                "record": deepcopy(item),
            })
        else:
            withdrawn_projection.append({
                key: item[key] for key in ("adaptationId", "status", "evidenceRef") if key in item
            } | {"detailsComplete": False})
    preferences["withdrawnUserAdaptations"] = withdrawn_projection
    visible_adaptation_ids = {item["adaptationId"] for field in
        ("activeUserAdaptations", "userAdaptationCandidates", "withdrawnUserAdaptations")
        for item in preferences[field] if item.get("adaptationId")}
    visible_adaptation_ids.update(selected or ())
    # Inline short records are fully readable too. Their citations must remain
    # visible to the same evidence projection used by correction diagnostics.
    visible_adaptation_ids.update(item["adaptationId"] for item in catalog if item.get("detailsComplete"))
    # Aliases refer to the same evidence, not additional sources. Without this
    # projection a few S1/URL aliases consume the entire evidence window.
    unique_evidence: list[tuple[str, Mapping[str, Any], list[str]]] = []
    for ref, item in (evidence or {}).items():
        if not isinstance(item, Mapping):
            continue
        if adaptations and item.get("sourceType") == "user_adaptation" and item.get("name") not in visible_adaptation_ids:
            # Do not bypass the full-record retrieval contract through the
            # general evidence window (which may truncate an exception).
            continue
        matching = next((entry for entry in unique_evidence if entry[1] == item), None)
        if matching is not None:
            matching[2].append(ref)
        else:
            unique_evidence.append((ref, item, []))
    for index, (ref, item, aliases) in enumerate(unique_evidence):
        stable = str(item.get("sourceUid") or item.get("canonicalUrl") or item.get("url") or "")
        if item.get("sourceType") == "web" and stable in aliases:
            unique_evidence[index] = (stable, item, [ref, *[alias for alias in aliases if alias != stable]])

    current_receipts = []
    for ref, item, _aliases in unique_evidence:
        contract = claim_contract_for_evidence(item, evidence_ref=ref, compact=True)
        if contract["receiptPresent"] and not contract["historical"]:
            current_receipts.append({"id": ref, "name": item.get("name") or (item.get("receipt") or {}).get("name"),
                                     "resultStatus": contract["claimRequirements"]["tool_completion"]["required"]["resultStatus"]})

    def project_evidence(ref: str, item: Mapping[str, Any], aliases: list[str]) -> dict[str, Any]:
        claim_contract = claim_contract_for_evidence(item, evidence_ref=ref, compact=True)
        belief_problem = belief_evidence_problem(item)
        return {
            "id": ref, "ok": bool(item.get("ok")), "sourceType": item.get("sourceType"),
            "aliases": aliases,
            "authority": item.get("authority"),
            "actionReceipt": {key: value for key, value in (item.get("receipt", {}).get("actionReceipt") or {}).items()
                              if key in {"actionId", "actionGroupId", "retryOfActionId", "status", "requestedTarget", "resolvedTarget", "error",
                                         "tool", "action", "taskId", "taskVersionBefore", "taskVersionAfter", "timeFacts", "timeSpec"}}
                             if isinstance(item.get("receipt"), Mapping) else None,
            "executionStatus": item.get("executionStatus"),
            "currentStateVerification": deepcopy(item.get("currentStateVerification")),
            "attempted": ((item.get("receipt") or {}).get("attempted", item.get("executionStatus") != "not_attempted")
                          if item.get("sourceType") in {"tool", "visual"} else None),
            "executionError": str((item.get("receipt") or {}).get("error") or "")[:500],
            "searchDiagnostics": deepcopy(item.get("searchDiagnostics")),
            "title": str(item.get("title") or item.get("name") or "")[:160],
            "url": str(item.get("url") or item.get("canonicalUrl") or "")[:4096],
            "historical": bool(item.get("historical")),
            "freshness": claim_contract["freshness"],
            "observedAt": item.get("observedAt"),
            "sourceTurnId": item.get("sourceTurnId"),
            "answerCommitted": item.get("answerCommitted"),
            "bodyExcerpt": str(item.get("body") or "")[:1200],
            "fetchStatus": item.get("fetchStatus"),
            "snippet": str(item.get("snippet") or "")[:500],
            "bodyError": str(item.get("bodyError") or "")[:240],
            "claimContract": claim_contract,
            "beliefUpdateEligibility": {"allowed": belief_problem is None,
                                        "reason": belief_problem[0] if belief_problem else "readable_evidence"},
        }
    return {
        "conversationId": state.get("conversationId"),
        "rootSnapshotId": snapshot.get("rootSnapshotId"),
        "currentUserRequest": str((snapshot.get("currentUserMessage") or {}).get("text") or ""),
        "currentTurnId": snapshot.get("currentTurnId"),
        "currentUserEvidenceRef": next((ref for ref, item in (evidence or {}).items()
            if isinstance(item, Mapping) and item.get("sourceType") == "user_message"
            and item.get("sourceTurnId") == snapshot.get("currentTurnId")), None),
        "revision": state.get("revision", 0),
        "lastDecisionDigest": deepcopy(state.get("lastDecisionDigest") or {}),
        "task": deepcopy(state.get("task") or {}),
        "successCriteriaCatalog": success_criteria_catalog(state.get("task") or {}),
        "activeBeliefs": deepcopy(active[-16:]),
        "withdrawnOrChallengedBeliefs": deepcopy(disputed[-8:]),
        "references": deepcopy(state.get("references") or {}),
        "context": deepcopy(state.get("context") or {}),
        "preferences": preferences,
        "experiences": deepcopy(active_experiences[-5:]),
        "evidence": [project_evidence(ref, item, aliases) for ref, item, aliases in unique_evidence[-12:]],
        "currentToolReceipts": current_receipts,
    }


def read_user_adaptation_selection(preferences: Mapping[str, Any], query: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read model-selected records from the frozen user's catalogue only."""
    ids = query.get("adaptationIds")
    if not isinstance(ids, list) or len(ids) > 3 or any(not isinstance(ident, str) or not ident for ident in ids):
        raise ValueError("adaptationIds must be a list of at most three real catalogue IDs")
    catalog = {item.get("adaptationId"): item for item in preferences.get("userAdaptations", []) if isinstance(item, Mapping)}
    if any(ident not in catalog for ident in ids):
        raise ValueError("adaptation_id_missing: select IDs from the current userAdaptationCatalog")
    return [deepcopy(dict(catalog[ident])) for ident in dict.fromkeys(ids)]


def task_tool_violations(
    state: Mapping[str, Any], requests: Sequence[Mapping[str, Any]], registry: Any,
) -> list[str]:
    """Enforce model-selected *restrictions* against real tool metadata.

    These checks cannot grant permissions. The existing registry/preflight
    still validates every allowed operation against user/settings authority.
    """
    task = state.get("task") or {}
    if not isinstance(task, Mapping):
        return []
    if requests and task.get("status") in {"paused", "cancelled", "canceled"}:
        return ["task_is_" + str(task["status"])]
    if requests and task.get("status") == "failed" and task.get("terminalFailure"):
        return ["task_is_terminal_failed"]
    constraints = task.get("constraints") or []
    if isinstance(constraints, Mapping):
        constraints = list(constraints.values())
    restrictions = {
        str(item.get("restriction") or "") for item in constraints if isinstance(item, Mapping)
    }
    violations = tool_plan_violations(task, requests)
    invalidated = set(state.get("_invalidatedPendingActionIds") or [])
    for request in requests:
        name = str(request.get("name") or request.get("tool") or "")
        args = request.get("args") or {}
        if isinstance(args, Mapping) and str(args.get("actionId") or args.get("pendingActionId") or "") in invalidated:
            violations.append("pending_action_invalidated:" + name)
        definition = registry.get(name)
        if "read_only" in restrictions and (definition is None or not definition.read_only):
            violations.append("task_read_only:" + name)
        if "no_web" in restrictions and name in {"web_search", "read_web_page"}:
            violations.append("task_no_web:" + name)
        if "no_observation" in restrictions and definition is not None and definition.side_effect_class == "sensor_capture":
            violations.append("task_no_observation:" + name)
        if "no_navigation" in restrictions and name == "open_browser":
            violations.append("task_no_navigation:" + name)
        if "no_retry" in restrictions and (
            (request.get("selection") or {}).get("actionRelation") == "retry"
            or (request.get("args") or {}).get("_retryOfActionId")
        ):
            violations.append("task_no_retry:" + name)
    return violations
