"""文件工具只在配置工作区内读写，保留权限开关、大小限制和原子替换。

每次解析真实绝对路径后再检查归属，不能只靠字符串前缀。默认根目录按当前配置读取。
"""
from __future__ import annotations
from datetime import datetime
import os
from pathlib import Path
import uuid
from typing import Any, Callable
from backend.common.values import clamp_int

def agent_file_permission_denied(name: str, permission: str, message: str) -> dict[str, Any]:
    return {
        "name": name,
        "ok": False,
        "error": "permission_denied",
        "permission": permission,
        "message": message,
    }


def agent_relative_path(root: Path, target: Path) -> str:
    relative = target.relative_to(root)
    return "." if not relative.parts else relative.as_posix()


class WorkspaceFileTools:
    def __init__(self, *, data_dir: Callable[[], Path], defaults: Callable[[], dict[str, Any]],
                 max_file_bytes: Callable[[], int], max_directory_entries: Callable[[], int]) -> None:
        self.data_dir = data_dir
        self.defaults = defaults
        self.max_file_bytes = max_file_bytes
        self.max_directory_entries = max_directory_entries

    def agent_workspace_root(self, settings: dict[str, Any]) -> Path:
        raw = str(settings.get("agentFileWorkspace") or self.defaults()["agentFileWorkspace"]).strip()
        root = Path(raw).expanduser()
        if not root.is_absolute():
            root = self.data_dir() / root
        return root.resolve()


    def resolve_agent_workspace_path(
        self,
        settings: dict[str, Any],
        raw_path: Any,
        *,
        allow_root: bool = False,
    ) -> tuple[Path, Path]:
        root = self.agent_workspace_root(settings)
        text = str(raw_path or ".").strip().strip("\"'")
        candidate = Path(text).expanduser()
        target = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if target != root and root not in target.parents:
            raise ValueError("path_outside_agent_workspace")
        if target == root and not allow_root:
            raise ValueError("workspace_root_is_not_a_file")
        return root, target


    def tool_list_directory(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionFileRead"):
            return agent_file_permission_denied(
                "list_directory",
                "permissionFileRead",
                "Agent 文件读取权限未授予，不能列出目录。",
            )
        try:
            root, target = self.resolve_agent_workspace_path(settings, args.get("path"), allow_root=True)
            if not target.exists():
                return {"name": "list_directory", "ok": False, "error": "directory_not_found"}
            if not target.is_dir():
                return {"name": "list_directory", "ok": False, "error": "not_a_directory"}
            recursive = bool(args.get("recursive"))
            max_depth = clamp_int(args.get("maxDepth"), 1, 4, 2)
            entries: list[dict[str, Any]] = []
            pending = [(target, 0)]
            while pending and len(entries) < self.max_directory_entries():
                current, depth = pending.pop(0)
                for item in sorted(current.iterdir(), key=lambda value: (not value.is_dir(), value.name.lower())):
                    resolved = item.resolve()
                    if resolved != root and root not in resolved.parents:
                        continue
                    stat = item.stat()
                    entries.append({
                        "path": agent_relative_path(root, resolved),
                        "type": "directory" if item.is_dir() else "file",
                        "size": None if item.is_dir() else stat.st_size,
                        "modifiedAt": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
                    })
                    if len(entries) >= self.max_directory_entries():
                        break
                    if recursive and item.is_dir() and not item.is_symlink() and depth < max_depth:
                        pending.append((resolved, depth + 1))
            return {
                "name": "list_directory",
                "ok": True,
                "path": agent_relative_path(root, target),
                "entries": entries,
                "truncated": len(entries) >= self.max_directory_entries(),
            }
        except (OSError, ValueError) as error:
            return {"name": "list_directory", "ok": False, "error": str(error)[:300]}


    def tool_read_text_file(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionFileRead"):
            return agent_file_permission_denied(
                "read_text_file",
                "permissionFileRead",
                "Agent 文件读取权限未授予，不能读取文件。",
            )
        try:
            root, target = self.resolve_agent_workspace_path(settings, args.get("path"))
            if not target.is_file():
                return {"name": "read_text_file", "ok": False, "error": "file_not_found"}
            size = target.stat().st_size
            if size > self.max_file_bytes():
                return {"name": "read_text_file", "ok": False, "error": "file_too_large", "size": size}
            content = target.read_text(encoding="utf-8")
            return {
                "name": "read_text_file",
                "ok": True,
                "path": agent_relative_path(root, target),
                "size": size,
                "content": content,
            }
        except UnicodeDecodeError:
            return {"name": "read_text_file", "ok": False, "error": "file_is_not_utf8_text"}
        except (OSError, ValueError) as error:
            return {"name": "read_text_file", "ok": False, "error": str(error)[:300]}


    def tool_create_directory(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionFileWrite"):
            return agent_file_permission_denied(
                "create_directory",
                "permissionFileWrite",
                "Agent 文件修改权限未授予，不能创建目录。",
            )
        try:
            root, target = self.resolve_agent_workspace_path(settings, args.get("path"), allow_root=True)
            target.mkdir(parents=bool(args.get("createParents", True)), exist_ok=True)
            return {
                "name": "create_directory",
                "ok": True,
                "path": agent_relative_path(root, target),
            }
        except (OSError, ValueError) as error:
            return {"name": "create_directory", "ok": False, "error": str(error)[:300]}


    def tool_write_text_file(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionFileWrite"):
            return agent_file_permission_denied(
                "write_text_file",
                "permissionFileWrite",
                "Agent 文件修改权限未授予，不能写入文件。",
            )
        mode = str(args.get("mode") or "create").strip().lower()
        if mode not in {"create", "overwrite", "append"}:
            return {"name": "write_text_file", "ok": False, "error": "invalid_write_mode"}
        content = str(args.get("content") or "")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes():
            return {"name": "write_text_file", "ok": False, "error": "content_too_large"}
        try:
            root, target = self.resolve_agent_workspace_path(settings, args.get("path"))
            if target.exists() and not target.is_file():
                return {"name": "write_text_file", "ok": False, "error": "target_is_not_a_file"}
            if mode == "create" and target.exists():
                return {
                    "name": "write_text_file",
                    "ok": False,
                    "error": "file_exists_requires_explicit_overwrite",
                }
            if not target.parent.exists():
                if not bool(args.get("createParents")):
                    return {"name": "write_text_file", "ok": False, "error": "parent_directory_not_found"}
                target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with target.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(content)
            elif mode == "create":
                with target.open("x", encoding="utf-8", newline="") as handle:
                    handle.write(content)
            else:
                temporary = target.with_name(f".{target.name}.amadeus-{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_text(content, encoding="utf-8", newline="")
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            return {
                "name": "write_text_file",
                "ok": True,
                "path": agent_relative_path(root, target),
                "mode": mode,
                "size": target.stat().st_size,
            }
        except (OSError, ValueError) as error:
            return {"name": "write_text_file", "ok": False, "error": str(error)[:300]}
