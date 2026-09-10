"""Progressive, attributable conversation compaction.

Compaction is a storage transformation, not an answer generator.  Every level
keeps source turn IDs, speaker ownership, unresolved acts, and validation
evidence so a shorter prompt cannot silently rewrite history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import json
import re
from typing import Any, Callable, Mapping, Sequence


class CompactionLevel(str, Enum):
    SNIP = "snip"
    MICROCOMPACT = "microcompact"
    CONTEXT_COLLAPSE = "context_collapse"
    AUTOCOMPACT = "autocompact"


@dataclass(frozen=True)
class CompactionArtifact:
    version: int
    level: CompactionLevel
    text: str
    source_turn_ids: tuple[str, ...]
    required_acts: tuple[str, ...]
    unresolved_items: tuple[str, ...]
    source_count: int
    original_characters: int
    compacted_characters: int
    protection_reasons: tuple[str, ...] = ()
    protected_claims: tuple[str, ...] = ()
    validation_issues: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.validation_issues

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["level"] = self.level.value
        for key in (
            "source_turn_ids",
            "required_acts",
            "unresolved_items",
            "protection_reasons",
            "protected_claims",
            "validation_issues",
        ):
            value[key] = list(value[key])
        return value


def _one_line(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[: max(0, limit)]


def canonical_turn_id(turn: Mapping[str, Any], index: int = 0) -> str:
    """Return the stable conversation identity used by every memory layer.

    ``turnId`` identifies the persisted dialogue turn. ``requestId`` identifies
    the transport request and may also be present, so it must never take
    precedence when compaction and pending-reference state compare identities.
    """

    return str(
        turn.get("turnId")
        or turn.get("requestId")
        or turn.get("id")
        or f"turn_{index + 1}"
    )[:120]


def _turn_id(turn: Mapping[str, Any], index: int) -> str:
    return canonical_turn_id(turn, index)


def _required_acts(turn: Mapping[str, Any]) -> tuple[str, ...]:
    contract = turn.get("turnContract") if isinstance(turn.get("turnContract"), Mapping) else {}
    raw = contract.get("requiredActs") or contract.get("required_acts") or ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item)[:100] for item in raw if str(item).strip())


def _unresolved_items(turn: Mapping[str, Any]) -> tuple[str, ...]:
    items: list[str] = []
    pending = turn.get("pendingAssistantQuestion")
    if isinstance(pending, Mapping):
        pending = pending.get("text")
    if pending:
        items.append("pending_question:" + _one_line(pending, 240))
    for key, label in (
        ("activeTask", "active_task"),
        ("unfinishedTask", "unfinished_task"),
        ("clarificationRequired", "clarification"),
    ):
        value = turn.get(key)
        if isinstance(value, Mapping):
            value = value.get("text") or value.get("summary") or value.get("target")
        if value:
            items.append(f"{label}:" + _one_line(value, 240))
    result = turn.get("result") if isinstance(turn.get("result"), Mapping) else {}
    if result.get("requiresUserResponse"):
        expected = _one_line(result.get("expectedResponseType"), 80) or "unspecified"
        items.append("requires_user_response:" + expected)
    return tuple(items)


def _protection_reasons(turn: Mapping[str, Any]) -> tuple[str, ...]:
    """Read semantic protection emitted by the live turn pipeline.

    The compactor does not rediscover meaning from prose.  Current turns carry
    an explicit ``contextProtection`` object; legacy turns without it remain
    compactable under the older attribution rules.
    """

    protection = (
        turn.get("contextProtection")
        if isinstance(turn.get("contextProtection"), Mapping) else {}
    )
    raw = protection.get("reasons") or ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(
        str(item).strip()[:80]
        for item in raw
        if str(item).strip()
    ))


def _protected_claims(turn: Mapping[str, Any], turn_id: str) -> tuple[str, ...]:
    protection = (
        turn.get("contextProtection")
        if isinstance(turn.get("contextProtection"), Mapping) else {}
    )
    raw = protection.get("claims") or ()
    if not isinstance(raw, (list, tuple)):
        return ()
    reasons = _protection_reasons(turn)
    result: list[str] = []
    for item in raw:
        proposition = _one_line(item, 800)
        if not proposition:
            continue
        result.append(json.dumps({
            "turnId": turn_id,
            "speaker": "user",
            "proposition": proposition,
            "reasons": list(reasons),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return tuple(dict.fromkeys(result))


def snip_turns(
    turns: Sequence[Mapping[str, Any]],
    *,
    max_field_characters: int = 1200,
) -> tuple[dict[str, Any], ...]:
    """Remove bulky transport payloads without summarizing human language."""

    private_or_bulky = {
        "audioBase64",
        "dataBase64",
        "imageBase64",
        "rawPayload",
        "raw_payload",
        "messages",
        "prompt",
        "systemPrompt",
    }

    def compact(value: Any, depth: int = 0) -> Any:
        if depth >= 5:
            return _one_line(value, 240)
        if isinstance(value, str):
            return value[:max_field_characters]
        if isinstance(value, Mapping):
            return {
                str(key): compact(item, depth + 1)
                for key, item in value.items()
                if str(key) not in private_or_bulky
            }
        if isinstance(value, (list, tuple)):
            return [compact(item, depth + 1) for item in value[:40]]
        return value

    return tuple(compact(dict(turn)) for turn in turns)


def microcompact_turns(
    turns: Sequence[Mapping[str, Any]],
    *,
    user_provenance: Callable[[str], str] | None = None,
    max_characters: int = 3600,
) -> CompactionArtifact:
    """Create a deterministic dialogue summary with explicit attribution."""

    source = [dict(item) for item in turns if isinstance(item, Mapping)]
    turn_ids = tuple(_turn_id(turn, index) for index, turn in enumerate(source))
    required = tuple(dict.fromkeys(
        act for turn in source for act in _required_acts(turn)
    ))
    unresolved = tuple(dict.fromkeys(
        item for turn in source for item in _unresolved_items(turn)
    ))
    protection_reasons = tuple(dict.fromkeys(
        reason for turn in source for reason in _protection_reasons(turn)
    ))
    protected_claims = tuple(dict.fromkeys(
        claim
        for index, turn in enumerate(source)
        for claim in _protected_claims(turn, turn_ids[index])
    ))
    original_chars = sum(len(str(item)) for item in source)
    header: list[str] = [
        "[compaction:microcompact v1]",
        "[source_turns] " + ",".join(turn_ids),
    ]
    if required:
        header.append("[required_acts] " + ",".join(required))
    if unresolved:
        header.extend("[unresolved] " + item for item in unresolved)
    if protection_reasons:
        header.append("[protection_reasons] " + ",".join(protection_reasons))
    if protected_claims:
        header.extend("[protected_claim] " + item for item in protected_claims)

    dialogue: list[str] = []
    for index, turn in enumerate(source):
        turn_id = turn_ids[index]
        user = _one_line(turn.get("user"), 240)
        assistant = _one_line(turn.get("assistant"), 240)
        if user:
            provenance = (
                str(user_provenance(user))
                if user_provenance is not None
                else "user_assertion"
            )
            dialogue.append(f"[turn:{turn_id}][{provenance}] 用户：{user}")
        if assistant:
            claims = turn.get("claimRecords") if isinstance(turn.get("claimRecords"), list) else []
            assistant_claims = [
                item for item in claims
                if isinstance(item, Mapping)
                and str(item.get("provenance_type") or item.get("provenanceType") or "").startswith(
                    ("assistant_", "verified_", "tool_")
                )
            ]
            provenance = str(
                assistant_claims[-1].get("provenance_type")
                or assistant_claims[-1].get("provenanceType")
                or "assistant_unverified_claim"
            ) if assistant_claims else "assistant_unverified_claim"
            dialogue.append(f"[turn:{turn_id}][{provenance}] AMADEUS：{assistant}")

    prefix = "\n".join(header)
    available = max(0, max(800, int(max_characters)) - len(prefix) - 1)
    kept: list[str] = []
    used = 0
    # Preserve the most recent renderings while source IDs and unresolved acts
    # remain complete in the header.
    for line in reversed(dialogue):
        cost = len(line) + (1 if kept else 0)
        if used + cost > available:
            continue
        kept.append(line)
        used += cost
    kept.reverse()
    text = prefix + ("\n" + "\n".join(kept) if kept else "")
    provisional = CompactionArtifact(
        version=1,
        level=CompactionLevel.MICROCOMPACT,
        text=text,
        source_turn_ids=turn_ids,
        required_acts=required,
        unresolved_items=unresolved,
        source_count=len(source),
        original_characters=original_chars,
        compacted_characters=len(text),
        protection_reasons=protection_reasons,
        protected_claims=protected_claims,
    )
    issues = validate_compaction(provisional, source)
    return CompactionArtifact(
        **{**provisional.__dict__, "validation_issues": issues}
    )


def collapse_compaction_artifacts(
    artifacts: Sequence[CompactionArtifact],
    *,
    max_characters: int = 6000,
) -> CompactionArtifact:
    """Collapse older summaries without erasing their provenance metadata."""

    valid = [item for item in artifacts if isinstance(item, CompactionArtifact)]
    turn_ids = tuple(dict.fromkeys(item for artifact in valid for item in artifact.source_turn_ids))
    required = tuple(dict.fromkeys(item for artifact in valid for item in artifact.required_acts))
    unresolved = tuple(dict.fromkeys(item for artifact in valid for item in artifact.unresolved_items))
    protection_reasons = tuple(dict.fromkeys(
        item for artifact in valid for item in artifact.protection_reasons
    ))
    protected_claims = tuple(dict.fromkeys(
        item for artifact in valid for item in artifact.protected_claims
    ))
    header = [
        "[compaction:context_collapse v1]",
        "[source_turns] " + ",".join(turn_ids),
    ]
    if required:
        header.append("[required_acts] " + ",".join(required))
    if unresolved:
        header.extend("[unresolved] " + item for item in unresolved)
    if protection_reasons:
        header.append("[protection_reasons] " + ",".join(protection_reasons))
    if protected_claims:
        header.extend("[protected_claim] " + item for item in protected_claims)
    bodies = [
        re.sub(r"^\[compaction:[^\n]+\]\n?", "", artifact.text).strip()
        for artifact in valid
        if artifact.text.strip()
    ]
    header_text = "\n".join(header)
    limit = max(1200, int(max_characters))
    remaining = max(0, limit - len(header_text) - 1)
    body_text = "\n".join(bodies)[:remaining]
    # Protection metadata is an invariant.  If it alone exceeds the target,
    # keep it intact and let the caller report budget overflow rather than
    # manufacture a semantically invalid compact.
    text = header_text + ("\n" + body_text if body_text else "")
    issues: list[str] = []
    for required_item in required:
        if required_item not in text:
            issues.append("missing_required_act:" + required_item)
    for unresolved_item in unresolved:
        if unresolved_item not in text:
            issues.append("missing_unresolved_item:" + unresolved_item)
    for reason in protection_reasons:
        if reason not in text:
            issues.append("missing_protection_reason:" + reason)
    for claim in protected_claims:
        if claim not in text:
            issues.append("missing_protected_claim:" + claim)
    return CompactionArtifact(
        version=1,
        level=CompactionLevel.CONTEXT_COLLAPSE,
        text=text,
        source_turn_ids=turn_ids,
        required_acts=required,
        unresolved_items=unresolved,
        source_count=sum(item.source_count for item in valid),
        original_characters=sum(item.original_characters for item in valid),
        compacted_characters=len(text),
        protection_reasons=protection_reasons,
        protected_claims=protected_claims,
        validation_issues=tuple(dict.fromkeys(issues)),
    )


def choose_compaction_level(*, prompt_characters: int, budget_characters: int) -> CompactionLevel:
    budget = max(1, int(budget_characters))
    ratio = max(0.0, int(prompt_characters)) / budget
    if ratio < 0.72:
        return CompactionLevel.SNIP
    if ratio < 1.05:
        return CompactionLevel.MICROCOMPACT
    if ratio < 1.45:
        return CompactionLevel.CONTEXT_COLLAPSE
    return CompactionLevel.AUTOCOMPACT


def validate_compaction(
    artifact: CompactionArtifact,
    source_turns: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    issues: list[str] = []
    source = [item for item in source_turns if isinstance(item, Mapping)]
    expected_ids = tuple(_turn_id(turn, index) for index, turn in enumerate(source))
    if artifact.source_count != len(source):
        issues.append("source_count_mismatch")
    for turn_id in expected_ids:
        if turn_id not in artifact.source_turn_ids or turn_id not in artifact.text:
            issues.append("missing_source_turn:" + turn_id)
    expected_required = tuple(dict.fromkeys(
        act for turn in source for act in _required_acts(turn)
    ))
    for act in expected_required:
        if act not in artifact.required_acts or act not in artifact.text:
            issues.append("missing_required_act:" + act)
    expected_unresolved = tuple(dict.fromkeys(
        item for turn in source for item in _unresolved_items(turn)
    ))
    for item in expected_unresolved:
        if item not in artifact.unresolved_items or item not in artifact.text:
            issues.append("missing_unresolved_item:" + item)
    expected_protection_reasons = tuple(dict.fromkeys(
        reason for turn in source for reason in _protection_reasons(turn)
    ))
    for reason in expected_protection_reasons:
        if reason not in artifact.protection_reasons or reason not in artifact.text:
            issues.append("missing_protection_reason:" + reason)
    expected_protected_claims = tuple(dict.fromkeys(
        claim
        for index, turn in enumerate(source)
        for claim in _protected_claims(turn, expected_ids[index])
    ))
    for claim in expected_protected_claims:
        if claim not in artifact.protected_claims or claim not in artifact.text:
            issues.append("missing_protected_claim:" + claim)
    if "用户：" in artifact.text and "[user_" not in artifact.text:
        issues.append("user_attribution_missing")
    if "AMADEUS：" in artifact.text and "[assistant_" not in artifact.text and "[verified_" not in artifact.text and "[tool_" not in artifact.text:
        issues.append("assistant_attribution_missing")
    return tuple(dict.fromkeys(issues))
