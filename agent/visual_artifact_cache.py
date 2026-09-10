"""One saved visual reference across uploads, screen captures and camera frames.

These adapters preserve acquisition identity. Reusing an understanding is not a
new capture, and a tool receipt must retain its original source domain.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from agent.artifact_architecture import ImageArtifact


VISUAL_SOURCES = {
    "read_image": ("uploaded_attachment", "uploaded_image", "image/unknown"),
    "capture_screen": ("screen_capture", "screen", "image/png"),
    "capture_camera": ("camera_capture", "physical_user", "image/jpeg"),
}
DESCRIPTOR_FIELDS = ("id", "name", "mimeType", "kind", "sourceType", "targetDomain", "capturedAt")


def visual_artifact_descriptor(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in DESCRIPTOR_FIELDS if key in value}


def compact_visual_artifact(result: Mapping[str, Any]) -> dict[str, Any] | None:
    tool_name = str(result.get("name") or "")
    if tool_name not in VISUAL_SOURCES or not result.get("ok"):
        return None
    source, domain, mime = VISUAL_SOURCES[tool_name]
    artifact = result.get("imageArtifact") or {}
    identity = str(artifact.get("artifactId") or result.get("captureId") or result.get("attachmentId") or "")
    if not identity:
        return None
    vision = result.get("vision")
    if not isinstance(vision, Mapping):
        vision = {
            "available": bool(result.get("visualCaption") or result.get("visualOcrText")),
            "caption": result.get("visualCaption", ""),
            "ocrText": result.get("visualOcrText", ""),
            "regions": result.get("visualRegions", []),
            "visualTiles": result.get("visualTiles", []),
            "ocrItems": result.get("visualOcrItems", []),
            "model": result.get("visionModel", ""),
            "directImageInput": bool(result.get("directImageInput")),
            "uncertainItems": result.get("visualUncertainItems") or [],
        }
    return {
        "id": identity, "name": str(result.get("fileName") or identity),
        "kind": "image", "mimeType": str(artifact.get("mimeType") or mime),
        "sourceType": source, "targetDomain": domain, "toolName": tool_name,
        "capturedAt": str(artifact.get("acquiredAt") or result.get("capturedAt") or ""),
        "path": str(result.get("path") or artifact.get("path") or ""),
        "width": result.get("width", artifact.get("width", 0)),
        "height": result.get("height", artifact.get("height", 0)),
        "foregroundWindowTitle": str(result.get("foregroundWindowTitle") or ""),
        "selfExcluded": result.get("selfExcluded", artifact.get("selfExcluded")),
        "vision": deepcopy(dict(vision)),
    }


def visual_artifacts_from_result(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    receipts = result.get("_conversationToolResults")
    if not isinstance(receipts, list):
        receipts = result.get("toolResults") or []
    artifacts: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        item = compact_visual_artifact(receipt) if isinstance(receipt, Mapping) else None
        if item:
            artifacts[item["id"]] = item
    return list(artifacts.values())


def cached_visual_tool_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Rehydrate a sensor observation, not permission to capture another frame."""
    tool_name = str(value["toolName"])
    source, domain, mime = VISUAL_SOURCES[tool_name]
    vision = deepcopy(dict(value.get("vision") or {}))
    artifact = ImageArtifact(
        artifact_id=str(value["id"]), source_type=source, target_domain=domain,
        freshness="historical", acquired_at=str(value.get("capturedAt") or ""),
        path=str(value.get("path") or ""), mime_type=str(value.get("mimeType") or mime),
        width=int(value.get("width") or 0), height=int(value.get("height") or 0),
        self_excluded=value.get("selfExcluded"),
    ).with_understanding(vision, completed_at=str(value.get("understandingUpdatedAt") or value.get("capturedAt") or ""))
    return {
        "name": tool_name, "ok": True, "captureId": value["id"],
        "capturedAt": value.get("capturedAt", ""), "path": value.get("path", ""),
        "width": value.get("width", 0), "height": value.get("height", 0),
        "foregroundWindowTitle": value.get("foregroundWindowTitle", ""),
        "selfExcluded": value.get("selfExcluded"), "vision": vision,
        "imageArtifact": artifact.to_dict(), "cacheHit": True,
        "visionRequestMade": False, "reusedArtifact": True,
        "message": "沿用已保存画面的理解；没有重新截图或拍摄，不代表当前实时画面。",
    }
