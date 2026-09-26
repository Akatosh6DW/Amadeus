"""有界数值转换，不读取运行时状态。"""
from __future__ import annotations
from typing import Any


def clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return fallback


def clamp_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return fallback


def safe_nonnegative_int(value: Any) -> int:
    """把缺失或无效的计数归零，不引入上限或额外的类型强制。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
