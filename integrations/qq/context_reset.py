"""Durable, reversible reset state for one QQ conversation.

The gateway keeps the reset marker beside (rather than inside) its policy.  A
marker is scoped by the configured QQ account, conversation type, and target;
it is therefore safe to use when one gateway process serves several allowed
recipients.  Dialogue profiles are moved into an archive directory so a
resetting operation can be inspected or restored locally without touching QQ
cloud history.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
import uuid
from typing import Any


_KINDS = frozenset({"private", "group"})


def validate_scope(account: Any, kind: Any, target: Any) -> tuple[str, str, str]:
    """Return a canonical numeric QQ scope or raise ``ValueError``."""
    account_text = str(account).strip() if not isinstance(account, bool) else ""
    target_text = str(target).strip() if not isinstance(target, bool) else ""
    if (kind not in _KINDS or not account_text or not target_text
            or not account_text.isascii() or not target_text.isascii()
            or not account_text.isdigit() or not target_text.isdigit()):
        raise ValueError("QQ 会话身份无效")
    return account_text, str(kind), target_text


def scope_key(account: Any, kind: Any, target: Any) -> str:
    account_text, kind_text, target_text = validate_scope(account, kind, target)
    # The type separator makes the key unambiguous even if IDs contain a
    # separator in a future protocol revision; IDs accepted above are digits.
    return f"{account_text}:{kind_text}:{target_text}"


def _marker(account: str, kind: str, target: str, *, epoch: int,
            cutoff: float, archived: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "account": account,
        "targetType": kind,
        "targetId": target,
        "epoch": int(epoch),
        "cutoff": float(cutoff),
    }
    if archived:
        value["archived"] = archived
    return value


class ContextResetStore:
    """Read and atomically write reset markers for all QQ scopes."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"version": 1, "scopes": {}}
        if not isinstance(value, dict) or not isinstance(value.get("scopes"), dict):
            return {"version": 1, "scopes": {}}
        return {"version": 1, "scopes": dict(value["scopes"])}

    def get(self, account: Any, kind: Any, target: Any) -> dict[str, Any]:
        account_text, kind_text, target_text = validate_scope(account, kind, target)
        value = self._read()["scopes"].get(scope_key(account_text, kind_text, target_text))
        if not isinstance(value, dict):
            return _marker(account_text, kind_text, target_text, epoch=0, cutoff=0.0)
        try:
            epoch = int(value.get("epoch", 0))
            cutoff = float(value.get("cutoff", 0.0))
        except (TypeError, ValueError, OverflowError):
            epoch, cutoff = 0, 0.0
        if epoch < 0 or cutoff < 0:
            epoch, cutoff = 0, 0.0
        result = _marker(account_text, kind_text, target_text, epoch=epoch, cutoff=cutoff)
        if isinstance(value.get("archived"), str) and value["archived"]:
            result["archived"] = value["archived"]
        return result

    def save(self, account: Any, kind: Any, target: Any, *, epoch: int,
             cutoff: float, archived: str | None = None) -> dict[str, Any]:
        account_text, kind_text, target_text = validate_scope(account, kind, target)
        if isinstance(epoch, bool) or int(epoch) < 1:
            raise ValueError("QQ 会话重置代际无效")
        if isinstance(cutoff, bool) or float(cutoff) <= 0:
            raise ValueError("QQ 会话重置时间无效")
        document = self._read()
        value = _marker(account_text, kind_text, target_text, epoch=int(epoch),
                        cutoff=float(cutoff), archived=archived)
        document["scopes"][scope_key(account_text, kind_text, target_text)] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return value


def archive_profiles(runtime: Path, account: str, kind: str, target: str, *,
                     epoch: int, cutoff: float) -> str | None:
    """Move both identity profiles to a uniquely named local archive.

    ``None`` means no profile had been created yet.  The returned path is
    relative to ``runtime`` so the marker remains portable across installations.
    """
    account_text, kind_text, target_text = validate_scope(account, kind, target)
    runtime_root = Path(runtime).resolve()
    try:
        sessions_root = (runtime_root / "amadeus-sessions").resolve()
        sessions_root.relative_to(runtime_root)
    except (OSError, ValueError) as exc:
        raise OSError("QQ 会话运行时目录超出范围") from exc
    scope_root = sessions_root / f"{account_text}-{kind_text}-{target_text}"
    try:
        scope_root_resolved = scope_root.resolve()
        scope_root_resolved.relative_to(sessions_root)
        if (scope_root_resolved.parent != sessions_root
                or scope_root_resolved.name != scope_root.name):
            raise OSError("QQ 会话归档目录不是当前会话")
    except (OSError, ValueError) as exc:
        raise OSError("QQ 会话归档目录超出运行时范围") from exc
    source = scope_root / "dialogue-v2"
    if not source.exists():
        return None
    try:
        source_resolved = source.resolve()
    except OSError as exc:
        raise OSError("QQ 会话归档源目录不可解析") from exc
    if source_resolved.parent != scope_root_resolved:
        raise OSError("QQ 会话归档源目录超出会话范围")
    if not source_resolved.is_dir():
        raise OSError("QQ 会话归档源目录无效")
    archive_parent = scope_root_resolved / "archives"
    archive_parent.mkdir(parents=True, exist_ok=True)
    try:
        archive_parent_resolved = archive_parent.resolve()
        archive_parent_resolved.relative_to(scope_root_resolved)
        if (archive_parent_resolved.parent != scope_root_resolved
                or archive_parent_resolved.name != "archives"):
            raise OSError("QQ 会话归档目录不是当前会话")
        archive_parent = archive_parent_resolved
    except (OSError, ValueError) as exc:
        raise OSError("QQ 会话归档目录超出会话范围") from exc
    stamp = int(float(cutoff))
    archive = archive_parent / f"reset-{int(epoch)}-{stamp}"
    if archive.exists():
        archive = archive_parent / f"reset-{int(epoch)}-{stamp}-{uuid.uuid4().hex[:8]}"
    archive.mkdir(parents=True)
    destination = archive / "dialogue-v2"
    try:
        archive_resolved = archive.resolve()
        archive_resolved.relative_to(scope_root_resolved)
        if archive_resolved.parent != archive_parent:
            raise OSError("QQ 会话归档目标不是当前会话")
        destination.parent.resolve().relative_to(scope_root_resolved)
    except (OSError, ValueError) as exc:
        raise OSError("QQ 会话归档目标超出会话范围") from exc
    shutil.move(str(source_resolved), str(destination))
    return str(destination.relative_to(runtime_root))


def restore_profiles(runtime: Path, archived: str) -> None:
    """Restore one archive produced by :func:`archive_profiles`.

    The path is marker data, so validate its resolved location before moving
    anything.  A pre-existing live profile is an error rather than an overwrite
    that could destroy a concurrently-created fresh dialogue.
    """
    runtime_root = Path(runtime).resolve()
    try:
        sessions_root = (runtime_root / "amadeus-sessions").resolve()
        sessions_root.relative_to(runtime_root)
    except (OSError, ValueError) as exc:
        raise OSError("QQ 会话运行时目录超出范围") from exc
    archive_path = (runtime_root / str(archived)).resolve()
    try:
        archive_path.relative_to(sessions_root)
        if archive_path.name != "dialogue-v2" or archive_path.parent.parent.name != "archives":
            raise ValueError("QQ 会话归档路径无效")
        scope_root = archive_path.parent.parent.parent.resolve()
        scope_root.relative_to(sessions_root)
    except (OSError, ValueError) as exc:
        raise OSError("QQ 会话归档路径无效") from exc
    destination = scope_root / "dialogue-v2"
    if destination.exists():
        raise FileExistsError("QQ 会话新旧档案同时存在，拒绝覆盖")
    shutil.move(str(archive_path), str(destination))
