"""网页研究的可恢复查询/阅读/复核循环。

入口层负责认知回执与历史指纹；本模块持有循环状态转换和持久化时序。
三个领域端口只执行真实轮次、存取状态与复核检查，不依赖 server。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol, Sequence

from agent.research_architecture import (
    CapabilityEnvelope,
    ResearchDecision,
    ResearchLoopState,
    normalized_query_hash,
)
from backend.common.text import unique_strings
from backend.research.current_info import web_research_temporal_contract
from backend.research.evidence import (
    merge_web_search_results,
    research_evidence_fingerprint,
    research_loop_sources,
)
from backend.research.planning import (
    fallback_web_research_query,
    merge_research_plan_queries,
    safe_outbound_web_query,
)
from backend.research.sources import source_has_readable_evidence
from tools.safe_web import SafeWebError, canonicalize_public_url


@dataclass(frozen=True)
class PreparedResearchLoop:
    """入口预备的持久身份与用户原始目标；不重复读取认知状态。"""

    initial_fingerprint: str
    answer_scope: dict[str, Any]


_QUERY_STRATEGY_FIELDS = ("provider", "locale", "region", "freshness")
_EXPLICIT_QUERY_STRATEGY_FIELDS = ("provider", "locale", "region")


def _source_identity(source: dict[str, Any]) -> str:
    """Return a stable identity for comparing results across rounds.

    Search metadata (snippet, ranking, query labels) is intentionally excluded.
    A canonical URL is preferred because a search adapter can add ``sourceUid``
    only after enrichment, while the URL remains stable between discovery and
    a later read.
    """

    raw_url = str(source.get("canonicalUrl") or source.get("url") or "").strip()
    if raw_url:
        try:
            canonical = canonicalize_public_url(raw_url)
        except (SafeWebError, ValueError, TypeError):
            canonical = raw_url
        if canonical:
            # canonicalize_public_url already normalizes scheme and host. Keep
            # the path/query case because public URLs may be case-sensitive.
            return f"url:{canonical}"
    source_uid = str(source.get("sourceUid") or "").strip()
    if source_uid:
        return f"uid:{source_uid.casefold()}"
    source_id = str(source.get("id") or source.get("title") or "").strip()
    return f"id:{source_id.casefold()}" if source_id else ""


def _source_map(sources: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        identity: source
        for source in sources
        if isinstance(source, dict)
        for identity in [_source_identity(source)]
        if identity
    }


def _readable_content_gain(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> bool:
    """Treat a newly fetched body as progress, while ignoring snippet churn."""

    for identity, current in after.items():
        previous = before.get(identity)
        if previous is None:
            continue
        current_body = str(current.get("body") or "").strip()
        previous_body = str(previous.get("body") or "").strip()
        if not source_has_readable_evidence(current) or current_body == previous_body:
            continue
        return True
    return False


def _within_round_duplicate_ids(
    sources: Sequence[dict[str, Any]],
) -> list[str]:
    """Find one source returned for more than one query in this round."""

    duplicate_ids: list[str] = []
    for source in sources:
        identity = _source_identity(source)
        indexes = source.get("researchQueryIndexes")
        if not identity or not isinstance(indexes, list):
            continue
        distinct_indexes = {
            str(item).strip() for item in indexes if str(item).strip()
        }
        if len(distinct_indexes) > 1:
            duplicate_ids.append(identity)
    return sorted(set(duplicate_ids))


def _query_strategy_signature(item: dict[str, Any]) -> str:
    """Identify a query plus the model-selected retrieval strategy."""

    query_hash = normalized_query_hash(str(item.get("query") or ""))
    options = [
        re.sub(r"\s+", " ", str(item.get(field) or "")).strip().casefold()
        for field in _QUERY_STRATEGY_FIELDS
    ]
    return "|".join([query_hash, *options])


def _has_explicit_query_strategy(item: dict[str, Any]) -> bool:
    return any(
        str(item.get(field) or "").strip()
        for field in _EXPLICIT_QUERY_STRATEGY_FIELDS
    )


def _query_already_executed(
    item: dict[str, Any],
    executed_hashes: set[str],
    executed_strategies: set[str],
) -> bool:
    query_hash = normalized_query_hash(str(item.get("query") or ""))
    if not query_hash or query_hash not in executed_hashes:
        return False
    signature = _query_strategy_signature(item)
    if signature in executed_strategies:
        return True
    # Older persisted states only recorded query hashes. Preserve their
    # default-query reuse while allowing a model-requested provider/locale
    # change to be a genuinely new attempt.
    return not _has_explicit_query_strategy(item)


def _grounded_term_recovery_query(
    plan: dict[str, Any],
    executed_hashes: set[str],
) -> str:
    """Build a short retry from the plan's already-grounded term schema.

    ``questionAnalysis.protectedTerms`` contains phrases that the planner
    deliberately keeps intact.  ``entityTerms`` is the loop's existing
    entity hand-off from the research decision.  Reading only these fields
    avoids rebuilding a long question or leaking loop diagnostics into a
    query.  The fields are bounded here as well because persisted plans may
    come from an older caller.
    """

    analysis = plan.get("questionAnalysis")
    protected_values = (
        analysis.get("protectedTerms")
        if isinstance(analysis, dict)
        and isinstance(analysis.get("protectedTerms"), list)
        else []
    )
    entity_values = (
        plan.get("entityTerms")
        if isinstance(plan.get("entityTerms"), list)
        else []
    )

    def normalize_terms(values: Any) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for value in values:
            phrase = re.sub(r"\s+", " ", str(value or "")).strip(
                " \t\r\n，。！？；,:;!?"
            )
            # Preserve each grounded phrase as a unit.  A single malformed
            # persisted term must not be truncated into a different entity.
            if not phrase or len(phrase) > 96:
                continue
            phrase = safe_outbound_web_query(phrase, max_length=96)
            key = normalized_query_hash(phrase)
            if not phrase or not key or key in seen:
                continue
            seen.add(key)
            terms.append(phrase)
        return terms

    protected_terms = normalize_terms(protected_values)
    entity_terms = normalize_terms(entity_values)
    protected_compact = {
        re.sub(r"\s+", "", phrase).casefold()
        for phrase in protected_terms
    }
    entity_terms = [
        phrase
        for phrase in entity_terms
        if not any(
            re.sub(r"\s+", "", phrase).casefold() in protected
            for protected in protected_compact
        )
    ]
    # Keep the planner's phrase order and give entities only the remaining
    # slots.  No generic words or question text are added by this branch.
    terms = [*protected_terms[:3], *entity_terms[:3]][:4]
    if not terms:
        return ""
    query = safe_outbound_web_query(" ".join(terms), max_length=220)
    if not query or normalized_query_hash(query) in executed_hashes:
        return ""
    return query


def _empty_result_recovery_query(
    plan: dict[str, Any],
    user_message: str,
    messages: list[dict[str, Any]] | None,
    resolution: dict[str, Any],
    attempted_query_items: Sequence[dict[str, Any]],
    executed_hashes: set[str],
) -> dict[str, Any]:
    """Build at most one grounded retry after an empty first search.

    Empty results have no review evidence from which a model can safely plan a
    new route.  Prefer an unexecuted query already present in the plan, then a
    genuinely different phrase derived from the user/resolution.  The caller
    owns the one-attempt budget; this helper never uses an error or retrieval
    diagnostic as query text.
    """

    attempted = next(
        (item for item in attempted_query_items if isinstance(item, dict)),
        {},
    )

    # ``execute_web_research_plan`` sends at most three items in one call.  If
    # the original plan had more, preserve the next query instead of silently
    # abandoning it.  A different provider alone is deliberately not enough:
    # this recovery must change the search text or select a truly new planned
    # query, never replay the same text under another engine label.
    for item in (
        plan.get("queries") if isinstance(plan.get("queries"), list) else []
    ):
        if not isinstance(item, dict):
            continue
        query = safe_outbound_web_query(item.get("query"), max_length=500)
        if not query or normalized_query_hash(query) in executed_hashes:
            continue
        return {**item, "query": query}

    grounded_query = _grounded_term_recovery_query(plan, executed_hashes)
    candidates = (
        grounded_query,
        resolution.get("searchQuery"),
        resolution.get("resolvedQuestion"),
        fallback_web_research_query(user_message, resolution, messages),
        user_message,
    )
    for candidate in candidates:
        query = safe_outbound_web_query(candidate, max_length=500)
        if not query or normalized_query_hash(query) in executed_hashes:
            continue
        recovery: dict[str, Any] = {
            "query": query,
            "reason": (
                "首轮搜索没有可用来源，使用计划中已确认术语重建检索词"
                if grounded_query and candidate == grounded_query
                else "首轮搜索没有可用来源，改用用户问题重建检索词"
            ),
        }
        # Preserve the temporal and regional contract of the failed query;
        # this branch introduces no provider-only retry.
        for field in ("freshness", "locale", "region"):
            value = str(attempted.get(field) or "").strip()
            if value:
                recovery[field] = value[:80]
        return recovery
    return {}


class ResearchRounds(Protocol):
    """逐轮执行搜索或读页，适配器应晚绑定 server 的原公共入口。"""

    def search(
        self, settings: dict[str, Any], plan: dict[str, Any],
        resolution: dict[str, Any], *, request_id: str,
        cognitive_body: dict[str, Any] | None,
    ) -> dict[str, Any]: ...

    def read(
        self, settings: dict[str, Any], requests: Sequence[dict[str, Any]],
        plan: dict[str, Any], *, request_id: str,
        cognitive_body: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]: ...


class ResearchStore(Protocol):
    """持久研究状态；create 保留旧入口对 ResearchLoopState 的动态替换。"""

    def load(self, request_id: str) -> ResearchLoopState | None: ...

    def save(self, state: ResearchLoopState) -> dict[str, Any]: ...

    def create(
        self, *, request_id: str, policy: Any, active_plan: dict[str, Any],
    ) -> ResearchLoopState: ...


class ResearchChecks(Protocol):
    """证据门槛与模型复核；原 server patch 入口在适配器中晚绑定。"""

    def planned_status(
        self, evidence: dict[str, Any], plan: dict[str, Any],
    ) -> dict[str, Any]: ...

    def field_coverage(
        self, evidence: dict[str, Any], intent: str,
        entities: list[str], analysis_focus: list[str],
    ) -> dict[str, Any]: ...

    def needs_review(
        self, plan: dict[str, Any], evidence: dict[str, Any],
        user_message: str, messages: list[dict[str, Any]] | None,
    ) -> bool: ...

    def review(
        self, settings: dict[str, Any], user_message: str,
        messages: list[dict[str, Any]] | None, plan: dict[str, Any],
        evidence: dict[str, Any], *, request_id: str,
    ) -> dict[str, Any]: ...


def run_research_query_loop(
    settings: dict[str, Any],
    user_message: str,
    messages: list[dict[str, Any]] | None,
    initial_plan: dict[str, Any],
    resolution: dict[str, Any] | None,
    decision: ResearchDecision,
    envelope: CapabilityEnvelope,
    *,
    prepared: PreparedResearchLoop,
    rounds: ResearchRounds,
    store: ResearchStore,
    checks: ResearchChecks,
    log_event: Callable[..., None],
    perf_counter: Callable[[], float],
    request_id: str = "",
    cognitive_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
    """恢复已完成状态，依次执行读页/搜索/复核并逐阶段保存。

    已完成状态必须在任何工具前返回；每轮先 with_round 再保存，复核修改后
    再保存，退出前最终保存，保持旧版恢复与不重复调用语义。
    """

    resolution = resolution or {}
    initial_fingerprint = prepared.initial_fingerprint
    answer_scope = prepared.answer_scope
    immutable_resolved_question = re.sub(
        r"\s+",
        " ",
        str(
            answer_scope.get("originalGoal")
            or initial_plan.get("_originalResolvedQuestion")
            or initial_plan.get("resolvedQuestion")
            or resolution.get("resolvedQuestion")
            or user_message
        ),
    ).strip()[:500]
    initial_with_contract = {
        **initial_plan,
        "resolvedQuestion": immutable_resolved_question,
        "_originalResolvedQuestion": immutable_resolved_question,
        **(
            {"answerScope": answer_scope}
            if any(answer_scope.values())
            else {}
        ),
    }
    temporal_contract = web_research_temporal_contract(
        user_message,
        initial_with_contract,
    )
    initial_with_contract["_temporalContract"] = temporal_contract
    initial_with_contract["_temporalContractVersion"] = 1
    restored = store.load(request_id)
    restored_matches = bool(
        restored
        and restored.policy == decision.policy
        and restored.active_plan.get("_initialPlanFingerprint") == initial_fingerprint
    )
    if (
        restored_matches
        and temporal_contract.get("requiresFreshnessEvidence")
        and restored.active_plan.get("_temporalContractVersion") != 1
    ):
        # Completed state from the old contract may have stopped on an older
        # event after review narrowed the question. Re-evaluate it once.
        restored_matches = False
    state = (
        restored
        if restored_matches
        else store.create(
            request_id=request_id,
            policy=decision.policy,
            active_plan={
                **initial_with_contract,
                "_initialPlanFingerprint": initial_fingerprint,
            },
        )
    )
    plan = dict(state.active_plan or initial_with_contract)
    plan["resolvedQuestion"] = immutable_resolved_question
    plan["_originalResolvedQuestion"] = immutable_resolved_question
    plan["_temporalContract"] = temporal_contract
    plan["_temporalContractVersion"] = 1
    if any(answer_scope.values()):
        plan["answerScope"] = answer_scope
    if decision.source_policy_id:
        plan["sourcePolicyId"] = decision.source_policy_id
    entity_terms = unique_strings([
        *decision.unknown_entities,
        *(
            plan.get("entityTerms")
            if isinstance(plan.get("entityTerms"), list)
            else []
        ),
    ])
    if entity_terms:
        plan["entityTerms"] = entity_terms
    if decision.verification_targets:
        plan["verificationTargets"] = list(decision.verification_targets)
    evidence = dict(state.evidence_result or {})
    last_review = dict(state.last_review or {})
    if (
        state.status == "completed"
        and evidence.get("ok")
        and isinstance(evidence.get("evidenceStatus"), dict)
    ):
        log_event(
            "research_loop_restored_completed",
            requestId=request_id,
            toolRounds=state.tool_rounds,
            sourceCount=len(research_loop_sources(evidence)),
            originalResolvedQuestion=immutable_resolved_question,
            temporalCoverage=(evidence.get("evidenceStatus") or {}).get("temporalCoverage", {}),
        )
        evidence["researchLoop"] = {
            **state.to_dict(),
            "evidenceGatePassed": True,
            "restored": True,
            "retrievalDiagnostics": dict(
                plan.get("retrievalDiagnostics")
                if isinstance(plan.get("retrievalDiagnostics"), dict)
                else {}
            ),
        }
        return evidence, plan, last_review, 0

    executed_hashes = set(state.query_hashes)
    executed_strategies = {
        str(item)
        for item in (
            plan.get("_executedQueryStrategies")
            if isinstance(plan.get("_executedQueryStrategies"), list)
            else []
        )
        if str(item)
    }
    attempted_urls = set(state.canonical_urls)
    normalized_plan_queries: list[dict[str, Any]] = []
    for item in (
        plan.get("queries")
        if isinstance(plan.get("queries"), list)
        else []
    ):
        if not isinstance(item, dict):
            continue
        query = safe_outbound_web_query(item.get("query"), max_length=500)
        if query:
            normalized_plan_queries.append({**item, "query": query})
    if len(normalized_plan_queries) != len(
        plan.get("queries") if isinstance(plan.get("queries"), list) else []
    ):
        plan["queries"] = normalized_plan_queries
    pending_queries = [
        dict(item)
        for item in normalized_plan_queries
        if not _query_already_executed(item, executed_hashes, executed_strategies)
    ]
    pending_reads: list[dict[str, Any]] = []
    review_ms = 0
    # Search and read are different recovery actions.  Keep the configured
    # round limit for searches, while reserving one bounded read pass so a
    # normal search -> read flow is not cut off by the same counter.
    max_search_rounds = max(1, int(envelope.max_tool_rounds or 3))
    max_read_rounds = max(1, min(2, max_search_rounds - 1 or 1))
    search_rounds = max(0, int(plan.get("_searchRoundCount") or 0))
    read_rounds = max(0, int(plan.get("_readRoundCount") or 0))
    empty_result_recovery_count = max(
        0, int(plan.get("_emptyResultRecoveryCount") or 0),
    )
    # A failed read can leave enough search budget for one informed review
    # recovery.  Persist the count so a resumed loop cannot turn this into an
    # unbounded model-review cycle.
    review_recovery_count = max(
        0, int(plan.get("_reviewRecoveryCount") or 0),
    )
    budget_exhausted = False

    def apply_review(
        retrieval_diagnostics: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """Run one review and queue only its bounded, validated actions."""

        nonlocal evidence, last_review, pending_reads, pending_queries
        nonlocal plan, review_ms, state

        review_started = perf_counter()
        try:
            review_value = checks.review(
                settings,
                user_message,
                messages,
                plan,
                evidence,
                request_id=request_id,
            )
            last_review = (
                dict(review_value)
                if isinstance(review_value, dict)
                else {
                    "readyToAnswer": False,
                    "planIssues": ["review_output_invalid"],
                    "readUrls": [],
                    "additionalQueries": [],
                }
            )
        except Exception as error:
            last_review = {
                "readyToAnswer": False,
                "planIssues": [f"review_failed:{type(error).__name__}"],
                "readUrls": [],
                "additionalQueries": [],
            }
            log_event(
                "web_research_review_failed",
                requestId=request_id,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )
        review_ms += round((perf_counter() - review_started) * 1000)
        retrieval_diagnostics["reviewRequested"] = True
        retrieval_diagnostics["reviewAdditionalQueryCount"] = len(
            last_review.get("additionalQueries")
            if isinstance(last_review.get("additionalQueries"), list)
            else []
        )
        retrieval_diagnostics["reviewReadUrlCount"] = len(
            last_review.get("readUrls")
            if isinstance(last_review.get("readUrls"), list)
            else []
        )
        evidence_interpretation = str(
            last_review.get("evidenceInterpretation")
            or last_review.get("correctedResolvedQuestion")
            or ""
        ).strip()
        if evidence_interpretation and evidence_interpretation != immutable_resolved_question:
            plan["evidenceInterpretation"] = evidence_interpretation[:500]
        plan["resolvedQuestion"] = immutable_resolved_question
        plan["entityFindings"] = list(last_review.get("entityFindings") or [])
        plan["planIssues"] = list(last_review.get("planIssues") or [])
        existing_status = (
            evidence.get("evidenceStatus")
            if isinstance(evidence.get("evidenceStatus"), dict)
            else {}
        )
        status = (
            existing_status
            if (
                existing_status
                and not plan.get("sourcePolicyId")
                and not plan.get("strictAnchorMatch")
                and not temporal_contract.get("requiresFreshnessEvidence")
                and str(existing_status.get("kind") or "") in {
                    "generic",
                    "planned_research",
                }
            )
            else checks.planned_status(evidence, plan)
        )
        evidence["evidenceStatus"] = status
        evidence["researchPlan"] = plan
        if bool(last_review.get("readyToAnswer")) and bool(status.get("sufficient")):
            state = state.finished(
                status="completed",
                stop_reason="evidence_gate_passed",
                active_plan=plan,
                evidence_result=evidence,
                last_review=last_review,
            )
            return True, status

        pending_reads = []
        for item in (
            last_review.get("readUrls")
            if isinstance(last_review.get("readUrls"), list)
            else []
        ):
            if not isinstance(item, dict):
                continue
            try:
                url = canonicalize_public_url(str(item.get("url") or "").strip())
            except (SafeWebError, ValueError, TypeError):
                continue
            if url and url not in attempted_urls:
                pending_reads.append({**item, "url": url})
        combined_plan, additions, _ = merge_research_plan_queries(
            plan,
            [
                item
                for item in (
                    last_review.get("additionalQueries")
                    if isinstance(last_review.get("additionalQueries"), list)
                    else []
                )
                if isinstance(item, dict)
            ],
        )
        plan = combined_plan
        pending_queries = [
            item
            for item in additions
            if not _query_already_executed(item, executed_hashes, executed_strategies)
        ]
        retrieval_diagnostics["reviewAdditionalQueryCount"] = len(pending_queries)
        retrieval_diagnostics["reviewReadUrlCount"] = len(pending_reads)
        plan["retrievalDiagnostics"] = retrieval_diagnostics
        evidence["retrievalDiagnostics"] = dict(retrieval_diagnostics)
        state = replace(
            state,
            active_plan=plan,
            evidence_result=evidence,
            last_review=last_review,
        )
        store.save(state)
        if not pending_reads and not pending_queries:
            stop_reason = (
                "duplicate_results"
                if retrieval_diagnostics.get("repeatedResult")
                else "review_not_ready"
                if not status.get("sufficient")
                else "source_policy_not_confirmed"
            )
            state = state.finished(
                status="insufficient",
                stop_reason=stop_reason,
                active_plan=plan,
                evidence_result=evidence,
                last_review=last_review,
            )
            return True, status
        return False, status

    while pending_reads or pending_queries:
        if pending_reads and read_rounds >= max_read_rounds:
            budget_exhausted = True
            break
        if not pending_reads and pending_queries and search_rounds >= max_search_rounds:
            budget_exhausted = True
            break
        if not pending_reads and not pending_queries:
            break
        log_event(
            "research_loop_round_started",
            requestId=request_id,
            round=state.tool_rounds + 1,
            pendingQueries=[
                str(item.get("query") or "")[:240] for item in pending_queries
            ],
            pendingReadUrls=[
                str(item.get("url") or "")[:500] for item in pending_reads
            ],
        )
        before = research_evidence_fingerprint(evidence)
        before_sources = research_loop_sources(evidence)
        before_source_map = _source_map(before_sources)
        attempted_queries: list[str] = []
        attempted_query_items: list[dict[str, Any]] = []
        attempted_read_urls: list[str] = []
        round_result: dict[str, Any] = {}
        round_errors: list[dict[str, str]] = []
        round_kind = ""
        if pending_reads:
            round_kind = "read"
            read_plan = {**plan, "_currentEvidence": evidence}
            round_result, attempted_read_urls, round_errors = rounds.read(
                settings,
                pending_reads,
                read_plan,
                cognitive_body=cognitive_body, request_id=request_id,
            )
            pending_reads = []
            read_rounds += 1
        elif pending_queries:
            round_kind = "search"
            # rounds.execute_web_research_plan has the same three-query cap;
            # persist only calls that this loop actually handed to it.
            attempted_query_items = [dict(item) for item in pending_queries[:3]]
            attempted_queries = [
                str(item.get("query") or "").strip()
                for item in attempted_query_items
                if str(item.get("query") or "").strip()
            ]
            query_indexes = [
                index
                for index, item in enumerate(plan.get("queries") or [])
                if isinstance(item, dict)
                and normalized_query_hash(str(item.get("query") or ""))
                in {normalized_query_hash(query) for query in attempted_queries}
            ]
            offset = min(query_indexes) if query_indexes else 0
            round_plan = {
                **plan,
                "queries": pending_queries,
                "_queryIndexOffset": offset,
            }
            round_result = rounds.search(
                settings,
                round_plan,
                resolution,
                request_id=request_id,
                cognitive_body=cognitive_body,
            )
            pending_queries = []
            search_rounds += 1
            for item in attempted_query_items:
                signature = _query_strategy_signature(item)
                if signature:
                    executed_strategies.add(signature)
            if not round_result.get("ok"):
                round_errors = list(round_result.get("queryErrors") or [])
        else:
            break

        if round_result.get("ok"):
            evidence = (
                merge_web_search_results(evidence, round_result)
                if evidence.get("ok")
                else dict(round_result)
            )
            evidence["name"] = "web_search"
            evidence["ok"] = True
        elif not evidence:
            evidence = {
                **round_result,
                "name": "web_search",
                "ok": False,
                "error": str(
                    round_result.get("error")
                    or "research_evidence_unavailable"
                ),
            }
        evidence["researchPlan"] = plan
        if evidence.get("ok"):
            precomputed_status = (
                round_result.get("evidenceStatus")
                if isinstance(round_result.get("evidenceStatus"), dict)
                else {}
            )
            computed_status = (
                checks.field_coverage(
                    evidence,
                    str(resolution.get("intent") or ""),
                    [
                        str(item)
                        for item in resolution.get("entities", [])
                        if str(item).strip()
                    ],
                    [
                        str(item)
                        for item in resolution.get("analysisFocus", [])
                        if str(item).strip()
                    ],
                )
                if resolution.get("kind") == "sports" and resolution.get("intent")
                else checks.planned_status(evidence, plan)
            )
            evidence["evidenceStatus"] = (
                precomputed_status
                if (
                    precomputed_status
                    and not plan.get("sourcePolicyId")
                    and not plan.get("strictAnchorMatch")
                    and not temporal_contract.get("requiresFreshnessEvidence")
                    and (
                        bool(precomputed_status.get("sufficient"))
                        or str(precomputed_status.get("kind") or "") in {
                            "generic",
                            "planned_research",
                        }
                    )
                )
                else computed_status
            )
        after = research_evidence_fingerprint(evidence)
        sources = research_loop_sources(evidence)
        after_source_map = _source_map(sources)
        round_source_map = _source_map(research_loop_sources(round_result))
        new_source_ids = sorted(set(after_source_map) - set(before_source_map))
        duplicate_source_ids = sorted(
            set(round_source_map).intersection(before_source_map)
        )
        within_round_duplicate_ids = _within_round_duplicate_ids(
            research_loop_sources(round_result)
        )
        empty_search_round = bool(
            round_kind == "search"
            and not round_result.get("ok")
            and not before_source_map
            and not round_source_map
        )
        # Search adapters may read the top pages in the same call, so a newly
        # fetched body is progress even when no separate read round occurred.
        readable_content_gain = _readable_content_gain(
            before_source_map, after_source_map,
        )
        evidence_gain = bool(new_source_ids or readable_content_gain)
        retrieval_diagnostics: dict[str, Any] = {
            "action": round_kind,
            "searchRoundCount": search_rounds,
            "readRoundCount": read_rounds,
            "searchRoundLimit": max_search_rounds,
            "readRoundLimit": max_read_rounds,
            "resultSourceCount": len(round_source_map),
            "sourceCountBefore": len(before_source_map),
            "sourceCountAfter": len(after_source_map),
            "newSourceCount": len(new_source_ids),
            "newSourceIds": new_source_ids[:24],
            "duplicateSourceCount": len(duplicate_source_ids),
            "duplicateSourceIds": duplicate_source_ids[:24],
            "withinRoundDuplicateSourceCount": len(within_round_duplicate_ids),
            "withinRoundDuplicateSourceIds": within_round_duplicate_ids[:24],
            "queryResultOverlap": bool(within_round_duplicate_ids),
            "readableContentGain": readable_content_gain,
            "evidenceFingerprintChanged": after != before,
            "evidenceGain": evidence_gain,
            "repeatedResult": bool(round_source_map and not new_source_ids and not readable_content_gain),
            "attemptedQueries": [item[:240] for item in attempted_queries],
        }
        plan["_executedQueryStrategies"] = sorted(executed_strategies)[-64:]
        plan["_searchRoundCount"] = search_rounds
        plan["_readRoundCount"] = read_rounds
        plan["retrievalDiagnostics"] = retrieval_diagnostics
        evidence["retrievalDiagnostics"] = dict(retrieval_diagnostics)
        state = state.with_round(
            queries=attempted_queries,
            urls=attempted_read_urls,
            source_ids=[
                str(source.get("sourceUid") or source.get("id") or "")
                for source in sources
                if str(source.get("sourceUid") or source.get("id") or "")
            ],
            active_plan=plan,
            evidence_result=evidence,
            last_review=last_review,
            evidence_gain=evidence_gain,
        )
        store.save(state)
        executed_hashes = set(state.query_hashes)
        attempted_urls = set(state.canonical_urls)
        status = (
            evidence.get("evidenceStatus")
            if isinstance(evidence.get("evidenceStatus"), dict)
            else {}
        )
        log_event(
            "research_loop_round_completed",
            requestId=request_id,
            round=state.tool_rounds,
            evidenceGain=evidence_gain,
            retrievalDiagnostics=retrieval_diagnostics,
            noProgressCycles=state.no_progress_cycles,
            sourceCount=len(sources),
            evidenceSufficient=bool(status.get("sufficient")),
            originalResolvedQuestion=immutable_resolved_question,
            temporalCoverage={
                key: (status.get("temporalCoverage") or {}).get(key)
                for key in (
                    "requestedFreshness", "latestCandidateDate", "latestVerifiedDate",
                    "unresolvedNewerSourceIds", "unknownDateSourceIds",
                    "freshnessConfirmed", "coverageSufficient",
                )
            },
            errors=round_errors,
        )
        if (
            status.get("sufficient")
            and not empty_search_round
            and (
                plan.get("browserTargetResolution")
                or not checks.needs_review(
                    plan,
                    evidence,
                    user_message,
                    messages,
                )
            )
        ):
            state = state.finished(
                status="completed",
                stop_reason="deterministic_evidence_gate_passed",
                active_plan=plan,
                evidence_result=evidence,
                last_review=last_review,
            )
            break
        if empty_search_round:
            recovery_item = {}
            if (
                search_rounds < max_search_rounds
                and empty_result_recovery_count < 1
            ):
                recovery_item = _empty_result_recovery_query(
                    plan,
                    user_message,
                    messages,
                    resolution,
                    attempted_query_items,
                    executed_hashes,
                )
            if recovery_item:
                combined_plan, additions, _ = merge_research_plan_queries(
                    plan, [recovery_item],
                )
                recovery_candidates = additions or [recovery_item]
                pending_queries = [
                    item
                    for item in recovery_candidates
                    if not _query_already_executed(
                        item, executed_hashes, executed_strategies,
                    )
                ]
                if pending_queries:
                    empty_result_recovery_count += 1
                    plan = combined_plan
                    plan["_emptyResultRecoveryCount"] = empty_result_recovery_count
                    retrieval_diagnostics["emptyResultRecovery"] = "query_revision"
                    retrieval_diagnostics["emptyResultRecoveryCount"] = (
                        empty_result_recovery_count
                    )
                    plan["retrievalDiagnostics"] = retrieval_diagnostics
                    evidence["researchPlan"] = plan
                    evidence["retrievalDiagnostics"] = dict(retrieval_diagnostics)
                    state = replace(
                        state,
                        active_plan=plan,
                        evidence_result=evidence,
                        last_review=last_review,
                    )
                    store.save(state)
                    continue
            # A failed search with no source is a no-progress failure after
            # the one deterministic recovery this loop owns.
            if state.no_progress_cycles >= 1:
                state = state.finished(
                    status="insufficient",
                    stop_reason="no_progress",
                    active_plan=plan,
                    evidence_result=evidence,
                    last_review=last_review,
                )
                break
        # A review may queue a search alongside a read.  The read is given
        # priority for this iteration, but the queued query is already a
        # reviewed action.  Let the next iteration execute it before applying
        # a no-progress stop; this also avoids another review solely to run a
        # pending query.
        if pending_queries:
            continue
        # A failed read can still leave search budget.  Give the reviewer one
        # explicit recovery opportunity with the failed URLs and errors so it
        # can choose a distinct query or a known public entry point.  A
        # successful-but-duplicate read deliberately stays on the fast stop
        # path; there is no new failure information to review.
        read_failed_without_progress = bool(
            round_kind == "read"
            and not evidence_gain
            and (not round_result.get("ok") or bool(round_errors))
            and last_review
            and not bool(last_review.get("readyToAnswer"))
        )
        if (
            read_failed_without_progress
            and search_rounds < max_search_rounds
            and review_recovery_count < 1
        ):
            review_recovery_count += 1
            recovery_errors = list(round_errors[:4])
            if not recovery_errors and str(round_result.get("error") or "").strip():
                recovery_errors = [{
                    "url": attempted_read_urls[0] if attempted_read_urls else "",
                    "error": str(round_result.get("error") or "")[:500],
                }]
            recovery_context = {
                "reason": "read_failed_without_new_evidence",
                "attemptedUrls": [str(url)[:500] for url in attempted_read_urls[:4]],
                "errors": [
                    {
                        key: str(item.get(key) or "")[:500]
                        for key in ("url", "error", "message")
                        if str(item.get(key) or "").strip()
                    }
                    for item in recovery_errors
                    if isinstance(item, dict)
                ],
                "remainingSearchRounds": max(0, max_search_rounds - search_rounds),
                "attempt": review_recovery_count,
            }
            plan["_reviewRecoveryCount"] = review_recovery_count
            plan["reviewRecovery"] = recovery_context
            retrieval_diagnostics["reviewRecovery"] = dict(recovery_context)
            plan["retrievalDiagnostics"] = retrieval_diagnostics
            evidence["retrievalDiagnostics"] = dict(retrieval_diagnostics)
            review_finished, _ = apply_review(retrieval_diagnostics)
            if review_finished:
                break
            if pending_queries or pending_reads:
                continue

        # Once the bounded empty-result recovery is unavailable or exhausted,
        # retain the fast failure path.  A prior review with no new source/body
        # also has no new input for another model review, so stop deterministically
        # instead of sending the same evidence back repeatedly.
        if state.no_progress_cycles >= 1 and last_review and evidence.get("ok"):
            retrieval_diagnostics["reviewRequested"] = False
            retrieval_diagnostics["reviewSkipped"] = True
            retrieval_diagnostics["reviewSkipReason"] = "no_new_evidence"
            plan["retrievalDiagnostics"] = retrieval_diagnostics
            evidence["retrievalDiagnostics"] = dict(retrieval_diagnostics)
            state = state.finished(
                status="insufficient",
                stop_reason="duplicate_results",
                active_plan=plan,
                evidence_result=evidence,
                last_review=last_review,
            )
            break
        if state.no_progress_cycles > 1 and evidence.get("ok"):
            retrieval_diagnostics["stopReason"] = "duplicate_results"
            plan["retrievalDiagnostics"] = retrieval_diagnostics
            evidence["retrievalDiagnostics"] = dict(retrieval_diagnostics)
            state = state.finished(
                status="insufficient",
                stop_reason="duplicate_results",
                active_plan=plan,
                evidence_result=evidence,
                last_review=last_review,
            )
            break
        if state.no_progress_cycles >= 1 and not evidence.get("ok"):
            state = state.finished(
                status="insufficient",
                stop_reason="no_progress",
                active_plan=plan,
                evidence_result=evidence,
                last_review=last_review,
            )
            break
        if not evidence.get("ok"):
            if not pending_queries and not pending_reads:
                state = state.finished(
                    status="insufficient",
                    stop_reason="all_tools_failed",
                    active_plan=plan,
                    evidence_result=evidence,
                    last_review=last_review,
                )
                break
            continue

        review_finished, _ = apply_review(retrieval_diagnostics)
        if review_finished:
            break

    pending_actions = {
        "readUrls": [
            str(item.get("url") or "")[:500]
            for item in pending_reads
            if isinstance(item, dict) and str(item.get("url") or "").strip()
        ],
        "queries": [
            {
                "query": str(item.get("query") or "")[:240],
                **(
                    {"provider": str(item.get("provider") or "")[:80]}
                    if str(item.get("provider") or "").strip()
                    else {}
                ),
            }
            for item in pending_queries
            if isinstance(item, dict) and str(item.get("query") or "").strip()
        ],
    }
    if any(pending_actions.values()):
        plan["pendingActions"] = pending_actions
        plan["budgetExhausted"] = bool(budget_exhausted)
        if isinstance(plan.get("retrievalDiagnostics"), dict):
            plan["retrievalDiagnostics"] = {
                **plan["retrievalDiagnostics"],
                "budgetExhausted": bool(budget_exhausted),
                "pendingReadCount": len(pending_actions["readUrls"]),
                "pendingQueryCount": len(pending_actions["queries"]),
            }
            evidence["retrievalDiagnostics"] = dict(plan["retrievalDiagnostics"])
        evidence["pendingActions"] = pending_actions

    if state.status not in {"completed", "insufficient"}:
        status = (
            evidence.get("evidenceStatus")
            if isinstance(evidence.get("evidenceStatus"), dict)
            else {}
        )
        pending = bool(any(pending_actions.values()))
        state = state.finished(
            status=(
                "completed"
                if status.get("sufficient") and not pending
                else "insufficient"
            ),
            stop_reason=(
                "evidence_gate_passed"
                if status.get("sufficient") and not pending
                else "duplicate_results"
                if isinstance(plan.get("retrievalDiagnostics"), dict)
                and plan["retrievalDiagnostics"].get("repeatedResult")
                else "tool_round_limit"
            ),
            active_plan=plan,
            evidence_result=evidence,
            last_review=last_review,
        )
    store.save(state)
    evidence["researchPlan"] = plan
    evidence["researchLoop"] = {
        **state.to_dict(),
        "evidenceGatePassed": state.status == "completed",
        "retrievalDiagnostics": dict(
            plan.get("retrievalDiagnostics")
            if isinstance(plan.get("retrievalDiagnostics"), dict)
            else {}
        ),
    }
    log_event(
        "research_loop_stopped",
        requestId=request_id,
        status=state.status,
        stopReason=state.stop_reason,
        toolRounds=state.tool_rounds,
        evidenceGatePassed=state.status == "completed",
        originalResolvedQuestion=immutable_resolved_question,
        evidenceInterpretation=str(plan.get("evidenceInterpretation") or "")[:500],
        retrievalDiagnostics=(
            plan.get("retrievalDiagnostics")
            if isinstance(plan.get("retrievalDiagnostics"), dict)
            else {}
        ),
        temporalCoverage={
            key: (
                (evidence.get("evidenceStatus") or {}).get("temporalCoverage") or {}
            ).get(key)
            for key in (
                "requestedFreshness", "latestCandidateDate", "latestVerifiedDate",
                "unresolvedNewerSourceIds", "unknownDateSourceIds",
                "freshnessConfirmed", "coverageSufficient",
            )
        },
        pendingActions=pending_actions,
        budgetExhausted=bool(budget_exhausted),
    )
    return evidence, plan, last_review, review_ms
