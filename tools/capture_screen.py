#!/usr/bin/env python3
"""Capture all displays from a standalone process to avoid WebView capture conflicts."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image, ImageGrab, ImageStat


def is_amadeus_window_title(title: str) -> bool:
    """Recognize current and legacy Amadeus System native windows."""

    value = " ".join(str(title or "").split()).casefold()
    return (
        value in {"amadeus system", "amadeus system dev", "amadeus system test"}
        or value == "amadeus live2d"
        or value.startswith("amadeus live2d ")
    )


def foreground_window_title() -> str:
    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        if not hwnd or length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def hide_amadeus_windows() -> tuple[list[dict[str, object]], list[str]]:
    """Temporarily hide this app so the screenshot sees the window beneath it."""

    if os.name != "nt":
        return [], []
    hidden: list[dict[str, object]] = []
    failures: list[str] = []
    try:
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL

        @callback_type
        def visit(hwnd: int, _lparam: int) -> bool:
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value
                if not is_amadeus_window_title(title):
                    return True
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                restore_command = 7 if user32.IsIconic(hwnd) else 3 if user32.IsZoomed(hwnd) else 4
                if not user32.ShowWindowAsync(hwnd, 0):  # SW_HIDE
                    failures.append(f"ShowWindowAsync(SW_HIDE) failed for {title}")
                    return True
                hidden.append({
                    "handle": int(hwnd),
                    "title": title,
                    "rect": [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)],
                    "restoreCommand": restore_command,
                })
            except Exception as error:
                failures.append(f"{type(error).__name__}: {error}")
            return True

        user32.EnumWindows(visit, 0)
        if hidden:
            try:
                ctypes.windll.dwmapi.DwmFlush()
            except Exception:
                pass
            time.sleep(0.16)
    except Exception as error:
        failures.append(f"{type(error).__name__}: {error}")
    return hidden, failures


def restore_amadeus_windows(hidden: list[dict[str, object]]) -> list[str]:
    failures: list[str] = []
    if os.name != "nt":
        return failures
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    for item in hidden:
        try:
            hwnd = int(item.get("handle") or 0)
            command = int(item.get("restoreCommand") or 4)
            if hwnd and user32.IsWindow(hwnd) and not user32.ShowWindowAsync(hwnd, command):
                failures.append(f"ShowWindowAsync(restore) failed for {item.get('title')}")
        except Exception as error:
            failures.append(f"{type(error).__name__}: {error}")
    return failures


def capture_with_mss() -> Image.Image:
    import mss

    capture_class = getattr(mss, "MSS", mss.mss)
    with capture_class() as capture:
        shot = capture.grab(capture.monitors[0])
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def capture_with_dxcam() -> Image.Image:
    import dxcam

    with dxcam.create(output_color="RGB", processor_backend="numpy") as camera:
        frame = camera.grab(new_frame_only=False)
    if frame is None:
        raise RuntimeError("dxcam returned no frame")
    return Image.fromarray(frame, mode="RGB")


def image_stats(image: Image.Image) -> ImageStat.Stat:
    return ImageStat.Stat(image.convert("RGB").resize((64, 64)))


def validate_frame(image: Image.Image, backend: str) -> ImageStat.Stat:
    stats = image_stats(image)
    if max(stats.stddev) < 0.8 and max(stats.mean) < 3.0:
        raise RuntimeError(f"{backend} returned an all-black frame")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    hidden, exclusion_failures = hide_amadeus_windows()
    captured_foreground_title = foreground_window_title()
    try:
        candidates = (
            ("dxcam", capture_with_dxcam),
            ("mss", capture_with_mss),
            ("pillow-imagegrab", lambda: ImageGrab.grab(all_screens=True)),
        )
        failures: list[str] = []
        image: Image.Image | None = None
        stats: ImageStat.Stat | None = None
        backend = ""
        for candidate, capture in candidates:
            try:
                candidate_image = capture()
                candidate_stats = validate_frame(candidate_image, candidate)
                image, stats, backend = candidate_image, candidate_stats, candidate
                break
            except Exception as error:
                failures.append(f"{candidate}: {type(error).__name__}: {error}")
    finally:
        exclusion_failures.extend(restore_amadeus_windows(hidden))
    if image is None or stats is None:
        raise RuntimeError("screen capture failed; " + " | ".join(failures))
    image.save(output)
    print(json.dumps({
        "ok": True,
        "path": str(output),
        "width": image.width,
        "height": image.height,
        "backend": backend,
        "foregroundWindowTitle": captured_foreground_title,
        "selfWindowPolicy": "hidden_before_capture" if os.name == "nt" else "not_applicable",
        "selfExcluded": bool(hidden),
        "excludedWindows": [
            {key: value for key, value in item.items() if key != "restoreCommand"}
            for item in hidden
        ],
        "selfExclusionWarnings": exclusion_failures,
        "mean": [round(value, 3) for value in stats.mean],
        "stddev": [round(value, 3) for value in stats.stddev],
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
        }, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
