"""附件正文提取；不承担上传落盘、视觉调用或对话状态。"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile


def decode_attachment_text(data: bytes) -> str:
    """按既有优先级读取常见文本编码。"""

    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def extract_office_xml_text(path: Path) -> str:
    """从 Office Open XML 容器中取出可见文本，忽略损坏的单个 XML 成员。"""

    suffix = path.suffix.lower()
    prefixes = {
        ".docx": ("word/document.xml",),
        ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"),
        ".pptx": ("ppt/slides/",),
    }.get(suffix, ())
    if not prefixes:
        return ""
    parts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        for member in sorted(archive.namelist()):
            if not any(member == prefix or member.startswith(prefix) for prefix in prefixes):
                continue
            if not member.endswith(".xml"):
                continue
            try:
                root = ET.fromstring(archive.read(member))
            except (ET.ParseError, KeyError, OSError):
                continue
            texts = [
                str(node.text or "").strip()
                for node in root.iter()
                if str(node.text or "").strip()
            ]
            if texts:
                parts.append("\n".join(texts))
    return "\n\n".join(parts)


def extract_attachment_text(
    path: Path,
    data: bytes,
    *,
    text_extensions: Collection[str],
) -> tuple[str, str]:
    """返回正文与既有 extraction 标签；PDF 依次尝试 pypdf 和 pdftotext。"""

    suffix = path.suffix.lower()
    try:
        if suffix in text_extensions:
            return decode_attachment_text(data), "text"
        if suffix in {".docx", ".xlsx", ".pptx"}:
            return extract_office_xml_text(path), "office_xml"
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader  # type: ignore

                text = "\n\n".join(
                    str(page.extract_text() or "") for page in PdfReader(str(path)).pages
                )
                if text.strip():
                    return text, "pypdf"
            except Exception:
                pass
            pdftotext = shutil.which("pdftotext")
            if pdftotext:
                try:
                    completed = subprocess.run(
                        [pdftotext, "-layout", str(path), "-"],
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
                    text = completed.stdout.decode("utf-8", errors="replace")
                    if completed.returncode == 0 and text.strip():
                        return text, "pdftotext"
                except (OSError, subprocess.SubprocessError):
                    pass
            return "", "pdf_text_unavailable"
    except (OSError, zipfile.BadZipFile) as error:
        return "", f"extract_failed:{type(error).__name__}"
    return "", "metadata_only"
