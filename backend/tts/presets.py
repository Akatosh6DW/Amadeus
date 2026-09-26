"""语音预设目录与便携资源解析，保留受根目录约束的模型/参考音频路径。

目录决定用户可选的预设，资源清单只提供已支持项的实际路径。该服务不启动
语音进程、不生成音频，也不持有设置副本；调用方负责保存返回的设置。
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable

class VoicePresetCatalog:
    def __init__(self, *, root: Callable[[], Path], runtime_root: Callable[[], Path],
                 release_mode: Callable[[], bool], presets_path: Callable[[], Path],
                 defaults_path: Callable[[], Path], read_json: Callable[..., Any]) -> None:
        self.root = root
        self.runtime_root = runtime_root
        self.release_mode = release_mode
        self.presets_path = presets_path
        self.defaults_path = defaults_path
        self.read_json = read_json

    def _load_portable_voice_presets(self, voice_root: Path) -> dict[str, Any]:
        """Load a self-contained voice asset bundle without trusting outside paths."""

        voice_root = voice_root.resolve()
        manifest = self.read_json(voice_root / "manifest.json", {})
        raw_presets = manifest.get("presets") if isinstance(manifest, dict) else None
        result: dict[str, Any] = {}
        if not isinstance(raw_presets, dict):
            return result
        path_keys = (
            "gptModel",
            "sovitsModel",
            "bootstrapGptModel",
            "bootstrapSovitsModel",
            "refAudio",
            "refText",
            "styleMap",
        )
        for preset_id, raw in raw_presets.items():
            if not isinstance(raw, dict):
                continue
            preset = {
                "label": str(raw.get("label") or preset_id),
                "description": str(raw.get("description") or ""),
            }
            valid = True
            for key in path_keys:
                value = str(raw.get(key) or "").strip()
                if not value:
                    if key == "styleMap":
                        preset[key] = ""
                        continue
                    valid = False
                    break
                candidate = (voice_root / value).resolve()
                try:
                    candidate.relative_to(voice_root)
                except ValueError:
                    valid = False
                    break
                if not candidate.is_file():
                    valid = False
                    break
                preset[key] = str(candidate)
            if not valid:
                continue
            for optional_key in (
                "speechLanguage",
                "speechProsody",
                "deliveryPostProcessingEnabled",
                "gptRouteShortMaxChars",
                "gptRouteLongMinChars",
            ):
                if optional_key in raw:
                    preset[optional_key] = raw[optional_key]
            raw_routes = raw.get("gptModelRoutes")
            if isinstance(raw_routes, dict):
                routes: dict[str, str] = {}
                for route, route_value in raw_routes.items():
                    route_name = str(route).strip().lower()
                    value = str(route_value or "").strip()
                    if route_name not in {"short", "long"} or not value:
                        continue
                    candidate = (voice_root / value).resolve()
                    try:
                        candidate.relative_to(voice_root)
                    except ValueError:
                        valid = False
                        break
                    if not candidate.is_file():
                        valid = False
                        break
                    routes[route_name] = str(candidate)
                if not valid:
                    continue
                preset["gptModelRoutes"] = routes
            result[str(preset_id)] = preset
        if result:
            result.setdefault(
                "custom",
                {
                    "label": "自定义路径",
                    "description": "手动填写权重、参考音频和 style map。",
                    "gptModel": "",
                    "sovitsModel": "",
                    "refAudio": "",
                    "refText": "",
                    "styleMap": "",
                },
            )
        return result


    def voice_runtime_root(self, ) -> Path:
        return (
            (self.runtime_root() / "voice").resolve()
            if self.release_mode()
            else (self.root() / ".runtime-assets" / "voice").resolve()
        )


    def load_voice_presets(self, ) -> dict[str, Any]:
        # Release builds and source checkouts use the same portable bundle format.
        # A clean developer checkout receives this directory from bootstrap_dev.ps1.
        catalog = self.read_json(self.presets_path(), {})
        catalog = catalog if isinstance(catalog, dict) else {}
        supported = self.read_json(self.defaults_path(), {}).get(
            "supportedPresets", ["emotionclarity_v7_retrained_short", "emotionclarity_v6"])
        # The shipped catalog owns selectable presets. An older downloaded asset
        # bundle may still contain retired models, but must not expose them again.
        voice_root = self.voice_runtime_root()
        bundled = self._load_portable_voice_presets(voice_root)
        portable_presets = {key: bundled[key] for key in supported if key in bundled}
        if portable_presets and "custom" in bundled:
            portable_presets["custom"] = bundled["custom"]
        if portable_presets:
            return portable_presets

        if self.release_mode():

            # Compatibility for portable folders produced before the multi-preset manifest.
            shared = {
                "bootstrapGptModel": str(voice_root / "emotionlite-bootstrap.ckpt"),
                "bootstrapSovitsModel": str(voice_root / "emotionlite-bootstrap.pth"),
                "refAudio": str(voice_root / "refs" / "audio" / "CRS_0659.wav"),
                "refText": str(voice_root / "refs" / "text" / "calm_normal_CRS_0659.txt"),
            }
            return {
                "emotionclarity_v6": {
                    "label": "EmotionClarity V6（清晰推荐）",
                    "description": "V4-E2 音色权重、EmotionLite 兼容引导与 V6 清晰参考。",
                    "gptModel": str(voice_root / "emotiondetail_v4-e2.ckpt"),
                    "sovitsModel": str(voice_root / "emotiondetail_v4_e2_s328.pth"),
                    "styleMap": str(voice_root / "presets" / "emotionclarity_v6" / "style_map.json"),
                    **shared,
                },
            }
        return {key: catalog[key] for key in (*supported, "custom") if key in catalog}


    def apply_voice_preset(self, settings: dict[str, Any]) -> dict[str, Any]:
        preset_id = str(settings.get("gptSovitsModelPreset") or "").strip()
        if not preset_id or preset_id == "custom":
            return settings
        preset = self.load_voice_presets().get(preset_id)
        if not isinstance(preset, dict):
            return settings
        mapping = {
            "gptSovitsGptModel": "gptModel",
            "gptSovitsSovitsModel": "sovitsModel",
            "gptSovitsBootstrapGptModel": "bootstrapGptModel",
            "gptSovitsBootstrapSovitsModel": "bootstrapSovitsModel",
            "gptSovitsRefAudio": "refAudio",
            "gptSovitsRefText": "refText",
            "gptSovitsStyleMap": "styleMap",
            "speechLanguage": "speechLanguage",
            "speechProsody": "speechProsody",
        }
        for setting_key, preset_key in mapping.items():
            value = str(preset.get(preset_key) or "").strip()
            if value:
                settings[setting_key] = value
            elif setting_key == "gptSovitsStyleMap":
                settings[setting_key] = ""
        if "deliveryPostProcessingEnabled" in preset:
            settings["ttsDeliveryPostProcessingEnabled"] = bool(
                preset.get("deliveryPostProcessingEnabled")
            )
        raw_routes = preset.get("gptModelRoutes")
        settings["gptSovitsGptModelRoutes"] = (
            {
                str(route).strip().lower(): str(path).strip()
                for route, path in raw_routes.items()
                if str(route).strip().lower() in {"short", "long"} and str(path).strip()
            }
            if isinstance(raw_routes, dict)
            else {}
        )
        settings["gptSovitsGptRouteShortMaxChars"] = preset.get(
            "gptRouteShortMaxChars", 32
        )
        settings["gptSovitsGptRouteLongMinChars"] = preset.get(
            "gptRouteLongMinChars", 60
        )
        return settings
