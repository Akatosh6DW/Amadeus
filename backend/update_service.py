"""Release-only update orchestration for the packaged desktop application.

The updater deliberately has one remote source (the public GitHub release for
this repository) and one install target (the running executable's directory).
It downloads the same verified split archive used by the release installer,
stages it beside the installation, and asks a detached PowerShell helper to
perform the process-aware directory swap after this process exits.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Mapping

from backend.providers import network as provider_network
from tools.download_runtime_file import DownloadError, extract_archive, download_file


GITHUB_RELEASES_URL = "https://api.github.com/repos/Akatosh6DW/Amadeus/releases/latest"
GITHUB_REPOSITORY = "Akatosh6DW/Amadeus"
USER_AGENT = "Amadeus-update/1"
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLAIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_RELEASE_JSON_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_UPDATE_STATUS_BYTES = 16 * 1024
CHUNK_SIZE = 4 * 1024 * 1024


class UpdateError(RuntimeError):
    """Raised when a release is unavailable or fails verification."""


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: str
    html_url: str
    manifest_name: str
    manifest_url: str
    assets: Mapping[str, dict[str, Any]]


def _version_key(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        raise UpdateError("GitHub release tag is not a stable semantic version")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _normalise_sha256(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith("sha256:"):
        raw = raw[7:]
    return raw if SHA256_RE.fullmatch(raw) else ""


def _safe_asset_name(value: object) -> str:
    name = str(value or "").strip()
    if not PLAIN_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise UpdateError("release asset name is unsafe")
    return name


def _github_asset_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "api.github.com"}:
        raise UpdateError("release asset URL is outside the fixed GitHub repository")
    return url


def _github_api_asset_url(value: object) -> str:
    """Return only an API URL for an asset in this exact repository.

    The GitHub browser URL can redirect through a release CDN and has proved
    less reliable for multi-gigabyte downloads.  The API asset endpoint with
    ``Accept: application/octet-stream`` preserves the tested range/resume
    path used by the release installer.
    """

    url = str(value or "").strip()
    parsed = urllib.parse.urlsplit(url)
    prefix = f"/repos/{GITHUB_REPOSITORY}/releases/assets/"
    suffix = parsed.path[len(prefix):] if parsed.path.startswith(prefix) else ""
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or not suffix.isdigit()
        or parsed.query
        or parsed.fragment
    ):
        raise UpdateError("release asset URL is not the fixed GitHub API asset endpoint")
    return url


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


class ReleaseUpdateService:
    """Check, download, stage, and hand off one release update.

    ``shutdown`` and ``launch_helper`` are injected so tests can exercise the
    full state machine without stopping the test runner or the real desktop.
    """

    def __init__(
        self,
        *,
        release_mode: bool,
        current_version: str,
        install_root: Path,
        cache_root: Path,
        helper_path: Path,
        shutdown: Callable[[], None] | None = None,
        settings: Callable[[], Mapping[str, Any]] | None = None,
        process_ids: Callable[[], list[int]] | None = None,
        http_json: Callable[[str], dict[str, Any]] | None = None,
        downloader: Callable[..., Path] | None = None,
        launcher: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self.release_mode = bool(release_mode)
        self.current_version = str(current_version or "").removeprefix("v")
        self.install_root = Path(install_root).resolve()
        self.cache_root = Path(cache_root).resolve()
        self.helper_path = Path(helper_path).resolve()
        self._shutdown = shutdown or (lambda: None)
        self._settings = settings or (lambda: {})
        self._process_ids = process_ids or (lambda: [])
        self._http_json = http_json or self._fetch_json
        self._downloader = downloader or self._download_file
        self._launcher = launcher or self._launch_helper
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "phase": "idle",
            "available": None,
            "currentVersion": self.current_version,
            "latestVersion": None,
            "latestTag": None,
            "releaseUrl": None,
            "totalBytes": 0,
            "downloadedBytes": 0,
            "error": None,
        }
        persisted_error = self._read_persisted_failure()
        if persisted_error:
            self._state.update(phase="failed", available=False, error=persisted_error)

    def _set_state(self, **updates: Any) -> None:
        with self._lock:
            self._state.update(updates)

    def _read_persisted_failure(self) -> str:
        """Restore a helper failure for the first UI render after restart."""

        if not self.release_mode or not self.cache_root.is_dir():
            return ""
        candidates: list[Path] = []
        try:
            candidates = [
                path
                for path in self.cache_root.glob("*/update-status.json")
                if path.is_file() and not path.is_symlink()
            ]
        except OSError:
            return ""
        for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                raw = path.read_bytes()
                if len(raw) > MAX_UPDATE_STATUS_BYTES:
                    continue
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("ok") is False:
                return str(payload.get("error") or "上次更新失败，已恢复原版本。")[:500]
            if isinstance(payload, dict) and payload.get("ok") is True:
                return ""
        return ""

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            worker = self._worker
        state["running"] = bool(worker and worker.is_alive())
        state["releaseMode"] = self.release_mode
        state["repository"] = GITHUB_REPOSITORY
        return {"ok": True, **state} if self.release_mode else {
            "ok": False,
            "releaseMode": False,
            "available": False,
            "code": "release_only",
            "message": "更新功能仅在发行版中可用。",
        }

    def check(self) -> dict[str, Any]:
        if not self.release_mode:
            return self.status()
        self._set_state(phase="checking", error=None)
        try:
            info = self._resolve_release()
            available = _version_key(info.version) > _version_key(self.current_version)
            self._set_state(
                phase="idle",
                available=available,
                latestVersion=info.version,
                latestTag=info.tag,
                releaseUrl=info.html_url,
                error=None,
            )
            return self.status()
        except Exception as error:
            self._set_state(phase="failed", available=False, error=self._public_error(error))
            return self.status()

    def start(self, *, confirmed: bool) -> dict[str, Any]:
        if not self.release_mode:
            return self.status()
        if not confirmed:
            return {"ok": False, "code": "confirmation_required", "message": "请先确认立即更新。"}
        with self._lock:
            if self._worker and self._worker.is_alive():
                return {"ok": True, "started": False, "alreadyRunning": True, **self.status()}
            self._set_state(phase="queued", error=None, downloadedBytes=0)
            worker = threading.Thread(target=self._run_update, name="amadeus-release-update", daemon=True)
            self._worker = worker
            worker.start()
        return {"ok": True, "started": True, "message": "更新已开始，请等待应用重启。", **self.status()}

    def _fetch_json(self, url: str) -> dict[str, Any]:
        if url != GITHUB_RELEASES_URL:
            raise UpdateError("only the fixed GitHub latest-release endpoint is allowed")
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
        try:
            response = self._opener_for(url).open(request, timeout=20)
        except (urllib.error.URLError, OSError) as error:
            raise UpdateError("无法读取 GitHub 最新发行版信息") from error
        try:
            payload = response.read(MAX_RELEASE_JSON_BYTES + 1)
        finally:
            response.close()
        if len(payload) > MAX_RELEASE_JSON_BYTES:
            raise UpdateError("GitHub 发行版信息过大")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("GitHub 发行版信息格式无效") from error
        if not isinstance(value, dict):
            raise UpdateError("GitHub 发行版信息格式无效")
        return value

    def _opener_for(self, url: str) -> urllib.request.OpenerDirector:
        settings = dict(self._settings() or {})
        mode = provider_network.api_network_mode(url, settings)
        mapping, _source = provider_network._api_proxy_mapping(url, mode, settings)
        return urllib.request.build_opener(urllib.request.ProxyHandler(mapping))

    def _download_file(self, url: str, output: Path, **kwargs: Any) -> Path:
        return download_file(url, output, opener=self._opener_for(url), **kwargs)

    def _resolve_release(self) -> ReleaseInfo:
        payload = self._http_json(GITHUB_RELEASES_URL)
        tag = str(payload.get("tag_name") or "").strip()
        version = tag.removeprefix("v")
        _version_key(version)
        if bool(payload.get("draft")) or bool(payload.get("prerelease")):
            raise UpdateError("最新 GitHub 发行版不是稳定版本")
        raw_assets = payload.get("assets")
        if not isinstance(raw_assets, list):
            raise UpdateError("GitHub 发行版缺少资产列表")
        assets: dict[str, dict[str, Any]] = {}
        for raw in raw_assets:
            if not isinstance(raw, dict):
                continue
            name = _safe_asset_name(raw.get("name"))
            size = int(raw.get("size") or 0)
            digest = _normalise_sha256(raw.get("digest"))
            if size <= 0 or not digest:
                raise UpdateError(f"GitHub 资产缺少可验证的大小或 SHA-256：{name}")
            assets[name] = {
                "name": name,
                "url": _github_api_asset_url(raw.get("url")),
                "size": size,
                "sha256": digest,
            }
        manifest_name = f"AMADEUS-portable-{tag}.parts.json"
        manifest = assets.get(manifest_name)
        if manifest is None:
            raise UpdateError("最新发行版缺少可验证的分卷 manifest")
        html_url = str(payload.get("html_url") or "").strip()
        if html_url:
            html_url = _github_asset_url(html_url)
        return ReleaseInfo(
            tag=tag,
            version=version,
            html_url=html_url,
            manifest_name=manifest_name,
            manifest_url=str(manifest["url"]),
            assets=assets,
        )

    def _load_manifest(self, info: ReleaseInfo, directory: Path) -> dict[str, Any]:
        asset = info.assets[info.manifest_name]
        manifest_path = directory / info.manifest_name
        expected_size = int(asset.get("size") or 0) or None
        self._downloader(
            info.manifest_url,
            manifest_path,
            expected_size=expected_size,
            expected_sha256=str(asset.get("sha256") or ""),
            validate_archive=False,
            request_headers={"Accept": "application/octet-stream"},
        )
        try:
            raw = manifest_path.read_bytes()
            if len(raw) > MAX_MANIFEST_BYTES:
                raise UpdateError("分卷 manifest 过大")
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("分卷 manifest 无效") from error
        self._validate_manifest(info, manifest)
        return manifest

    @staticmethod
    def _validate_manifest(info: ReleaseInfo, manifest: object) -> None:
        if not isinstance(manifest, dict) or manifest.get("formatVersion") != 1:
            raise UpdateError("分卷 manifest 版本无效")
        if str(manifest.get("version") or "") != info.tag:
            raise UpdateError("分卷 manifest 版本与发行版不一致")
        archive_name = str(manifest.get("archiveName") or "")
        if not PLAIN_NAME_RE.fullmatch(archive_name) or not archive_name.endswith(".zip"):
            raise UpdateError("分卷 manifest archiveName 不安全")
        archive_bytes = manifest.get("archiveBytes")
        archive_sha = str(manifest.get("archiveSha256") or "").lower()
        if not isinstance(archive_bytes, int) or archive_bytes <= 0 or not SHA256_RE.fullmatch(archive_sha):
            raise UpdateError("分卷 manifest archive 校验字段无效")
        parts = manifest.get("parts")
        if not isinstance(parts, list) or not parts:
            raise UpdateError("分卷 manifest 没有 parts")
        total = 0
        for expected_index, part in enumerate(parts, 1):
            if not isinstance(part, dict) or int(part.get("index") or 0) != expected_index:
                raise UpdateError("分卷序号不连续")
            expected_name = f"{archive_name}.part{expected_index:03d}"
            if str(part.get("name") or "") != expected_name:
                raise UpdateError("分卷名称不匹配")
            size = part.get("bytes")
            digest = str(part.get("sha256") or "").lower()
            if not isinstance(size, int) or size <= 0 or not SHA256_RE.fullmatch(digest):
                raise UpdateError("分卷校验字段无效")
            total += size
        if total != archive_bytes:
            raise UpdateError("分卷大小与 archiveBytes 不一致")

    def _run_update(self) -> None:
        stage: Path | None = None
        try:
            info = self._resolve_release()
            if _version_key(info.version) <= _version_key(self.current_version):
                self._set_state(phase="idle", available=False, latestVersion=info.version, latestTag=info.tag)
                return
            cache = self.cache_root / info.tag
            cache.mkdir(parents=True, exist_ok=True)
            manifest = self._load_manifest(info, cache)
            parts = list(manifest["parts"])
            total_bytes = int(manifest["archiveBytes"])
            self._set_state(
                phase="downloading",
                available=True,
                latestVersion=info.version,
                latestTag=info.tag,
                releaseUrl=info.html_url,
                totalBytes=total_bytes,
                downloadedBytes=0,
                error=None,
            )
            downloaded_before = 0
            for part in parts:
                name = str(part["name"])
                asset = info.assets.get(name)
                if asset is None:
                    raise UpdateError(f"发行版缺少分卷：{name}")
                declared_size = int(asset.get("size") or 0)
                expected_size = int(part["bytes"])
                if declared_size and declared_size != expected_size:
                    raise UpdateError(f"分卷大小与 GitHub 资产不一致：{name}")
                declared_sha = str(asset.get("sha256") or "")
                if declared_sha and declared_sha != str(part["sha256"]).lower():
                    raise UpdateError(f"分卷 SHA-256 与 GitHub 资产不一致：{name}")

                def progress(done: int, _total: int | None, *, base=downloaded_before) -> None:
                    self._set_state(downloadedBytes=min(total_bytes, base + int(done)))

                self._downloader(
                    str(asset["url"]),
                    cache / name,
                    expected_size=expected_size,
                    expected_sha256=str(part["sha256"]),
                    progress=progress,
                    validate_archive=False,
                    request_headers={"Accept": "application/octet-stream"},
                )
                downloaded_before += expected_size
                self._set_state(downloadedBytes=downloaded_before)

            self._set_state(phase="verifying", downloadedBytes=total_bytes)
            archive = self._merge_parts(cache, str(manifest["archiveName"]), parts, total_bytes, str(manifest["archiveSha256"]))
            self._set_state(phase="staging")
            staging_parent = self.install_root.parent / ".amadeus-update-staging"
            staging_parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=f"{info.tag}-", dir=staging_parent))
            extract_archive(archive, stage)
            roots = [child for child in stage.iterdir()]
            if len(roots) != 1 or roots[0].name != "AMADEUS":
                raise UpdateError("更新归档必须只有一个 AMADEUS 根目录")
            staged_release = stage / "AMADEUS"
            self._validate_staged_release(staged_release, info.version)
            helper_copy = cache / "update_release.ps1"
            helper_temporary = cache / f".{helper_copy.name}.{uuid.uuid4().hex}.tmp"
            if not self.helper_path.is_file() or self.helper_path.is_symlink():
                raise UpdateError("更新 helper 不存在或不安全")
            try:
                shutil.copy2(self.helper_path, helper_temporary)
                os.replace(helper_temporary, helper_copy)
            finally:
                helper_temporary.unlink(missing_ok=True)
            command = [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper_copy),
                "-InstallDirectory",
                str(self.install_root),
                "-StagedRelease",
                str(staged_release),
                "-StagingDirectory",
                str(stage),
                "-ParentPid",
                str(os.getpid()),
                "-ExecutableName",
                "AMADEUS.exe",
                "-LogPath",
                str(cache / "update-helper.log"),
                "-StatusPath",
                str(cache / "update-status.json"),
            ]
            owned_process_ids = []
            try:
                owned_process_ids = [
                    int(value)
                    for value in self._process_ids()
                    if int(value) > 0
                ]
            except (TypeError, ValueError):
                owned_process_ids = []
            if owned_process_ids:
                command.extend(["-ProcessIds", ",".join(str(value) for value in owned_process_ids)])
            self._launcher(command)
            self._set_state(phase="restarting", error=None)
            self._shutdown()
            stage = None
        except Exception as error:
            self._set_state(phase="failed", error=self._public_error(error))
            if stage is not None:
                _remove(stage)

    @staticmethod
    def _validate_staged_release(path: Path, expected_version: str) -> None:
        if not path.is_dir() or path.is_symlink():
            raise UpdateError("更新归档缺少安全的 AMADEUS 根目录")
        executable = path / "AMADEUS.exe"
        if not executable.is_file() or executable.is_symlink():
            raise UpdateError("更新归档缺少 AMADEUS.exe")
        internal = path / "_internal"
        if not internal.is_dir() or internal.is_symlink():
            raise UpdateError("更新归档缺少 _internal 运行时目录")
        manifest_path = path / "RELEASE_MANIFEST.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise UpdateError("更新归档缺少 RELEASE_MANIFEST.json")
        try:
            raw = manifest_path.read_bytes()
            if len(raw) > MAX_MANIFEST_BYTES:
                raise UpdateError("RELEASE_MANIFEST.json 过大")
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("RELEASE_MANIFEST.json 无效") from error
        if not isinstance(manifest, dict) or manifest.get("product") != "Amadeus System":
            raise UpdateError("RELEASE_MANIFEST.json product 不匹配")
        actual_build = str(manifest.get("build") or "").removeprefix("v")
        expected_build = str(expected_version or "").removeprefix("v")
        if not expected_build or actual_build != expected_build:
            raise UpdateError("RELEASE_MANIFEST.json build 与发行版不匹配")

    @staticmethod
    def _merge_parts(cache: Path, archive_name: str, parts: list[dict[str, Any]], expected_bytes: int, expected_sha: str) -> Path:
        archive = cache / archive_name
        if archive.is_file() and archive.stat().st_size == expected_bytes and _sha256(archive) == expected_sha.lower():
            return archive
        temporary = cache / f".{archive_name}.{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        actual = 0
        try:
            with temporary.open("wb") as output:
                for part in parts:
                    source = cache / str(part["name"])
                    expected_size = int(part["bytes"])
                    expected_sha_part = str(part["sha256"]).lower()
                    if not source.is_file() or source.stat().st_size != expected_size or _sha256(source) != expected_sha_part:
                        raise UpdateError(f"分卷校验失败：{source.name}")
                    with source.open("rb") as incoming:
                        while True:
                            chunk = incoming.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            output.write(chunk)
                            digest.update(chunk)
                            actual += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if actual != expected_bytes or digest.hexdigest() != expected_sha.lower():
                raise UpdateError("合并后的发布归档校验失败")
            os.replace(temporary, archive)
            return archive
        finally:
            temporary.unlink(missing_ok=True)

    def _launch_helper(self, arguments: list[str]) -> Any:
        powershell = shutil.which("powershell.exe") or "powershell.exe"
        flags = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "creationflags": flags,
            "close_fds": True,
        }
        startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_type is not None:
            startupinfo = startupinfo_type()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            kwargs["startupinfo"] = startupinfo
        return subprocess.Popen([powershell, *arguments], **kwargs)

    @staticmethod
    def _public_error(error: Exception) -> str:
        if isinstance(error, (UpdateError, DownloadError)):
            return str(error)[:500]
        return f"更新失败：{type(error).__name__}"


__all__ = [
    "GITHUB_RELEASES_URL",
    "GITHUB_REPOSITORY",
    "ReleaseInfo",
    "ReleaseUpdateService",
    "UpdateError",
]
