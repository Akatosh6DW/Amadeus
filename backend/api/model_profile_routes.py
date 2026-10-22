"""HTTP routes for private saved model connection profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from backend.model_profiles import ModelProfileError


class JsonResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class ModelProfileOperations:
    list_profiles: Callable[[], dict[str, Any]]
    save_profile: Callable[[Mapping[str, Any]], dict[str, Any]]
    activate_profile: Callable[[str], dict[str, Any]]
    delete_profile: Callable[[str], dict[str, Any]]


class ModelProfileRoutes:
    def __init__(self, operations: ModelProfileOperations) -> None:
        self.operations = operations

    @staticmethod
    def _error(error: ModelProfileError) -> dict[str, Any]:
        messages = {
            "profile_field_required": "配置档名称、Endpoint 和 Model 不能为空。",
            "profile_field_too_long": "配置档字段过长。",
            "profile_id_invalid": "配置档编号无效。",
            "profile_not_found": "找不到所选配置档。",
            "profile_network_mode_invalid": "连接路线无效。",
            "profile_proxy_invalid": "应用代理地址无效。",
            "profile_key_required_for_endpoint_change": "Endpoint 已更换，请填写新服务商的 API Key。",
            "profile_limit_reached": "保存的配置档数量已达到上限。",
            "profile_key_too_long": "API Key 过长。",
            "model_profiles_invalid": "本地配置档文件无效，请重新保存配置。",
            "model_profiles_unsafe_path": "本地配置档路径不安全。",
            "model_profiles_write_failed": "本地配置档保存失败。",
        }
        return {"ok": False, "code": error.code, "error": messages.get(error.code, "配置档操作失败。")}

    def get(self, response: JsonResponse, path: str) -> bool:
        if path != "/api/model-profiles":
            return False
        try:
            response.send_json(self.operations.list_profiles())
        except ModelProfileError as error:
            response.send_json(self._error(error), 400)
        return True

    def post(self, response: JsonResponse, path: str, body: dict[str, Any]) -> bool:
        try:
            if path == "/api/model-profiles/save":
                result = self.operations.save_profile(body)
            elif path == "/api/model-profiles/activate":
                result = self.operations.activate_profile(str(body.get("id") or ""))
            elif path == "/api/model-profiles/delete":
                result = self.operations.delete_profile(str(body.get("id") or ""))
            else:
                return False
        except ModelProfileError as error:
            response.send_json(self._error(error), 400)
            return True
        response.send_json(result)
        return True


__all__ = ["ModelProfileOperations", "ModelProfileRoutes"]
