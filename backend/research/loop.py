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
from backend.research.planning import merge_research_plan_queries
from tools.safe_web import SafeWebError, canonicalize_public_url


@dataclass(frozen=True)
class PreparedResearchLoop:
    """入口预备的持久身份与用户原始目标；不重复读取认知状态。"""

    initial_fingerprint: str
    answer_scope: dict[str, Any]


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
        }
        return evidence, plan, last_review, 0

    executed_hashes = set(state.query_hashes)
    attempted_urls = set(state.canonical_urls)
    pending_queries = [
        dict(item)
        for item in (
            plan.get("queries")
            if isinstance(plan.get("queries"), list)
            else []
        )
        if isinstance(item, dict)
        and str(item.get("query") or "").strip()
        and normalized_query_hash(str(item.get("query") or "")) not in executed_hashes
    ]
    pending_reads: list[dict[str, Any]] = []
    review_ms = 0
    max_rounds = max(1, int(envelope.max_tool_rounds or 3))

    while state.tool_rounds < max_rounds:
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
        attempted_queries: list[str] = []
        attempted_read_urls: list[str] = []
        round_result: dict[str, Any] = {}
        round_errors: list[dict[str, str]] = []
        if pending_reads:
            read_plan = {**plan, "_currentEvidence": evidence}
            round_result, attempted_read_urls, round_errors = rounds.read(
                settings,
                pending_reads,
                read_plan,
                cognitive_body=cognitive_body, request_id=request_id,
            )
            pending_reads = []
        elif pending_queries:
            attempted_queries = [
                str(item.get("query") or "").strip()
                for item in pending_queries
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
            evidence_gain=after != before,
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
            evidenceGain=after != before,
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
        if state.no_progress_cycles >= 1:
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

        review_started = perf_counter()
        try:
            last_review = checks.review(
                settings,
                user_message,
                messages,
                plan,
                evidence,
                request_id=request_id,
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
            break

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
            if normalized_query_hash(str(item.get("query") or ""))
            not in executed_hashes
        ]
        state = replace(
            state,
            active_plan=plan,
            evidence_result=evidence,
            last_review=last_review,
        )
        store.save(state)
        if not pending_reads and not pending_queries:
            state = state.finished(
                status="insufficient",
                stop_reason=(
                    "review_not_ready"
                    if not status.get("sufficient")
                    else "source_policy_not_confirmed"
                ),
                active_plan=plan,
                evidence_result=evidence,
                last_review=last_review,
            )
            break

    if state.status not in {"completed", "insufficient"}:
        status = (
            evidence.get("evidenceStatus")
            if isinstance(evidence.get("evidenceStatus"), dict)
            else {}
        )
        state = state.finished(
            status="completed" if status.get("sufficient") else "insufficient",
            stop_reason=(
                "evidence_gate_passed"
                if status.get("sufficient")
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
    )
    return evidence, plan, last_review, review_ms
