"""参数实验截图及伴随元数据的保存边界，限制格式、大小与文件名。"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import base64
import binascii
import json
from backend.common.values import clamp_float, clamp_int
from backend.common.text import unique_strings
from backend.runtime.requests import RequestValidationError

from .model_rules import safe_live2d_capture_label

class ExpressionCaptureStore:
    def __init__(self, *, capture_dir: Callable[[], Path]) -> None:
        self.capture_dir = capture_dir

    def save_live2d_expression_capture(self, body: dict[str, Any]) -> dict[str, Any]:
        label = safe_live2d_capture_label(body.get("label"))
        if not label:
            raise RequestValidationError("missing_capture_label", "请先填写截图标签。")
        raw_data_url = str(body.get("imageDataUrl") or "")
        if not raw_data_url.startswith("data:image/png;base64,"):
            raise RequestValidationError("invalid_capture_image", "参数截图必须是 PNG。")
        encoded = raw_data_url.split(",", 1)[1]
        try:
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise RequestValidationError("invalid_capture_base64", "参数截图内容无法读取。") from error
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RequestValidationError("invalid_capture_png", "参数截图不是有效 PNG。")
        if len(image) > 24 * 1024 * 1024:
            raise RequestValidationError("capture_too_large", "参数截图超过 24MB。")

        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        file_name = f"{label}__{timestamp}.png"
        self.capture_dir().mkdir(parents=True, exist_ok=True)
        file_path = self.capture_dir() / file_name
        file_path.write_bytes(image)

        raw_parameters = body.get("parameters")
        parameters: list[dict[str, Any]] = []
        for raw in raw_parameters[:512] if isinstance(raw_parameters, list) else []:
            if not isinstance(raw, dict):
                continue
            parameter_id = str(raw.get("id") or "").strip()[:120]
            if not parameter_id:
                continue
            parameters.append({
                "id": parameter_id,
                "index": clamp_int(raw.get("index"), 0, 2048, len(parameters)),
                "value": clamp_float(raw.get("value"), -10000, 10000, 0),
                "baselineValue": clamp_float(raw.get("baselineValue"), -10000, 10000, 0),
                "normalizedDelta": clamp_float(raw.get("normalizedDelta"), -10, 10, 0),
                "minimum": clamp_float(raw.get("minimum"), -10000, 10000, -1),
                "maximum": clamp_float(raw.get("maximum"), -10000, 10000, 1),
                "defaultValue": clamp_float(raw.get("defaultValue"), -10000, 10000, 0),
                "name": str(raw.get("name") or "").strip()[:120],
                "authorName": str(raw.get("authorName") or "").strip()[:120],
                "known": bool(raw.get("known")),
                "foundation": bool(raw.get("foundation")),
            })

        metadata = {
            "schemaVersion": 1,
            "createdAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "label": label,
            "userEmotionLabel": str(body.get("userEmotionLabel") or label).strip()[:120],
            "calibrationMode": str(body.get("calibrationMode") or "").strip()[:80],
            "presetLayersDisabled": bool(body.get("presetLayersDisabled")),
            "fileName": file_name,
            "emotion": str(body.get("emotion") or "")[:80],
            "foregroundEmotion": str(body.get("foregroundEmotion") or "")[:80],
            "emotionIntensity": clamp_float(body.get("emotionIntensity"), 0, 3, 1),
            "modelId": str(body.get("modelId") or "")[:120],
            "parameters": parameters,
            "compositionParameterIds": unique_strings([
                str(item).strip()
                for item in body.get("compositionParameterIds", [])
                if str(item).strip()
            ])[:256] if isinstance(body.get("compositionParameterIds"), list) else [],
            "baselineParameters": {
                str(key)[:120]: clamp_float(value, -10000, 10000, 0)
                for key, value in list((body.get("baselineParameters") or {}).items())[:512]
            } if isinstance(body.get("baselineParameters"), dict) else {},
            "manualOverrides": {
                str(key)[:120]: clamp_float(value, -10000, 10000, 0)
                for key, value in list((body.get("manualOverrides") or {}).items())[:512]
            } if isinstance(body.get("manualOverrides"), dict) else {},
        }
        metadata_path = file_path.with_suffix(".json")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ok": True,
            "fileName": file_name,
            "filePath": str(file_path),
            "metadataPath": str(metadata_path),
            "parameterCount": len(parameters),
        }
