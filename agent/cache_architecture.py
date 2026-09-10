"""Safe, allow-listed cleanup of disposable runtime data.

Conversation history, memories, relationship state, user attachments,
generated files, screenshots, model weights and reference audio are never
members of a cleanup plan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import shutil
import time
from typing import Iterable, Literal


CacheMode = Literal["unused", "all_disposable"]


@dataclass(frozen=True)
class CacheTarget:
    path: Path
    category: str
    reason: str


@dataclass(frozen=True)
class CacheSummary:
    mode: str
    bytes: int
    files: int
    directories: int
    targets: tuple[str, ...]
    measurement_complete: bool

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["measurementComplete"] = value.pop("measurement_complete")
        return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolved_is_within(path: Path, root: Path) -> bool:
    """Containment check for paths already resolved by the plan builder."""

    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _qa_edge_profiles(project_root: Path, data_root: Path) -> Iterable[CacheTarget]:
    qa_root = (data_root / "qa_runs").resolve()
    if not qa_root.is_dir() or not _is_within(qa_root, data_root):
        return ()
    result: list[CacheTarget] = []
    for run_dir in qa_root.iterdir():
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        for child in run_dir.iterdir():
            if (
                child.is_dir()
                and not child.is_symlink()
                and child.name.startswith("edge-profile-")
            ):
                result.append(CacheTarget(
                    child.absolute(),
                    "qa_browser_profile",
                    "isolated Edge profile created by a QA run",
                ))
    return tuple(result)


def build_cache_plan(
    project_root: Path,
    data_root: Path,
    mode: CacheMode,
) -> tuple[CacheTarget, ...]:
    """Build a resolved allow-list; never infer deletion from a broad glob."""

    project_root = project_root.resolve()
    data_root = data_root.resolve()
    targets: list[CacheTarget] = list(_qa_edge_profiles(project_root, data_root))
    ephemeral = (
        (data_root / "webview" / "Cache", "webview_cache"),
        (data_root / "webview" / "Code Cache", "webview_code_cache"),
        (data_root / "webview" / "GPUCache", "webview_gpu_cache"),
        (data_root / "webview" / "DawnCache", "webview_gpu_cache"),
        (data_root / "runtime" / "tmp", "runtime_temp"),
        (data_root / "runtime" / "tts-segments", "tts_temp"),
    )
    for path, category in ephemeral:
        resolved = path.resolve()
        if resolved.exists() and _is_within(resolved, data_root):
            targets.append(CacheTarget(resolved, category, "regenerated runtime cache"))
    if mode == "all_disposable":
        for path, category, reason in (
            (data_root / "cache", "runtime_cache", "regenerated acceleration and TTS cache"),
            (data_root / "logs", "logs", "diagnostic logs do not participate in dialogue"),
        ):
            resolved = path.resolve()
            if resolved.exists() and _is_within(resolved, data_root):
                targets.append(CacheTarget(resolved, category, reason))
    elif mode != "unused":
        raise ValueError(f"unsupported cache cleanup mode: {mode}")

    unique: list[CacheTarget] = []
    covered: list[Path] = []
    for target in sorted(targets, key=lambda item: (len(item.path.parts), str(item.path))):
        if target.path in {project_root, data_root, Path(target.path.anchor)}:
            continue
        if any(_resolved_is_within(target.path, parent) for parent in covered):
            continue
        covered.append(target.path)
        unique.append(target)
    return tuple(unique)


def _measure(
    path: Path,
    *,
    deadline: float,
    entry_budget: int,
) -> tuple[int, int, int, int, bool]:
    if not path.exists():
        return 0, 0, 0, 0, True
    if path.is_file():
        try:
            return path.stat().st_size, 1, 0, 1, True
        except OSError:
            return 0, 1, 0, 1, True
    total_bytes = 0
    files = 0
    directories = 1
    visited = 0
    complete = True
    stack = [path]
    while stack:
        if time.monotonic() >= deadline or visited >= entry_budget:
            complete = False
            break
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                visited += 1
                if time.monotonic() >= deadline or visited >= entry_budget:
                    complete = False
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories += 1
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        files += 1
                        total_bytes += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total_bytes, files, directories, visited, complete


def cache_summary(project_root: Path, data_root: Path, mode: CacheMode) -> CacheSummary:
    targets = build_cache_plan(project_root, data_root, mode)
    total_bytes = files = directories = 0
    entry_budget = 20_000
    deadline = time.monotonic() + 0.9
    measurement_complete = True
    for target in targets:
        if time.monotonic() >= deadline or entry_budget <= 0:
            measurement_complete = False
            break
        size, file_count, directory_count, visited, complete = _measure(
            target.path,
            deadline=deadline,
            entry_budget=entry_budget,
        )
        total_bytes += size
        files += file_count
        directories += directory_count
        entry_budget -= visited
        measurement_complete = measurement_complete and complete
    return CacheSummary(
        mode=mode,
        bytes=total_bytes,
        files=files,
        directories=directories,
        targets=tuple(str(item.path) for item in targets),
        measurement_complete=measurement_complete,
    )


def cache_status(project_root: Path, data_root: Path) -> dict[str, CacheSummary]:
    """Measure both UI categories in one bounded traversal."""

    targets = build_cache_plan(project_root, data_root, "all_disposable")
    unused_categories = {
        "qa_browser_profile", "webview_cache", "webview_code_cache",
        "webview_gpu_cache", "runtime_temp", "tts_temp",
    }
    totals = {
        "unused": [0, 0, 0],
        "all_disposable": [0, 0, 0],
    }
    deadline = time.monotonic() + 1.1
    entry_budget = 24_000
    complete = True
    for target in targets:
        if time.monotonic() >= deadline or entry_budget <= 0:
            complete = False
            break
        size, files, directories, visited, target_complete = _measure(
            target.path,
            deadline=deadline,
            entry_budget=entry_budget,
        )
        entry_budget -= visited
        complete = complete and target_complete
        for index, value in enumerate((size, files, directories)):
            totals["all_disposable"][index] += value
            if target.category in unused_categories:
                totals["unused"][index] += value
    unused_targets = tuple(
        str(item.path) for item in targets if item.category in unused_categories
    )
    all_targets = tuple(str(item.path) for item in targets)
    return {
        "unused": CacheSummary(
            mode="unused",
            bytes=totals["unused"][0],
            files=totals["unused"][1],
            directories=totals["unused"][2],
            targets=unused_targets,
            measurement_complete=complete,
        ),
        "all_disposable": CacheSummary(
            mode="all_disposable",
            bytes=totals["all_disposable"][0],
            files=totals["all_disposable"][1],
            directories=totals["all_disposable"][2],
            targets=all_targets,
            measurement_complete=complete,
        ),
    }


def clear_cache(project_root: Path, data_root: Path, mode: CacheMode) -> dict[str, object]:
    targets = build_cache_plan(project_root, data_root, mode)
    before = cache_summary(project_root, data_root, mode)
    removed: list[str] = []
    failed: list[dict[str, str]] = []
    for target in targets:
        try:
            if target.path.is_dir():
                shutil.rmtree(target.path)
            elif target.path.exists():
                target.path.unlink()
            removed.append(str(target.path))
        except OSError as error:
            failed.append({"path": str(target.path), "error": str(error)})
    return {
        "ok": not failed,
        "mode": mode,
        "freedBytes": before.bytes,
        "measurementComplete": before.measurement_complete,
        "removedTargets": removed,
        "failedTargets": failed,
        "preserved": [
            "conversation_history",
            "memory",
            "relationship_state",
            "attachments",
            "generated_files",
            "screenshots",
            "model_weights",
            "reference_audio",
        ],
    }
