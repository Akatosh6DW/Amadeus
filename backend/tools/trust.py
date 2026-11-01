"""Evidence taint and side-effect target authorization.

The web, attachment, and other-party history readers return user-visible
evidence.  Evidence may explain a task, but it is not an owner instruction.
This module supplies the small boundary needed by the execution planner:
when a turn contains untrusted evidence, a target for a side effect must be
present in the owner's original request or in a host-owned authorization set.

The helpers are deliberately independent from ``server`` and from QQ's
gateway.  The caller supplies the original owner message and a snapshot of
the real gateway policy; model-selected fields such as ``selection`` or
``toolAuthorization`` are never read as authorization here.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import PureWindowsPath
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


TRUST_CONTEXT_SCHEMA = "amadeus.tool-trust"
TRUST_CONTEXT_SCHEMA_VERSION = 1

UNTRUSTED_SOURCE_TYPES = frozenset({
    "web",
    "web_page",
    "web_search",
    "read_web_page",
    "attachment",
    "file_attachment",
    "qq_history",
    "qq_message_history",
    "external_message",
    "external",
})

_UNKNOWN_STATES = frozenset({"unknown", "sending", "side_effect_unknown"})
_TARGETED_QQ_ACTIONS = frozenset({"send", "delegate", "approve", "configure"})
_TARGETED_QZONE_ACTIONS = frozenset({"comment", "like"})
_CONFIG_TARGET_LISTS = (
    ("private_ids", "private"),
    ("group_ids", "group"),
    ("owner_ids", "private"),
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_url(value: Any) -> str:
    """Canonicalize an exact URL target without resolving or fetching it."""

    raw = _text(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.rstrip("/").casefold()
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/").casefold()
    hostname = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = hostname
    if parsed.username or parsed.password:
        # A userinfo-bearing URL is still compared exactly; no credentials are
        # persisted in trust records beyond the value the caller supplied.
        netloc = parsed.netloc.casefold()
    elif port and not ((parsed.scheme.casefold() == "https" and port == 443)
                       or (parsed.scheme.casefold() == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, parsed.fragment))


def _canonical_path(value: Any) -> str:
    raw = _text(value).replace("\\", "/")
    if not raw:
        return ""
    parts: list[str] = []
    # This regex recognizes a Windows drive prefix; it never classifies or
    # rejects user-provided content.
    # regex-purpose: resource; regex-boundary: start-of-drive-prefix only.
    absolute = raw.startswith("/") or bool(re.match(r"^[A-Za-z]:/", raw))
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append(part)
            continue
        parts.append(part)
    prefix = "/" if absolute else ""
    # regex-purpose: resource; regex-boundary: start-of-drive-prefix only.
    # Keep the same start-of-path boundary when restoring a drive path.
    if re.match(r"^[A-Za-z]:/", raw):
        prefix = ""
    result = prefix + "/".join(parts)
    return result.casefold() or ("/" if absolute else "")


def _canonical_target_type(value: Any) -> str:
    value = _text(value).casefold()
    return {
        "friend": "private",
        "private_chat": "private",
        "group_chat": "group",
    }.get(value, value)


def _target_item(value: Any, *, kind: str = "") -> dict[str, str] | None:
    """Normalize one owner/host target while dropping unknown model fields."""

    if isinstance(value, str):
        if kind == "url":
            url = _canonical_url(value)
            return {"kind": "url", "value": url} if url else None
        if kind == "path":
            path = _canonical_path(value)
            return {"kind": "path", "value": path} if path else None
        if kind in {"qq", "qq_private", "qq_group"}:
            target_id = _text(value)
            target_type = "group" if kind == "qq_group" else "private"
            return (
                {"kind": "qq", "targetType": target_type, "targetId": target_id}
                if target_id else None
            )
        return None
    item = _mapping(value)
    if not item:
        return None
    hint = _text(kind).casefold()
    if hint in {"url", "urls"} or item.get("url") is not None:
        url = _canonical_url(item.get("url") or item.get("value"))
        return {"kind": "url", "value": url} if url else None
    if hint in {"path", "paths"} or item.get("path") is not None:
        path = _canonical_path(item.get("path") or item.get("value"))
        return {"kind": "path", "value": path} if path else None
    target_type = _canonical_target_type(item.get("targetType") or item.get("type"))
    target_id = _text(item.get("targetId") or item.get("id") or item.get("target"))
    if target_type not in {"private", "group"} or not target_id:
        return None
    result = {"kind": "qq", "targetType": target_type, "targetId": target_id}
    account = _text(item.get("account") or item.get("accountId"))
    if account:
        result["account"] = account
    return result


def _target_list(value: Any, *, kind: str = "") -> list[dict[str, str]]:
    if isinstance(value, (str, Mapping)):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[dict[str, str]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for item in value:
        target = _target_item(item, kind=kind)
        if not target:
            continue
        key = tuple(sorted(target.items()))
        if key not in seen:
            seen.add(key)
            result.append(target)
    return result


def _targets_from_container(value: Any) -> list[dict[str, str]]:
    """Accept the exact schema plus harmless gateway-policy aliases."""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, Mapping)):
        return _target_list(value)
    container = _mapping(value)
    result: list[dict[str, str]] = []
    for key in ("qq", "qqTargets", "recipients", "targets"):
        result.extend(_target_list(container.get(key), kind="qq"))
    result.extend(_target_list(container.get("qq_private"), kind="qq_private"))
    result.extend(_target_list(container.get("qq_group"), kind="qq_group"))
    for key in ("urls", "urlTargets"):
        result.extend(_target_list(container.get(key), kind="url"))
    for key in ("paths", "pathTargets"):
        result.extend(_target_list(container.get(key), kind="path"))
    # A flat target object is useful for a single caller-supplied target.
    if not result:
        target = _target_item(container)
        if target:
            result.append(target)
    unique: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    for item in result:
        unique[tuple(sorted(item.items()))] = item
    return list(unique.values())


def _group_targets(items: Sequence[Mapping[str, str]]) -> dict[str, list[dict[str, str]]]:
    """Serialize normalized target items using the public schema keys."""

    grouped: dict[str, list[dict[str, str]]] = {
        "qq_private": [],
        "qq_group": [],
        "urls": [],
        "paths": [],
        "workspace": [],
    }
    seen: dict[str, set[tuple[tuple[str, str], ...]]] = {key: set() for key in grouped}
    for item in items:
        kind = _text(item.get("kind"))
        if kind == "qq":
            key = "qq_group" if _canonical_target_type(item.get("targetType")) == "group" else "qq_private"
        elif kind == "url":
            key = "urls"
        elif kind == "path":
            key = "paths"
        else:
            continue
        key_value = tuple(sorted((str(k), str(v)) for k, v in item.items()))
        if key_value not in seen[key]:
            seen[key].add(key_value)
            grouped[key].append(dict(item))
    return grouped


def _workspace_roots(value: Any) -> list[str]:
    container = _mapping(value)
    roots = container.get("workspace") or container.get("workspaceRoots") or []
    if isinstance(roots, (str, bytes, bytearray)):
        roots = [roots]
    if not isinstance(roots, Sequence):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for root in roots:
        normalized = _canonical_path(root)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def authorized_targets_from_gateway_config(config: Mapping[str, Any] | None) -> dict[str, list[dict[str, str]]]:
    """Derive host authorization targets from a real QQ gateway policy.

    This function accepts a configuration snapshot supplied by the host.  It
    does not read files, tokens, account profiles, or live gateway state.  A
    QQ allowlist entry is an authorization target, while ``muted_*_ids`` are
    intentionally omitted because they remain a separate executor constraint.
    """

    root = _mapping(config)
    policy = _mapping(root.get("policy")) or root
    account = _text(policy.get("account_id") or policy.get("accountId"))
    targets: list[dict[str, str]] = []
    for target_type, key in (("private", "private_ids"), ("group", "group_ids")):
        values = policy.get(key, ())
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
            values = ()
        for value in values:
            target_id = _text(value)
            if target_id:
                item = {"kind": "qq", "targetType": target_type, "targetId": target_id}
                if account:
                    item["account"] = account
                targets.append(item)
    # Owner IDs are real host-authorized private targets even when the QQ
    # reply allowlist stores them separately from ordinary private_ids.
    owner_ids = policy.get("owner_ids", ())
    if isinstance(owner_ids, Sequence) and not isinstance(owner_ids, (str, bytes, bytearray)):
        for value in owner_ids:
            target_id = _text(value)
            if target_id:
                item = {"kind": "qq", "targetType": "private", "targetId": target_id}
                if account:
                    item["account"] = account
                targets.append(item)
    return {"qq_private": [item for item in targets if item.get("targetType") == "private"],
            "qq_group": [item for item in targets if item.get("targetType") == "group"],
            "urls": [], "paths": [], "workspace": []}


def _normalize_host_policy(value: Any) -> dict[str, bool]:
    """Keep only host-owned QZone booleans used by the taint gate.

    ``qzone_autonomous`` controls the periodic worker.  It is deliberately
    kept as a diagnostic policy field, but it is not required for a direct
    owner-requested publish; the gateway's enabled/write/publish switches are
    the execution boundary for that action.
    """

    policy = _mapping(value)

    def flag(*keys: str) -> bool:
        # Host policy fields are JSON booleans; strings such as ``"false"``
        # must never become truthy authorization.
        return any(policy.get(key) is True for key in keys)

    return {
        "qzoneEnabled": flag("qzone_enabled", "qzoneEnabled"),
        "qzoneAutonomous": flag("qzone_autonomous", "qzoneAutonomous"),
        "qzoneAllowWrite": flag("qzone_allow_write", "qzoneAllowWrite"),
        "qzoneAllowPublish": flag("qzone_allow_publish", "qzoneAllowPublish"),
    }


def normalize_trust_context(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the exact, non-secret schema consumed by the planner.

    ``ownerRequest`` must be the original user message captured by the host.
    The function intentionally ignores model output fields, including
    ``selection`` and ``toolAuthorization``.
    """

    raw = _mapping(value)
    sources: list[dict[str, str]] = []
    raw_sources = raw.get("sources") or raw.get("untrustedSources") or []
    if isinstance(raw_sources, Mapping):
        raw_sources = [raw_sources]
    if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, (str, bytes, bytearray)):
        for source in raw_sources:
            item = _mapping(source)
            source_type = _text(item.get("sourceType") or item.get("type")).casefold()
            if not source_type:
                continue
            source_id = _text(item.get("sourceId") or item.get("id"))
            marker = {"sourceType": source_type, "untrusted": True}
            if source_id:
                marker["sourceId"] = source_id[:200]
            sources.append(marker)
    tainted = bool(raw.get("tainted") or raw.get("untrustedInput")) or any(
        item["sourceType"] in UNTRUSTED_SOURCE_TYPES for item in sources
    )
    owner_target_container = raw.get("ownerTargets") or raw.get("ownerTargetRefs")
    owner_target_items = _targets_from_container(owner_target_container)
    authorized_value = (
        raw.get("authorizedTargets")
        or raw.get("hostAuthorizedTargets")
        or raw.get("gatewayAuthorizedTargets")
    )
    authorized_target_items = _targets_from_container(authorized_value)
    workspace_roots = _workspace_roots(authorized_value)
    if not workspace_roots:
        workspace_roots = _workspace_roots({"workspace": raw.get("workspaceRoots")})
    gateway_config = raw.get("gatewayConfig") or raw.get("gatewayPolicy")
    host_policy = _normalize_host_policy(raw.get("hostPolicy"))
    if gateway_config:
        host_policy = _normalize_host_policy(_mapping(gateway_config).get("policy") or gateway_config)
        authorized_target_items.extend(_targets_from_container(
            authorized_targets_from_gateway_config(_mapping(gateway_config))
        ))
    # Permit the parent to pass a policy snapshot directly, while keeping
    # selection/toolAuthorization out of the trust decision.  Derive its
    # host policy even when explicit targets were supplied alongside it.
    if not gateway_config and raw.get("policy"):
        host_policy = _normalize_host_policy(raw.get("policy"))
        authorized_target_items.extend(_targets_from_container(
            authorized_targets_from_gateway_config(_mapping(raw.get("policy")))
        ))
    deduped: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    for item in authorized_target_items:
        deduped[tuple(sorted(item.items()))] = item
    authorized_target_items = list(deduped.values())
    unknown_deliveries: list[dict[str, str]] = []
    raw_unknown = raw.get("unknownDeliveries") or raw.get("unknownDeliveryReceipts") or []
    if isinstance(raw_unknown, Mapping):
        raw_unknown = [raw_unknown]
    if isinstance(raw_unknown, Sequence) and not isinstance(raw_unknown, (str, bytes, bytearray)):
        for receipt in raw_unknown:
            item = _mapping(receipt)
            state = _text(item.get("deliveryState") or item.get("state") or item.get("status")).casefold()
            if state not in _UNKNOWN_STATES:
                continue
            safe: dict[str, str] = {"state": state}
            for output, keys in {
                "toolName": ("toolName", "name", "tool"),
                "action": ("action",),
                "targetType": ("targetType",),
                "targetId": ("targetId",),
                "account": ("account", "accountId"),
                "requestFingerprint": ("requestFingerprint", "fingerprint"),
            }.items():
                for key in keys:
                    current = _text(item.get(key))
                    if current:
                        safe[output] = current[:240]
                        break
            unknown_deliveries.append(safe)
    return {
        "schema": TRUST_CONTEXT_SCHEMA,
        "schemaVersion": TRUST_CONTEXT_SCHEMA_VERSION,
        "tainted": tainted,
        "untrustedInput": tainted,
        "sources": sources,
        "ownerRequest": _text(raw.get("ownerRequest") or raw.get("userRequest") or raw.get("userText"))[:20_000],
        "userText": _text(raw.get("userText") or raw.get("ownerRequest") or raw.get("userRequest"))[:20_000],
        "ownerTargets": _group_targets(owner_target_items),
        "authorizedTargets": _group_targets(authorized_target_items),
        "hostPolicy": host_policy,
        "qzoneAccount": _text(raw.get("qzoneAccount")),
        "workspaceRoots": workspace_roots,
        "unknownDeliveries": unknown_deliveries,
    }


def build_trust_context(
    *,
    user_text: str = "",
    untrusted_input: bool = False,
    sources: Sequence[Mapping[str, Any]] | None = None,
    owner_targets: Mapping[str, Any] | Sequence[Any] | None = None,
    authorized_targets: Mapping[str, Any] | Sequence[Any] | None = None,
    gateway_config: Mapping[str, Any] | None = None,
    host_policy: Mapping[str, Any] | None = None,
    unknown_deliveries: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct the planner's canonical trust context from host-owned data.

    The execution host should call this with the original owner message and
    the real gateway policy snapshot.  Model output is intentionally absent
    from the signature, so ``selection`` or ``toolAuthorization`` cannot
    become an authorization source by accident.  The returned mapping is
    already normalized and may be passed directly as ``trust_context`` to
    :func:`backend.tools.validation.build_execution_plan`.
    """

    raw: dict[str, Any] = {
        "userText": _text(user_text),
        "untrustedInput": bool(untrusted_input),
        "sources": list(sources or ()),
        "ownerTargets": owner_targets or {},
        "authorizedTargets": authorized_targets or {},
        "hostPolicy": host_policy or {},
        "unknownDeliveries": unknown_deliveries or [],
    }
    if gateway_config:
        raw["gatewayConfig"] = dict(gateway_config)
    return normalize_trust_context(raw)


def _target_from_fields(kind: str, args: Mapping[str, Any]) -> dict[str, str] | None:
    if kind == "qq":
        return _target_item({
            "targetType": args.get("targetType"),
            "targetId": args.get("targetId") or args.get("targetQQ"),
            "account": args.get("account") or args.get("expectedAccount"),
        })
    if kind == "url":
        return _target_item({"url": args.get("url") or args.get("targetUrl")}, kind="url")
    if kind == "path":
        return _target_item({"path": args.get("path") or args.get("directory")}, kind="path")
    return None


def classify_side_effect_target(tool_name: str, args: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Identify only concrete recipient/URL/path targets for side effects."""

    name = _text(tool_name)
    data = _mapping(args)
    action = _text(data.get("action")).casefold()
    if name == "qq_manage" and action in _TARGETED_QQ_ACTIONS:
        return _target_from_fields("qq", data)
    if name == "qq_space_write" and action in _TARGETED_QZONE_ACTIONS:
        return _target_from_fields("qq", data)
    if name in {"write_text_file", "create_directory"}:
        return _target_from_fields("path", data)
    # Opening a public page is navigation, not publishing or transferring a
    # message. The browser adapter owns URL validity and the existing browser
    # permission. Requiring the owner's exact URL spelling here confused site
    # names with missing authority whenever any prior tool evidence was present.
    # Saving a local bookmark also does not transmit data to the saved URL.
    # Its own permission and versioned mutation contract are sufficient.
    return None


def request_fingerprint(tool_name: str, args: Mapping[str, Any] | None) -> str:
    payload = {"name": _text(tool_name), "args": dict(_mapping(args))}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8", errors="replace")).hexdigest()


def _target_matches(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left.get("kind") != right.get("kind"):
        return False
    if left.get("kind") == "qq":
        if _canonical_target_type(left.get("targetType")) != _canonical_target_type(right.get("targetType")):
            return False
        if _text(left.get("targetId")) != _text(right.get("targetId")):
            return False
        left_account = _text(left.get("account"))
        right_account = _text(right.get("account"))
        return not left_account or not right_account or left_account == right_account
    if left.get("kind") == "url":
        return _canonical_url(left.get("value") or left.get("url")) == _canonical_url(right.get("value") or right.get("url"))
    if left.get("kind") == "path":
        return _canonical_path(left.get("value") or left.get("path")) == _canonical_path(right.get("value") or right.get("path"))
    return False


def _owner_text_mentions_target(owner_request: str, target: Mapping[str, str]) -> bool:
    text = _text(owner_request)
    if not text:
        return False
    kind = target.get("kind")
    value = (
        target.get("value")
        if kind in {"url", "path"}
        else target.get("targetId")
    )
    value = _text(value)
    if not value:
        return False
    if kind == "url":
        # Compare complete URL tokens.  A substring check would let an owner
        # mention of ``https://example.com.evil`` authorize
        # ``https://example.com`` (or a longer path on that host).
        target_url = _canonical_url(value)
        # regex-purpose: resource; regex-boundary: complete URL token only.
        for match in re.finditer(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"'，。！？；、）》】]+", text):
            candidate = match.group(0).rstrip(".,;:!?)]}》】")
            if _canonical_url(candidate) == target_url:
                return True
        return _canonical_url(text) == target_url
    if kind == "path":
        normalized = text.replace("\\", "/").casefold()
        # Keep a path token boundary as well: mentioning ``notes.txt.evil``
        # must not authorize a write to ``notes.txt``.  A terminal sentence
        # period remains valid, while a dot followed by path characters does
        # not.
        # regex-purpose: resource; regex-boundary: exact path token only.
        pattern = (
            rf"(?<![A-Za-z0-9._-]){re.escape(value.casefold())}"
            r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"
        )
        # regex-purpose: resource; regex-boundary: exact path token only.
        return bool(re.search(pattern, normalized))
    # QQ identifiers are matched as a token so a short ID cannot accidentally
    # authorize a larger number in the owner's sentence.
    # regex-purpose: resource; regex-boundary: exact QQ identifier token only.
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", text))


def _target_in_context(target: Mapping[str, str], context: Mapping[str, Any]) -> tuple[bool, str]:
    owner_targets = _targets_from_container(context.get("ownerTargets"))
    if any(_target_matches(target, item) for item in owner_targets if isinstance(item, Mapping)):
        return True, "owner_target"
    if _owner_text_mentions_target(_text(context.get("ownerRequest") or context.get("userText")), target):
        return True, "owner_request"
    authorized = _targets_from_container(context.get("authorizedTargets"))
    if any(_target_matches(target, item) for item in authorized if isinstance(item, Mapping)):
        return True, "host_authorized_target"
    if target.get("kind") == "path":
        target_path = _canonical_path(target.get("value"))
        for root in context.get("workspaceRoots") or []:
            normalized_root = _canonical_path(root)
            resolved_target = target_path
            # The file executor resolves ordinary relative paths against its
            # workspace. Mirror that binding; traversal is checked after join.
            if not target_path.startswith('/') and not PureWindowsPath(target_path).drive:
                resolved_target = _canonical_path(normalized_root.rstrip('/') + '/' + target_path)
            if normalized_root and (
                resolved_target == normalized_root
                or resolved_target.startswith(normalized_root.rstrip("/") + "/")
            ):
                return True, "host_workspace_root"
    return False, ""


def _configure_policy_targets(args: Mapping[str, Any]) -> list[dict[str, str]]:
    """Extract only recipient IDs introduced by a global QQ policy patch."""

    policy = _mapping(args.get("policy"))
    result: list[dict[str, str]] = []
    for field, target_type in _CONFIG_TARGET_LISTS:
        values = policy.get(field)
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
            continue
        for value in values:
            target_id = _text(
                value.get("targetId") if isinstance(value, Mapping) else value
            )
            if target_id:
                result.append({
                    "kind": "qq",
                    "targetType": target_type,
                    "targetId": target_id,
                })
    unique: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
    for target in result:
        unique[tuple(sorted(target.items()))] = target
    return list(unique.values())


def _configure_trust_decision(
    args: Mapping[str, Any],
    context: Mapping[str, Any],
) -> TrustDecision | None:
    """Authorize an owner-selected QQ policy patch without ID text matching.

    The QQ control permission and mutation authorization are established by
    the execution validator.  Once that host boundary is present, the
    authenticated owner request authorizes the configure operation itself;
    requiring every selected ID to occur verbatim in ``ownerRequest`` would be
    a second, wording-based permission gate.  The gateway still applies the
    account, revision, policy and allowlist checks at execution time.

    A context without an owner request remains conservative for direct trust
    callers and requires a configured/host-authorized target.
    """

    if _text(args.get("action")).casefold() != "configure":
        return None
    configured_target = _target_from_fields("qq", args)
    owner_request = _text(context.get("ownerRequest") or context.get("userText"))
    if owner_request:
        return TrustDecision(
            "allow",
            "owner_configure_authorized",
            configured_target,
            "owner_request",
        )
    matched_source = ""
    for target in _configure_policy_targets(args):
        matched, matched_by = _target_in_context(target, context)
        if not matched:
            return TrustDecision(
                "confirm",
                "untrusted_config_target_requires_owner_confirmation",
                target,
            )
        # Keep the first matched source for diagnostics when all IDs are safe.
        if not matched_source:
            matched_source = matched_by
    if configured_target:
        matched, matched_by = _target_in_context(configured_target, context)
        if not matched:
            return TrustDecision(
                "confirm",
                "untrusted_config_target_requires_owner_confirmation",
                configured_target,
            )
        return TrustDecision(
            "allow",
            "configuration_target_authorized",
            configured_target,
            matched_by,
        )
    if _configure_policy_targets(args):
        return TrustDecision(
            "allow",
            "configuration_targets_authorized",
            None,
            matched_source,
        )
    return None


def _qzone_publish_is_host_authorized(context: Mapping[str, Any]) -> bool:
    """Require the gateway's direct publish switches for a publish call."""

    policy = _mapping(context.get("hostPolicy"))
    return all(policy.get(key) is True for key in (
        "qzoneEnabled", "qzoneAllowWrite", "qzoneAllowPublish",
    ))


def _unknown_delivery_matches(tool_name: str, args: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    target = classify_side_effect_target(tool_name, args)
    fingerprint = request_fingerprint(tool_name, args)
    action = _text(args.get("action")).casefold()
    for receipt in context.get("unknownDeliveries") or []:
        if not isinstance(receipt, Mapping):
            continue
        receipt_name = _text(receipt.get("toolName") or receipt.get("name") or receipt.get("tool")).casefold()
        if receipt_name and receipt_name != _text(tool_name).casefold():
            continue
        receipt_action = _text(receipt.get("action")).casefold()
        if receipt_action and receipt_action != action:
            continue
        recorded_fingerprint = _text(receipt.get("requestFingerprint") or receipt.get("fingerprint"))
        if recorded_fingerprint and recorded_fingerprint == fingerprint:
            return True
        # A host receipt with a fingerprint belongs to one concrete request.
        # A different fingerprint is new content even when the recipient is
        # the same; falling through to the target check would turn an
        # uncertain send into a blanket recipient lock.  Fingerprint-less
        # legacy receipts remain conservatively target-scoped.
        if target and not recorded_fingerprint:
            recorded_target = _target_item({
                "targetType": receipt.get("targetType"),
                "targetId": receipt.get("targetId"),
                "account": receipt.get("account"),
            })
            if recorded_target and _target_matches(target, recorded_target):
                return True
        elif not recorded_fingerprint and not receipt.get("targetId"):
            return True
    return False


@dataclass(frozen=True)
class TrustDecision:
    """Planner-facing result; ``action`` is ``allow``, ``confirm`` or ``block``."""

    action: str
    reason: str
    target: dict[str, str] | None = None
    matched_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "target": dict(self.target) if self.target else None,
            "matchedBy": self.matched_by,
        }


def evaluate_tool_trust(
    tool_name: str,
    args: Mapping[str, Any] | None,
    trust_context: Mapping[str, Any] | None,
) -> TrustDecision:
    """Gate tainted evidence targets while preserving ordinary operations."""

    context = normalize_trust_context(trust_context)
    data = _mapping(args)
    if _unknown_delivery_matches(tool_name, data, context):
        return TrustDecision(
            "block",
            "unknown_delivery_not_retryable",
            classify_side_effect_target(tool_name, data),
        )
    name = _text(tool_name)
    action = _text(data.get("action")).casefold()
    if context.get("tainted") and name == "qq_space_write" and action == "publish":
        if _qzone_publish_is_host_authorized(context):
            return TrustDecision(
                "allow",
                "host_qzone_publish_authorized",
                {"kind": "qzone_publish", "value": "publish"},
                "host_policy",
            )
        return TrustDecision(
            "block",
            "untrusted_qzone_publish_requires_host_authorization",
            {"kind": "qzone_publish", "value": "publish"},
        )
    if context.get("tainted") and name == "qq_manage" and action == "configure":
        configured = _configure_trust_decision(data, context)
        if configured is not None:
            return configured
    target = classify_side_effect_target(tool_name, data)
    if not context.get("tainted") or target is None:
        return TrustDecision("allow", "no_tainted_target", target)
    matched, matched_by = _target_in_context(target, context)
    if matched:
        return TrustDecision("allow", "target_authorized", target, matched_by)
    return TrustDecision("confirm", "untrusted_target_requires_owner_confirmation", target)


def unknown_delivery_is_not_retryable(
    tool_name: str,
    args: Mapping[str, Any] | None,
    trust_context: Mapping[str, Any] | None,
) -> bool:
    """Return true when a prior unknown send receipt forbids another attempt."""

    context = normalize_trust_context(trust_context)
    return _unknown_delivery_matches(tool_name, _mapping(args), context)


__all__ = [
    "TRUST_CONTEXT_SCHEMA",
    "TRUST_CONTEXT_SCHEMA_VERSION",
    "TrustDecision",
    "authorized_targets_from_gateway_config",
    "build_trust_context",
    "classify_side_effect_target",
    "evaluate_tool_trust",
    "normalize_trust_context",
    "request_fingerprint",
    "unknown_delivery_is_not_retryable",
]
