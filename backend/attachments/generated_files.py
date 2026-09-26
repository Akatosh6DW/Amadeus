"""模型生成文件的批量保存与下载解析，不写入用户指定的外部工作目录。"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Collection

from agent.attachment_editing import file_output_issues
from agent.cognitive_brain import CognitiveProtocolError
from backend.attachments.storage import safe_attachment_name


MAX_GENERATED_FILES = 4
MAX_GENERATED_FILE_BYTES = 2 * 1024 * 1024
GENERATED_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".csv", ".html", ".css", ".js", ".ts", ".py", ".ps1",
    ".xml", ".yaml", ".yml",
}


class GeneratedFileStore:
    """显式管理一个生成文件目录；读取和交付使用同一份元数据。

    路径、限制和时钟在入口调用时提供，存储层不猜测当前用户或发布目录。
    整批文件写入并读回验证后，才从回复移除待交付正文；失败保留正文供上层处理。
    """

    def __init__(
        self, directory: Path, *, max_files: int, max_bytes: int,
        extensions: Collection[str], new_id: Callable[[], str], utc_now: Callable[[], str],
    ) -> None:
        self.directory = directory
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.extensions = extensions
        self.new_id = new_id
        self.utc_now = utc_now

    def materialize(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        outputs = result.get("_fileOutputs", [])
        issues = file_output_issues(outputs, max_files=self.max_files, max_bytes=self.max_bytes)
        if issues:
            raise CognitiveProtocolError("generated_file_validation_failed:" + ",".join(issues))
        if not outputs:
            return []
        self.directory.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        created_dirs: list[Path] = []
        root = self.directory.resolve()
        try:
            for index, item in enumerate(outputs):
                name = safe_attachment_name(item.get("fileName") or item.get("name"), f"output-{index + 1}.txt")
                if Path(name).suffix.lower() not in self.extensions:
                    name = f"{Path(name).stem or f'output-{index + 1}'}.txt"
                data = item["content"].encode("utf-8")
                file_id = self.new_id()
                item_dir = (root / file_id).resolve()
                if item_dir.parent != root:
                    raise OSError("generated_file_path_invalid")
                item_dir.mkdir(exist_ok=False)
                created_dirs.append(item_dir)
                path = item_dir / name
                path.write_bytes(data)
                if path.read_bytes() != data:
                    raise OSError("generated_file_write_verification_failed")
                metadata = {
                    "id": file_id, "name": name,
                    "mimeType": mimetypes.guess_type(name)[0] or "text/plain",
                    "size": len(data), "sourceAttachmentId": item.get("sourceAttachmentId"),
                    "createdAt": self.utc_now(),
                }
                (item_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
                files.append({**metadata, "url": f"/api/files/{file_id}"})
        except (OSError, UnicodeError):
            # 只回滚本批在已校验根目录下创建的目录，不影响已有下载文件。
            for item_dir in created_dirs:
                if item_dir.resolve().parent == root:
                    shutil.rmtree(item_dir, ignore_errors=True)
            raise
        result.pop("_fileOutputs", None)
        return files


    def resolve(self, file_id: Any) -> tuple[Path, dict[str, Any]] | None:
        normalized = str(file_id or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", normalized):
            return None
        item_dir = (self.directory / normalized).resolve()
        if self.directory.resolve() not in item_dir.parents:
            return None
        metadata_path = item_dir / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict):
            return None
        path = (item_dir / safe_attachment_name(metadata.get("name"), "output.txt")).resolve()
        if item_dir not in path.parents or not path.is_file():
            return None
        return path, metadata
