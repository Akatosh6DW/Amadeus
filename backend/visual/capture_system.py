"""调用屏幕/相机采集辅助程序，统一隐藏子进程与输出错误解析。"""
from __future__ import annotations

import json

def foreground_window_title() -> str:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def subprocess_error_message(stderr: str, fallback: str) -> str:
    value = str(stderr or "").strip()
    if not value:
        return fallback
    last_line = value.splitlines()[-1]
    try:
        payload = json.loads(last_line)
    except json.JSONDecodeError:
        return last_line[:1000]
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])[:1000]
    return last_line[:1000]
