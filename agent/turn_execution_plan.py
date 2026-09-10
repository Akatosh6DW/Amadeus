"""Immutable per-turn authority plan.

This module is the single place where advisory capability hints become an
execution policy.  Hints may change which schemas are shown to the model, but
they can never require a tool, force a route, or escalate web research.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class CapabilityHint:
    family: str
    score: float
    source: str
    target: str = ""
    ambiguity: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityObligation:
    capability: str
    source: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class TurnExecutionPlan:
    turn_id: str
    revision: int
    research_policy: str
    required_capabilities: tuple[str, ...]
    forbidden_capabilities: tuple[str, ...]
    candidate_hints: tuple[CapabilityHint, ...]
    model_may_request_tools: bool
    max_tool_rounds: int
    authority_reasons: tuple[str, ...]
    policy_writer: str = "TurnExecutionPlanner.finalize"

    def to_dict(self) -> dict[str, object]:
        return {
            "turnId": self.turn_id,
            "revision": self.revision,
            "researchPolicy": self.research_policy,
            "requiredCapabilities": list(self.required_capabilities),
            "forbiddenCapabilities": list(self.forbidden_capabilities),
            "candidateHints": [item.to_dict() for item in self.candidate_hints],
            "modelMayRequestTools": self.model_may_request_tools,
            "maxToolRounds": self.max_tool_rounds,
            "authorityReasons": list(self.authority_reasons),
            "policyWriter": self.policy_writer,
        }


class TurnExecutionPlanner:
    """The sole writer of final tool/research authority for a turn."""

    _HARD_RESEARCH_REASONS = frozenset({
        "browser_target_requires_verified_public_url",
        "model_research_escalation",
        "pre_answer_model_research_audit",
        "pre_answer_model_soft_research_audit",
        "grounded_claim_gate_evidence_gap",
    })

    @classmethod
    def finalize(
        cls,
        *,
        turn_id: str,
        proposed_research_policy: str,
        research_reasons: Sequence[str] = (),
        obligations: Sequence[CapabilityObligation] = (),
        hints: Sequence[CapabilityHint] = (),
        web_permission: bool = True,
        revision: int = 1,
    ) -> TurnExecutionPlan:
        reasons = tuple(dict.fromkeys(str(item) for item in research_reasons if str(item)))
        required = tuple(dict.fromkeys(
            item.capability for item in obligations if item.capability
        ))
        authority = [
            f"obligation:{item.source}:{item.capability}:{item.reason}"
            for item in obligations
        ]

        proposed = str(proposed_research_policy or "allowed").lower()
        explicit_web_obligation = any(
            item.capability in {"web_search", "read_web_page"}
            and item.source in {"explicit_user_request", "browser_target_resolution"}
            for item in obligations
        )
        user_forbids_web = "user_forbids_web" in reasons
        if not web_permission or user_forbids_web:
            research_policy = "forbidden"
            if not web_permission:
                authority.append("permission:web_search_disabled")
        elif explicit_web_obligation:
            research_policy = "required"
            authority.append("research:explicit_capability_obligation")
        elif proposed == "required" and any(
            item in cls._HARD_RESEARCH_REASONS for item in reasons
        ):
            research_policy = "required"
            if "web_search" not in required and "read_web_page" not in required:
                required = (*required, "web_search")
            authority.extend(f"research:{item}" for item in reasons)
        else:
            # Stable questions, comparisons, uncertain entity names, and other
            # heuristics remain model-discretionary.  They do not become an
            # execution obligation merely because browsing may be useful.
            research_policy = "allowed"
            authority.append("research:model_discretion")

        forbidden = ()
        if research_policy == "forbidden":
            forbidden = ("web_search", "read_web_page")
            required = tuple(item for item in required if item not in forbidden)

        deduped_hints: list[CapabilityHint] = []
        seen_hints: set[tuple[str, str]] = set()
        for hint in sorted(hints, key=lambda item: (-item.score, item.family, item.source)):
            key = (hint.family, hint.target)
            if key in seen_hints:
                continue
            seen_hints.add(key)
            deduped_hints.append(hint)

        return TurnExecutionPlan(
            turn_id=str(turn_id or ""),
            revision=max(1, int(revision or 1)),
            research_policy=research_policy,
            required_capabilities=tuple(required),
            forbidden_capabilities=tuple(forbidden),
            candidate_hints=tuple(deduped_hints),
            model_may_request_tools=True,
            # One primary tool round plus at most one evidence recovery round.
            # Repeating three near-identical searches was the dominant long
            # tail in the old research pipeline.
            max_tool_rounds=(
                1 if "pre_answer_model_soft_research_audit" in reasons else 2
            ),
            authority_reasons=tuple(dict.fromkeys(authority)),
        )


def capability_hints_from_preflight(preflight: Any) -> tuple[CapabilityHint, ...]:
    """Convert a semantic preflight into non-authoritative schema hints."""

    candidates = tuple(getattr(preflight, "candidate_capabilities", ()) or ())
    perception = getattr(preflight, "perception", None)
    target = str(getattr(perception, "target_domain", "") or "")
    score = max(0.0, min(1.0, float(getattr(perception, "confidence", 0.0) or 0.0)))
    ambiguities = tuple(getattr(perception, "ambiguities", ()) or ())
    return tuple(
        CapabilityHint(
            family=str(tool_id),
            score=score,
            source="semantic_preflight",
            target=target,
            ambiguity=";".join(str(item) for item in ambiguities),
        )
        for tool_id in candidates
    )


def execution_plan_from_dict(value: Mapping[str, Any] | None) -> TurnExecutionPlan | None:
    data = value or {}
    if not data.get("turnId"):
        return None
    hints = tuple(
        CapabilityHint(
            family=str(item.get("family") or ""),
            score=float(item.get("score") or 0.0),
            source=str(item.get("source") or ""),
            target=str(item.get("target") or ""),
            ambiguity=str(item.get("ambiguity") or ""),
        )
        for item in data.get("candidateHints", ())
        if isinstance(item, Mapping)
    )
    return TurnExecutionPlan(
        turn_id=str(data.get("turnId") or ""),
        revision=max(1, int(data.get("revision") or 1)),
        research_policy=str(data.get("researchPolicy") or "allowed"),
        required_capabilities=tuple(map(str, data.get("requiredCapabilities", ()))),
        forbidden_capabilities=tuple(map(str, data.get("forbiddenCapabilities", ()))),
        candidate_hints=hints,
        model_may_request_tools=bool(data.get("modelMayRequestTools", True)),
        max_tool_rounds=max(0, int(data.get("maxToolRounds") or 0)),
        authority_reasons=tuple(map(str, data.get("authorityReasons", ()))),
        policy_writer=str(data.get("policyWriter") or "TurnExecutionPlanner.finalize"),
    )
