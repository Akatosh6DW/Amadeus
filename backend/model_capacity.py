"""Model context-capacity metadata and settings normalization.

The context budget used by the pressure pipeline has two separate inputs:

``apiContextLimit``
    The hard input-context ceiling used for a request.
``apiContextCompactThreshold``
    An optional earlier trigger for compaction.  ``None`` disables the
    explicit trigger and lets the pressure owner use its normal policy.

Only provider/model combinations with an explicit, verified route are given a
larger built-in capacity.  In particular, a DeepSeek-compatible gateway is
never treated as the official DeepSeek route merely because its model name
looks familiar.
"""

from __future__ import annotations

from dataclasses import dataclass
import urllib.parse
from typing import Any, Mapping


CONTEXT_LIMIT_SETTING = "apiContextLimit"
COMPACT_THRESHOLD_SETTING = "apiContextCompactThreshold"

# The shipped 65,536-token setting is retained as the conservative fallback
# for unknown providers.  A persisted value is always explicit: without a
# provenance marker we cannot safely distinguish a user's deliberate 65,536
# cap from an old default.
LEGACY_DEFAULT_CONTEXT_LIMIT = 65_536
MIN_CONTEXT_LIMIT = 4_096
MAX_CONTEXT_LIMIT = 16 * 1024 * 1024
MIN_COMPACT_THRESHOLD = 1_024

OFFICIAL_DEEPSEEK_HOST = "api.deepseek.com"
# DeepSeek's public API documentation describes this as a 1M-token context
# length.  Keep the API setting conservative instead of translating a binary
# power-of-two model-window value into an endpoint guarantee.
OFFICIAL_DEEPSEEK_CONTEXT_LIMIT = 1_000_000
OFFICIAL_DEEPSEEK_MODELS = frozenset({
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4.1-flash-1017",
    "deepseek-v4-pro",
    "deepseek-v4-pro-0813",
})

_MISSING = object()


@dataclass(frozen=True)
class ModelCapacityProfile:
    """Conservative capacity metadata for one endpoint/model pair."""

    context_limit: int | None
    provider: str = ""
    model: str = ""
    official: bool = False
    source: str = "unknown"

    @property
    def known(self) -> bool:
        return self.context_limit is not None

    def to_public_dict(self) -> dict[str, Any]:
        """Return non-sensitive metadata suitable for a browser response."""

        return {
            "known": self.known,
            "contextLimit": self.context_limit,
            "provider": self.provider,
            "model": self.model,
            "official": self.official,
            "source": self.source,
        }


def _normalised_model(value: object) -> str:
    return str(value or "").strip().casefold()


def _official_deepseek_route(endpoint: object) -> bool:
    """Return whether *endpoint* is the HTTPS official DeepSeek host.

    A hostname check is intentionally strict: compatible gateways frequently
    reuse DeepSeek model IDs but can expose a different context window.
    """

    value = str(endpoint or "").strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname or ""
    if parsed.scheme.casefold() != "https" or hostname.casefold() != OFFICIAL_DEEPSEEK_HOST:
        return False
    return port in (None, 443)


def model_capacity_profile(endpoint: object, model: object) -> ModelCapacityProfile:
    """Resolve only capacities backed by a known provider route."""

    endpoint_text = str(endpoint or "").strip()
    model_text = str(model or "").strip()
    model_key = _normalised_model(model_text)
    if _official_deepseek_route(endpoint_text) and model_key in OFFICIAL_DEEPSEEK_MODELS:
        return ModelCapacityProfile(
            context_limit=OFFICIAL_DEEPSEEK_CONTEXT_LIMIT,
            provider="deepseek",
            model=model_text,
            official=True,
            source="deepseek-official-models-pricing",
        )
    return ModelCapacityProfile(
        context_limit=None,
        provider="",
        model=model_text,
        official=False,
        source="unknown-provider-or-model",
    )


def model_context_limit(
    endpoint: object,
    model: object,
    *,
    fallback: int = LEGACY_DEFAULT_CONTEXT_LIMIT,
) -> int:
    """Return a known model limit or the conservative fallback."""

    profile = model_capacity_profile(endpoint, model)
    if profile.context_limit is not None:
        return profile.context_limit
    return _bounded_context_limit(fallback, LEGACY_DEFAULT_CONTEXT_LIMIT)


def _integer(value: object) -> int | None:
    if value is _MISSING or value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _bounded_context_limit(value: object, fallback: int) -> int:
    candidate = _integer(value)
    fallback_value = _integer(fallback)
    if fallback_value is None:
        fallback_value = LEGACY_DEFAULT_CONTEXT_LIMIT
    fallback_value = max(MIN_CONTEXT_LIMIT, min(MAX_CONTEXT_LIMIT, fallback_value))
    if candidate is None:
        return fallback_value
    return max(MIN_CONTEXT_LIMIT, min(MAX_CONTEXT_LIMIT, candidate))


def _normalise_threshold(value: object, *, context_limit: int) -> int | None:
    candidate = _integer(value)
    if candidate is None or candidate <= 0:
        return None
    if context_limit <= MIN_COMPACT_THRESHOLD:
        return None
    # A trigger at or beyond the hard limit would never be an earlier trigger.
    return max(MIN_COMPACT_THRESHOLD, min(context_limit - 1, candidate))


def normalize_model_capacity_settings(
    raw: Mapping[str, Any] | None,
    *,
    endpoint: object,
    model: object,
    fallback_limit: int = LEGACY_DEFAULT_CONTEXT_LIMIT,
    fallback_threshold: object = None,
) -> dict[str, int | None]:
    """Normalize persisted hard-limit and optional compaction settings.

    A missing limit uses known model metadata and otherwise the conservative
    fallback.  Every persisted value remains explicit, including 65,536: the
    settings file does not carry provenance that would distinguish an old
    default from a user's deliberate smaller cap.  Values are bounded to the
    generic setting range and known providers are capped at their documented
    API window.
    """

    values = raw if isinstance(raw, Mapping) else {}
    profile = model_capacity_profile(endpoint, model)
    raw_limit = values.get(CONTEXT_LIMIT_SETTING, _MISSING)
    fallback = _bounded_context_limit(fallback_limit, LEGACY_DEFAULT_CONTEXT_LIMIT)
    parsed_limit = _integer(raw_limit)
    if parsed_limit is None:
        limit = profile.context_limit or fallback
    else:
        limit = _bounded_context_limit(parsed_limit, fallback)
        if profile.context_limit is not None:
            # Never send a known official route a locally configured value
            # beyond the provider's documented model window.  Smaller caps,
            # including the old 65,536 default, remain deliberate settings.
            limit = min(limit, profile.context_limit)

    threshold_value = values.get(COMPACT_THRESHOLD_SETTING, fallback_threshold)
    threshold = _normalise_threshold(threshold_value, context_limit=limit)
    return {
        CONTEXT_LIMIT_SETTING: limit,
        COMPACT_THRESHOLD_SETTING: threshold,
    }


def model_capacity_suggestion(
    endpoint: object,
    model: object,
    configured_limit: object,
) -> dict[str, Any] | None:
    """Return a safe upgrade suggestion for a known route, if one exists."""

    profile = model_capacity_profile(endpoint, model)
    configured = _integer(configured_limit)
    if profile.context_limit is None or configured is None:
        return None
    if configured >= profile.context_limit:
        return None
    return {
        "suggestedLimit": profile.context_limit,
        "currentLimit": max(0, configured),
        "source": profile.source,
        "official": profile.official,
        "model": profile.model,
    }


__all__ = [
    "COMPACT_THRESHOLD_SETTING",
    "CONTEXT_LIMIT_SETTING",
    "LEGACY_DEFAULT_CONTEXT_LIMIT",
    "MAX_CONTEXT_LIMIT",
    "MIN_COMPACT_THRESHOLD",
    "MIN_CONTEXT_LIMIT",
    "ModelCapacityProfile",
    "OFFICIAL_DEEPSEEK_CONTEXT_LIMIT",
    "OFFICIAL_DEEPSEEK_HOST",
    "OFFICIAL_DEEPSEEK_MODELS",
    "model_capacity_profile",
    "model_capacity_suggestion",
    "model_context_limit",
    "normalize_model_capacity_settings",
]
