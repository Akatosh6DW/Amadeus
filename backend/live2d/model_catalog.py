"""合并模型注册表与用户覆盖；路径按调用读取，切换资料后不复用旧路径。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable
import json

from .model_rules import normalize_live2d_model_override

class ModelCatalog:
    def __init__(self, *, registry_path: Callable[[], Path], override_path: Callable[[], Path],
                 model_ids: Callable[[], Any], read_json: Callable[..., Any]) -> None:
        self.registry_path = registry_path
        self.override_path = override_path
        self.model_ids = model_ids
        self.read_json = read_json

    def read_live2d_model_overrides(self, ) -> dict[str, dict[str, Any]]:
        raw = self.read_json(self.override_path(), {})
        if not isinstance(raw, dict):
            return {}
        return {
            model_id: normalize_live2d_model_override(value)
            for model_id, value in raw.items()
            if model_id in self.model_ids() and isinstance(value, dict)
        }


    def live2d_models_payload(self, ) -> dict[str, Any]:
        registry = self.read_json(self.registry_path(), {"defaultId": "aipoet-kurisu", "models": []})
        models = registry.get("models") if isinstance(registry, dict) else []
        overrides = self.read_live2d_model_overrides()
        merged_models = []
        for profile in models if isinstance(models, list) else []:
            if not isinstance(profile, dict):
                continue
            model_id = str(profile.get("id") or "").strip()
            merged_models.append({**profile, **overrides.get(model_id, {})})
        return {
            "defaultId": str(registry.get("defaultId") or "aipoet-kurisu"),
            "models": merged_models,
            "overrides": overrides,
            "overridePath": str(self.override_path()),
        }


    def write_live2d_model_override(self, model_id: str, value: Any, reset: bool = False) -> dict[str, Any]:
        model_id = str(model_id or "").strip()
        if model_id not in self.model_ids():
            raise ValueError("unknown Live2D model id")
        overrides = self.read_live2d_model_overrides()
        if reset:
            overrides.pop(model_id, None)
        else:
            overrides[model_id] = normalize_live2d_model_override(value)
        self.override_path().parent.mkdir(parents=True, exist_ok=True)
        self.override_path().write_text(
            json.dumps(overrides, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.live2d_models_payload()
