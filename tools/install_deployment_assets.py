"""Install versioned runtime resources without storing weights in source Git."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import urllib.request
from urllib.parse import quote, unquote, urlsplit
import zipfile


ROOT = Path(__file__).resolve().parents[1]
GITHUB_AUTH_HOSTS = frozenset({"github.com", "api.github.com"})
GITHUB_RELEASE_DOWNLOAD_RE = re.compile(
    r"^/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)$"
)


class _CredentialSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never carry the GitHub bearer header onto a non GitHub host."""

    def redirect_request(self, request, file, code, message, headers, newurl):
        redirected = super().redirect_request(request, file, code, message, headers, newurl)
        if redirected and (urlsplit(newurl).hostname or "").lower() not in GITHUB_AUTH_HOSTS:
            redirected.headers.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


def github_credential() -> str:
    """Read an existing GitHub credential without persisting or printing it."""

    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value

    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run(
                [gh, "auth", "token", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            value = result.stdout.strip()
            if value:
                return value

    # Git Credential Manager is often already configured for a private source
    # checkout even when GitHub CLI is not installed.  Ask it in memory only;
    # the noninteractive environment prevents a new prompt during deployment.
    git = shutil.which("git")
    if git:
        child_env = os.environ.copy()
        child_env["GIT_TERMINAL_PROMPT"] = "0"
        child_env["GCM_INTERACTIVE"] = "Never"
        try:
            result = subprocess.run(
                [git, "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
                env=child_env,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if key == "password" and separator and value.strip():
                    return value.strip()
    return ""


def _github_release_descriptor(url: str) -> tuple[str, str, str, str] | None:
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() != "github.com":
        return None
    match = GITHUB_RELEASE_DOWNLOAD_RE.fullmatch(parsed.path)
    if not match:
        return None
    owner, repository, tag, asset_name = (unquote(item) for item in match.groups())
    if any("/" in item for item in (owner, repository, asset_name)):
        return None
    return owner, repository, tag, asset_name


def _github_release_asset_url(
    url: str,
    credential: str,
    opener: urllib.request.OpenerDirector,
) -> str:
    """Resolve a private release download through GitHub's asset API.

    GitHub release browser URLs may return a 404 for a private repository even
    when a bearer token is present.  The API release and asset endpoints accept
    that token, then redirect the actual bytes to a signed CDN URL.  The
    redirect handler above removes Authorization before that CDN request.
    """

    descriptor = _github_release_descriptor(url)
    if descriptor is None:
        return url
    owner, repository, tag, asset_name = descriptor
    release_url = (
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}/releases/tags/"
        f"{quote(tag, safe='')}"
    )
    release_request = urllib.request.Request(
        release_url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {credential}",
            "User-Agent": "AMADEUS-resource-installer",
        },
    )
    with opener.open(release_request, timeout=60) as response:
        release = json.load(response)
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        raise ValueError("GitHub release response did not contain an asset list")
    for asset in assets:
        if not isinstance(asset, dict) or str(asset.get("name") or "") != asset_name:
            continue
        asset_id = asset.get("id")
        if isinstance(asset_id, bool) or not isinstance(asset_id, int) or asset_id <= 0:
            break
        return (
            "https://api.github.com/repos/"
            f"{quote(owner, safe='')}/{quote(repository, safe='')}/releases/assets/{asset_id}"
        )
    raise ValueError(f"GitHub release asset was not found: {asset_name}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in relative:
        raise ValueError(f"Invalid resource path: {relative}")
    target = (root / str(path)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Resource path escapes installation: {relative}")
    return target


def verify_bundle(root: Path, bundle: dict) -> list[str]:
    target = inside(root, bundle["target"])
    problems = []
    for relative, expected in bundle["files"].items():
        path = inside(target, relative)
        if not path.is_file() or path.stat().st_size != expected["bytes"]:
            problems.append(relative)
        elif sha256(path) != expected["sha256"]:
            problems.append(relative)
    return problems


def install_archive(root: Path, bundle: dict, archive: Path) -> None:
    # Validate the complete archive and all contents before changing runtime files.
    if archive.stat().st_size != bundle["bytes"] or sha256(archive) != bundle["sha256"]:
        raise ValueError(f"Resource archive checksum mismatch: {archive.name}")
    target = inside(root, bundle["target"])
    staging_parent = root / ".runtime-assets"
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="install-", dir=staging_parent) as directory:
        staged = Path(directory)
        with zipfile.ZipFile(archive) as source:
            members = [item for item in source.infolist() if not item.is_dir()]
            names = [item.filename for item in members]
            if len(names) != len(set(names)) or set(names) != set(bundle["files"]):
                raise ValueError("Resource archive file list differs from its manifest")
            for member in members:
                path = inside(staged, member.filename)
                expected = bundle["files"][member.filename]
                if member.file_size != expected["bytes"] or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"Invalid resource archive member: {member.filename}")
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as incoming, path.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                if sha256(path) != expected["sha256"]:
                    raise ValueError(f"Resource file checksum mismatch: {member.filename}")
        destinations = {name: inside(target, name) for name in names}
        for name, destination in destinations.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(inside(staged, name), destination)


def download(bundle: dict, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    destination = inside(cache, bundle["assetName"])
    if destination.is_file() and destination.stat().st_size == bundle["bytes"] and sha256(destination) == bundle["sha256"]:
        return destination
    url = str(bundle.get("url") or "")
    if not url.startswith("https://"):
        raise ValueError("The resource release URL has not been configured")
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        headers = {"User-Agent": "AMADEUS-resource-installer"}
        opener = urllib.request.build_opener(_CredentialSafeRedirectHandler)
        host = (urlsplit(url).hostname or "").lower()
        credential = github_credential() if host in GITHUB_AUTH_HOSTS else ""
        if credential and host in GITHUB_AUTH_HOSTS:
            url = _github_release_asset_url(url, credential, opener)
            headers["Authorization"] = f"Bearer {credential}"
            if url.startswith("https://api.github.com/") and "/releases/assets/" in url:
                headers["Accept"] = "application/octet-stream"
        request = urllib.request.Request(url, headers=headers)
        with opener.open(request, timeout=60) as incoming, partial.open("wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        if partial.stat().st_size != bundle["bytes"] or sha256(partial) != bundle["sha256"]:
            raise ValueError("Downloaded resource archive checksum mismatch")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--archive-dir", type=Path, help="Use previously downloaded archives without network access")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--skip-voice", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve()
    manifest = json.loads((args.manifest or root / "configs/deployment_assets.json").read_text(encoding="utf-8"))
    if manifest.get("formatVersion") != 2 or not manifest.get("bundles"):
        raise ValueError("Invalid deployment resource manifest")
    failed = False
    for name, bundle in manifest["bundles"].items():
        if args.skip_voice and name == "voice":
            continue
        if not bundle.get("files"):
            raise ValueError(f"Resource inventory is empty: {name}")
        problems = verify_bundle(root, bundle)
        if problems and not args.verify_only:
            print(f"Installing {name}: {bundle['assetName']}", flush=True)
            archive = (inside(args.archive_dir.resolve(), bundle["assetName"]) if args.archive_dir
                       else download(bundle, root / ".cache/deployment-assets"))
            install_archive(root, bundle, archive)
            problems = verify_bundle(root, bundle)
        print(json.dumps({"bundle": name, "ok": not problems, "missingOrChanged": problems}, ensure_ascii=False))
        failed |= bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
