"""The shared text-delivery boundary for normal, fallback, QQ and proactive turns.

Permission failures belong to tool results. Quality and presentation failures
belong to annotations. Only missing/unparseable reply and actual private content
can block this boundary. No model calls and no side effects occur here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any, Callable, Mapping, Sequence

from agent.check_policy import Action, RepairBudget, record, soft_findings
from backend.security.outbound import DEFAULT_GUARD, PrivateContentGuard
from backend.providers.errors import UpstreamProtocolError


ENVELOPE_FIELDS = frozenset({"reply", "spokenTextJa", "emotion", "gesture", "speechUnits", "voiceStyle", "speechPace", "speechSpeed", "kind", "cognitive", "outcome"})
_QQ_QUERY_TOOLS = frozenset({"qq_status", "qq_activity", "qq_history", "qq_identity"})
_QQ_SEND_ACTIONS = frozenset({"send", "delegate", "approve"})
_QQ_PREFLIGHT_ERRORS = frozenset({
    "invalid_target_type",
    "qq_request_id_required",
    "invalid_qq_attachment_ids",
    "qq_attachments_only_draft_send",
    "invalid_qq_image_ids",
    "qq_account_required",
    "qq_account_changed",
    "qq_attachment_import_failed",
    "qq_attachment_not_found",
    "invalid_imported_qq_image_ids",
    "qq_image_selection_limit",
    "qq_draft_required",
})
_QQ_ACTION_ONLY_ACT = "execute_or_report_action"
_QQ_GENERIC_RESPONSE_ACT = "respond_to_current_utterance"


class DeliveryError(UpstreamProtocolError):
    def __init__(self, code: str, findings: Sequence[Mapping] = ()):
        self.code = code
        self.findings = list(findings)
        super().__init__(code)


class HostSilentDelivery(dict):
    """Host-owned no-message disposition; JSON/model fields cannot create it."""


@dataclass(frozen=True)
class DeliveryDecision:
    allowed: bool
    candidate: dict[str, Any]
    failure: str = ""
    findings: list[dict] = field(default_factory=list)


def _qq_signature_args(result: Mapping[str, Any]) -> Mapping[str, Any]:
    signature = result.get("_cognitiveRequestSignature")
    if isinstance(signature, Mapping):
        args = signature.get("args")
        return args if isinstance(args, Mapping) else {}
    if not isinstance(signature, str):
        return {}
    try:
        parsed = json.loads(signature)
    except (TypeError, ValueError):
        return {}
    args = parsed.get("args") if isinstance(parsed, Mapping) else None
    return args if isinstance(args, Mapping) else {}


def _qq_tool_name(result: Mapping[str, Any]) -> str:
    return str(result.get("name") or result.get("tool") or "").strip().lower()


def _qq_tool_action(result: Mapping[str, Any]) -> str:
    """Read the host-bound QQ action without trusting model prose fields."""

    args = _qq_signature_args(result)
    action = str(result.get("action") or args.get("action") or "").strip().lower()
    if action:
        return action
    # Scheduled receipts are deliberately reduced to the adapter's delivery
    # facts.  A bounded delivery field identifies the send operation, while
    # sent+messageId remains mandatory for a success claim below.
    if _qq_tool_name(result) == "qq_manage" and (
        result.get("deliveryState") or result.get("messageId") or isinstance(result.get("sent"), bool)
    ):
        return "send"
    return ""


def _qq_target(result: Mapping[str, Any]) -> tuple[str, str, str]:
    args = _qq_signature_args(result)
    account = str(result.get("account") or args.get("account") or args.get("accountId") or "").strip()
    target_type = str(result.get("targetType") or args.get("targetType") or "").strip().lower()
    target_id = str(
        result.get("targetId") or args.get("targetId") or args.get("target") or ""
    ).strip()
    return account, target_type, target_id


def _qq_error_code(result: Mapping[str, Any]) -> str:
    value = result.get("errorCode") or result.get("error")
    if isinstance(value, str):
        code = value.strip().lower()
        if code:
            return code
    elif isinstance(value, Mapping):
        code = str(value.get("code") or "").strip().lower()
        if code:
            return code
    envelope = result.get("_toolResultEnvelope")
    error = envelope.get("error") if isinstance(envelope, Mapping) else None
    if isinstance(error, Mapping):
        return str(error.get("code") or "").strip().lower()
    return ""


def _qq_receipt_rows(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for key in ("_conversationToolResults", "_toolResults", "toolResults", "scheduledToolReceipts"):
        value = candidate.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    scheduled_delivery = candidate.get("scheduledDelivery")
    if isinstance(scheduled_delivery, Mapping):
        status = str(scheduled_delivery.get("status") or "").strip().lower()
        if status and status != "not_attempted":
            target = scheduled_delivery.get("target")
            target = target if isinstance(target, Mapping) else {}
            scheduled_row = {
                "name": "qq_manage",
                "action": "send",
                "ok": status == "sent",
                "sent": status == "sent",
                "deliveryState": status,
                "messageId": str(scheduled_delivery.get("messageId") or ""),
                "account": target.get("accountId") or target.get("account"),
                "targetType": target.get("targetType"),
                "targetId": target.get("targetId"),
            }
            # Prefer the reduced scheduled receipt when the step row omitted
            # a platform ID; otherwise the two durable views describe one
            # operation and must not look like two sends.
            has_matching_step = any(
                _qq_tool_name(row) == "qq_manage"
                and str(row.get("deliveryState") or "").strip().lower() == status
                and _qq_target(row)[1:] == (
                    str(target.get("targetType") or "").strip().lower(),
                    str(target.get("targetId") or "").strip(),
                )
                and (status != "sent" or (
                    row.get("sent") is True and str(row.get("messageId") or "").strip()
                ))
                for row in rows
            )
            if not has_matching_step:
                rows.append(scheduled_row)
    # The same host receipt can be present in evidence and the reduced result;
    # count each structured operation once so a duplicate copy cannot turn one
    # successful send into a mixed-action turn.
    unique: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str, str, str]] = set()
    for row in rows:
        account, target_type, target_id = _qq_target(row)
        identity = (
            _qq_tool_name(row), _qq_tool_action(row),
            str(row.get("deliveryState") or "").strip().lower(),
            str(row.get("messageId") or row.get("draftId") or "").strip(),
            account, target_type, target_id,
            str(row.get("toolCallId") or row.get("callId") or "").strip(),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return unique


def _qq_successful_send(result: Mapping[str, Any]) -> bool:
    return bool(
        _qq_tool_name(result) == "qq_manage"
        and _qq_tool_action(result) in _QQ_SEND_ACTIONS
        and result.get("ok") is not False
        and result.get("sent") is True
        and str(result.get("deliveryState") or "").strip().lower() == "sent"
        and str(result.get("messageId") or "").strip()
    )


def _qq_preflight_failure(result: Mapping[str, Any]) -> bool:
    """Recognize only a bounded argument rejection before gateway delivery."""

    _, _, target_id = _qq_target(result)
    return bool(
        _qq_tool_name(result) == "qq_manage"
        and _qq_tool_action(result) in _QQ_SEND_ACTIONS
        and result.get("ok") is False
        and not str(result.get("deliveryState") or "").strip()
        and not str(result.get("messageId") or "").strip()
        and not str(result.get("draftId") or "").strip()
        and result.get("sent") is not True
        and _qq_error_code(result) in _QQ_PREFLIGHT_ERRORS
        and target_id
    )


def _qq_same_target(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_account, left_type, left_id = _qq_target(left)
    right_account, right_type, right_id = _qq_target(right)
    if not left_id or not right_id:
        return False
    if left_id != right_id:
        return False
    # invalid_target_type can leave the rejected call without a type; the
    # exact target ID still binds it to a later corrected private/group call.
    # An explicit type mismatch remains a different target.
    if left_type and right_type and left_type != right_type:
        return False
    return not left_account or not right_account or left_account == right_account


def _qq_outcome_only_tool_completion(candidate: Mapping[str, Any]) -> bool:
    """Accept only a non-empty outcome made entirely of successful completions."""

    outcome: Mapping[str, Any] | None = None
    for key in ("outcome", "_cognitiveOutcome"):
        value = candidate.get(key)
        if isinstance(value, Mapping):
            outcome = value
            break
    if outcome is None:
        cognitive = candidate.get("cognitive")
        if isinstance(cognitive, Mapping) and isinstance(cognitive.get("outcome"), Mapping):
            outcome = cognitive["outcome"]
    claims = outcome.get("claims") if isinstance(outcome, Mapping) else None
    return bool(
        isinstance(claims, list)
        and claims
        and all(
            isinstance(claim, Mapping)
            and str(claim.get("kind") or "").strip().lower() == "tool_completion"
            and str(claim.get("resultStatus") or "").strip().lower() == "success"
            for claim in claims
        )
    )


def _qq_required_acts(candidate: Mapping[str, Any]) -> tuple[str, ...] | None:
    envelope = candidate.get("finalTurnEnvelope")
    if not isinstance(envelope, Mapping):
        return None
    raw = envelope.get("required_acts")
    if raw is None:
        raw = envelope.get("requiredActs")
    if not isinstance(raw, list):
        return None
    return tuple(str(item).strip().lower() for item in raw if str(item).strip())


def _qq_required_acts_allow_silence(candidate: Mapping[str, Any]) -> bool:
    """Keep any host-declared substantive obligation visible."""

    acts = _qq_required_acts(candidate)
    if acts is None or not acts:
        return True
    if any(item not in {_QQ_ACTION_ONLY_ACT, _QQ_GENERIC_RESPONSE_ACT} for item in acts):
        return False
    # The generic response act is the fallback emitted for an action turn. It
    # is safe to suppress only when the structured outcome has no additional
    # fact or answer claim; an explicit answer/task act stays visible above.
    return _QQ_GENERIC_RESPONSE_ACT not in acts or _qq_outcome_only_tool_completion(candidate)


def _successful_qq_send_only(candidate: Mapping[str, Any]) -> bool:
    """Return true only for a complete, host-receipted QQ send-only turn.

    Query tools are allowed as preparation.  A preflight argument rejection
    may be folded only when the same turn has a later confirmed send to the
    exact same target. Gateway failed/unknown/draft receipts and mixed
    non-QQ side-effecting actions remain visible.
    """

    raw_results = _qq_receipt_rows(candidate)
    if not raw_results:
        return False
    successful: list[Mapping[str, Any]] = []
    preflight: list[Mapping[str, Any]] = []
    for raw in raw_results:
        name = _qq_tool_name(raw)
        if name in _QQ_QUERY_TOOLS:
            if raw.get("ok") is not True:
                return False
            continue
        action = _qq_tool_action(raw)
        if name != "qq_manage" or action not in _QQ_SEND_ACTIONS:
            return False
        if _qq_successful_send(raw):
            successful.append(raw)
        elif _qq_preflight_failure(raw):
            preflight.append(raw)
        else:
            return False
    if not successful:
        return False
    return all(
        any(_qq_same_target(item, sent) for sent in successful)
        for item in preflight
    )


def _project_successful_qq_send_silence(result: dict[str, Any]) -> None:
    """Annotate and persist the host-owned silent success disposition.

    The Live2D projection already suppresses a turn whose claims contain only
    successful ``tool_completion`` entries.  QQ action models commonly add a
    useful ``tool_fact`` sentence about the generated QQ text; that sentence
    is retained under ``supportingClaims`` while the durable presentation
    claims use the same receipt-backed success criterion as the live trailer.
    """

    if not _qq_required_acts_allow_silence(result) or not _successful_qq_send_only(result):
        return
    result["deliveryPresentation"] = "silent"
    for key in ("_cognitiveOutcome", "outcome"):
        outcome = result.get(key)
        if not isinstance(outcome, dict):
            continue
        claims = outcome.get("claims")
        if not isinstance(claims, list) or not claims:
            continue
        completion_claims: list[dict[str, Any]] = []
        supporting_claims: list[dict[str, Any]] = []
        invalid_completion = False
        for claim in claims:
            if not isinstance(claim, Mapping):
                invalid_completion = True
                break
            if str(claim.get("kind") or "").strip().lower() == "tool_completion":
                if str(claim.get("resultStatus") or "").strip().lower() != "success":
                    invalid_completion = True
                    break
                completion_claims.append(dict(claim))
            else:
                supporting_claims.append(dict(claim))
        if invalid_completion or not completion_claims:
            continue
        if supporting_claims:
            outcome["supportingClaims"] = supporting_claims
            outcome["claims"] = completion_claims


def unwrap_reply(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap only a complete envelope in the entire reply, never JSON examples."""
    result = deepcopy(dict(candidate))
    for depth in range(4):
        reply = result.get("reply")
        if not isinstance(reply, str):
            break
        try:
            inner = json.loads(reply)
        except (ValueError, TypeError):
            break
        if not (isinstance(inner, dict) and isinstance(inner.get("reply"), str)
                and len(ENVELOPE_FIELDS.intersection(inner)) >= 2):
            break
        result["reply"] = inner["reply"]
        for key in (
            "spokenTextJa", "speechUnits", "emotion", "gesture", "speechPace", "speech_pace",
            "speechSpeed", "speech_speed",
        ):
            if key in inner:
                result[key] = inner[key]
        result["protocolUnwrappedLayers"] = depth + 1
    return result


def validate_for_delivery(candidate: Any, *, settings: Mapping | None = None,
                          guard: PrivateContentGuard | None = None,
                          annotations: Sequence[tuple[str, str]] = (),
                          persona_snapshot: Any | None = None,
                          logger: Callable | None = None, request_id: str = "") -> DeliveryDecision:
    # Persona is frozen during generation; delivery only checks protocol and
    # actual private content, never character wording.
    findings = []
    if isinstance(candidate, HostSilentDelivery) and not candidate.get('reply'):
        result = HostSilentDelivery(candidate)
        result['deliveryValidation'] = {'allowed': True, 'authority': 'host_silence',
                                        'disposition': 'no_message', 'findings': [], 'extraModelCalls': 0}
        return DeliveryDecision(True, result)
    if not isinstance(candidate, Mapping):
        reason = "protocol_invalid"
        result = {}
    else:
        result = unwrap_reply(candidate)
        reply = result.get("reply")
        reason = "protocol_invalid" if result.get("structuredOutputInvalidFromModel") else (
            "empty_answer" if not isinstance(reply, str) or not reply.strip() or result.get("emptyReply") else ""
        )
        if not reason:
            _project_successful_qq_send_silence(result)
    if reason:
        # regex-consumer: protocol.envelope
        findings.append(record("protocol.envelope", Action.BLOCK_DELIVERY, reason=reason,
                               decisive=True, logger=logger, request_id=request_id))
        return DeliveryDecision(False, result, reason, findings)
    active_guard = guard or DEFAULT_GUARD
    try:
        active_guard.configure(settings)
        leak = active_guard.reason(result["reply"])
    except Exception:
        leak = "leak_check_unavailable"
    if leak:
        # regex-consumer: outbound.private_content
        findings.append(record("outbound.private_content", Action.BLOCK_DELIVERY, reason=leak,
                               decisive=True, logger=logger, request_id=request_id))
        return DeliveryDecision(False, result, "leak_blocked", findings)
    # Speech is a projection: an unavailable/leaking translation loses audio,
    # not an otherwise usable canonical answer.
    spoken = result.get("spokenTextJa")
    try:
        speech_leak = active_guard.reason(spoken) if isinstance(spoken, str) else ""
    except Exception:
        speech_leak = "leak_check_unavailable"
    if speech_leak:
        result["spokenTextJa"] = ""
        result["speechUnavailableReason"] = "speech_private_content"
        result.pop("speechUnits", None)
        result.pop("_modelSpeechUnits", None)
        findings.append(record("presentation.speech", Action.DEGRADE_FIELD, reason=speech_leak,
                               logger=logger, request_id=request_id))
    if not isinstance(result.get("emotion"), str) or not result["emotion"]:
        result["emotion"] = "neutral"
    if not isinstance(result.get("gesture"), str) or not result["gesture"]:
        result["gesture"] = "none"
    findings.extend(soft_findings(annotations, logger=logger, request_id=request_id))
    result["deliveryValidation"] = {"allowed": True, "authority": "host_delivery_boundary",
                                    "findings": findings, "extraModelCalls": 0}
    return DeliveryDecision(True, result, findings=findings)


def require_delivery(candidate: Any, **kwargs) -> dict[str, Any]:
    decision = validate_for_delivery(candidate, **kwargs)
    if not decision.allowed:
        raise DeliveryError(decision.failure, decision.findings)
    return decision.candidate


def first_deliverable(candidates: Sequence[Any], *, repair: Callable | None = None,
                      budget: RepairBudget | None = None, **kwargs) -> dict[str, Any]:
    """Original candidates win; at most one repair, and never replay tools."""
    first_failure = None
    for candidate in candidates:
        decision = validate_for_delivery(candidate, **kwargs)
        if decision.allowed:
            return decision.candidate
        first_failure = first_failure or decision
    budget = budget or RepairBudget()
    if repair is not None and first_failure and first_failure.failure != "leak_blocked" and budget.claim():
        repaired = repair(deepcopy(first_failure.candidate))
        return require_delivery(repaired, **kwargs)
    raise DeliveryError(first_failure.failure if first_failure else "empty_answer",
                        first_failure.findings if first_failure else ())
