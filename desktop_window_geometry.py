"""Window size policy and anchored edge dragging (logical pixels)."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class WindowLimits:
    min_width: int
    min_height: int
    min_ratio: float
    max_ratio: float
    max_width: int = 2200
    max_height: int = 2200


def window_limits(mode: str) -> WindowLimits:
    return WindowLimits(1040, 720, 1.15, 1.90) if mode == 'full' else WindowLimits(360, 520, 0.55, 0.95)


def normalize_window_size(width, height, mode='lite', *, limits=None):
    limits = limits or window_limits(mode)
    height = min(max(round(height), limits.min_height), limits.max_height,
                 math.floor(limits.max_width / limits.min_ratio))
    height = max(height, math.ceil(limits.min_width / limits.max_ratio))
    low = max(limits.min_width, math.ceil(height * limits.min_ratio))
    high = min(limits.max_width, math.floor(height * limits.max_ratio))
    return min(max(round(width), low), high), height


def resize_from_edge(rect, edge, dx, dy, limits):
    """Clamp before moving; the edge opposite the cursor remains stationary."""
    if edge not in {'n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'}:
        raise ValueError('invalid_resize_edge')
    x, y, width, height = rect
    candidate_w = width + (dx if 'e' in edge else -dx if 'w' in edge else 0)
    candidate_h = height + (dy if 's' in edge else -dy if 'n' in edge else 0)
    if edge in {'e', 'w'}:
        low = max(limits.min_width, math.ceil(height * limits.min_ratio))
        high = min(limits.max_width, math.floor(height * limits.max_ratio))
        new_w, new_h = min(max(round(candidate_w), low), high), height
    elif edge in {'n', 's'}:
        low = max(limits.min_height, math.ceil(width / limits.max_ratio))
        high = min(limits.max_height, math.floor(width / limits.min_ratio))
        new_w, new_h = width, min(max(round(candidate_h), low), high)
    else:
        new_w, new_h = normalize_window_size(candidate_w, candidate_h, limits=limits)
    return (x + width - new_w if 'w' in edge else x,
            y + height - new_h if 'n' in edge else y, new_w, new_h)
