"""屏幕与相机采集子进程适配器；不授予权限，也不调用视觉模型。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable

from dataclasses import dataclass
import json
import subprocess
import sys
from .capture_system import subprocess_error_message

@dataclass(frozen=True)
class CaptureHelperPorts:
    settings: Callable[[], dict[str, Any]]
    local_path: Callable[[Any], Path]
    environment: Callable[[], dict[str, str]]
    root: Callable[[], Path]
    screen_script: Callable[[], Path]
    camera_script: Callable[[], Path]

class CaptureHelpers:
    def __init__(self, ports: CaptureHelperPorts) -> None:
        self.ports = ports

    def capture_screen_to_file(self, output: Path) -> dict[str, Any]:
        settings = self.ports.settings()
        vision_python = self.ports.local_path(settings.get("visionCondaEnv")) / "python.exe"
        python_path = vision_python if vision_python.exists() else Path(sys.executable)
        if python_path.name.lower() == "pythonw.exe" and python_path.with_name("python.exe").exists():
            python_path = python_path.with_name("python.exe")
        completed = subprocess.run(
            [str(python_path), "-B", "-X", "utf8", str(self.ports.screen_script()), "--output", str(output)],
            cwd=str(self.ports.root()),
            env=self.ports.environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise RuntimeError(subprocess_error_message(completed.stderr, "screen capture helper failed"))
        metadata = json.loads(completed.stdout.strip().splitlines()[-1])
        if not metadata.get("ok"):
            raise RuntimeError(str(metadata.get("error") or "screen capture helper failed"))
        if max(metadata.get("stddev") or [0]) < 0.8 and max(metadata.get("mean") or [0]) < 3.0:
            raise RuntimeError("screen capture returned an all-black frame")
        return metadata


    def capture_camera_to_file(self, output: Path, camera_index: int) -> dict[str, Any]:
        settings = self.ports.settings()
        vision_python = self.ports.local_path(settings.get("visionCondaEnv")) / "python.exe"
        python_path = vision_python if vision_python.exists() else Path(sys.executable)
        if python_path.name.lower() == "pythonw.exe" and python_path.with_name("python.exe").exists():
            python_path = python_path.with_name("python.exe")
        completed = subprocess.run(
            [
                str(python_path), "-B", "-X", "utf8", str(self.ports.camera_script()),
                "--output", str(output), "--index", str(camera_index),
            ],
            cwd=str(self.ports.root()),
            env=self.ports.environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise RuntimeError(subprocess_error_message(completed.stderr, "camera capture helper failed"))
        metadata = json.loads(completed.stdout.strip().splitlines()[-1])
        if not metadata.get("ok"):
            raise RuntimeError(str(metadata.get("error") or "camera capture helper failed"))
        return metadata
