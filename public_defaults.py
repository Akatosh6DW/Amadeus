"""Credential-free defaults shared by source and release runtimes.

The JSON file is intentionally a narrow, publishable contract.  Runtime
locations, credentials, memory selectors, and persona material belong to the
active installation and are added by the server after this module loads.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any


PUBLIC_DEFAULTS_PATH = Path(__file__).resolve().parent / "configs" / "public_defaults.json"

# Keep this list explicit.  In particular, do not derive it from a private
# settings object: settings.json may contain user data and arbitrary keys.
PUBLIC_DEFAULT_KEYS = frozenset({
    "alignSpokenJa",
    "alwaysOnTop",
    "apiContextLimit",
    "apiEndpoint",
    "apiModel",
    "apiNetworkMode",
    "autoStartGptSovits",
    "backgroundPreset",
    "backgroundViews",
    "cognitiveRepairModel",
    "contextSemanticRouterEnabled",
    "fullWindowHeight",
    "fullWindowWidth",
    "gptSovitsGptRouteLongMinChars",
    "gptSovitsGptRouteShortMaxChars",
    "gptSovitsModelPreset",
    "gptSovitsTargetLanguage",
    "hudCollapsed",
    "layoutMode",
    "lipSyncEnabled",
    "liteWindowHeight",
    "liteWindowWidth",
    "live2dModelId",
    "modelOffsetX",
    "modelOffsetY",
    "modelScale",
    "modelTransforms",
    "permissionFileRead",
    "permissionFileWrite",
    "permissionOpenBrowser",
    "permissionReadCamera",
    "permissionReadScreen",
    "permissionReadSystemTime",
    "permissionWebSearch",
    "proactiveFrequency",
    "proactiveInteractionsEnabled",
    "proactiveScreenObservationChance",
    "proactiveScreenObservationEnabled",
    "proactiveTopicCategories",
    "speechLanguage",
    "speechProsody",
    "speechRate",
    "ttsDeliveryPostProcessingEnabled",
    "ttsProvider",
    "ttsSpeaker",
    "useResponseFormat",
    "visionEnabled",
    "visionModel",
    "voiceEnabled",
    "voiceLocale",
    "voicePitch",
    "webReadTopPages",
    "webRequestTimeout",
    "webSearchMaxResults",
    "webSearchProvider",
    "windowHeight",
    "windowWidth",
})


def load_public_defaults(path: Path | None = None) -> dict[str, Any]:
    """Load only the publishable default keys from *path*.

    A missing or malformed file yields an empty mapping so the server's
    runtime fallbacks remain available.  Unknown keys are discarded before
    they can enter the settings merge chain.
    """

    source = PUBLIC_DEFAULTS_PATH if path is None else Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return deepcopy({key: raw[key] for key in PUBLIC_DEFAULT_KEYS if key in raw})
