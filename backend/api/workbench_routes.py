"""Workbench 与开发者 HTTP 接口。

这些路径是发布模式下的 source-workbench 表面。发布拦截仍由 Handler 在
派发前完成；开发者存储作用域检查保留在各原分支对应的位置。本模块只
解析查询/请求字段和组织响应，不读取全局配置，也不反向导入 server。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
import mimetypes
import urllib.parse


class WorkbenchResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...

    def send_binary(self, data: bytes, content_type: str, status: int = 200) -> None: ...

    def send_error(self, status: int) -> None: ...


@dataclass(frozen=True)
class WorkbenchOperations:
    salieri_transcript: Callable[[], dict[str, Any]]
    read_system_prompt: Callable[[], str]
    persona_payload: Callable[[], dict[str, Any]]
    live2d_models: Callable[[], dict[str, Any]]
    expression_training_preview: Callable[[Any], Path | None]
    current_storage_scope: Callable[[], str]
    expression_training_dataset: Callable[[], dict[str, Any]]
    voice_reference_audio: Callable[[Any, Any], Path | None]
    voice_reference_catalog: Callable[[], dict[str, Any]]
    chat_progress: Callable[[Any], dict[str, Any]]
    normalize_scope: Callable[[Any], str]
    persona_skills: Callable[[], Any]
    persona_skill_records: Callable[[str], Any]
    release_relationship: Callable[[], dict[str, Any]]
    relationship_for_scope: Callable[[str], dict[str, Any]]
    relationship_snapshot: Callable[[dict[str, Any]], dict[str, Any]]
    enable_salieri: Callable[[Any], dict[str, Any]]
    write_persona_override: Callable[[str], str]
    save_live2d_expression_capture: Callable[[dict[str, Any]], dict[str, Any]]
    update_expression_training: Callable[[dict[str, Any]], dict[str, Any]]
    update_voice_reference_review: Callable[[dict[str, Any]], dict[str, Any]]
    write_live2d_model_override: Callable[[str, Any, bool], dict[str, Any]]


class WorkbenchRoutes:
    """开发者/Workbench 路由，保持原有 scope 判断和二进制错误顺序。"""

    def __init__(self, operations: WorkbenchOperations) -> None:
        self.operations = operations

    def _developer_scope_allowed(self, response: WorkbenchResponse) -> bool:
        if self.operations.current_storage_scope() == "developer":
            return True
        response.send_json({"ok": False, "error": "developer_scope_required"}, 404)
        return False

    def get(self, response: WorkbenchResponse, path: str, query_string: str = "") -> bool:
        if path == "/api/salieri":
            response.send_json(self.operations.salieri_transcript())
            return True
        if path == "/api/prompt":
            response.send_json({"prompt": self.operations.read_system_prompt()})
            return True
        if path == "/api/persona":
            response.send_json({"ok": True, "persona": self.operations.persona_payload()})
            return True
        if path == "/api/developer/live2d-models":
            response.send_json({"ok": True, "registry": self.operations.live2d_models()})
            return True
        if path == "/api/developer/expression-training/preview":
            query = urllib.parse.parse_qs(query_string)
            preview = self.operations.expression_training_preview((query.get("id") or [""])[0])
            if not preview:
                response.send_error(404)
                return True
            response.send_binary(preview.read_bytes(), "image/png")
            return True
        if path == "/api/developer/expression-training":
            if not self._developer_scope_allowed(response):
                return True
            response.send_json({
                "ok": True,
                "dataset": self.operations.expression_training_dataset(),
            })
            return True
        if path == "/api/developer/voice-reference-review/audio":
            if not self._developer_scope_allowed(response):
                return True
            query = urllib.parse.parse_qs(query_string)
            audio = self.operations.voice_reference_audio(
                (query.get("id") or [""])[0],
                (query.get("variant") or ["candidate"])[0],
            )
            if not audio:
                response.send_error(404)
                return True
            response.send_binary(
                audio.read_bytes(),
                mimetypes.guess_type(audio.name)[0] or "audio/wav",
            )
            return True
        if path == "/api/developer/voice-reference-review":
            if not self._developer_scope_allowed(response):
                return True
            response.send_json(self.operations.voice_reference_catalog())
            return True
        if path == "/api/developer/chat-progress":
            query = urllib.parse.parse_qs(query_string)
            request_id = (query.get("requestId") or [""])[0]
            response.send_json(self.operations.chat_progress(request_id))
            return True
        if path == "/api/developer/persona-skills":
            if not self._developer_scope_allowed(response):
                return True
            query = urllib.parse.parse_qs(query_string)
            scope = self.operations.normalize_scope((query.get("scope") or ["normal"])[0])
            response.send_json({
                "ok": True,
                "scope": scope,
                "skills": self.operations.persona_skills(),
                "records": self.operations.persona_skill_records(scope),
                "historyLimit": 24,
            })
            return True
        if path == "/api/developer/relationship":
            query = urllib.parse.parse_qs(query_string)
            requested_scope = str((query.get("scope") or ["release"])[0]).strip().lower()
            view_scope = requested_scope if requested_scope in {"release", "normal", "developer"} else "release"
            relationship = (
                self.operations.release_relationship()
                if view_scope == "release"
                else self.operations.relationship_for_scope(view_scope)
            )
            response.send_json({
                "ok": True,
                "scope": view_scope,
                "readOnly": True,
                "relationship": self.operations.relationship_snapshot(relationship),
            })
            return True
        return False

    def post_release(self, response: WorkbenchResponse, path: str, body: dict[str, Any]) -> bool:
        """Handle the salieri unlock branch immediately after the release gate."""
        if path != "/api/salieri/unlock":
            return False
        result = self.operations.enable_salieri(body.get("code"))
        response.send_json(result, 200 if result.get("ok") else 403)
        return True

    def post(self, response: WorkbenchResponse, path: str, body: dict[str, Any]) -> bool:
        if path == "/api/persona":
            override = self.operations.write_persona_override(str(body.get("override") or ""))
            response.send_json({
                "ok": True,
                "override": override,
                "persona": self.operations.persona_payload(),
            })
            return True
        if path == "/api/developer/live2d-expression-capture":
            response.send_json(self.operations.save_live2d_expression_capture(body))
            return True
        if path == "/api/developer/expression-training":
            if not self._developer_scope_allowed(response):
                return True
            response.send_json(self.operations.update_expression_training(body))
            return True
        if path == "/api/developer/voice-reference-review":
            if not self._developer_scope_allowed(response):
                return True
            response.send_json(self.operations.update_voice_reference_review(body))
            return True
        if path == "/api/developer/live2d-models":
            registry = self.operations.write_live2d_model_override(
                str(body.get("modelId") or ""),
                body.get("override"),
                bool(body.get("reset")),
            )
            response.send_json({"ok": True, "registry": registry})
            return True
        return False
