"""表情训练样本和运行时选择的格式规则；不读取或保存训练文件。"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Callable
import re
import uuid
from backend.common.values import clamp_float, clamp_int
from backend.common.text import unique_strings
from backend.chat.presentation import ALLOWED_EMOTIONS
from backend.runtime.requests import RequestValidationError

class ExpressionTrainingRules:
    def __init__(self, *, model_ids: Callable[[], Any]) -> None:
        self.model_ids = model_ids

    def _expression_training_timestamp(self, ) -> str:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")


    def _expression_training_text(self, value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]


    def _expression_training_parameter_map(self, value: Any) -> dict[str, float]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, float] = {}
        for raw_id, raw_value in list(value.items())[:256]:
            parameter_id = self._expression_training_text(raw_id, 120)
            if parameter_id:
                normalized[parameter_id] = clamp_float(raw_value, -10000, 10000, 0)
        return normalized


    def _expression_training_string_list(self, value: Any, limit: int = 32) -> list[str]:
        if not isinstance(value, list):
            return []
        return unique_strings(
            self._expression_training_text(item, 160)
            for item in value[:limit]
            if self._expression_training_text(item, 160)
        )


    def normalize_expression_training_sample(self, value: Any, existing: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RequestValidationError("invalid_training_sample", "表情训练样本必须是对象。")
        existing = existing if isinstance(existing, dict) else {}
        sample_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", self._expression_training_text(value.get("id"), 120)).strip(".-")
        if not sample_id:
            sample_id = uuid.uuid4().hex
        label = self._expression_training_text(value.get("label"), 120)
        if not label:
            raise RequestValidationError("missing_training_label", "表情训练样本需要标签。")
        recommended_label = self._expression_training_text(
            value.get("recommendedLabel") or existing.get("recommendedLabel") or label,
            120,
        )
        emotion = self._expression_training_text(value.get("emotion"), 40).lower()
        if emotion not in ALLOWED_EMOTIONS:
            emotion = "neutral"
        strength_band = self._expression_training_text(value.get("strengthBand"), 24).lower()
        if strength_band not in {"subtle", "typical", "strong", "custom"}:
            strength_band = "custom"
        created_at = self._expression_training_text(existing.get("createdAt") or value.get("createdAt"), 80)
        if not created_at:
            created_at = self._expression_training_timestamp()
        preview_file = self._expression_training_text(existing.get("previewFile"), 180)
        raw_support = value.get("parameterSupport")
        parameter_support = (
            {
                self._expression_training_text(key, 120): self._expression_training_text(support, 40)
                for key, support in list(raw_support.items())[:256]
                if self._expression_training_text(key, 120)
            }
            if isinstance(raw_support, dict)
            else dict(existing.get("parameterSupport") or {})
        )
        raw_recommended_metadata = value.get("recommendedMetadata")
        if not isinstance(raw_recommended_metadata, dict):
            raw_recommended_metadata = existing.get("recommendedMetadata")
        if not isinstance(raw_recommended_metadata, dict):
            raw_recommended_metadata = {}
        recommended_emotion = self._expression_training_text(raw_recommended_metadata.get("emotion"), 40).lower()
        if recommended_emotion not in ALLOWED_EMOTIONS:
            recommended_emotion = emotion
        recommended_strength = self._expression_training_text(
            raw_recommended_metadata.get("strengthBand"),
            24,
        ).lower()
        if recommended_strength not in {"subtle", "typical", "strong", "custom"}:
            recommended_strength = strength_band
        return {
            "id": sample_id,
            "schemaVersion": 1,
            "label": label,
            "recommendedLabel": recommended_label,
            "emotion": emotion,
            "strengthBand": strength_band,
            "intensity": clamp_float(value.get("intensity"), 0, 2.5, 1),
            "tags": self._expression_training_string_list(value.get("tags")),
            "notes": self._expression_training_text(value.get("notes"), 2000),
            "modelId": self._expression_training_text(value.get("modelId"), 120),
            "source": self._expression_training_text(value.get("source"), 40) or "user",
            "recommendedParameters": self._expression_training_parameter_map(
                value.get("recommendedParameters") or existing.get("recommendedParameters")
            ),
            "recommendedMetadata": {
                "emotion": recommended_emotion,
                "strengthBand": recommended_strength,
                "intensity": clamp_float(
                    raw_recommended_metadata.get("intensity"),
                    0,
                    2.5,
                    clamp_float(value.get("intensity"), 0, 2.5, 1),
                ),
                "tags": self._expression_training_string_list(
                    raw_recommended_metadata.get("tags")
                    if isinstance(raw_recommended_metadata.get("tags"), list)
                    else value.get("tags")
                ),
                "notes": self._expression_training_text(
                    raw_recommended_metadata.get("notes")
                    if "notes" in raw_recommended_metadata
                    else value.get("notes"),
                    2000,
                ),
            },
            "parameters": self._expression_training_parameter_map(value.get("parameters")),
            "parameterSupport": parameter_support,
            "couplingWarnings": self._expression_training_string_list(value.get("couplingWarnings")),
            "userEdited": bool(value.get("userEdited")),
            "revision": clamp_int(value.get("revision"), 0, 1_000_000, 0),
            "previewFile": preview_file,
            "createdAt": created_at,
            "updatedAt": self._expression_training_timestamp(),
        }


    def normalize_expression_runtime(self, value: Any) -> dict[str, Any]:
        source = value if isinstance(value, dict) else {}
        raw_models = source.get("activeByModel") if isinstance(source.get("activeByModel"), dict) else {}
        active_by_model: dict[str, dict[str, Any]] = {}
        for model_id, raw_emotions in list(raw_models.items())[:32]:
            normalized_model_id = self._expression_training_text(model_id, 120)
            if normalized_model_id not in self.model_ids() or not isinstance(raw_emotions, dict):
                continue
            emotions: dict[str, Any] = {}
            for emotion, raw_entry in list(raw_emotions.items())[:64]:
                normalized_emotion = self._expression_training_text(emotion, 40).lower()
                if normalized_emotion not in ALLOWED_EMOTIONS or not isinstance(raw_entry, dict):
                    continue
                sample_id = re.sub(
                    r"[^A-Za-z0-9_.-]+",
                    "-",
                    self._expression_training_text(raw_entry.get("sampleId"), 120),
                ).strip(".-")
                if not sample_id:
                    continue
                emotions[normalized_emotion] = {
                    "sampleId": sample_id,
                    "sampleRevision": clamp_int(raw_entry.get("sampleRevision"), 0, 1_000_000, 0),
                    "label": self._expression_training_text(raw_entry.get("label"), 120),
                    "parameters": self._expression_training_parameter_map(raw_entry.get("parameters")),
                    "activatedAt": self._expression_training_text(raw_entry.get("activatedAt"), 80),
                }
            if emotions:
                active_by_model[normalized_model_id] = emotions
        return {
            "schemaVersion": 1,
            "activeByModel": active_by_model,
            "updatedAt": self._expression_training_text(source.get("updatedAt"), 80),
        }
