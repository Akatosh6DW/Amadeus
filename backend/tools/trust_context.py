"""Build tool authority from host state, outside model output and HTTP fields."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path


_OWNER_INPUT = ContextVar("amadeus_authenticated_owner_input", default=None)
_QQ_ACCOUNT = ContextVar("amadeus_bound_qq_account", default="")


@contextmanager
def qq_account_scope(account_id: str):
    """Keep a saved task on its original account across model/tool rounds."""
    token = _QQ_ACCOUNT.set(str(account_id or ""))
    try:
        yield
    finally:
        _QQ_ACCOUNT.reset(token)


def bound_qq_account() -> str:
    return _QQ_ACCOUNT.get()


@contextmanager
def owner_input_scope(text: str, *, has_external_context: bool = True,
                      delivery_target: Mapping | None = None):
    """Only the authenticated QQ owner endpoint enters this scope."""
    token = _OWNER_INPUT.set((str(text), bool(has_external_context), dict(delivery_target or {})))
    try:
        yield
    finally:
        _OWNER_INPUT.reset(token)


def authenticated_owner_delivery_target() -> dict:
    """Default reminder destination from the verified transport, never prose."""
    owner = _OWNER_INPUT.get()
    return dict(owner[2]) if owner is not None else {}


def configured_owner_delivery_target(data_root: Path) -> dict:
    """Resolve the explicit owner role once, before persisting a scheduled task."""
    verified = authenticated_owner_delivery_target()
    if verified:
        return verified
    try:
        config = json.loads((data_root / 'qq-agent/gateway.json').read_text(encoding='utf-8-sig'))
        policy = config.get('policy') or {}
        owners = list(dict.fromkeys(str(item) for item in policy.get('owner_ids', [])))
        if policy.get('account_id') and len(owners) == 1:
            return {'channel': 'qq', 'accountId': str(policy['account_id']),
                    'targetType': 'private', 'targetId': owners[0]}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _host_request_fingerprint(
    item: Mapping[str, object],
    receipt: Mapping[str, object],
) -> str:
    """Read the request identity from a host-owned tool result envelope.

    ``execute_prepared_agent_tool`` keeps trust provenance in the private
    ``_toolResultEnvelope``.  The public result still carries the delivery
    state, so unknown-send detection must join the two without trusting
    arbitrary model/result payload fields as host provenance.
    """

    for candidate in (receipt, item):
        envelope = candidate.get("_toolResultEnvelope")
        if not isinstance(envelope, Mapping):
            continue
        provenance = envelope.get("provenance")
        trust = provenance.get("trust") if isinstance(provenance, Mapping) else None
        if not isinstance(trust, Mapping):
            continue
        fingerprint = trust.get("requestFingerprint") or trust.get("fingerprint")
        if fingerprint:
            return str(fingerprint).strip()[:240]
    return ""


def execution_trust_context(user_text: str, body: Mapping | None, *, data_root: Path,
                            workspace_root: Path | None = None) -> dict:
    from backend.tools.trust import build_trust_context

    source = body if isinstance(body, Mapping) else {}
    authenticated_owner = _OWNER_INPUT.get()
    original_text = authenticated_owner[0] if authenticated_owner is not None else str(user_text)
    sources = []
    if authenticated_owner is not None and authenticated_owner[1]:
        sources.append({"sourceType": "qq_history"})
    if source.get("attachments"):
        sources.append({"sourceType": "attachment"})
    # Evidence containers and receipts are populated by the host tool loop.
    evidence = source.get("_cognitiveEvidence") or {}
    results = [*source.get("_toolResults", []), *source.get("_rescueToolResults", [])]
    if isinstance(evidence, Mapping):
        for evidence_id, item in evidence.items():
            if isinstance(item, Mapping):
                receipt = item.get('receipt') if isinstance(item.get('receipt'), Mapping) else item
                results.append({**receipt, '_hostEvidenceId': str(evidence_id),
                                '_hostSourceType': str(item.get('sourceType') or ''),
                                '_hostHistorical': bool(item.get('historical'))})
    unknown = []
    qzone_account = ""
    external_tools = {"web_search", "read_web_page", "read_text_file", "read_image",
                      "capture_screen", "capture_camera", "qq_history", "qq_space"}
    for item in results:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or item.get("tool") or "")
        if (name == "qq_space" and item.get("ok") is True and item.get("account")
                and not item.get("_hostHistorical") and not item.get("historical")):
            qzone_account = str(item["account"])
        if name in external_tools or (item.get('_hostSourceType') or item.get("sourceType")) in {"web", "attachment", "tool", "qq_history"}:
            source_id = str(item.get('toolCallId') or item.get('sourceUid') or item.get('_hostEvidenceId') or '')
            marker = {"sourceType": "tool_result", "sourceId": source_id}
            if marker not in sources:
                sources.append(marker)
        receipt = item.get("actionReceipt") if isinstance(item.get("actionReceipt"), Mapping) else item
        if receipt.get("status") == "unknown" or receipt.get("deliveryState") == "unknown":
            unknown_receipt = {**receipt, "toolName": name}
            if not unknown_receipt.get("requestFingerprint"):
                fingerprint = _host_request_fingerprint(item, receipt)
                if fingerprint:
                    unknown_receipt["requestFingerprint"] = fingerprint
            unknown.append(unknown_receipt)
    policy = {}
    try:
        config = json.loads((Path(data_root) / "qq-agent" / "gateway.json").read_text(encoding="utf-8"))
        raw_policy = config.get("policy")
        if isinstance(raw_policy, Mapping):
            # Never pass the control token into prompts, diagnostics, or the target matcher.
            policy = {key: raw_policy[key] for key in (
                "account_id", "private_ids", "group_ids", "owner_ids", "qzone_enabled",
                "qzone_allow_write", "qzone_allow_publish", "qzone_autonomous",
            ) if key in raw_policy}
    except (OSError, ValueError, TypeError):
        pass
    targets = {"workspace": [str(workspace_root)]} if workspace_root is not None else {}
    context = build_trust_context(user_text=original_text, untrusted_input=bool(sources),
                                  sources=sources, authorized_targets=targets,
                                  gateway_config={"policy": policy}, unknown_deliveries=unknown)
    # The host carries account identity across rounds. The model chooses the
    # operation, not whether to copy an already observed account a second time.
    context["qzoneAccount"] = bound_qq_account() or qzone_account or str(policy.get("account_id") or "")
    return context
