"""读取本地与分发背景清单；只允许清单所属目录内的图片。"""
from __future__ import annotations

import json
from pathlib import Path
import re
import urllib.parse

from backend.config.normalization import BACKGROUND_IMAGE_EXTENSIONS


def available_background_images(*, local_directory: Path, published_directory: Path) -> list[dict[str, str]]:
    """Keep local choices while also supporting the small published catalog."""
    images = []
    seen = set()
    catalogs = (
        (local_directory, "/assets/backgrounds/original/"),
        (published_directory, "/assets/backgrounds/presets/"),
    )
    for directory, url_prefix in catalogs:
        try:
            catalog = json.loads((directory / "presets.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(catalog, list):
            continue
        root = directory.resolve()
        for item in catalog:
            if not isinstance(item, dict):
                continue
            preset_id = str(item.get("id") or "")
            if not re.fullmatch(r"local-[a-z0-9-]+", preset_id) or preset_id in seen:
                continue
            try:
                # 清单中的图片路径也要解析并校验目录边界，防止越界读取任意本地文件。
                path = (root / str(item.get("file") or "")).resolve()
                if root not in path.parents or path.suffix.lower() not in BACKGROUND_IMAGE_EXTENSIONS or not path.is_file():
                    continue
            except OSError:
                continue
            images.append({
                "id": preset_id,
                "label": str(item.get("label") or preset_id),
                "url": url_prefix + urllib.parse.quote(path.relative_to(root).as_posix()),
            })
            seen.add(preset_id)
    return images
