"""模型覆盖参数的范围、白名单和截图文件标签规则。"""
from __future__ import annotations
from typing import Any
import re
from backend.common.values import clamp_float, clamp_int
from backend.common.text import unique_strings

def normalize_live2d_model_override(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    if "scaleMultiplier" in source:
        result["scaleMultiplier"] = clamp_float(source.get("scaleMultiplier"), 0.5, 2.5, 1.0)
    if "fullScaleMultiplier" in source:
        result["fullScaleMultiplier"] = clamp_float(source.get("fullScaleMultiplier"), 0.5, 2.5, 1.0)
    for key in ("offsetX", "offsetY"):
        if key in source:
            result[key] = clamp_float(source.get(key), -1200, 1200, 0)
    if "supportsNativeExpressions" in source:
        result["supportsNativeExpressions"] = bool(source.get("supportsNativeExpressions"))
    if "interactionSummary" in source:
        result["interactionSummary"] = str(source.get("interactionSummary") or "").strip()[:600]

    lip_sync = source.get("lipSync")
    if isinstance(lip_sync, dict):
        result["lipSync"] = {
            "openMultiplier": clamp_float(lip_sync.get("openMultiplier"), 0.5, 2.2, 1.2),
            "formMultiplier": clamp_float(lip_sync.get("formMultiplier"), 0.4, 1.8, 1.12),
            "formOffset": clamp_float(lip_sync.get("formOffset"), -0.5, 0.5, 0),
            "openSmoothing": clamp_float(lip_sync.get("openSmoothing"), 0.08, 0.8, 0.5),
            "formSmoothing": clamp_float(lip_sync.get("formSmoothing"), 0.08, 0.8, 0.34),
        }

    eye_visibility = source.get("eyeVisibilityCompensation")
    if isinstance(eye_visibility, dict):
        raw_families = eye_visibility.get("families")
        families: dict[str, list[list[float]]] = {}
        if isinstance(raw_families, dict):
            for family, raw_points in list(raw_families.items())[:12]:
                family_name = str(family).strip()
                if not family_name or not isinstance(raw_points, list):
                    continue
                points: list[list[float]] = []
                for raw_point in raw_points[:12]:
                    if not isinstance(raw_point, list) or len(raw_point) < 3:
                        continue
                    points.append([
                        clamp_float(raw_point[0], 0, 1, 0),
                        clamp_float(raw_point[1], 0, 1, 1),
                        clamp_float(raw_point[2], -0.35, 0.35, 0),
                        clamp_float(raw_point[3] if len(raw_point) >= 4 else 0, -0.25, 0.1, 0),
                    ])
                if len(points) >= 2:
                    families[family_name] = sorted(points, key=lambda item: item[0])
        if families:
            result["eyeVisibilityCompensation"] = {
                "sustainedDelayMs": clamp_int(
                    eye_visibility.get("sustainedDelayMs"),
                    80,
                    1200,
                    240,
                ),
                "minimumVisibleOpening": clamp_float(
                    eye_visibility.get("minimumVisibleOpening"),
                    0.32,
                    0.62,
                    0.42,
                ),
                "closeThreshold": clamp_float(
                    eye_visibility.get("closeThreshold"),
                    0.16,
                    0.46,
                    0.32,
                ),
                "families": families,
            }

    head_turn = source.get("headTurn")
    if isinstance(head_turn, dict):
        result["headTurn"] = {
            "maxYaw": clamp_float(head_turn.get("maxYaw"), 24, 90, 30),
            "auxiliaryMax": clamp_float(head_turn.get("auxiliaryMax"), 0, 32, 0),
            "bodyMax": clamp_float(head_turn.get("bodyMax"), 0, 5, 2),
            "pointerSwitchThreshold": clamp_float(
                head_turn.get("pointerSwitchThreshold"),
                0.12,
                0.65,
                0.3,
            ),
            "springFrequency": clamp_float(head_turn.get("springFrequency"), 1.5, 8, 4.8),
            "springDamping": clamp_float(head_turn.get("springDamping"), 0.55, 1.25, 0.88),
        }

    display_bounds = source.get("displayBounds")
    if isinstance(display_bounds, list) and len(display_bounds) == 4:
        x = clamp_float(display_bounds[0], 0, 0.99, 0)
        y = clamp_float(display_bounds[1], 0, 0.99, 0)
        width = clamp_float(display_bounds[2], 0.01, 1 - x, 1 - x)
        height = clamp_float(display_bounds[3], 0.01, 1 - y, 1 - y)
        result["displayBounds"] = [x, y, width, height]

    for key in ("hiddenParameters", "hiddenParts"):
        if key in source:
            raw_items = source.get(key) if isinstance(source.get(key), list) else []
            result[key] = unique_strings([str(item) for item in raw_items])[:128]

    fixed_parameters = source.get("fixedParameters")
    if isinstance(fixed_parameters, dict):
        result["fixedParameters"] = {
            str(name).strip(): clamp_float(raw, -10, 10, 0)
            for name, raw in list(fixed_parameters.items())[:128]
            if str(name).strip()
        }

    parameter_aliases = source.get("parameterAliases")
    if isinstance(parameter_aliases, dict):
        result["parameterAliases"] = {
            str(name).strip(): unique_strings([str(item) for item in raw])[:32]
            for name, raw in list(parameter_aliases.items())[:64]
            if str(name).strip() and isinstance(raw, list)
        }

    motion_map = source.get("motionMap")
    if isinstance(motion_map, dict):
        result["motionMap"] = {
            str(name).strip(): str(group).strip()
            for name, group in list(motion_map.items())[:64]
            if str(name).strip() and str(group).strip()
        }

    expression_map = source.get("expressionMap")
    if isinstance(expression_map, dict):
        result["expressionMap"] = {
            str(name).strip(): str(expression).strip()
            for name, expression in list(expression_map.items())[:64]
            if str(name).strip()
        }
    return result


def safe_live2d_capture_label(value: Any) -> str:
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    label = re.sub(r"\s+", "_", label).strip(" ._")
    return label[:80]
