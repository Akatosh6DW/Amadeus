"""原生窗口操作：句柄查找、DPI 坐标换算、缩放与置顶。

不导入或启动桌面入口；Win32 依赖在操作时读取，便于独立测试。
"""
from __future__ import annotations

import os
import sys
import time

from desktop_window_resize import native_rect, set_native_rect


def native_window_handle(window) -> int:
    """取得本进程顶层窗口句柄，避免 JS 桥线程跨线程读取 WinForms 属性。"""

    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    def top_level(handle: int) -> int:
        if not handle or not user32.IsWindow(handle):
            return 0
        root = int(user32.GetAncestor(handle, 2) or 0)  # GA_ROOT
        return root if root and user32.IsWindow(root) else int(handle)

    try:
        gui = getattr(window, "gui", None)
        native = gui.BrowserView.instances.get(window.uid) if gui and hasattr(gui, "BrowserView") else None
        handle = int(native.Handle.ToInt64()) if native is not None else 0
        handle = top_level(handle)
        if handle:
            return handle
    except Exception:
        pass

    # 部分 pywebview/WinForms 版本不暴露 BrowserView。回退枚举仅检查本进程，
    # 优先精确匹配标题，避免错误操作其他程序的窗口。
    candidates: list[tuple[int, str]] = []
    process_id = os.getpid()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd, _lparam):
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        if owner_pid.value != process_id or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        candidates.append((top_level(int(hwnd)), buffer.value))
        return True

    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(collect, 0)
    title = str(getattr(window, "title", "") or "")
    exact = next((handle for handle, value in candidates if value == title), 0)
    if exact:
        return exact
    return next((handle for handle, value in candidates
                 if value.casefold() in {"amadeus system", "amadeus system dev", "amadeus system test"}
                 or "amadeus live2d" in value.casefold()), 0)


def window_logical_position(window) -> tuple[int, int]:
    """将 Win32 屏幕位置换算为 pywebview 使用的逻辑像素。"""

    if sys.platform != "win32":
        return int(getattr(window, "x", 0) or 0), int(getattr(window, "y", 0) or 0)
    import ctypes
    from ctypes import wintypes

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long), ("top", ctypes.c_long),
            ("right", ctypes.c_long), ("bottom", ctypes.c_long),
        ]

    handle = native_window_handle(window)
    if not handle:
        raise RuntimeError("native_window_unavailable")
    user32 = ctypes.windll.user32
    # ctypes 将签名保存在共享 DLL 对象中。输出参数使用通用指针，
    # 避免局部 RECT 类型拒绝同进程其他合法的 RECT 调用方。
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowRect.restype = wintypes.BOOL
    rect = Rect()
    if not user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect)):
        raise ctypes.WinError()
    try:
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        dpi = int(user32.GetDpiForWindow(wintypes.HWND(handle)) or 96)
    except (AttributeError, OSError):
        dpi = 96
    scale = max(0.5, dpi / 96.0)
    return round(rect.left / scale), round(rect.top / scale)


def move_native_window(window, x: int, y: int) -> int:
    """以逻辑像素移动顶层窗口；Windows 直接操作 HWND，避免跨 UI 线程。"""

    if sys.platform != "win32":
        window.move(int(x), int(y))
        return 0
    import ctypes
    from ctypes import wintypes

    handle = native_window_handle(window)
    if not handle:
        raise RuntimeError("native_window_unavailable")
    user32 = ctypes.windll.user32
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    dpi = int(user32.GetDpiForWindow(wintypes.HWND(handle)) or 96)
    scale = max(0.5, dpi / 96.0)
    flags = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE
    if not user32.SetWindowPos(
        wintypes.HWND(handle), None, round(int(x) * scale), round(int(y) * scale), 0, 0, flags
    ):
        raise ctypes.WinError()
    return handle


def resize_native_window(window, width: int, height: int) -> None:
    """按当前 DPI 改变窗口大小，保留原来的屏幕位置。"""
    if sys.platform != "win32":
        window.resize(width, height)
        return
    handle = native_window_handle(window)
    if not handle:
        raise RuntimeError("native_window_unavailable")
    rect, scale = native_rect(handle)
    set_native_rect(handle, (rect[0], rect[1], width * scale, height * scale))


def set_window_topmost(window, enabled: bool) -> int:
    """通过 Win32 设置并核验置顶；桥线程调用 WinForms.TopMost 可能死锁。"""

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        handle = native_window_handle(window)
        if not handle:
            raise RuntimeError("native_window_unavailable")
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        hwnd_insert_after = wintypes.HWND(-1 if enabled else -2)  # HWND_TOPMOST / HWND_NOTOPMOST
        flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
        if not user32.SetWindowPos(handle, hwnd_insert_after, 0, 0, 0, 0, flags):
            raise ctypes.WinError()
        actual = bool(user32.GetWindowLongW(handle, -20) & 0x00000008)  # GWL_EXSTYLE / WS_EX_TOPMOST
        if actual != bool(enabled):
            raise RuntimeError(f"topmost_state_mismatch: requested={bool(enabled)} actual={actual}")
        # 同步 pywebview 缓存，但不调用会跨 UI 线程的 setter。
        setattr(window, "_Window__on_top", bool(enabled))
        return handle
    window.on_top = bool(enabled)
    return 0


def set_window_topmost_with_retry(window, enabled: bool, attempts: int = 12) -> int:
    """窗口句柄尚未就绪时有限重试，耗尽后保留最后的失败原因。"""
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return set_window_topmost(window, enabled)
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.06 + attempt * 0.015)
    raise last_error or RuntimeError("topmost_apply_failed")
