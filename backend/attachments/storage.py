"""上传附件原件与理解元数据的专属磁盘存储。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any


_ATTACHMENT_ID_PATTERN = re.compile(r"[a-z0-9-]{8,64}")


def safe_attachment_name(value: Any, fallback: str = "attachment") -> str:
    """保留上传文件名的可读部分，同时去除跨平台危险字符。"""

    name = Path(str(value or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]", "_", name).strip(" .")
    if not name:
        name = fallback
    stem = Path(name).stem[:90].strip(" .") or fallback
    suffix = Path(name).suffix[:20].lower()
    return f"{stem}{suffix}"[:120]


class AttachmentStorage:
    """管理 ``chat-attachments`` 根目录下的上传原件和 metadata.json。

    该类只认一个显式根目录，永远不会将上传附件解析为屏幕或摄像头运行时帧。
    原件和初始元数据沿用既有直接写入行为；仅续接重读后的 metadata 使用临时文件
    原子替换，避免中途读取到半份理解缓存。
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def resolved_root(self) -> Path:
        return self.root.resolve()

    def ensure_root(self) -> None:
        """保留接收入口在校验后即创建附件根目录的行为。"""

        self.root.mkdir(parents=True, exist_ok=True)

    def normalized_id(self, attachment_id: Any) -> str:
        value = str(attachment_id or "").strip().lower()
        return value if _ATTACHMENT_ID_PATTERN.fullmatch(value) else ""

    def item_dir(self, attachment_id: Any) -> Path | None:
        normalized = self.normalized_id(attachment_id)
        if not normalized:
            return None
        item_dir = (self.root / normalized).resolve()
        return item_dir if self.resolved_root in item_dir.parents else None

    def file_path(self, attachment_id: Any, stored_name: Any) -> Path | None:
        item_dir = self.item_dir(attachment_id)
        if item_dir is None:
            return None
        path = (item_dir / safe_attachment_name(stored_name, "attachment")).resolve()
        return path if self.resolved_root in path.parents else None

    def metadata_path(self, attachment_id: Any) -> Path | None:
        item_dir = self.item_dir(attachment_id)
        if item_dir is None:
            return None
        path = (item_dir / "metadata.json").resolve()
        return path if self.resolved_root in path.parents else None

    def read_metadata(self, attachment_id: Any) -> dict[str, Any] | None:
        metadata_path = self.metadata_path(attachment_id)
        if metadata_path is None or not metadata_path.is_file():
            return None
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def write_upload(self, attachment_id: Any, stored_name: Any, data: bytes) -> Path | None:
        """写入原始上传字节，保持既有非原子落盘顺序。"""

        item_dir = self.item_dir(attachment_id)
        path = self.file_path(attachment_id, stored_name)
        if item_dir is None or path is None:
            return None
        self.ensure_root()
        item_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_initial_metadata(self, attachment_id: Any, metadata: dict[str, Any]) -> bool:
        """写入接收时元数据，保留原有直接写入的可观察行为。"""

        metadata_path = self.metadata_path(attachment_id)
        if metadata_path is None:
            return False
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True

    def replace_cached_metadata(self, attachment_id: Any, metadata: dict[str, Any]) -> bool:
        """原子替换续接重读后的理解缓存，不接触同目录的原始文件。"""

        metadata_path = self.metadata_path(attachment_id)
        if metadata_path is None:
            return False
        temporary = metadata_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, metadata_path)
        return True
