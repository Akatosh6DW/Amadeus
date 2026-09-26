"""屏幕/相机工具：先检查现有权限，再采集、理解并生成可追溯图像回执。

每次采集使用独立 artifact 路径；屏幕 latest 副本原子替换，相机只保留既定缓存数量。
权限拒绝不会调用采集或模型，底层采集器和图片理解通过明确端口注入。
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable

from dataclasses import dataclass
from datetime import datetime
import os
import shutil
import uuid
from agent.artifact_architecture import ImageArtifact
from backend.common.values import clamp_int
from backend.config.media_limits import SCREEN_CAPTURE_HISTORY_LIMIT

@dataclass(frozen=True)
class SensorToolPorts:
    data_dir: Callable[[], Path]
    capture_screen: Callable[..., dict[str, Any]]
    capture_camera: Callable[..., dict[str, Any]]
    read_image: Callable[..., dict[str, Any]]
    window_title: Callable[[], str]
    now: Callable[[], str]

class SensorTools:
    def __init__(self, ports: SensorToolPorts) -> None:
        self.ports = ports

    def screen_capture_storage_paths(self, ) -> tuple[Path, Path]:
        capture_dir = (self.ports.data_dir() / "runtime" / "screen-captures").resolve()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        capture_path = capture_dir / f"screen_{timestamp}_{uuid.uuid4().hex[:8]}.png"
        latest_path = (self.ports.data_dir() / "runtime" / "screen_latest.png").resolve()
        return capture_path, latest_path


    def prune_screen_capture_history(
        self,
        capture_dir: Path, limit: int = SCREEN_CAPTURE_HISTORY_LIMIT, *, pattern: str = "screen_*.png",
    ) -> None:
        resolved_dir = capture_dir.resolve()
        files = sorted(
            (
                item for item in resolved_dir.glob(pattern)
                if item.is_file() and item.resolve().parent == resolved_dir
            ),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
        for item in files[max(1, int(limit)):]:
            item.unlink(missing_ok=True)


    def tool_capture_screen(self, settings: dict[str, Any], user_question: str = "") -> dict[str, Any]:
        if not settings.get("permissionReadScreen"):
            return {
                "name": "capture_screen",
                "ok": False,
                "error": "permission_denied",
                "message": "屏幕读取权限未授予，不能截图或读取屏幕。",
            }
        output, latest_output = self.screen_capture_storage_paths()
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            capture = self.ports.capture_screen(output)
            latest_output.parent.mkdir(parents=True, exist_ok=True)
            temporary_latest = latest_output.with_name(f".screen_latest-{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(output, temporary_latest)
                os.replace(temporary_latest, latest_output)
            finally:
                temporary_latest.unlink(missing_ok=True)
            self.prune_screen_capture_history(output.parent)
            captured_at = self.ports.now()
            vision = self.ports.read_image(output, settings, user_question)
            artifact = ImageArtifact(
                artifact_id=output.stem,
                source_type="screen_capture",
                target_domain="screen",
                freshness="current",
                acquired_at=captured_at,
                path=str(output),
                mime_type="image/png",
                width=int(capture.get("width") or 0),
                height=int(capture.get("height") or 0),
                self_excluded=bool(capture.get("selfExcluded")),
            ).with_understanding(vision, completed_at=self.ports.now())
            return {
                "name": "capture_screen",
                "ok": True,
                "captureId": output.stem,
                "capturedAt": captured_at,
                "path": str(output),
                "latestPath": str(latest_output),
                "historyDirectory": str(output.parent),
                "historyLimit": SCREEN_CAPTURE_HISTORY_LIMIT,
                "width": capture.get("width"),
                "height": capture.get("height"),
                "foregroundWindowTitle": str(capture.get("foregroundWindowTitle") or self.ports.window_title()),
                "selfWindowPolicy": str(capture.get("selfWindowPolicy") or "unknown"),
                "selfExcluded": bool(capture.get("selfExcluded")),
                "excludedWindows": capture.get("excludedWindows") if isinstance(capture.get("excludedWindows"), list) else [],
                "selfExclusionWarnings": (
                    capture.get("selfExclusionWarnings")
                    if isinstance(capture.get("selfExclusionWarnings"), list)
                    else []
                ),
                "vision": vision,
                "imageArtifact": artifact.to_dict(),
            }
        except Exception as error:
            return {
                "name": "capture_screen",
                "ok": False,
                "error": str(error),
                "foregroundWindowTitle": self.ports.window_title(),
            }


    def tool_capture_camera(self, settings: dict[str, Any], user_question: str = "") -> dict[str, Any]:
        if not settings.get("permissionReadCamera"):
            return {
                "name": "capture_camera",
                "ok": False,
                "error": "permission_denied",
                "message": "摄像头读取权限未授予，不能拍摄或识别摄像头画面。",
            }
        camera_index = clamp_int(settings.get("cameraIndex"), 0, 8, 0)
        # Never overwrite bytes referred to by a previous artifact ID. Only two
        # recent camera frames are retained; this is a cache, not a photo archive.
        output = (self.ports.data_dir() / "runtime" / "camera-captures" / f"camera_{uuid.uuid4().hex}.jpg").resolve()
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            capture = self.ports.capture_camera(output, camera_index)
            self.prune_screen_capture_history(output.parent, limit=2, pattern="camera_*.jpg")
            captured_at = self.ports.now()
            vision = self.ports.read_image(output, settings, user_question)
            artifact = ImageArtifact(
                artifact_id=output.stem,
                source_type="camera_capture",
                target_domain="physical_user",
                freshness="current",
                acquired_at=captured_at,
                path=str(output),
                mime_type="image/jpeg",
                width=int(capture.get("width") or 0),
                height=int(capture.get("height") or 0),
            ).with_understanding(vision, completed_at=self.ports.now())
            return {
                "name": "capture_camera",
                "ok": True,
                "captureId": output.stem,
                "path": str(output),
                "cameraIndex": camera_index,
                "backend": capture.get("backend"),
                "width": capture.get("width"),
                "height": capture.get("height"),
                "capturedAt": captured_at,
                "vision": vision,
                "imageArtifact": artifact.to_dict(),
            }
        except Exception as error:
            return {
                "name": "capture_camera",
                "ok": False,
                "cameraIndex": camera_index,
                "error": str(error),
            }
