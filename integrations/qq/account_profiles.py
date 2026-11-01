"""Pure helpers for keeping QQ account settings separate.

The gateway configuration contains credentials and transport settings alongside
the selected account's policy.  ``account_profiles`` deliberately stores only
the account-scoped policy and the two routing preferences which belong with it;
credentials never enter this map.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .policy import normalize_policy


_RESUME_MODES = frozenset({"auto", "review"})
_REPLY_AGENTS = frozenset({"astrbot", "amadeus"})


def validate_account_id(value: Any) -> str:
    """Return a canonical QQ account id or raise for an unsafe identity."""
    if isinstance(value, bool):
        raise ValueError("QQ 账号必须为非空 ASCII 数字")
    if isinstance(value, int):
        account_id = str(value)
    elif isinstance(value, str):
        account_id = value.strip()
    else:
        raise ValueError("QQ 账号必须为非空 ASCII 数字")
    if not account_id or not account_id.isascii() or not account_id.isdigit():
        raise ValueError("QQ 账号必须为非空 ASCII 数字")
    return account_id


def valid_account_id(value: Any) -> bool:
    """Return whether *value* is a nonempty ASCII-digit QQ account id."""
    try:
        validate_account_id(value)
    except ValueError:
        return False
    return True


def _try_account_id(value: Any) -> str | None:
    try:
        return validate_account_id(value)
    except ValueError:
        return None


def _resume_mode(value: Any, policy: dict) -> str:
    if value in _RESUME_MODES:
        return value
    mode = policy.get("mode")
    return mode if mode in _RESUME_MODES else "review"


def _reply_agent(value: Any) -> str:
    return value if value in _REPLY_AGENTS else "astrbot"


def _clean_profiles(value: Any) -> dict[str, dict[str, Any]]:
    """Copy only valid, account-scoped profile fields from persisted data."""
    if not isinstance(value, dict):
        return {}
    profiles: dict[str, dict[str, Any]] = {}
    for raw_account, raw_profile in value.items():
        account_id = _try_account_id(raw_account)
        if account_id is None or not isinstance(raw_profile, dict):
            continue
        raw_policy = raw_profile.get("policy")
        if not isinstance(raw_policy, dict):
            continue
        try:
            # The map key is authoritative.  This also strips unknown nested
            # values, including an accidentally embedded credential.
            policy = normalize_policy({**raw_policy, "account_id": account_id})
        except (TypeError, ValueError, OverflowError):
            continue
        profiles[account_id] = {
            "policy": policy,
            "resume_mode": _resume_mode(raw_profile.get("resume_mode"), policy),
            "reply_agent": _reply_agent(raw_profile.get("reply_agent")),
        }
    return profiles


def _current_profile(config: dict[str, Any]) -> tuple[str, dict] | None:
    raw_policy = config.get("policy")
    if not isinstance(raw_policy, dict):
        return None
    account_id = _try_account_id(raw_policy.get("account_id"))
    if account_id is None:
        # An empty account is a valid initial setup state, but it cannot have
        # an account-keyed profile yet.  Other malformed identities are errors.
        raw_account = raw_policy.get("account_id", "")
        if raw_account == "":
            return None
        raise ValueError("QQ 账号必须为非空 ASCII 数字")
    try:
        policy = normalize_policy({**raw_policy, "account_id": account_id})
    except (TypeError, ValueError, OverflowError):
        raise
    return account_id, policy


def remember_account(config: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy *config* and save its selected account's scoped settings.

    Only ``policy``, ``resume_mode`` and ``reply_agent`` are copied into each
    profile.  The input object is never mutated, and the top-level credentials
    remain where the gateway already keeps them.
    """
    if not isinstance(config, dict):
        raise ValueError("QQ 配置必须为对象")
    result = deepcopy(config)
    profiles = _clean_profiles(result.get("account_profiles"))
    current = _current_profile(result)
    if current is not None:
        account_id, policy = current
        result["policy"] = policy
        profiles[account_id] = {
            "policy": deepcopy(policy),
            "resume_mode": _resume_mode(result.get("resume_mode"), policy),
            "reply_agent": _reply_agent(result.get("reply_agent")),
        }
    result["account_profiles"] = profiles
    return result


def select_account(config: dict[str, Any], account_id: Any) -> dict[str, Any]:
    """Select an account while preserving each account's private settings.

    The old account is remembered first.  A selected account always starts
    paused; its own resume preference and other policy fields are restored.  A
    new account receives a canonical empty policy and conservative defaults.
    """
    account_id = validate_account_id(account_id)
    result = remember_account(config)
    profile = result["account_profiles"].get(account_id)
    if profile is None:
        policy = normalize_policy({"account_id": account_id})
        resume_mode = "review"
        reply_agent = "astrbot"
    else:
        policy = normalize_policy({**profile["policy"], "account_id": account_id})
        resume_mode = _resume_mode(profile.get("resume_mode"), policy)
        reply_agent = _reply_agent(profile.get("reply_agent"))
    policy["mode"] = "paused"
    result["policy"] = policy
    result["resume_mode"] = resume_mode
    result["reply_agent"] = reply_agent
    # Materialize the newly selected account as a profile too, so status can
    # list it immediately after a switch before the owner edits its allowlist.
    return remember_account(result)


def account_summaries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return safe, compact account rows for status and setup UIs."""
    remembered = remember_account(config)
    selected = None
    raw_policy = remembered.get("policy")
    if isinstance(raw_policy, dict):
        selected = _try_account_id(raw_policy.get("account_id"))
    summaries = []
    for account_id in sorted(remembered["account_profiles"]):
        policy = remembered["account_profiles"][account_id]["policy"]
        summaries.append({
            "accountId": account_id,
            "selected": account_id == selected,
            "privateCount": len(policy.get("private_ids", [])),
            "groupCount": len(policy.get("group_ids", [])),
        })
    return summaries


__all__ = ["account_summaries", "remember_account", "select_account", "valid_account_id", "validate_account_id"]
