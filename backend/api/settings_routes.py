"""设置读写和连接测试接口。

HTTP 入口完成认证、作用域绑定和发布路由限制后才派发到这里。
配置持久化与密钥裁剪由应用服务完成，路由只保留原字段与响应状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from desktop_window_geometry import window_limits


class JsonResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class SettingsOperations:
    public_settings: Callable[..., dict[str, Any]]
    save_settings: Callable[[dict[str, Any]], dict[str, Any]]
    background_images: Callable[[], list[dict[str, str]]]
    test_api: Callable[[], dict[str, Any]]
    test_web_search: Callable[[], dict[str, Any]]


class SettingsRoutes:
    def __init__(self, operations: SettingsOperations) -> None:
        self.operations = operations

    def get(self, response: JsonResponse, path: str) -> bool:
        if path != "/api/settings":
            return False
        response.send_json({
            "settings": self.operations.public_settings(),
            "backgroundPresets": self.operations.background_images(),
            "windowAspectRatios": {
                mode: [window_limits(mode).min_ratio, window_limits(mode).max_ratio]
                for mode in ("lite", "full")
            },
        })
        return True

    def post(self, response: JsonResponse, path: str, body: dict[str, Any]) -> bool:
        if path == "/api/settings":
            settings = self.operations.save_settings(body.get("settings", body))
            response.send_json({"ok": True, "settings": self.operations.public_settings(settings)})
        elif path == "/api/test-api":
            response.send_json(self.operations.test_api())
        elif path == "/api/test-web-search":
            response.send_json(self.operations.test_web_search())
        else:
            return False
        return True
