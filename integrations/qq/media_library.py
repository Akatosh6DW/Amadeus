"""Per QQ-account image and meme storage for gateway-side sending.

The library is deliberately independent from the model and OneBot client.  Its
metadata APIs never expose bytes or local paths; only ``segments`` returns a
OneBot file segment for the gateway to send after an explicit owner action.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any
import warnings


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BATCH_ITEMS = 20
MAX_PIXELS = 20_000_000
MAX_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500
MAX_TAGS = 20
MAX_TAG_LENGTH = 80
ASSET_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
# QQ identifiers are validated as identifiers rather than as phone-like values:
# the contract requires non-empty ASCII digits, without imposing an arbitrary
# minimum length on test/local accounts.
ACCOUNT_RE = re.compile(r"[0-9]+\Z")
SUPPORTED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
FORMAT_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}


class MediaLibraryError(ValueError):
    """A local media library input or manifest is invalid."""


def _error(code: str, index: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"error": code}
    if index is not None:
        value["index"] = index
    return value


def _safe_text(value: Any, *, limit: int, required: bool = False,
               field: str = "text") -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise MediaLibraryError(field)
    if required and not value.strip():
        raise MediaLibraryError(field)
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise MediaLibraryError(field)
    return value.strip() if field != "name" else value.strip()


def _safe_name(value: Any) -> str:
    value = _safe_text(value, limit=MAX_NAME_LENGTH, required=True, field="name")
    if ("/" in value or "\\" in value or value in {".", ".."}
            or Path(value).name != value):
        raise MediaLibraryError("name")
    return value


def _safe_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_TAGS:
        raise MediaLibraryError("tags")
    result: list[str] = []
    seen: set[str] = set()
    for tag in value:
        tag = _safe_text(tag, limit=MAX_TAG_LENGTH, required=True, field="tags")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def _check_dimensions(width: int, height: int) -> tuple[int, int]:
    if (width <= 0 or height <= 0 or width > 100_000 or height > 100_000
            or width * height > MAX_PIXELS):
        raise MediaLibraryError("image_dimensions")
    return width, height


def _inspect_image(raw: bytes) -> tuple[str, int, int, bool]:
    """Validate image bytes with Pillow and return safe public properties."""
    if not isinstance(raw, bytes) or not raw:
        raise MediaLibraryError("invalid_image")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as error:
        raise MediaLibraryError("image_decoder_unavailable") from error
    try:
        with warnings.catch_warnings():
            # Pillow otherwise emits a warning before our own pixel bound can
            # reject a decompression bomb.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                image_format = str(image.format or "")
                if image_format not in FORMAT_EXTENSIONS:
                    raise MediaLibraryError("unsupported_image")
                width, height = image.size
                _check_dimensions(int(width), int(height))
                frame_count = int(getattr(image, "n_frames", 1) or 1)
                animated = bool(getattr(image, "is_animated", False) and frame_count > 1)
                image.verify()
                return image_format, int(width), int(height), animated
    except MediaLibraryError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, EOFError, OSError, ValueError,
            SyntaxError) as error:
        raise MediaLibraryError("invalid_image") from error


class MediaLibrary:
    """An account-scoped, content-addressed image library."""

    _MANIFEST_NAME = "manifest.json"
    _MANIFEST_VERSION = 1
    _ASSET_KEYS = frozenset({
        "id", "name", "kind", "tags", "description", "width", "height",
        "animated", "format", "reusable",
    })

    def __init__(self, root: str | Path, account: str):
        account = str(account) if isinstance(account, (str, int)) and not isinstance(account, bool) else ""
        if not ACCOUNT_RE.fullmatch(account):
            raise MediaLibraryError("invalid_account")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.library_root = self.root / "media-library"
        if self.library_root.is_symlink():
            raise MediaLibraryError("unsafe_library")
        self.library_root.mkdir(exist_ok=True)
        self.account = account
        self.account_root = self.library_root / account
        if self.account_root.is_symlink():
            raise MediaLibraryError("unsafe_library")
        self.account_root.mkdir(exist_ok=True)
        self.manifest_path = self.account_root / self._MANIFEST_NAME
        if self.manifest_path.is_symlink():
            raise MediaLibraryError("unsafe_manifest")
        self._lock = threading.RLock()

    def _manifest_assets(self) -> dict[str, dict[str, Any]]:
        if not self.manifest_path.exists():
            return {}
        if self.manifest_path.is_symlink():
            raise MediaLibraryError("unsafe_manifest")
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise MediaLibraryError("invalid_manifest") from error
        if not isinstance(value, Mapping) or value.get("version") != self._MANIFEST_VERSION:
            raise MediaLibraryError("invalid_manifest")
        assets = value.get("assets")
        if not isinstance(assets, Mapping):
            raise MediaLibraryError("invalid_manifest")
        result: dict[str, dict[str, Any]] = {}
        for asset_id, metadata in assets.items():
            if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
                raise MediaLibraryError("invalid_manifest")
            result[asset_id] = self._normalize_metadata(asset_id, metadata)
        return result

    @staticmethod
    def _normalize_metadata(asset_id: str, metadata: Any) -> dict[str, Any]:
        if not isinstance(metadata, Mapping) or set(metadata) - MediaLibrary._ASSET_KEYS:
            raise MediaLibraryError("invalid_manifest")
        if metadata.get("id") != asset_id:
            raise MediaLibraryError("invalid_manifest")
        name = _safe_name(metadata.get("name"))
        kind = metadata.get("kind")
        if kind not in {"image", "meme"}:
            raise MediaLibraryError("invalid_manifest")
        tags = _safe_tags(metadata.get("tags", []))
        description = _safe_text(metadata.get("description", ""), limit=MAX_DESCRIPTION_LENGTH,
                                 field="description")
        width, height = metadata.get("width"), metadata.get("height")
        if (isinstance(width, bool) or not isinstance(width, int)
                or isinstance(height, bool) or not isinstance(height, int)):
            raise MediaLibraryError("invalid_manifest")
        _check_dimensions(width, height)
        animated = metadata.get("animated")
        if not isinstance(animated, bool):
            raise MediaLibraryError("invalid_manifest")
        image_format = metadata.get("format")
        if image_format not in FORMAT_EXTENSIONS:
            raise MediaLibraryError("invalid_manifest")
        reusable = metadata.get("reusable", True)
        if not isinstance(reusable, bool):
            raise MediaLibraryError("invalid_manifest")
        return {
            "id": asset_id, "name": name, "kind": kind, "tags": tags,
            "description": description, "width": width, "height": height,
            "animated": animated, "format": image_format, "reusable": reusable,
        }

    @staticmethod
    def _public_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {key: metadata[key] for key in (
            "id", "name", "kind", "tags", "description", "width", "height", "animated"
        )}

    def _asset_path(self, metadata: Mapping[str, Any]) -> Path:
        extension = FORMAT_EXTENSIONS.get(metadata.get("format"))
        asset_id = metadata.get("id")
        if not extension or not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
            raise MediaLibraryError("invalid_manifest")
        path = self.account_root / (asset_id + extension)
        if path.parent != self.account_root or path.is_symlink():
            raise MediaLibraryError("unsafe_asset")
        return path

    def _write_manifest(self, assets: Mapping[str, Mapping[str, Any]]) -> None:
        if self.manifest_path.is_symlink():
            raise MediaLibraryError("unsafe_manifest")
        data = {
            "version": self._MANIFEST_VERSION,
            "assets": {asset_id: dict(metadata) for asset_id, metadata in assets.items()},
        }
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.account_root,
                prefix=".manifest-", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(data, temporary, ensure_ascii=False, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.manifest_path)
            temporary_name = None
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def import_items(self, items: list[dict], *, reusable: bool = True) -> dict[str, Any]:
        if not isinstance(reusable, bool):
            return {"ok": False, "account": self.account, "items": [],
                    "errors": [_error("invalid_reusable")]}
        if not isinstance(items, list) or len(items) > MAX_BATCH_ITEMS:
            return {"ok": False, "account": self.account, "items": [],
                    "errors": [_error("batch_too_large")]}
        with self._lock:
            assets = self._manifest_assets()
            imported: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            created_paths: list[Path] = []
            for index, item in enumerate(items):
                try:
                    if not isinstance(item, Mapping) or set(item) - {"name", "base64", "kind", "tags", "description"}:
                        raise MediaLibraryError("invalid_item")
                    name = _safe_name(item.get("name"))
                    kind = item.get("kind")
                    if kind not in {"image", "meme"}:
                        raise MediaLibraryError("kind")
                    tags = _safe_tags(item.get("tags"))
                    description = _safe_text(item.get("description", ""),
                                             limit=MAX_DESCRIPTION_LENGTH, field="description")
                    encoded = item.get("base64")
                    if (not isinstance(encoded, str) or not encoded
                            or len(encoded) > ((MAX_FILE_BYTES + 2) * 4 // 3 + 8)
                            or not encoded.isascii() or any(char.isspace() for char in encoded)):
                        raise MediaLibraryError("base64")
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as error:
                        raise MediaLibraryError("base64") from error
                    if not raw or len(raw) > MAX_FILE_BYTES:
                        raise MediaLibraryError("file_too_large")
                    image_format, width, height, animated = _inspect_image(raw)
                    asset_id = hashlib.sha256(raw).hexdigest()
                    existing = assets.get(asset_id)
                    if existing is None:
                        metadata = {
                            "id": asset_id, "name": name, "kind": kind,
                            "tags": tags, "description": description,
                            "width": width, "height": height,
                            "animated": animated, "format": image_format,
                            "reusable": reusable,
                        }
                        path = self.account_root / (asset_id + FORMAT_EXTENSIONS[image_format])
                        if path.is_symlink() or path.parent != self.account_root:
                            raise MediaLibraryError("unsafe_asset")
                        if path.exists():
                            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                                raise MediaLibraryError("unsafe_asset")
                            if hashlib.sha256(path.read_bytes()).hexdigest() != asset_id:
                                # A previous interrupted write may have left an
                                # orphan with the content-addressed filename.
                                raise MediaLibraryError("unsafe_asset")
                        else:
                            try:
                                with path.open("xb") as output:
                                    output.write(raw)
                                    output.flush()
                                    os.fsync(output.fileno())
                                created_paths.append(path)
                            except FileExistsError:
                                if (path.is_symlink() or not path.is_file()
                                        or path.stat().st_size > MAX_FILE_BYTES
                                        or hashlib.sha256(path.read_bytes()).hexdigest() != asset_id):
                                    raise MediaLibraryError("unsafe_asset")
                        assets[asset_id] = metadata
                        existing = metadata
                    elif reusable and not existing["reusable"]:
                        # An explicit library import may promote a previously
                        # temporary forwarding copy.  A later temporary copy
                        # must never make a reusable asset private again.
                        existing["reusable"] = True
                    imported.append(self._public_metadata(existing))
                except (MediaLibraryError, OSError):
                    errors.append(_error("invalid_item", index))
            try:
                self._write_manifest(assets)
            except Exception:
                for path in created_paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            result: dict[str, Any] = {
                "ok": not errors, "account": self.account, "items": imported,
            }
            if errors:
                result["errors"] = errors
            return result

    def import_directory(self, path: str | Path) -> dict[str, Any]:
        try:
            directory = Path(path)
            if directory.is_symlink() or not directory.is_dir():
                return {"ok": False, "account": self.account, "items": [],
                        "errors": [_error("invalid_directory")]}
            children = []
            for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
                if child.is_symlink() or not child.is_file() or child.suffix.casefold() not in SUPPORTED_SUFFIXES:
                    continue
                try:
                    if child.stat().st_size > MAX_FILE_BYTES:
                        children.append((child, None))
                        continue
                    children.append((child, child.read_bytes()))
                except OSError:
                    children.append((child, None))
            if len(children) > MAX_BATCH_ITEMS:
                return {"ok": False, "account": self.account, "items": [],
                        "errors": [_error("batch_too_large")]}
            items = []
            for child, raw in children:
                if raw is None:
                    # Keep the index/name stable without exposing the source path.
                    items.append({"name": child.name, "base64": "", "kind": "image"})
                else:
                    items.append({
                        "name": child.name, "base64": base64.b64encode(raw).decode("ascii"),
                        "kind": "image", "description": child.stem,
                    })
            return self.import_items(items)
        except OSError:
            return {"ok": False, "account": self.account, "items": [],
                    "errors": [_error("invalid_directory")]}

    def search(self, query: str = "", limit: int = MAX_BATCH_ITEMS) -> dict[str, Any]:
        if not isinstance(query, str) or isinstance(limit, bool) or not isinstance(limit, int):
            return {"ok": False, "account": self.account, "items": [],
                    "errors": [_error("invalid_query")]}
        limit = max(1, min(MAX_BATCH_ITEMS, limit))
        query = query.strip().casefold()
        with self._lock:
            assets = self._manifest_assets()
            rows = []
            for metadata in assets.values():
                if not metadata["reusable"]:
                    continue
                haystack = " ".join([
                    metadata["id"], metadata["name"], metadata["kind"],
                    metadata["description"], *metadata["tags"],
                ]).casefold()
                if not query or query in haystack:
                    rows.append(self._public_metadata(metadata))
            rows.sort(key=lambda item: (item["name"].casefold(), item["id"]))
            return {"ok": True, "account": self.account, "items": rows[:limit]}

    def catalog(self, limit: int = 64) -> list[dict[str, Any]]:
        """Small reusable catalogue for model selection, never forwarded uploads."""
        with self._lock:
            rows = [self._public_metadata(item) for item in self._manifest_assets().values()
                    if item['reusable']]
            return sorted(rows, key=lambda item: (item['name'].casefold(), item['id']))[:max(1, min(64, limit))]

    def get(self, asset_ids: list[str], *, reusable_only: bool = False) -> dict[str, Any]:
        if not isinstance(reusable_only, bool):
            return {"ok": False, "account": self.account, "items": [], "missing": [],
                    "errors": [_error("invalid_reusable_only")]}
        if not isinstance(asset_ids, list):
            return {"ok": False, "account": self.account, "items": [], "missing": [],
                    "errors": [_error("invalid_ids")]}
        with self._lock:
            assets = self._manifest_assets()
            rows = []
            missing = []
            errors = []
            seen: set[str] = set()
            for asset_id in asset_ids:
                if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
                    errors.append(_error("invalid_id"))
                    continue
                if asset_id in seen:
                    continue
                seen.add(asset_id)
                metadata = assets.get(asset_id)
                if metadata is None:
                    missing.append(asset_id)
                elif reusable_only and not metadata["reusable"]:
                    errors.append({"error": "not_reusable", "id": asset_id})
                else:
                    rows.append(self._public_metadata(metadata))
            result = {"ok": not errors, "account": self.account, "items": rows}
            if missing:
                result["missing"] = missing
            if errors:
                result["errors"] = errors
            return result

    def delete(self, asset_ids: list[str]) -> dict[str, Any]:
        if not isinstance(asset_ids, list):
            return {"ok": False, "account": self.account, "deleted": [], "missing": [],
                    "errors": [_error("invalid_ids")]}
        with self._lock:
            assets = self._manifest_assets()
            deleted: list[str] = []
            missing: list[str] = []
            errors: list[dict[str, Any]] = []
            for asset_id in asset_ids:
                if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
                    errors.append(_error("invalid_id"))
                    continue
                metadata = assets.get(asset_id)
                if metadata is None:
                    missing.append(asset_id)
                    continue
                path = self._asset_path(metadata)
                if path.is_symlink():
                    errors.append(_error("unsafe_asset"))
                    continue
                assets.pop(asset_id, None)
                deleted.append(asset_id)
            self._write_manifest(assets)
            for asset_id in deleted:
                # The extension is taken from the old metadata captured above
                # only through a fresh manifest-independent lookup in memory.
                # No path is returned to callers.
                # Find the now-unreachable asset by allowed digest extensions.
                for extension in FORMAT_EXTENSIONS.values():
                    path = self.account_root / (asset_id + extension)
                    if path.is_symlink() or path.parent != self.account_root:
                        continue
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        errors.append(_error("delete_failed"))
            return {"ok": not errors, "account": self.account, "deleted": deleted,
                    "missing": missing, "errors": errors}

    def segments(self, asset_ids: list[str]) -> list[dict[str, Any]]:
        if not isinstance(asset_ids, list):
            return []
        with self._lock:
            assets = self._manifest_assets()
            result: list[dict[str, Any]] = []
            seen: set[str] = set()
            for asset_id in asset_ids:
                if (not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id)
                        or asset_id in seen):
                    continue
                seen.add(asset_id)
                metadata = assets.get(asset_id)
                if metadata is None:
                    continue
                path = self._asset_path(metadata)
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    raw = path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                except OSError:
                    continue
                if digest != asset_id:
                    continue
                # OneBot accepts a base64 URI for image segments.  Keep the
                # absolute storage path inside this library: callers that
                # surface metadata to the model never receive either path or
                # bytes, while the gateway can send the exact stored bytes.
                data: dict[str, Any] = {
                    "file": "base64://" + base64.b64encode(raw).decode("ascii"),
                }
                if metadata["kind"] == "meme":
                    data["sub_type"] = 1
                result.append({"type": "image", "data": data})
            return result
