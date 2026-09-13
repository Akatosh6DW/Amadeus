#!/usr/bin/env python3
"""Download and extract deployment archives with bounded, resumable I/O.

The upstream GPT-SoVITS installer writes large archives directly to their final
name and then uses PowerShell's ``Expand-Archive``.  A dropped connection can
therefore leave a multi-gigabyte file that is neither a valid archive nor safe
to append blindly.  This helper keeps a ``.part`` file and source metadata,
resumes only after a range/source check, and validates ZIP CRCs before making a
completed file visible to the installer.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from typing import Callable


CHUNK_SIZE = 4 * 1024 * 1024
SAMPLE_SIZE = 1024 * 1024
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 4
DEFAULT_BACKOFF = 1.0
PROGRESS_INTERVAL = 20.0
USER_AGENT = "AMADEUS-runtime-downloader/1"
CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


class DownloadError(RuntimeError):
    """Raised when a file cannot be downloaded or validated completely."""


class SourceChanged(DownloadError):
    """Raised when a partial file does not belong to the current source."""


class InvalidArchive(DownloadError):
    """Raised when a completed ZIP is structurally incomplete or corrupt."""


@dataclass(frozen=True)
class RemoteInfo:
    url: str
    total: int | None
    etag: str
    last_modified: str
    accepts_ranges: bool


def _header(headers: object, name: str) -> str:
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    return str(getter(name, "") or "").strip()


def _status(response: object) -> int:
    value = getattr(response, "status", None)
    if value is None:
        value = getattr(response, "getcode", lambda: 0)()
    return int(value or 0)


def _content_range(value: str) -> tuple[int, int, int | None] | None:
    match = CONTENT_RANGE_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    if end < start or total is not None and total <= end:
        raise DownloadError(f"invalid Content-Range: {value}")
    return start, end, total


def _request(url: str, headers: dict[str, str]) -> object:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, **headers},
    )
    return urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT)


def probe_remote(url: str) -> RemoteInfo:
    """Read remote size/identity with a one-byte range request.

    Hugging Face returns a useful ``Content-Range`` for this request.  A server
    that ignores ranges is still safe: only one byte is read and the caller
    will restart a partial file instead of appending to a 200 response.
    """

    try:
        response = _request(url, {"Range": "bytes=0-0"})
    except urllib.error.HTTPError as error:
        raise DownloadError(f"remote probe failed ({error.code}): {url}") from error
    try:
        status = _status(response)
        headers = getattr(response, "headers", {})
        content_range = _content_range(_header(headers, "Content-Range"))
        content_length = _header(headers, "Content-Length")
        total: int | None = content_range[2] if content_range else None
        if total is None and content_length.isdigit():
            total = int(content_length)
        if status == 206 and content_range is None:
            raise DownloadError("range probe omitted Content-Range")
        if status not in (200, 206):
            raise DownloadError(f"remote probe returned HTTP {status}")
        # Do not consume a server's full response when it ignores Range.
        response.read(1)
        return RemoteInfo(
            url=str(getattr(response, "geturl", lambda: url)() or url),
            total=total,
            etag=_header(headers, "ETag"),
            last_modified=_header(headers, "Last-Modified"),
            accepts_ranges=status == 206 or _header(headers, "Accept-Ranges").lower() == "bytes",
        )
    finally:
        response.close()


def _fetch_range(url: str, start: int, end: int) -> bytes:
    if start < 0 or end < start:
        raise ValueError("invalid range")
    try:
        response = _request(url, {"Range": f"bytes={start}-{end}"})
    except urllib.error.HTTPError as error:
        raise SourceChanged(f"range request failed ({error.code})") from error
    try:
        status = _status(response)
        headers = getattr(response, "headers", {})
        if status != 206:
            # A 200 response to a non-zero range cannot identify the requested
            # fragment without reading the entire remote object.
            if start != 0:
                raise SourceChanged("server ignored a non-zero range request")
        content_range = _content_range(_header(headers, "Content-Range"))
        if status == 206 and (content_range is None or content_range[0] != start):
            raise SourceChanged("server returned a different range")
        expected = end - start + 1
        payload = response.read(expected)
        if len(payload) != expected:
            raise SourceChanged("remote range ended before the requested fragment")
        return payload
    finally:
        response.close()


def _metadata_path(path: Path) -> Path:
    return Path(str(path) + ".meta.json")


def _read_metadata(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_metadata(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_zip(path: Path) -> None:
    """Validate the central directory and every member CRC before extraction."""

    try:
        if not zipfile.is_zipfile(path):
            raise InvalidArchive(f"not a readable ZIP archive: {path.name}")
        with zipfile.ZipFile(path) as source:
            if not _validated_members(source):
                raise InvalidArchive(f"ZIP archive is empty: {path.name}")
            broken = source.testzip()
            if broken is not None:
                raise InvalidArchive(f"ZIP CRC check failed: {broken}")
    except InvalidArchive:
        raise
    except (OSError, zipfile.BadZipFile, EOFError) as error:
        raise InvalidArchive(f"invalid or incomplete ZIP archive: {path.name}") from error


def _validated_members(source: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()
    for member in source.infolist():
        normalized = str(member.filename or "").replace("\\", "/")
        parts = tuple(PurePosixPath(normalized).parts)
        if (
            not parts
            or any(part in {"", ".", ".."} for part in parts)
            or PurePosixPath(normalized).is_absolute()
            or ":" in parts[0]
        ):
            raise InvalidArchive(f"unsafe ZIP member path: {member.filename}")
        key = tuple(part.casefold() for part in parts)
        if key in seen:
            raise InvalidArchive(f"duplicate ZIP member path: {member.filename}")
        seen.add(key)
        mode = (member.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise InvalidArchive(f"symbolic links are not allowed in ZIP archives: {member.filename}")
        members.append((member, parts))
    return members


def _validate_completed(
    path: Path,
    *,
    expected_size: int | None,
    expected_sha256: str,
    validate_archive: bool,
) -> str:
    if not path.is_file():
        raise DownloadError(f"download did not create a file: {path}")
    size = path.stat().st_size
    if expected_size is not None and size != expected_size:
        raise DownloadError(f"download size {size} differs from expected {expected_size}")
    if validate_archive:
        validate_zip(path)
    digest = _sha256(path)
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        raise DownloadError("download SHA-256 does not match the expected value")
    return digest


def _source_matches(metadata: dict[str, object], requested_url: str, remote: RemoteInfo) -> bool:
    if not metadata:
        return False
    if str(metadata.get("url") or "") != requested_url:
        return False
    old_total = metadata.get("total")
    if remote.total is not None and old_total is not None:
        try:
            if int(old_total) != remote.total:
                return False
        except (TypeError, ValueError):
            return False
    old_etag = str(metadata.get("etag") or "")
    if old_etag and remote.etag and old_etag != remote.etag:
        return False
    old_modified = str(metadata.get("lastModified") or "")
    if old_modified and remote.last_modified and old_modified != remote.last_modified:
        return False
    return True


def _partial_matches_remote(path: Path, remote: RemoteInfo) -> bool:
    size = path.stat().st_size
    if size == 0:
        return True
    if remote.total is not None and size > remote.total:
        return False
    sample = min(SAMPLE_SIZE, size)
    with path.open("rb") as source:
        prefix = source.read(sample)
        source.seek(size - sample)
        suffix = source.read(sample)
    try:
        return prefix == _fetch_range(remote.url, 0, sample - 1) and suffix == _fetch_range(
            remote.url, size - sample, size - 1
        )
    except (DownloadError, OSError):
        return False


def _promote_completed(part: Path, output: Path, metadata: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part, output)
    _write_metadata(_metadata_path(output), metadata)
    _metadata_path(part).unlink(missing_ok=True)


def _progress_report(
    callback: Callable[[int, int | None], None] | None,
    downloaded: int,
    total: int | None,
    last_report: float,
) -> float:
    now = time.monotonic()
    if callback is not None and (last_report == 0.0 or now - last_report >= PROGRESS_INTERVAL):
        callback(downloaded, total)
        return now
    return last_report


def download_file(
    url: str,
    output: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str = "",
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
    progress: Callable[[int, int | None], None] | None = None,
    validate_archive: bool | None = None,
) -> Path:
    """Download one file, resuming a verified partial file when possible."""

    if not str(url).strip():
        raise ValueError("download URL is required")
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if expected_size is not None and expected_size < 0:
        raise ValueError("expected size must be non-negative")
    expected_sha256 = str(expected_sha256 or "").strip().lower()
    if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected SHA-256 must be 64 hexadecimal characters")
    if validate_archive is None:
        validate_archive = output.suffix.casefold() == ".zip"
    retries = max(1, int(retries))
    part = Path(str(output) + ".part")
    part_metadata = _metadata_path(part)
    output_metadata = _metadata_path(output)

    # Reuse an already complete result when it has an explicit expected digest
    # or is a valid ZIP. Older installers may have left no sidecar at all.
    if output.is_file():
        try:
            digest = _validate_completed(
                output,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                validate_archive=bool(validate_archive),
            )
            if expected_size is not None or expected_sha256 or validate_archive:
                return output
            _ = digest
        except (DownloadError, OSError):
            pass
        if part.exists():
            output.unlink()
        else:
            os.replace(output, part)
        if output_metadata.exists() and not part_metadata.exists():
            os.replace(output_metadata, part_metadata)

    if part.exists() and not part.is_file():
        _remove(part)

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            remote = probe_remote(url)
            metadata = _read_metadata(part_metadata)
            size = part.stat().st_size if part.exists() else 0
            if expected_size is not None and remote.total is not None and expected_size != remote.total:
                raise DownloadError(
                    f"remote size {remote.total} differs from expected {expected_size}"
                )
            total = expected_size if expected_size is not None else remote.total
            if size and (
                (metadata and not _source_matches(metadata, url, remote))
                or not _partial_matches_remote(part, remote)
                or (total is not None and size > total)
            ):
                _remove(part)
                part_metadata.unlink(missing_ok=True)
                metadata = {}
                size = 0

            if total is not None and size == total:
                digest = _validate_completed(
                    part,
                    expected_size=total,
                    expected_sha256=expected_sha256,
                    validate_archive=bool(validate_archive),
                )
                completed_metadata = {
                    "url": url,
                    "resolvedUrl": remote.url,
                    "etag": remote.etag,
                    "lastModified": remote.last_modified,
                    "total": total,
                    "sha256": digest,
                }
                _promote_completed(part, output, completed_metadata)
                return output

            metadata = {
                "url": url,
                "resolvedUrl": remote.url,
                "etag": remote.etag,
                "lastModified": remote.last_modified,
                "total": total,
            }
            _write_metadata(part_metadata, metadata)
            headers = {"Accept-Encoding": "identity"}
            if size:
                headers["Range"] = f"bytes={size}-"
            try:
                response = _request(remote.url, headers)
            except urllib.error.HTTPError as error:
                if error.code == 416 and total is not None and size == total:
                    raise DownloadError("range completed but archive validation is required") from error
                raise DownloadError(f"download request failed ({error.code})") from error
            try:
                status = _status(response)
                response_headers = getattr(response, "headers", {})
                response_etag = _header(response_headers, "ETag")
                response_modified = _header(response_headers, "Last-Modified")
                if remote.etag and response_etag and remote.etag != response_etag:
                    raise SourceChanged("remote ETag changed during download")
                if (
                    remote.last_modified
                    and response_modified
                    and remote.last_modified != response_modified
                ):
                    raise SourceChanged("remote Last-Modified changed during download")
                content_range = _content_range(_header(response_headers, "Content-Range"))
                if status == 206:
                    if content_range is None or content_range[0] != size:
                        raise SourceChanged("server returned a mismatched resume range")
                    response_total = content_range[2]
                    if response_total is not None and total is not None and response_total != total:
                        raise SourceChanged("remote size changed during resume")
                    total = response_total or total
                    mode = "ab"
                elif status == 200:
                    # The server ignored Range.  Write from zero, never append
                    # a full response after an existing partial prefix.
                    size = 0
                    mode = "wb"
                    content_length = _header(response_headers, "Content-Length")
                    if content_length.isdigit():
                        total = int(content_length)
                else:
                    raise DownloadError(f"download request returned HTTP {status}")
                if total is not None and expected_size is not None and total != expected_size:
                    raise SourceChanged("remote size changed during download")
                downloaded = size
                last_report = 0.0
                with part.open(mode) as destination:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        destination.write(chunk)
                        downloaded += len(chunk)
                        last_report = _progress_report(progress, downloaded, total, last_report)
                    destination.flush()
                    os.fsync(destination.fileno())
            finally:
                response.close()

            actual_size = part.stat().st_size
            if total is not None and actual_size != total:
                raise DownloadError(
                    f"download ended at {actual_size} bytes; expected {total}"
                )
            digest = _validate_completed(
                part,
                expected_size=total,
                expected_sha256=expected_sha256,
                validate_archive=bool(validate_archive),
            )
            completed_metadata = {
                "url": url,
                "resolvedUrl": remote.url,
                "etag": remote.etag,
                "lastModified": remote.last_modified,
                "total": total,
                "sha256": digest,
            }
            _promote_completed(part, output, completed_metadata)
            return output
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            last_error = error
            if isinstance(error, (SourceChanged, InvalidArchive)):
                _remove(part)
                part_metadata.unlink(missing_ok=True)
            if attempt + 1 < retries:
                time.sleep(max(0.0, float(backoff)) * (2 ** attempt))

    raise DownloadError(f"download failed after {retries} attempts: {url}") from last_error


def _extract_to_stage(archive: Path, stage: Path) -> list[tuple[str, ...]]:
    try:
        if not zipfile.is_zipfile(archive):
            raise InvalidArchive(f"not a readable ZIP archive: {archive.name}")
        with zipfile.ZipFile(archive) as source:
            members = _validated_members(source)
            if not members:
                raise InvalidArchive(f"ZIP archive is empty: {archive.name}")
            broken = source.testzip()
            if broken is not None:
                raise InvalidArchive(f"ZIP CRC check failed: {broken}")
            roots: set[tuple[str, ...]] = set()
            for member, parts in members:
                roots.add((parts[0],))
                target = stage.joinpath(*parts)
                if member.is_dir() or member.filename.replace("\\", "/").endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=CHUNK_SIZE)
            return sorted(roots)
    except InvalidArchive:
        raise
    except (OSError, zipfile.BadZipFile, EOFError) as error:
        raise InvalidArchive(f"invalid or incomplete ZIP archive: {archive.name}") from error


def extract_archive(archive: Path, destination: Path) -> Path:
    """Validate/extract a ZIP, atomically replacing each top-level subtree."""

    archive = Path(archive).resolve()
    destination = Path(destination).resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.extract-", dir=destination.parent))
    backups: list[tuple[Path, Path]] = []
    promoted: list[Path] = []
    try:
        roots = _extract_to_stage(archive, stage)
        destination.mkdir(parents=True, exist_ok=True)
        for root_parts in roots:
            name = root_parts[0]
            source = stage / name
            target = destination / name
            backup = destination / f".{name}.backup-{uuid.uuid4().hex}"
            if target.exists() or target.is_symlink():
                os.replace(target, backup)
                backups.append((target, backup))
            os.replace(source, target)
            promoted.append(target)
        for _target, backup in backups:
            _remove(backup)
    except Exception:
        for target in reversed(promoted):
            _remove(target)
        for target, backup in reversed(backups):
            if backup.exists() or backup.is_symlink():
                os.replace(backup, target)
        raise
    finally:
        _remove(stage)
    return destination


def _cli_progress(downloaded: int, total: int | None) -> None:
    if total:
        percent = downloaded * 100 / total
        message = f"downloaded {downloaded}/{total} bytes ({percent:.1f}%)"
    else:
        message = f"downloaded {downloaded} bytes"
    print(message, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-size", type=int)
    parser.add_argument("--sha256", default="")
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.extract:
            if args.archive is None or args.destination is None:
                parser.error("--extract requires --archive and --destination")
            extract_archive(args.archive, args.destination)
            return 0
        if args.url is None or args.output is None:
            parser.error("download requires --url and --output")
        download_file(
            args.url,
            args.output,
            expected_size=args.expected_size,
            expected_sha256=args.sha256,
            progress=_cli_progress,
        )
        return 0
    except (DownloadError, OSError, ValueError) as error:
        print(f"AMADEUS runtime download failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
