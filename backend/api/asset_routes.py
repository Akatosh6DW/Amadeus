"""生成文件与背景图片的二进制 HTTP 交付。

路径校验/元数据解析仍由附件服务负责；本模块只保留精确路由匹配、404
分支和原有 download/binary 响应调用，避免把二进制协议重新放回业务入口。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
import re


class AssetResponse(Protocol):
    def send_error(self, status: int) -> None: ...

    def send_download(self, path: Path, metadata: dict[str, Any]) -> None: ...

    def send_binary(self, data: bytes, content_type: str, status: int = 200) -> None: ...


@dataclass(frozen=True)
class AssetOperations:
    resolve_generated_file: Callable[[str], tuple[Path, dict[str, Any]] | None]
    configured_background_image: Callable[[], Path | None]
    content_type_for: Callable[[Path], str]


class AssetRoutes:
    _FILE_ROUTE = re.compile(r"/api/files/([a-fA-F0-9]{32})")

    def __init__(self, operations: AssetOperations) -> None:
        self.operations = operations

    def get_file(self, response: AssetResponse, path: str) -> bool:
        match = self._FILE_ROUTE.fullmatch(path)
        if not match:
            return False
        resolved = self.operations.resolve_generated_file(match.group(1))
        if not resolved:
            response.send_error(404)
            return True
        response.send_download(*resolved)
        return True

    def get_background(self, response: AssetResponse, path: str) -> bool:
        if path != "/api/background-image":
            return False
        background = self.operations.configured_background_image()
        if not background:
            response.send_error(404)
            return True
        response.send_binary(background.read_bytes(), self.operations.content_type_for(background))
        return True
