"""Explicit frameless resize bridge; never resize through WinForms properties."""
import ctypes
from ctypes import wintypes
import threading
import uuid

from desktop_window_geometry import window_limits, normalize_window_size, resize_from_edge


def native_rect(handle):
    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowRect.restype = wintypes.BOOL
    rect = wintypes.RECT()
    if not user32.GetWindowRect(handle, ctypes.byref(rect)):
        raise ctypes.WinError()
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    scale = (user32.GetDpiForWindow(handle) or 96) / 96
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top), scale


def set_native_rect(handle, rect):
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    if not user32.SetWindowPos(handle, None, *map(round, rect), 0x0004 | 0x0010):
        raise ctypes.WinError()


class WindowResizeController:
    def __init__(self):
        self.lock = threading.Lock()
        self.session = None

    def begin(self, handle, edge, mode, screen_x, screen_y):
        if edge not in {'n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'}:
            raise ValueError('invalid_resize_edge')
        with self.lock:
            physical, scale = native_rect(handle)
            x, y, width, height = (v / scale for v in physical)
            width, height = normalize_window_size(width, height, mode)
            set_native_rect(handle, (physical[0], physical[1], width * scale, height * scale))
            token = uuid.uuid4().hex
            self.session = {'token': token, 'handle': handle, 'scale': scale,
                            'rect': (x, y, width, height), 'edge': edge,
                            'pointer': (float(screen_x), float(screen_y)), 'limits': window_limits(mode)}
            return {'ok': True, 'token': token, 'width': width, 'height': height}

    def update(self, token, screen_x, screen_y, finish=False):
        with self.lock:
            state = self.session
            if not state or state['token'] != token:
                return {'ok': False, 'error': 'resize_session_expired'}
            dx = float(screen_x) - state['pointer'][0]
            dy = float(screen_y) - state['pointer'][1]
            rect = resize_from_edge(state['rect'], state['edge'], dx, dy, state['limits'])
            set_native_rect(state['handle'], tuple(v * state['scale'] for v in rect))
            if finish:
                self.session = None
            return {'ok': True, 'width': rect[2], 'height': rect[3]}
