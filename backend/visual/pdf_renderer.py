"""有界 PDF 栅格化与逐页识图，复用图片理解服务，不另建模型路径。"""
from __future__ import annotations
from pathlib import Path

import shutil
import subprocess
from backend.config.media_limits import MAX_PDF_VISION_PAGES

def render_pdf_pages_for_vision(
    path: Path,
    output_dir: Path,
    max_pages: int = MAX_PDF_VISION_PAGES,
) -> tuple[list[Path], int]:
    """Render a bounded number of PDF pages for cloud image understanding."""

    page_limit = max(1, min(MAX_PDF_VISION_PAGES, int(max_pages)))
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore

        document = fitz.open(str(path))
        try:
            total_pages = len(document)
            rendered: list[Path] = []
            matrix = fitz.Matrix(1.7, 1.7)
            for page_index in range(min(total_pages, page_limit)):
                target = output_dir / f"page-{page_index + 1:04d}.png"
                pixmap = document.load_page(page_index).get_pixmap(matrix=matrix, alpha=False)
                pixmap.save(str(target))
                rendered.append(target)
            return rendered, total_pages
        finally:
            document.close()
    except Exception as fitz_error:
        pdftoppm = shutil.which("pdftoppm")
        if not pdftoppm:
            raise RuntimeError(f"pdf_renderer_unavailable:{type(fitz_error).__name__}") from fitz_error
        prefix = output_dir / "page"
        completed = subprocess.run(
            [
                pdftoppm,
                "-f", "1",
                "-l", str(page_limit),
                "-r", "144",
                "-png",
                str(path),
                str(prefix),
            ],
            capture_output=True,
            timeout=90,
            check=False,
        )
        rendered = sorted(output_dir.glob("page-*.png"))
        if completed.returncode != 0 or not rendered:
            raise RuntimeError("pdf_render_failed") from fitz_error
        return rendered, len(rendered)
