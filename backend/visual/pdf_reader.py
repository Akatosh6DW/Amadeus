"""有界 PDF 栅格化与逐页识图，复用图片理解服务，不另建模型路径。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable

import tempfile
from agent.deepseek_vision import vision_model
from backend.config.media_limits import MAX_PDF_VISION_PAGES, MAX_ATTACHMENT_CONTEXT_CHARS

class PdfImageUnderstanding:
    def __init__(self, *, render_pages: Callable[..., Any], read_image: Callable[..., dict[str, Any]]) -> None:
        self.render_pages = render_pages
        self.read_image = read_image

    def try_vision_pdf(
        self,
        path: Path,
        settings: dict[str, Any],
        user_question: str = "",
        max_pages: int = MAX_PDF_VISION_PAGES,
    ) -> dict[str, Any]:
        """Read an image-only PDF by rendering pages into the existing vision path."""

        page_blocks: list[str] = []
        captions: list[str] = []
        regions: list[dict[str, Any]] = []
        visual_tiles: list[dict[str, Any]] = []
        ocr_items: list[dict[str, Any]] = []
        analysis_focus: list[str] = []
        errors: list[str] = []
        total_pages = 0
        analyzed_pages = 0
        page_limit = max(1, min(MAX_PDF_VISION_PAGES, int(max_pages)))
        try:
            with tempfile.TemporaryDirectory(prefix="pdf-vision-", dir=str(path.parent)) as temp_dir:
                rendered, total_pages = self.render_pages(
                    path,
                    Path(temp_dir),
                    max_pages=page_limit,
                )
                for page_number, image_path in enumerate(rendered, start=1):
                    page_question = (
                        f"{str(user_question or '')[:800]}\n"
                        f"当前图像是 PDF 第 {page_number} 页，共 {total_pages} 页。请读取本页正文和关键信息。"
                    ).strip()
                    vision = self.read_image(
                        image_path,
                        settings,
                        page_question,
                        "standard",
                    )
                    if not vision.get("available", vision.get("ok", False)):
                        errors.append(str(vision.get("error") or f"page_{page_number}_vision_unavailable"))
                        continue
                    analyzed_pages += 1
                    ocr_text = str(vision.get("ocrText") or vision.get("text") or "").strip()
                    caption = str(vision.get("caption") or vision.get("globalDescription") or "").strip()
                    if ocr_text:
                        page_blocks.append(f"[第 {page_number} 页 OCR]\n{ocr_text}")
                    if caption:
                        page_blocks.append(f"[第 {page_number} 页图像描述]\n{caption}")
                        captions.append(f"[第 {page_number} 页] {caption}")
                    for source, target in (
                        (vision.get("regions"), regions),
                        (vision.get("visualTiles"), visual_tiles),
                        (vision.get("ocrItems"), ocr_items),
                    ):
                        if not isinstance(source, list):
                            continue
                        for item in source:
                            if isinstance(item, dict):
                                target.append({**item, "pageNumber": page_number})
                    raw_focus = vision.get("analysisFocus")
                    for item in raw_focus if isinstance(raw_focus, list) else []:
                        value = str(item).strip()
                        if value and value not in analysis_focus:
                            analysis_focus.append(value)
        except Exception as error:
            errors.append(str(error))
        combined_text = "\n\n".join(page_blocks)[:MAX_ATTACHMENT_CONTEXT_CHARS]
        combined_caption = "\n".join(captions)[:8000]
        available = bool(combined_text.strip() or combined_caption.strip())
        return {
            "ok": available,
            "available": available,
            "provider": "deepseek", "model": vision_model(settings),
            "text": combined_text,
            "ocrText": combined_text,
            "caption": combined_caption,
            "regions": regions[:160],
            "visualTiles": visual_tiles[:24],
            "ocrItems": ocr_items[:240],
            "analysisFocus": analysis_focus[:12],
            "pageCount": total_pages,
            "analyzedPageCount": analyzed_pages,
            "truncatedByPageLimit": bool(total_pages > page_limit),
            "error": "; ".join(errors)[:1000] if errors and not available else "",
        }
