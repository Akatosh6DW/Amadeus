"""配置字段的兼容迁移、取值限制和发布模式约束。

不保存配置或启动服务。入口提供本次路径、默认值和语音/背景目录读取能力，
以保留原有惰性读取时序；密钥合并仍由 settings_store 负责。
"""
from __future__ import annotations

from pathlib import Path
import re
import urllib.parse
from typing import Any, Callable, Mapping, Collection

from agent.cognitive_repair_routing import COGNITIVE_MODEL_DEFAULTS, normalize_cognitive_model_choice
from agent.deepseek_vision import vision_model
from backend.chat.presentation import normalize_speech_language, normalize_speech_prosody, SPEECH_LANGUAGE_PROFILES
from backend.common.values import clamp_float, clamp_int
from desktop_window_geometry import normalize_window_size

from backend.common.text import unique_strings


BACKGROUND_PRESETS = {
    "boot", "grid", "solid", "custom",
}

BACKGROUND_PRESET_ALIASES = {
    "binary": "grid",
    # Migrate settings from private builds that included non-redistributable backgrounds.
    "gears": "grid",
    "divergence": "grid",
    "original-watch": "grid",
    "original-clockwork": "grid",
    "original-clock-array": "grid",
    "original-divergence": "grid",
}

BACKGROUND_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

PROACTIVE_TOPIC_CATEGORIES = {
    "neuroscience",
    "ai_technology",
    "experiments",
    "games_scifi",
    "books_research",
    "daily_science",
}

DEFAULT_PROACTIVE_TOPIC_CATEGORIES = [
    "neuroscience",
    "ai_technology",
    "experiments",
    "games_scifi",
    "books_research",
    "daily_science",
]

PROACTIVE_TOPIC_CATEGORY_LABELS = {
    "neuroscience": "神经科学与认知",
    "ai_technology": "AI、Agent 与技术",
    "experiments": "实验、工程与问题排查",
    "games_scifi": "游戏与科幻",
    "books_research": "书籍、论文与研究",
    "daily_science": "日常观察与科学思考",
}

def normalize_background_views(raw: Any) -> dict[str, dict[str, float]]:
    if not isinstance(raw, dict):
        return {}
    return {
        key: {
            "focusX": clamp_float(value.get("focusX"), 0, 1, 0.5),
            "focusY": clamp_float(value.get("focusY"), 0, 1, 0.5),
            "zoom": clamp_float(value.get("zoom"), 1, 3, 1),
        }
        for key, value in raw.items()
        if isinstance(key, str) and len(key) <= 2048 and isinstance(value, dict)
        and (key == "boot" or key.startswith("custom:") or re.fullmatch(r"local-[a-z0-9-]+", key))
    }


def normalize_settings(
    raw: dict[str, Any], *, defaults: Mapping[str, Any], voice_revision: int,
    live2d_model_ids: Collection[str], data_dir: Path, release_mode: bool,
    load_voice_presets: Callable[[], dict[str, Any]],
    apply_voice_preset: Callable[[dict[str, Any]], dict[str, Any]],
    available_background_images: Callable[[], list[dict[str, str]]],
) -> dict[str, Any]:
    raw = raw or {}
    settings = dict(defaults)
    settings.update(raw)
    available_voice_presets = load_voice_presets()
    requested_voice_preset = str(settings.get("gptSovitsModelPreset") or "").strip()
    if requested_voice_preset not in available_voice_presets:
        requested_voice_preset = defaults["gptSovitsModelPreset"]
    if requested_voice_preset not in available_voice_presets and available_voice_presets:
        requested_voice_preset = next(iter(available_voice_presets))
    settings["gptSovitsModelPreset"] = requested_voice_preset
    settings["voicePresetRevision"] = voice_revision
    settings = apply_voice_preset(settings)
    settings["apiEndpoint"] = str(settings.get("apiEndpoint") or "").strip() or defaults["apiEndpoint"]
    settings["apiModel"] = str(settings.get("apiModel") or "").strip() or defaults["apiModel"]
    settings["apiKey"] = str(settings.get("apiKey") or "").strip()
    network_mode = str(settings.get("apiNetworkMode") or "auto").strip().lower()
    settings["apiNetworkMode"] = network_mode if network_mode in {
        "auto", "direct", "system_proxy", "explicit_proxy"
    } else "auto"
    proxy_url = str(settings.get("apiProxyUrl") or "").strip()
    if proxy_url:
        parsed_proxy = urllib.parse.urlparse(proxy_url)
        if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
            proxy_url = ""
    settings["apiProxyUrl"] = proxy_url
    settings["memoryFile"] = str(settings.get("memoryFile") or defaults["memoryFile"]).strip()
    settings["useResponseFormat"] = bool(settings.get("useResponseFormat", True))
    # Retire saved settings from the removed optional reply reviewer.
    settings.pop("cognitiveSemanticAuditEnabled", None)
    settings.pop("cognitiveAuditModel", None)
    for key in COGNITIVE_MODEL_DEFAULTS:
        settings[key] = normalize_cognitive_model_choice(settings.get(key), key=key)
    settings["alwaysOnTop"] = bool(settings.get("alwaysOnTop", True))
    settings["hudCollapsed"] = bool(settings.get("hudCollapsed", False))
    settings["layoutMode"] = "full" if str(settings.get("layoutMode") or "").lower() == "full" else "lite"
    legacy_width = settings.get("windowWidth")
    legacy_height = settings.get("windowHeight")
    settings["liteWindowWidth"] = clamp_int(
        settings.get("liteWindowWidth", legacy_width if settings["layoutMode"] == "lite" else 520), 360, 2200, 520
    )
    settings["liteWindowHeight"] = clamp_int(
        settings.get("liteWindowHeight", legacy_height if settings["layoutMode"] == "lite" else 760), 520, 2200, 760
    )
    settings["fullWindowWidth"] = clamp_int(
        settings.get("fullWindowWidth", legacy_width if settings["layoutMode"] == "full" else 1040), 1040, 2200, 1040
    )
    settings["fullWindowHeight"] = clamp_int(
        settings.get("fullWindowHeight", legacy_height if settings["layoutMode"] == "full" else 760), 720, 2200, 760
    )
    for mode in ("lite", "full"):
        settings[mode + "WindowWidth"], settings[mode + "WindowHeight"] = normalize_window_size(
            settings[mode + "WindowWidth"], settings[mode + "WindowHeight"], mode)
    if settings["layoutMode"] == "lite":
        settings["windowWidth"] = settings["liteWindowWidth"]
        settings["windowHeight"] = settings["liteWindowHeight"]
    else:
        settings["windowWidth"] = settings["fullWindowWidth"]
        settings["windowHeight"] = settings["fullWindowHeight"]
    settings["modelScale"] = clamp_float(settings.get("modelScale"), 0.35, 2.4, 1.0)
    settings["modelOffsetX"] = clamp_int(settings.get("modelOffsetX"), -1200, 1200, 0)
    settings["modelOffsetY"] = clamp_int(settings.get("modelOffsetY"), -1200, 1200, -32)
    live2d_model_id = str(settings.get("live2dModelId") or "aipoet-kurisu").strip()
    settings["live2dModelId"] = live2d_model_id if live2d_model_id in live2d_model_ids else "aipoet-kurisu"
    raw_transforms = settings.get("modelTransforms") if isinstance(settings.get("modelTransforms"), dict) else {}
    settings["modelTransforms"] = {
        model_id: {
            "scale": clamp_float(value.get("scale"), 0.35, 2.4, 1.0),
            "offsetX": clamp_int(value.get("offsetX"), -1200, 1200, 0),
            "offsetY": clamp_int(value.get("offsetY"), -1200, 1200, 0),
        }
        for model_id, value in raw_transforms.items()
        if model_id in live2d_model_ids and isinstance(value, dict)
    }
    background_preset = str(settings.get("backgroundPreset") or "grid").strip().lower()
    background_preset = BACKGROUND_PRESET_ALIASES.get(background_preset, background_preset)
    valid_background = background_preset in BACKGROUND_PRESETS or any(
        item["id"] == background_preset for item in available_background_images()
    )
    settings["backgroundPreset"] = background_preset if valid_background else "grid"
    custom_background = str(settings.get("customBackgroundPath") or "").strip()
    settings["customBackgroundPath"] = custom_background if Path(custom_background).suffix.lower() in BACKGROUND_IMAGE_EXTENSIONS else ""
    settings["backgroundViews"] = normalize_background_views(settings.get("backgroundViews"))
    settings["voiceEnabled"] = bool(settings.get("voiceEnabled", True))
    settings["lipSyncEnabled"] = bool(settings.get("lipSyncEnabled", True))
    settings["ttsProvider"] = str(settings.get("ttsProvider") or defaults["ttsProvider"]).strip().lower() or defaults["ttsProvider"]
    settings["ttsEndpoint"] = str(settings.get("ttsEndpoint") or "").strip()
    settings["ttsSpeaker"] = str(settings.get("ttsSpeaker") or "1").strip() or "1"
    settings["speechRate"] = clamp_float(settings.get("speechRate"), 0.72, 1.35, 1.0)
    selected_preset_id = str(settings.get("gptSovitsModelPreset") or "")
    selected_preset = load_voice_presets().get(selected_preset_id)
    explicit_speech_language = raw.get("speechLanguage")
    if not explicit_speech_language and isinstance(selected_preset, dict):
        explicit_speech_language = selected_preset.get("speechLanguage")
    settings["speechLanguage"] = normalize_speech_language(
        explicit_speech_language,
        voice_locale=raw.get("voiceLocale"),
        target_language=raw.get("gptSovitsTargetLanguage"),
    )
    settings["speechProsody"] = normalize_speech_prosody(settings.get("speechProsody"))
    speech_profile = SPEECH_LANGUAGE_PROFILES[settings["speechLanguage"]]
    settings["voiceLocale"] = speech_profile["locale"]
    settings["voicePitch"] = clamp_float(settings.get("voicePitch"), 0.75, 1.65, 1.18)
    settings["ttsDeliveryPostProcessingEnabled"] = bool(
        settings.get("ttsDeliveryPostProcessingEnabled", False)
    )
    settings["gptSovitsPort"] = clamp_int(settings.get("gptSovitsPort"), 1024, 65535, 9881)
    settings["gptSovitsCondaEnv"] = str(settings.get("gptSovitsCondaEnv") or defaults["gptSovitsCondaEnv"]).strip()
    settings["gptSovitsRoot"] = str(settings.get("gptSovitsRoot") or defaults["gptSovitsRoot"]).strip()
    settings["gptSovitsModelPreset"] = str(
        settings.get("gptSovitsModelPreset") or defaults["gptSovitsModelPreset"]
    ).strip()
    settings["gptSovitsGptModel"] = str(settings.get("gptSovitsGptModel") or defaults["gptSovitsGptModel"]).strip()
    settings["gptSovitsSovitsModel"] = str(settings.get("gptSovitsSovitsModel") or defaults["gptSovitsSovitsModel"]).strip()
    settings["gptSovitsBootstrapGptModel"] = str(
        settings.get("gptSovitsBootstrapGptModel") or settings["gptSovitsGptModel"]
    ).strip()
    settings["gptSovitsBootstrapSovitsModel"] = str(
        settings.get("gptSovitsBootstrapSovitsModel") or settings["gptSovitsSovitsModel"]
    ).strip()
    raw_gpt_routes = settings.get("gptSovitsGptModelRoutes")
    settings["gptSovitsGptModelRoutes"] = (
        {
            str(route).strip().lower(): str(path).strip()
            for route, path in raw_gpt_routes.items()
            if str(route).strip().lower() in {"short", "long"} and str(path).strip()
        }
        if isinstance(raw_gpt_routes, dict)
        else {}
    )
    settings["gptSovitsGptRouteShortMaxChars"] = clamp_int(
        settings.get("gptSovitsGptRouteShortMaxChars"), 1, 200, 32
    )
    settings["gptSovitsGptRouteLongMinChars"] = clamp_int(
        settings.get("gptSovitsGptRouteLongMinChars"),
        settings["gptSovitsGptRouteShortMaxChars"] + 1,
        1000,
        60,
    )
    settings["gptSovitsRefAudio"] = str(settings.get("gptSovitsRefAudio") or defaults["gptSovitsRefAudio"]).strip()
    settings["gptSovitsRefText"] = str(settings.get("gptSovitsRefText") or defaults["gptSovitsRefText"]).strip()
    settings["gptSovitsStyleMap"] = str(settings.get("gptSovitsStyleMap") or "").strip()
    settings["gptSovitsTargetLanguage"] = speech_profile["targetLanguage"]
    settings["alignSpokenJa"] = bool(settings.get("alignSpokenJa", True))
    settings["autoStartGptSovits"] = bool(settings.get("autoStartGptSovits", True))
    settings["debugDirectSpeech"] = bool(settings.get("debugDirectSpeech", False))
    settings["permissionReadScreen"] = bool(settings.get("permissionReadScreen", True))
    settings["permissionReadCamera"] = bool(settings.get("permissionReadCamera", True))
    settings["cameraIndex"] = clamp_int(settings.get("cameraIndex"), 0, 8, 0)
    settings["permissionReadSystemTime"] = bool(settings.get("permissionReadSystemTime", True))
    settings["permissionWebSearch"] = bool(settings.get("permissionWebSearch", True))
    settings["permissionFileRead"] = bool(settings.get("permissionFileRead", True))
    settings["permissionFileWrite"] = bool(settings.get("permissionFileWrite", True))
    settings["permissionOpenBrowser"] = bool(settings.get("permissionOpenBrowser", True))
    workspace_text = re.sub(
        r"[\x00\r\n]",
        "",
        str(settings.get("agentFileWorkspace") or defaults["agentFileWorkspace"]),
    ).strip()
    try:
        workspace_path = Path(workspace_text).expanduser()
        if not workspace_path.is_absolute():
            workspace_path = data_dir / workspace_path
        workspace_path = workspace_path.resolve()
        if workspace_path.parent == workspace_path:
            raise ValueError("filesystem root cannot be an Agent workspace")
    except (OSError, ValueError):
        workspace_path = Path(defaults["agentFileWorkspace"]).resolve()
    settings["agentFileWorkspace"] = str(workspace_path)
    web_provider = str(settings.get("webSearchProvider") or "duckduckgo").strip().lower()
    settings["webSearchProvider"] = web_provider if web_provider in {"duckduckgo", "brave"} else "duckduckgo"
    settings["webSearchApiKey"] = str(settings.get("webSearchApiKey") or "").strip()
    settings["webSearchMaxResults"] = clamp_int(settings.get("webSearchMaxResults"), 1, 8, 5)
    settings["webReadTopPages"] = clamp_int(settings.get("webReadTopPages"), 0, 3, 2)
    settings["webRequestTimeout"] = clamp_int(settings.get("webRequestTimeout"), 3, 30, 12)
    settings["proactiveInteractionsEnabled"] = bool(settings.get("proactiveInteractionsEnabled", True))
    settings["proactiveScreenObservationEnabled"] = bool(
        settings.get("proactiveScreenObservationEnabled", True)
    )
    settings["proactiveScreenObservationChance"] = clamp_float(
        settings.get("proactiveScreenObservationChance"), 0.0, 0.60, 0.20
    )
    proactive_frequency = str(settings.get("proactiveFrequency") or "normal").strip().lower()
    settings["proactiveFrequency"] = (
        proactive_frequency
        if proactive_frequency in {"quiet", "low", "normal", "high"}
        else "normal"
    )
    raw_proactive_topics = settings.get("proactiveTopicCategories")
    if not isinstance(raw_proactive_topics, list):
        raw_proactive_topics = DEFAULT_PROACTIVE_TOPIC_CATEGORIES
    normalized_proactive_topics = unique_strings([
        str(item).strip()
        for item in raw_proactive_topics
        if str(item).strip() in PROACTIVE_TOPIC_CATEGORIES
    ])
    settings["proactiveTopicCategories"] = (
        normalized_proactive_topics
        if normalized_proactive_topics or not settings["proactiveInteractionsEnabled"]
        else list(DEFAULT_PROACTIVE_TOPIC_CATEGORIES)
    )
    settings["visionEnabled"] = bool(settings.get("visionEnabled", True))
    settings["visionPort"] = clamp_int(settings.get("visionPort"), 1024, 65535, 9892)
    settings["visionCondaEnv"] = str(settings.get("visionCondaEnv") or defaults["visionCondaEnv"]).strip()
    # The visible status and independent image reader follow the same selection.
    settings["visionModel"] = vision_model(settings)
    settings["visionApiKey"] = str(settings.get("visionApiKey") or "").strip()
    if release_mode:
        settings["gptSovitsGptModelRoutes"] = {}
        settings["memoryFile"] = "memory.json"
        settings["debugDirectSpeech"] = False
        settings["gptSovitsPort"] = defaults["gptSovitsPort"]
        settings["ttsEndpoint"] = defaults["ttsEndpoint"]
        settings["gptSovitsCondaEnv"] = defaults["gptSovitsCondaEnv"]
        settings["gptSovitsRoot"] = defaults["gptSovitsRoot"]
        available_presets = load_voice_presets()
        preset_id = settings["gptSovitsModelPreset"]
        if preset_id not in available_presets:
            preset_id = defaults["gptSovitsModelPreset"]
        if preset_id not in available_presets and available_presets:
            preset_id = next(iter(available_presets))
        settings["gptSovitsModelPreset"] = preset_id
        settings = apply_voice_preset(settings)
        settings["visionCondaEnv"] = defaults["visionCondaEnv"]
    return settings
