"""同一资料目录的跨进程互斥锁；创建者传入作用域，关闭者负责释放句柄。"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from agent.fingerprints import runtime_fingerprint

class RuntimeProcessLock:
    """Prevent two backend processes from mutating the same data profile."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        digest = runtime_fingerprint(str(self.data_dir).casefold())
        self.name = f"AmadeusRuntime-{digest}"
        self.handle: Any = None
        self.file_handle: Any = None

    def acquire(self) -> None:
        # 同一数据目录默认只允许一个后端；显式开启并行时才跳过实例锁。
        if os.environ.get("AMADEUS_ALLOW_PARALLEL_RUNTIME") == "1":
            return
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            handle = kernel32.CreateMutexW(None, True, f"Local\\{self.name}")
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                raise RuntimeError(
                    "已有一个 Amadeus System 后端正在使用同一份 data 目录。"
                    "桌面窗口会自动复用 5174，请不要同时运行 server.py。"
                )
            self.handle = handle
            return

        import fcntl

        lock_path = self.data_dir / ".runtime.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        file_handle = lock_path.open("a+b")
        try:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            file_handle.close()
            raise RuntimeError("已有一个 Amadeus System 后端正在使用同一份 data 目录。") from error
        self.file_handle = file_handle

    def release(self) -> None:
        if self.handle is not None and os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel32.ReleaseMutex.restype = ctypes.c_bool
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.ReleaseMutex(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle = None
        if self.file_handle is not None:
            try:
                import fcntl

                fcntl.flock(self.file_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.file_handle.close()
                self.file_handle = None

    def __enter__(self) -> "RuntimeProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()

