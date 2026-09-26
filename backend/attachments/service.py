"""上传附件接收、理解缓存与续接复用服务。"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import mimetypes
from pathlib import Path
from typing import Any

from backend.runtime.requests import RequestValidationError

from .storage import AttachmentStorage, safe_attachment_name


@dataclass(frozen=True)
class AttachmentLimits:
    """接收入口已有的数量、大小和正文上下文上限快照。"""

    max_count: int
    max_image_bytes: int
    max_file_bytes: int
    max_total_bytes: int
    max_context_chars: int
    image_extensions: frozenset[str]


@dataclass(frozen=True)
class AttachmentVisionGateway:
    """附件服务唯一的视觉端口，隔离云端视觉和原生图片输入实现。"""

    direct_visual_enabled: Callable[[dict[str, Any]], bool]
    load_direct_image: Callable[[Path, dict[str, Any]], Mapping[str, Any]]
    read_or_attach_image: Callable[[Path, dict[str, Any], str], Mapping[str, Any]]
    read_pdf: Callable[[Path, dict[str, Any], str], Mapping[str, Any]]


class AttachmentService:
    """接收上传附件，并对已绑定的上传图片复用或刷新理解缓存。

    服务只持有附件根目录与附件范围内的端口。它不能访问屏幕、摄像头或通用
    DATA_DIR；后两类传感器帧由独立的视觉续接流程管理，避免附件任务误用画面。
    """

    def __init__(
        self,
        storage: AttachmentStorage,
        *,
        limits: AttachmentLimits,
        extract_text: Callable[[Path, bytes], tuple[str, str]],
        vision: AttachmentVisionGateway,
        create_attachment_id: Callable[[], str],
        created_at: Callable[[], str],
        updated_at: Callable[[], str],
        log: Callable[..., None] | None = None,
    ) -> None:
        self.storage = storage
        self.limits = limits
        self._extract_text = extract_text
        self._vision = vision
        self._create_attachment_id = create_attachment_id
        self._created_at = created_at
        self._updated_at = updated_at
        self._logger = log

    def _log(self, event: str, **details: Any) -> None:
        if self._logger is not None:
            self._logger(event, **details)

    def ingest(self, body: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
        """验证、保存并提取本轮上传，返回既有 metadata 结构。"""

        raw_items = body.get("attachments") if isinstance(body.get("attachments"), list) else []
        if len(raw_items) > self.limits.max_count:
            raise RequestValidationError(
                "too_many_attachments",
                f"一次最多发送 {self.limits.max_count} 个附件。",
            )
        self.storage.ensure_root()
        processed: list[dict[str, Any]] = []
        attachment_question = "\n".join(
            str(item).strip()
            for item in (body.get("userMessages") if isinstance(body.get("userMessages"), list) else [])
            if str(item).strip()
        ) or str(body.get("userMessage") or "")
        total_bytes = 0
        total_context_chars = 0
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise RequestValidationError("invalid_attachment", "附件数据格式无效。")
            attachment_id = self.storage.normalized_id(item.get("id"))
            if not attachment_id:
                attachment_id = self._create_attachment_id()
            name = safe_attachment_name(item.get("name"), f"attachment-{index + 1}")
            encoded = str(item.get("dataBase64") or "")
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise RequestValidationError(
                    "invalid_attachment_base64",
                    f"附件 {name} 的内容无法读取。",
                ) from error
            if not data:
                raise RequestValidationError("empty_attachment", f"附件 {name} 是空文件。")
            mime_type = str(
                item.get("mimeType")
                or mimetypes.guess_type(name)[0]
                or "application/octet-stream"
            )[:160]
            suffix = Path(name).suffix.lower()
            is_image = mime_type.startswith("image/") or suffix in self.limits.image_extensions
            per_file_limit = self.limits.max_image_bytes if is_image else self.limits.max_file_bytes
            if len(data) > per_file_limit:
                limit_mb = per_file_limit // (1024 * 1024)
                raise RequestValidationError(
                    "attachment_too_large",
                    f"附件 {name} 超过 {limit_mb} MB。",
                )
            total_bytes += len(data)
            if total_bytes > self.limits.max_total_bytes:
                raise RequestValidationError("attachments_too_large", "本次附件总大小超过 64 MB。")
            stored_name = safe_attachment_name(name, "attachment")
            stored_path = self.storage.write_upload(attachment_id, stored_name, data)
            if stored_path is None:
                raise RequestValidationError("invalid_attachment_id", "附件标识无效。")
            suffix = stored_path.suffix.lower()
            extracted_text = ""
            extraction = "metadata_only"
            vision: Mapping[str, Any] = {}
            if mime_type.startswith("image/") or suffix in self.limits.image_extensions:
                vision = self._vision.read_or_attach_image(stored_path, settings, attachment_question)
                extracted_text = str(vision.get("ocrText") or vision.get("text") or "")
                extraction = "vision" if vision.get("available", vision.get("ok", False)) else "vision_unavailable"
                kind = "image"
            else:
                extracted_text, extraction = self._extract_text(stored_path, data)
                if suffix == ".pdf" and not extracted_text.strip() and settings.get("visionEnabled"):
                    vision = self._vision.read_pdf(stored_path, settings, attachment_question)
                    extracted_text = str(vision.get("ocrText") or vision.get("text") or "")
                    extraction = (
                        "pdf_vision"
                        if vision.get("available", vision.get("ok", False))
                        else "pdf_vision_unavailable"
                    )
                kind = "file"
            remaining = max(0, self.limits.max_context_chars - total_context_chars)
            content_truncated = len(extracted_text) > remaining
            extracted_text = extracted_text[:remaining]
            total_context_chars += len(extracted_text)
            metadata = {
                "id": attachment_id,
                "name": name,
                "storedName": stored_name,
                "mimeType": mime_type,
                "size": len(data),
                "sha256": sha256(data).hexdigest(),
                "kind": kind,
                "extraction": extraction,
                "contentTruncated": content_truncated,
                "extractedText": extracted_text,
                "visionCaption": str(vision.get("caption") or "")[:8000],
                "visionModel": str(vision.get("model") or ""),
                "visionProvider": str(vision.get("provider") or ""),
                "visionRegions": vision.get("regions") if isinstance(vision.get("regions"), list) else [],
                "visionTiles": vision.get("visualTiles") if isinstance(vision.get("visualTiles"), list) else [],
                "visionOcrItems": vision.get("ocrItems") if isinstance(vision.get("ocrItems"), list) else [],
                "visionAnalysisFocus": vision.get("analysisFocus") if isinstance(vision.get("analysisFocus"), list) else [],
                "visionUncertainItems": vision.get("uncertainItems") or [],
                "directImageInput": bool(vision.get("directImageInput")),
                "visionError": str(vision.get("error") or "")[:1000],
                "pageCount": int(vision.get("pageCount") or 0),
                "analyzedPageCount": int(vision.get("analyzedPageCount") or 0),
                "visionTruncatedByPageLimit": bool(vision.get("truncatedByPageLimit")),
                "createdAt": self._created_at(),
            }
            self.storage.write_initial_metadata(attachment_id, metadata)
            processed.append(metadata)
        return processed

    def refresh_uploaded_continuations(
        self,
        body: dict[str, Any],
        settings: dict[str, Any],
        user_message: str,
    ) -> list[dict[str, Any]]:
        """复用上传图片理解缓存，或只重读同一原件并原子更新其 metadata。"""

        continued = body.get("_continuedAttachments")
        if not isinstance(continued, list):
            return []
        task = body.get("_artifactTaskContext")
        question = str(
            (
                (task.get("inspectionQuestion") or task.get("question"))
                if isinstance(task, Mapping)
                else ""
            )
            or user_message
        ).strip()
        visual_action = str(
            task.get("visualAction") if isinstance(task, Mapping) else "reuse"
        ).strip().lower()
        if visual_action not in {"reuse", "reanalyze"}:
            visual_action = "reuse"
        refreshed: list[dict[str, Any]] = []
        for item in continued[:self.limits.max_count]:
            if not isinstance(item, Mapping):
                continue
            current = dict(item)
            current["_taskContinuation"] = True
            if str(current.get("kind") or "") == "image":
                artifact_id = str(current.get("id") or "").strip().lower()
                stored_name = safe_attachment_name(
                    current.get("storedName") or current.get("name"),
                    "attachment",
                )
                path = self.storage.file_path(artifact_id, stored_name)
                if self._vision.direct_visual_enabled(settings):
                    vision = (
                        self._vision.load_direct_image(path, settings)
                        if path is not None and self.storage.resolved_root in path.parents
                        else {"available": False}
                    )
                    current.update(
                        directImageInput=bool(vision.get("directImageInput")),
                        visionError=str(vision.get("error") or ""),
                        _visionRequestMade=False,
                        _visualCacheHit=False,
                        visionCaption="",
                        extractedText="",
                        visionRegions=[],
                        visionTiles=[],
                    )
                    refreshed.append(current)
                    continue
                cached_understanding = bool(
                    str(current.get("visionCaption") or "").strip()
                    or str(current.get("extractedText") or "").strip()
                )
                if visual_action == "reuse" and cached_understanding:
                    current["_visualCacheHit"] = True
                    current["_visionRequestMade"] = False
                    current["_visualAction"] = "reuse"
                    self._log(
                        "visual_artifact_cache_reused",
                        artifactId=str(current.get("id") or ""),
                        sourceType="uploaded_attachment",
                    )
                    refreshed.append(current)
                    continue
                if path is None or self.storage.resolved_root not in path.parents or not path.is_file():
                    current.update(
                        extraction="vision_unavailable",
                        visionError="saved_attachment_missing",
                        visionCaption="",
                        extractedText="",
                        visionRegions=[],
                        visionTiles=[],
                        visionOcrItems=[],
                        visionAnalysisFocus=[],
                    )
                else:
                    vision = self._vision.read_or_attach_image(path, settings, question)
                    current.update(
                        extraction=(
                            "vision"
                            if vision.get("available", vision.get("ok", False))
                            else "vision_unavailable"
                        ),
                        extractedText=str(vision.get("ocrText") or vision.get("text") or "")[:self.limits.max_context_chars],
                        visionCaption=str(vision.get("caption") or "")[:8000],
                        visionModel=str(vision.get("model") or ""),
                        visionProvider=str(vision.get("provider") or ""),
                        visionRegions=vision.get("regions") if isinstance(vision.get("regions"), list) else [],
                        visionTiles=vision.get("visualTiles") if isinstance(vision.get("visualTiles"), list) else [],
                        visionOcrItems=vision.get("ocrItems") if isinstance(vision.get("ocrItems"), list) else [],
                        visionAnalysisFocus=vision.get("analysisFocus") if isinstance(vision.get("analysisFocus"), list) else [],
                        visionUncertainItems=vision.get("uncertainItems") or [],
                        visionError=str(vision.get("error") or "")[:1000],
                        understandingUpdatedAt=self._updated_at(),
                        _visualCacheHit=False,
                        _visionRequestMade=True,
                        _visualAction="reanalyze",
                    )
                    self._log(
                        "visual_artifact_reanalyzed",
                        artifactId=artifact_id,
                        sourceType="uploaded_attachment",
                        available=bool(vision.get("available", vision.get("ok", False))),
                    )
                    if vision.get("available", vision.get("ok", False)):
                        # 只替换最新理解缓存；原始图片字节和历史轮次保持不动。
                        cached_metadata = {
                            key: value for key, value in current.items() if not key.startswith("_")
                        }
                        self.storage.replace_cached_metadata(artifact_id, cached_metadata)
            refreshed.append(current)
        return refreshed
