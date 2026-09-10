"""Immutable visual artifact identity separated from model understanding."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping


@dataclass(frozen=True)
class ImageUnderstanding:
    status: str = "not_requested"
    model: str = ""
    completed_at: str = ""
    caption: str = ""
    ocr_text: str = ""
    regions: tuple[Mapping[str, Any], ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "model": self.model,
            "completedAt": self.completed_at,
            "caption": self.caption,
            "ocrText": self.ocr_text,
            "regions": [dict(item) for item in self.regions],
            "error": self.error,
        }


@dataclass(frozen=True)
class ImageArtifact:
    artifact_id: str
    source_type: str
    target_domain: str
    freshness: str
    acquired_at: str
    path: str = ""
    mime_type: str = "image/unknown"
    width: int = 0
    height: int = 0
    content_hash: str = ""
    self_excluded: bool | None = None
    acquisition_status: str = "captured"
    understanding: ImageUnderstanding = ImageUnderstanding()

    def with_understanding(
        self,
        result: Mapping[str, Any],
        *,
        completed_at: str,
        model: str = "",
    ) -> "ImageArtifact":
        available = bool(result.get("available", result.get("ok", False)))
        understanding = ImageUnderstanding(
            status="not_requested" if result.get("directImageInput") else "completed" if available else "failed",
            model=str(result.get("model") or model or ""),
            completed_at="" if result.get("directImageInput") else str(completed_at or ""),
            caption=str(result.get("caption") or "")[:8000],
            ocr_text=str(result.get("ocrText") or result.get("text") or "")[:8000],
            regions=tuple(
                dict(item)
                for item in result.get("regions", ())
                if isinstance(item, Mapping)
            ),
            error=str(result.get("error") or "")[:1000],
        )
        return replace(self, understanding=understanding)

    def to_dict(self, *, include_private_path: bool = False) -> dict[str, object]:
        return {
            "artifactId": self.artifact_id,
            "sourceType": self.source_type,
            "targetDomain": self.target_domain,
            "freshness": self.freshness,
            "acquiredAt": self.acquired_at,
            **({"path": self.path} if include_private_path and self.path else {}),
            "mimeType": self.mime_type,
            "width": self.width,
            "height": self.height,
            "contentHash": self.content_hash,
            "selfExcluded": self.self_excluded,
            "acquisitionStatus": self.acquisition_status,
            "understanding": self.understanding.to_dict(),
        }


def validate_artifact_binding(
    artifact: ImageArtifact,
    *,
    target_domain: str,
    required_freshness: str,
    existing_artifact_id: str | None = None,
) -> tuple[str, ...]:
    issues: list[str] = []
    if target_domain and target_domain != "unknown_visual" and artifact.target_domain != target_domain:
        issues.append("artifact_target_mismatch")
    if existing_artifact_id and artifact.artifact_id != existing_artifact_id:
        issues.append("artifact_id_mismatch")
    if required_freshness == "current" and artifact.freshness != "current":
        issues.append("artifact_not_current")
    if required_freshness == "existing_artifact" and artifact.freshness not in {
        "existing_artifact",
        "bound_attachment",
        "historical",
    }:
        issues.append("artifact_not_existing_reference")
    if artifact.acquisition_status != "captured":
        issues.append("artifact_acquisition_failed")
    return tuple(issues)
