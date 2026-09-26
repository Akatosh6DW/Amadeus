"""表情训练数据集及预览持久化，拥有启用、撤销和删除时的关联更新。

规则校验由 training_rules 负责。这里保持预览与样本写入顺序、临时文件替换、
删除样本时移除全部运行时引用的原有行为，不维护内存中的第二份数据集。
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable
import base64
import binascii
import json
import re
from backend.chat.presentation import ALLOWED_EMOTIONS
from backend.runtime.requests import RequestValidationError

from .training_rules import ExpressionTrainingRules

class ExpressionTrainingStore:
    def __init__(self, *, rules: ExpressionTrainingRules, dataset_path: Callable[[], Path],
                 preview_dir: Callable[[], Path], model_ids: Callable[[], Any]) -> None:
        self.rules = rules
        self.dataset_path = dataset_path
        self.preview_dir = preview_dir
        self.model_ids = model_ids

    def read_expression_training_dataset(self, ) -> dict[str, Any]:
        empty = {
            "schemaVersion": 1,
            "samples": [],
            "runtime": self.rules.normalize_expression_runtime({}),
            "updatedAt": "",
        }
        if not self.dataset_path().is_file():
            return empty
        try:
            raw = json.loads(self.dataset_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        samples = raw.get("samples") if isinstance(raw, dict) else []
        if not isinstance(samples, list):
            return empty
        normalized: list[dict[str, Any]] = []
        for item in samples[:512]:
            try:
                normalized.append(self.rules.normalize_expression_training_sample(item, item))
            except RequestValidationError:
                continue
        return {
            "schemaVersion": 1,
            "samples": normalized,
            "runtime": self.rules.normalize_expression_runtime(raw.get("runtime")),
            "updatedAt": self.rules._expression_training_text(raw.get("updatedAt"), 80),
        }


    def write_expression_training_dataset(
        self,
        samples: list[dict[str, Any]],
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_runtime = self.rules.normalize_expression_runtime(runtime)
        normalized_runtime["updatedAt"] = self.rules._expression_training_timestamp()
        payload = {
            "schemaVersion": 1,
            "samples": samples[:512],
            "runtime": normalized_runtime,
            "updatedAt": self.rules._expression_training_timestamp(),
        }
        self.dataset_path().parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.dataset_path().with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(self.dataset_path())
        return payload


    def _save_expression_training_preview(self, sample_id: str, raw_data_url: Any) -> str:
        data_url = str(raw_data_url or "")
        if not data_url:
            return ""
        if not data_url.startswith("data:image/png;base64,"):
            raise RequestValidationError("invalid_training_preview", "表情样本预览必须是 PNG。")
        try:
            image = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error) as error:
            raise RequestValidationError("invalid_training_preview_base64", "表情样本预览无法读取。") from error
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RequestValidationError("invalid_training_preview_png", "表情样本预览不是有效 PNG。")
        if len(image) > 24 * 1024 * 1024:
            raise RequestValidationError("training_preview_too_large", "表情样本预览超过 24MB。")
        self.preview_dir().mkdir(parents=True, exist_ok=True)
        file_name = f"{sample_id}.png"
        (self.preview_dir() / file_name).write_bytes(image)
        return file_name


    def update_expression_training_dataset(self, body: dict[str, Any]) -> dict[str, Any]:
        action = self.rules._expression_training_text(body.get("action"), 40).lower() or "upsert"
        dataset = self.read_expression_training_dataset()
        samples = list(dataset["samples"])
        runtime = self.rules.normalize_expression_runtime(dataset.get("runtime"))
        by_id = {item["id"]: item for item in samples}
        if action == "initialize":
            if samples:
                return {"ok": True, "dataset": dataset, "initialized": False}
            raw_samples = body.get("samples")
            if not isinstance(raw_samples, list):
                raise RequestValidationError("missing_training_samples", "初始化需要表情样本列表。")
            samples = [self.rules.normalize_expression_training_sample(item) for item in raw_samples[:512]]
            return {
                "ok": True,
                "dataset": self.write_expression_training_dataset(samples, runtime),
                "initialized": True,
            }
        if action == "activate":
            sample_id = self.rules._expression_training_text(body.get("id"), 120)
            sample = by_id.get(sample_id)
            if not sample:
                raise RequestValidationError("training_sample_not_found", "找不到要启用的表情样本。")
            model_id = self.rules._expression_training_text(sample.get("modelId"), 120)
            if model_id not in self.model_ids():
                raise RequestValidationError("training_model_mismatch", "该样本没有匹配当前 Live2D 模型。")
            emotion = self.rules._expression_training_text(sample.get("emotion"), 40).lower()
            active_by_model = dict(runtime.get("activeByModel") or {})
            model_entries = dict(active_by_model.get(model_id) or {})
            model_entries[emotion] = {
                "sampleId": sample["id"],
                "sampleRevision": sample["revision"],
                "label": sample["label"],
                "parameters": dict(sample.get("parameters") or {}),
                "activatedAt": self.rules._expression_training_timestamp(),
            }
            active_by_model[model_id] = model_entries
            runtime["activeByModel"] = active_by_model
            return {
                "ok": True,
                "sample": sample,
                "dataset": self.write_expression_training_dataset(samples, runtime),
                "runtimeChanged": True,
            }
        if action == "reset_runtime":
            model_id = self.rules._expression_training_text(body.get("modelId"), 120)
            emotion = self.rules._expression_training_text(body.get("emotion"), 40).lower()
            active_by_model = dict(runtime.get("activeByModel") or {})
            model_entries = dict(active_by_model.get(model_id) or {})
            if emotion in ALLOWED_EMOTIONS:
                model_entries.pop(emotion, None)
            else:
                model_entries.clear()
            if model_entries:
                active_by_model[model_id] = model_entries
            else:
                active_by_model.pop(model_id, None)
            runtime["activeByModel"] = active_by_model
            return {
                "ok": True,
                "dataset": self.write_expression_training_dataset(samples, runtime),
                "runtimeChanged": True,
            }
        if action == "delete":
            sample_id = self.rules._expression_training_text(body.get("id"), 120)
            samples = [item for item in samples if item["id"] != sample_id]
            active_by_model = dict(runtime.get("activeByModel") or {})
            for model_id, raw_entries in list(active_by_model.items()):
                entries = {
                    emotion: entry
                    for emotion, entry in dict(raw_entries or {}).items()
                    if str(entry.get("sampleId") or "") != sample_id
                }
                if entries:
                    active_by_model[model_id] = entries
                else:
                    active_by_model.pop(model_id, None)
            runtime["activeByModel"] = active_by_model
            return {"ok": True, "dataset": self.write_expression_training_dataset(samples, runtime)}
        if action != "upsert":
            raise RequestValidationError("invalid_training_action", "未知的表情训练操作。")
        raw_sample = body.get("sample")
        incoming_id = self.rules._expression_training_text(raw_sample.get("id"), 120) if isinstance(raw_sample, dict) else ""
        existing = by_id.get(incoming_id)
        sample = self.rules.normalize_expression_training_sample(raw_sample, existing)
        preview_file = self._save_expression_training_preview(sample["id"], body.get("imageDataUrl"))
        if preview_file:
            sample["previewFile"] = preview_file
        samples = [item for item in samples if item["id"] != sample["id"]]
        samples.append(sample)
        samples.sort(key=lambda item: (item["emotion"], item["intensity"], item["createdAt"]))
        return {
            "ok": True,
            "sample": sample,
            "dataset": self.write_expression_training_dataset(samples, runtime),
        }


    def expression_training_preview_path(self, sample_id: Any) -> Path | None:
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.rules._expression_training_text(sample_id, 120)).strip(".-")
        if not normalized:
            return None
        path = (self.preview_dir() / f"{normalized}.png").resolve()
        if self.preview_dir().resolve() not in path.parents or not path.is_file():
            return None
        return path
