"""Private saved connection profiles for the local model settings page.

Profiles are deliberately separate from the public settings projection.  The
on-disk record contains the key so a profile can be activated later, while
every value returned to the browser contains only ``apiKeySet`` metadata.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable, Mapping
import urllib.parse
import uuid


PROFILE_VERSION = 1
MAX_PROFILES = 32
MAX_NAME_LENGTH = 80
MAX_ENDPOINT_LENGTH = 2048
MAX_MODEL_LENGTH = 256
MAX_KEY_LENGTH = 4096
MAX_PROXY_LENGTH = 2048
PROFILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
NETWORK_MODES = frozenset({"auto", "direct", "system_proxy", "explicit_proxy"})
PROFILE_FIELDS = (
    "apiEndpoint",
    "apiModel",
    "apiNetworkMode",
    "apiProxyUrl",
    "useResponseFormat",
    "cognitiveRepairModel",
)


class ModelProfileError(ValueError):
    """A safe, user-facing profile validation or storage error."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: object, *, maximum: int, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ModelProfileError("profile_field_required")
    if len(result) > maximum:
        raise ModelProfileError("profile_field_too_long")
    return result


def _normalise_endpoint(value: object) -> str:
    return _text(value, maximum=MAX_ENDPOINT_LENGTH, required=True)


def _provider_identity(endpoint: object) -> tuple[str, int | None, str]:
    """Return a conservative provider identity for masked-key reuse checks."""

    value = str(endpoint or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme and parsed.hostname:
        try:
            port = parsed.port
        except ValueError:
            return "", None, value.lower()
        return parsed.hostname.lower(), port, parsed.scheme.lower()
    return "", None, value.lower()


def _same_provider(source_endpoint: object, target_endpoint: object) -> bool:
    source = _provider_identity(source_endpoint)
    target = _provider_identity(target_endpoint)
    if source[0] or target[0]:
        return source == target
    return source == target


def _new_id() -> str:
    return uuid.uuid4().hex


class ModelProfileService:
    """Read, mutate, and activate local model connection profiles."""

    def __init__(
        self,
        path: Path,
        *,
        load_settings: Callable[[], Mapping[str, Any]],
        save_settings: Callable[[dict[str, Any]], Mapping[str, Any]],
        is_configured: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        self._load_settings = load_settings
        self._save_settings = save_settings
        self._is_configured = is_configured or (
            lambda settings: bool(settings.get("apiEndpoint") and settings.get("apiModel"))
        )

    def _empty(self) -> dict[str, Any]:
        return {
            "version": PROFILE_VERSION,
            "profiles": [],
            "activeProfileId": None,
            "currentSnapshotCreated": False,
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        if self.path.is_symlink():
            raise ModelProfileError("model_profiles_unsafe_path")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelProfileError("model_profiles_invalid") from error
        if not isinstance(payload, dict) or payload.get("version") != PROFILE_VERSION:
            raise ModelProfileError("model_profiles_invalid")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list) or len(raw_profiles) > MAX_PROFILES:
            raise ModelProfileError("model_profiles_invalid")
        profiles = []
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                raise ModelProfileError("model_profiles_invalid")
            profiles.append(self._normalise_record(raw, require_secret=True))
        active = payload.get("activeProfileId")
        if active is not None and (not isinstance(active, str) or not PROFILE_ID_RE.fullmatch(active)):
            raise ModelProfileError("model_profiles_invalid")
        return {
            "version": PROFILE_VERSION,
            "profiles": profiles,
            "activeProfileId": active,
            "currentSnapshotCreated": bool(payload.get("currentSnapshotCreated")),
        }

    def _write(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.is_symlink():
            raise ModelProfileError("model_profiles_unsafe_path")
        payload = json.dumps(dict(state), ensure_ascii=False, indent=2) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            temporary = None
        except OSError as error:
            raise ModelProfileError("model_profiles_write_failed") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _normalise_record(raw: Mapping[str, Any], *, require_secret: bool) -> dict[str, Any]:
        profile_id = str(raw.get("id") or "").strip()
        if not PROFILE_ID_RE.fullmatch(profile_id):
            raise ModelProfileError("profile_id_invalid")
        name = _text(raw.get("name"), maximum=MAX_NAME_LENGTH, required=True)
        endpoint = _normalise_endpoint(raw.get("apiEndpoint"))
        model = _text(raw.get("apiModel"), maximum=MAX_MODEL_LENGTH, required=True)
        mode = str(raw.get("apiNetworkMode") or "auto").strip().lower()
        if mode not in NETWORK_MODES:
            raise ModelProfileError("profile_network_mode_invalid")
        proxy = _text(raw.get("apiProxyUrl"), maximum=MAX_PROXY_LENGTH)
        if proxy:
            parsed = urllib.parse.urlsplit(proxy)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ModelProfileError("profile_proxy_invalid")
        key = str(raw.get("apiKey") or "").strip()
        if len(key) > MAX_KEY_LENGTH:
            raise ModelProfileError("profile_key_too_long")
        if require_secret and not isinstance(raw.get("apiKey", ""), str):
            raise ModelProfileError("model_profiles_invalid")
        cognitive = str(raw.get("cognitiveRepairModel") or "follow").strip().lower()
        if cognitive not in {"follow", "pro"}:
            cognitive = "follow"
        return {
            "id": profile_id,
            "name": name,
            "apiEndpoint": endpoint,
            "apiModel": model,
            "apiKey": key,
            "apiNetworkMode": mode,
            "apiProxyUrl": proxy,
            "useResponseFormat": bool(raw.get("useResponseFormat", True)),
            "cognitiveRepairModel": cognitive,
            "updatedAt": str(raw.get("updatedAt") or _now())[:64],
        }

    @staticmethod
    def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: record.get(key)
            for key in (
                "id",
                "name",
                "apiEndpoint",
                "apiModel",
                "apiNetworkMode",
                "apiProxyUrl",
                "useResponseFormat",
                "cognitiveRepairModel",
                "updatedAt",
            )
        } | {"apiKeySet": bool(record.get("apiKey"))}

    @staticmethod
    def _profile_matches(record: Mapping[str, Any], settings: Mapping[str, Any]) -> bool:
        return all(record.get(key) == settings.get(key) for key in PROFILE_FIELDS) and (
            str(record.get("apiKey") or "") == str(settings.get("apiKey") or "")
        )

    def _ensure_current_snapshot(self, state: dict[str, Any], settings: Mapping[str, Any]) -> bool:
        if state["currentSnapshotCreated"] or state["profiles"] or not self._is_configured(settings):
            return False
        record = {
            "id": _new_id(),
            "name": "当前配置",
            **{key: settings.get(key) for key in PROFILE_FIELDS},
            "apiKey": str(settings.get("apiKey") or "").strip(),
            "updatedAt": _now(),
        }
        state["profiles"] = [self._normalise_record(record, require_secret=True)]
        state["activeProfileId"] = record["id"]
        state["currentSnapshotCreated"] = True
        return True

    def _state_for_current(self, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
        current = settings if settings is not None else self._load_settings()
        state = self._read()
        if self._ensure_current_snapshot(state, current):
            self._write(state)
        return state

    def list_profiles(self, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
        current = settings if settings is not None else self._load_settings()
        state = self._state_for_current(current)
        active = state.get("activeProfileId")
        active_record = next((item for item in state["profiles"] if item["id"] == active), None)
        if active_record is None or not self._profile_matches(active_record, current):
            active = None
        return {
            "ok": True,
            "profiles": [self._public_record(item) for item in state["profiles"]],
            "activeProfileId": active,
        }

    def _find(self, state: Mapping[str, Any], profile_id: object) -> dict[str, Any]:
        value = str(profile_id or "").strip()
        if not PROFILE_ID_RE.fullmatch(value):
            raise ModelProfileError("profile_id_invalid")
        for record in state["profiles"]:
            if record["id"] == value:
                return record
        raise ModelProfileError("profile_not_found")

    def _resolve_key(
        self,
        raw_value: object,
        *,
        source: Mapping[str, Any] | None,
        current: Mapping[str, Any],
        target_endpoint: str,
    ) -> str:
        value = "__KEEP__" if raw_value is None else str(raw_value).strip()
        if value == "__CLEAR__":
            return ""
        if value == "__KEEP__":
            source_record = source if source is not None else current
            source_key = str(source_record.get("apiKey") or "").strip()
            source_endpoint = source_record.get("apiEndpoint")
            if source_key and not _same_provider(source_endpoint, target_endpoint):
                raise ModelProfileError("profile_key_required_for_endpoint_change")
            return source_key
        if len(value) > MAX_KEY_LENGTH:
            raise ModelProfileError("profile_key_too_long")
        return value

    def save_profile(
        self,
        body: Mapping[str, Any],
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = settings if settings is not None else self._load_settings()
        state = self._state_for_current(current)
        profile_id = str(body.get("id") or "").strip()
        existing = self._find(state, profile_id) if profile_id else None
        if existing is None and len(state["profiles"]) >= MAX_PROFILES:
            raise ModelProfileError("profile_limit_reached")
        source = existing or current
        endpoint = _normalise_endpoint(body.get("apiEndpoint", source.get("apiEndpoint")))
        model = _text(body.get("apiModel", source.get("apiModel")), maximum=MAX_MODEL_LENGTH, required=True)
        name = _text(body.get("name", existing.get("name") if existing else ""), maximum=MAX_NAME_LENGTH, required=True)
        mode = str(body.get("apiNetworkMode", source.get("apiNetworkMode") or "auto")).strip().lower()
        if mode not in NETWORK_MODES:
            raise ModelProfileError("profile_network_mode_invalid")
        proxy = _text(body.get("apiProxyUrl", source.get("apiProxyUrl")), maximum=MAX_PROXY_LENGTH)
        if proxy:
            parsed_proxy = urllib.parse.urlsplit(proxy)
            if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
                raise ModelProfileError("profile_proxy_invalid")
        key = self._resolve_key(
            body.get("apiKey", "__KEEP__"),
            source=existing or None,
            current=current,
            target_endpoint=endpoint,
        )
        record = {
            "id": existing["id"] if existing else _new_id(),
            "name": name,
            "apiEndpoint": endpoint,
            "apiModel": model,
            "apiKey": key,
            "apiNetworkMode": mode,
            "apiProxyUrl": proxy,
            "useResponseFormat": bool(body.get("useResponseFormat", source.get("useResponseFormat", True))),
            "cognitiveRepairModel": str(
                body.get("cognitiveRepairModel", source.get("cognitiveRepairModel", "follow")) or "follow"
            ).strip().lower(),
            "updatedAt": _now(),
        }
        if record["cognitiveRepairModel"] not in {"follow", "pro"}:
            record["cognitiveRepairModel"] = "follow"
        normalised = self._normalise_record(record, require_secret=True)
        if existing:
            state["profiles"] = [normalised if item["id"] == existing["id"] else item for item in state["profiles"]]
        else:
            state["profiles"].append(normalised)
        state["currentSnapshotCreated"] = True
        self._write(state)
        return self.list_profiles(current) | {"savedProfileId": normalised["id"]}

    def activate_profile(
        self,
        profile_id: object,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = settings if settings is not None else self._load_settings()
        state = self._state_for_current(current)
        record = self._find(state, profile_id)
        update = {key: record.get(key) for key in PROFILE_FIELDS}
        update["apiKey"] = record.get("apiKey") or "__CLEAR__"
        saved = dict(self._save_settings(update))
        state["activeProfileId"] = record["id"]
        self._write(state)
        return {
            "ok": True,
            "profile": self._public_record(record),
            "settings": saved,
            "profiles": [self._public_record(item) for item in state["profiles"]],
            "activeProfileId": record["id"],
        }

    def delete_profile(
        self,
        profile_id: object,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = settings if settings is not None else self._load_settings()
        state = self._state_for_current(current)
        record = self._find(state, profile_id)
        state["profiles"] = [item for item in state["profiles"] if item["id"] != record["id"]]
        if state.get("activeProfileId") == record["id"]:
            state["activeProfileId"] = None
        state["currentSnapshotCreated"] = True
        self._write(state)
        return self.list_profiles(current) | {"deletedProfileId": record["id"]}


__all__ = ["ModelProfileError", "ModelProfileService", "PROFILE_FIELDS"]
