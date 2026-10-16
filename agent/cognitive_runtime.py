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
from tools.web_links import compact_web_links
from tools.web_text import project_web_text


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
        candidates.extend(
            link["url"]
            for link in compact_web_links(
                item.get("links"),
                base_url=str(item.get("url") or item.get("canonicalUrl") or ""),
            )
        )
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


# Web bodies are the only evidence payloads that can grow with fetched page
# size.  Keep the per-page target high enough for ordinary release pages while
# bounding the complete packet independently of the number of search hits.
WEB_EVIDENCE_BODY_BUDGET = 32_000
WEB_EVIDENCE_SOURCE_CAP = 8_000
WEB_EVIDENCE_MIN_SOURCE_CHARS = 600


def deduplicate_evidence_chunks(
    body_excerpt: Any,
    chunks: Any,
) -> list[dict[str, Any]]:
    """Keep chunk identity while avoiding text already present in the body.

    ``project_web_text`` deliberately returns both a coverage projection and
    query-ranked chunks.  When the coverage projection contains a chunk in
    full, serializing both copies makes each model packet pay for the same
    passage twice.  The body remains the readable text authority; the chunk
    keeps its id and ranking metadata and points back to that body.  Chunks
    that are not contained in the body are left lossless.
    """

    body_text = " ".join(str(body_excerpt or "").split())
    if not isinstance(chunks, Sequence) or isinstance(chunks, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for raw in chunks:
        if not isinstance(raw, Mapping):
            continue
        item = deepcopy(dict(raw))
        text = str(item.get("text") or "")
        normalized = " ".join(text.split())
        if normalized and normalized in body_text:
            item["text"] = ""
            item["textRef"] = "bodyExcerpt"
        result.append(item)
    return result


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
            # Reading a search candidate supplies its body, but does not return
            # the publication metadata supplied by the search engine. Keep that
            # metadata attached to the same URL, separately from retrieval time.
            previous = evidence.get(ref) or {}
            for field in ("publishedAt", "publishedTimestamp", "dateEvidence", "snippet"):
                if not entry.get(field) and previous.get(field):
                    entry[field] = deepcopy(previous[field])
            entry["body"] = str(source.get("body") or "")
            if not str(entry.get("researchQuery") or "").strip():
                source_query = str(
                    source.get("query")
                    or result.get("researchQuery")
                    or result.get("query")
                    or ""
                ).strip()
                if source_query:
                    # Keep the retrieval hint with the source so a model-owned
                    # projection can still rank the body when its reference
                    # resolution deliberately omits resolvedQuestion.
                    entry["researchQuery"] = source_query[:500]
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
                ("query", "provider", "requestedProvider", "providerAttempts", "providerErrors", "dateFilter") if key in result
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


def _web_evidence_identity(ref: str, item: Mapping[str, Any]) -> str:
    """Return a stable page identity for deduplicating search/read aliases."""
    return evidence_url_key(
        item.get("canonicalUrl")
        or item.get("url")
        or item.get("sourceUid")
        or ref
    )


def _web_evidence_quality(item: Mapping[str, Any]) -> tuple[int, int, int]:
    """Prefer a readable fetched body over search metadata for one page."""
    status = str(item.get("fetchStatus") or "").strip().lower()
    readable_status = status in {"", "success", "partial"}
    body = str(item.get("body") or "").strip()
    readable = bool(body) and readable_status
    return (
        1 if readable else 0,
        len(body) if readable else 0,
        1 if status == "success" else 0,
    )


def _allocate_web_projection_budgets(
    targets: Mapping[str, int],
    *,
    total_budget: int = WEB_EVIDENCE_BODY_BUDGET,
    source_cap: int = WEB_EVIDENCE_SOURCE_CAP,
    minimum_chars: int = WEB_EVIDENCE_MIN_SOURCE_CHARS,
) -> dict[str, int]:
    """Allocate a bounded body budget while keeping every page represented.

    ``targets`` contains the length of each page's full capped projection. A
    short page therefore releases its unused share to longer pages. When the
    total target exceeds the packet budget, every page first receives a small
    floor and the remainder is water-filled in stable input order. The
    function is pure so the allocation contract can be tested without a
    server, provider, or filesystem.
    """
    normalized = {
        str(ref): min(max(0, int(length)), max(0, int(source_cap)))
        for ref, length in targets.items()
    }
    if not normalized:
        return {}
    budget = max(0, int(total_budget))
    if sum(normalized.values()) <= budget:
        return normalized
    refs = list(normalized)
    floor = min(max(0, int(minimum_chars)), budget // len(refs))
    allocations = {ref: min(normalized[ref], floor) for ref in refs}
    remaining = budget - sum(allocations.values())
    while remaining > 0:
        candidates = [ref for ref in refs if allocations[ref] < normalized[ref]]
        if not candidates:
            break
        share = max(1, remaining // len(candidates))
        progressed = False
        for ref in candidates:
            if remaining <= 0:
                break
            addition = min(normalized[ref] - allocations[ref], share, remaining)
            if addition <= 0:
                continue
            allocations[ref] += addition
            remaining -= addition
            progressed = True
        if not progressed:
            break
    return allocations


def _bounded_evidence_window(
    entries: Sequence[tuple[str, Mapping[str, Any], list[str]]],
    *,
    limit: int = 12,
) -> list[tuple[str, Mapping[str, Any], list[str]]]:
    """Keep readable web bodies visible while retaining recent tool receipts.

    Aliases are merged before this function is called. The window therefore
    counts distinct evidence objects, not S1/S2 aliases. Readable web bodies
    receive priority, recent non-web receipts retain a small reserved slice,
    and empty web metadata fills the remainder. The final list is returned in
    original order so source chronology and stable IDs remain observable.
    """
    if limit <= 0 or len(entries) <= limit:
        return list(entries)

    readable_web = [
        entry for entry in entries
        if entry[1].get("sourceType") == "web" and _web_evidence_quality(entry[1])[0]
    ]
    other_web = [
        entry for entry in entries
        if entry[1].get("sourceType") == "web" and not _web_evidence_quality(entry[1])[0]
    ]
    non_web = [entry for entry in entries if entry[1].get("sourceType") != "web"]

    # Keep a few latest receipts when a large batch of readable pages exists;
    # these carry action identity for writes/attachments but must not evict all
    # bodies from the prompt.
    reserved_receipts = min(len(non_web), 4, limit)
    body_budget = min(len(readable_web), max(0, limit - reserved_receipts))
    receipt_budget = min(len(non_web), limit - body_budget)
    selected_readable = readable_web[-body_budget:] if body_budget else []
    selected_receipts = non_web[-receipt_budget:] if receipt_budget else []
    remaining = max(0, limit - len(selected_readable) - len(selected_receipts))
    selected_metadata = other_web[-remaining:] if remaining else []
    selected_ids = {id(entry) for entry in (*selected_readable, *selected_receipts, *selected_metadata)}
    return [entry for entry in entries if id(entry) in selected_ids]


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
    evidence_query: str | None = None,
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
    # Aliases refer to the same evidence, not additional sources. Keep one
    # preferred object per page identity so a later full read can replace an
    # earlier search-only record without consuming another evidence slot.
    unique_evidence: list[tuple[str, Mapping[str, Any], list[str]]] = []
    web_indexes: dict[str, int] = {}
    for ref, item in (evidence or {}).items():
        if not isinstance(item, Mapping):
            continue
        if adaptations and item.get("sourceType") == "user_adaptation" and item.get("name") not in visible_adaptation_ids:
            # Do not bypass the full-record retrieval contract through the
            # general evidence window (which may truncate an exception).
            continue
        if item.get("sourceType") == "web":
            identity = _web_evidence_identity(str(ref), item)
            matching_index = web_indexes.get(identity) if identity else None
            if matching_index is not None:
                primary_ref, primary_item, aliases = unique_evidence[matching_index]
                merged_aliases = list(dict.fromkeys([
                    *aliases,
                    *(ref for ref in (str(primary_ref), str(ref)) if ref),
                ]))
                if _web_evidence_quality(item) > _web_evidence_quality(primary_item):
                    unique_evidence[matching_index] = (
                        str(ref), item, [alias for alias in merged_aliases if alias != str(ref)]
                    )
                else:
                    unique_evidence[matching_index] = (
                        primary_ref, primary_item,
                        [alias for alias in merged_aliases if alias != str(primary_ref)],
                    )
                continue
            if identity:
                web_indexes[identity] = len(unique_evidence)

        matching_index = next(
            (index for index, entry in enumerate(unique_evidence) if entry[1] == item),
            None,
        )
        if matching_index is not None:
            primary_ref, primary_item, aliases = unique_evidence[matching_index]
            unique_evidence[matching_index] = (
                primary_ref, primary_item,
                list(dict.fromkeys([*aliases, str(ref)])),
            )
        else:
            unique_evidence.append((str(ref), item, []))
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

    snapshot_message = snapshot.get("currentUserMessage") or {}
    snapshot_query = (
        str(snapshot_message.get("text") or "")
        if isinstance(snapshot_message, Mapping)
        else ""
    )
    projection_query = str(evidence_query or snapshot_query).strip()

    def source_projection_query(item: Mapping[str, Any]) -> str:
        # A source-level research query is more focused than a broad user
        # question, but retain both so model-owned and ordinary turns share
        # the same deterministic ranking behavior.
        source_query = str(item.get("researchQuery") or item.get("query") or "").strip()
        return " ".join(item for item in (source_query, projection_query) if item)[:1_200]

    selected_evidence = _bounded_evidence_window(unique_evidence)
    # First build each page's full capped projection. This lets short pages
    # use their actual size rather than an arbitrary fixed slice and makes the
    # packet budget account for the normalized text returned to the model.
    web_full_projections: dict[str, dict[str, Any]] = {}
    web_target_lengths: dict[str, int] = {}
    for ref, item, _aliases in selected_evidence:
        if item.get("sourceType") != "web":
            continue
        full_projection = project_web_text(
            item,
            source_projection_query(item),
            max_chars=WEB_EVIDENCE_SOURCE_CAP,
            chunk_limit=4,
        )
        body_excerpt = str(full_projection.get("bodyExcerpt") or "")
        if not body_excerpt:
            continue
        web_full_projections[ref] = full_projection
        web_target_lengths[ref] = len(body_excerpt)
    web_budgets = _allocate_web_projection_budgets(web_target_lengths)
    web_projections: dict[str, dict[str, Any]] = {}
    for ref, item, _aliases in selected_evidence:
        if item.get("sourceType") != "web":
            continue
        full_projection = web_full_projections.get(ref)
        target = web_target_lengths.get(ref, 0)
        budget = web_budgets.get(ref, 0)
        if not full_projection or budget <= 0:
            web_projections[ref] = {
                "bodyExcerpt": "",
                "evidenceChunks": [],
                "bodyTruncated": False,
                "projectionMode": "empty",
            }
        elif budget >= target:
            web_projections[ref] = full_projection
        else:
            projection = project_web_text(
                item,
                source_projection_query(item),
                max_chars=budget,
                chunk_limit=4,
            )
            # ``project_web_text`` has a defensive 120-character minimum for
            # callers with tiny custom budgets. The production budget gives
            # every readable page a larger floor, but keep the packet contract
            # true even if a unit test exercises a smaller allocation.
            if len(str(projection.get("bodyExcerpt") or "")) > budget:
                projection["bodyExcerpt"] = str(projection.get("bodyExcerpt") or "")[:budget]
                projection["bodyTruncated"] = True
            web_projections[ref] = projection

    def project_evidence(ref: str, item: Mapping[str, Any], aliases: list[str]) -> dict[str, Any]:
        claim_contract = claim_contract_for_evidence(item, evidence_ref=ref, compact=True)
        belief_problem = belief_evidence_problem(item)
        web_projection = web_projections.get(ref, {}) if item.get("sourceType") == "web" else {}
        projection = {
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
            "links": compact_web_links(
                item.get("links"),
                base_url=str(item.get("url") or item.get("canonicalUrl") or ""),
                max_links=16,
            ),
            "historical": bool(item.get("historical")),
            "freshness": claim_contract["freshness"],
            "observedAt": item.get("observedAt"),
            "publishedAt": item.get("publishedAt"),
            "publishedTimestamp": item.get("publishedTimestamp"),
            "dateEvidence": item.get("dateEvidence"),
            "accessedAt": item.get("accessedAt"),
            "sourceTurnId": item.get("sourceTurnId"),
            "answerCommitted": item.get("answerCommitted"),
            "bodyExcerpt": (
                web_projection.get("bodyExcerpt", "")
                if item.get("sourceType") == "web"
                else str(item.get("body") or "")[:1200]
            ),
            "fetchStatus": item.get("fetchStatus"),
            "snippet": str(item.get("snippet") or "")[:500],
            "bodyError": str(item.get("bodyError") or "")[:240],
            "claimContract": claim_contract,
            "beliefUpdateEligibility": {"allowed": belief_problem is None,
                                        "reason": belief_problem[0] if belief_problem else "readable_evidence"},
        }
        if item.get("sourceType") == "web":
            projection.update({
                "evidenceChunks": deduplicate_evidence_chunks(
                    web_projection.get("bodyExcerpt"),
                    web_projection.get("evidenceChunks") or [],
                ),
                "bodyTruncated": bool(web_projection.get("bodyTruncated")),
                "projectionMode": str(web_projection.get("projectionMode") or "empty"),
            })
        return projection
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
        "evidence": [
            project_evidence(ref, item, aliases)
            for ref, item, aliases in selected_evidence
        ],
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
