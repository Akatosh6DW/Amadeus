"""Runtime path ownership and fail-closed test isolation for AMADEUS."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Literal, Sequence


RuntimeMode = Literal["development", "test", "release"]
TEST_DATA_SENTINEL = ".amadeus-test-data"


@dataclass(frozen=True)
class RuntimePaths:
    mode: RuntimeMode
    root: Path
    conversation_context: Path
    conversation_archive: Path
    state_database: Path
    diagnostics_dir: Path
    cache_dir: Path


def test_runner_requested(argv: Sequence[str] | None = None) -> bool:
    values = [str(item).casefold() for item in (argv if argv is not None else sys.argv)]
    argv0 = values[0].replace("\\", "/") if values else ""
    executable = Path(values[0]).name if values else ""
    return bool(
        "pytest" in executable
        or "/unittest/" in argv0
        or any(item in {"pytest", "py.test", "unittest"} for item in values)
        or any("unittest" in item for item in values[:3])
    )


def resolve_runtime_paths(
    *,
    project_root: Path,
    release_mode: bool,
    release_default_root: Path,
) -> RuntimePaths:
    explicit_mode = str(os.environ.get("AMADEUS_RUNTIME_MODE") or "").strip().lower()
    if explicit_mode and explicit_mode not in {"development", "test", "release"}:
        raise RuntimeError(f"Unsupported AMADEUS_RUNTIME_MODE: {explicit_mode}")
    if release_mode and explicit_mode and explicit_mode != "release":
        raise RuntimeError(
            "AMADEUS_RUNTIME_MODE cannot override a packaged release; use release."
        )
    mode: RuntimeMode = (
        explicit_mode  # type: ignore[assignment]
        if explicit_mode
        else "release"
        if release_mode
        else "development"
    )
    explicit_root = str(os.environ.get("AMADEUS_DATA_DIR") or "").strip()
    project_data = (project_root / "data").resolve()
    # A packaged release owns one per-user data root. Environment overrides
    # remain useful for development and are mandatory for isolated tests, but
    # accepting one in release mode would let a launcher redirect the product
    # back into a source checkout's memories and diagnostics.
    root = Path(
        release_default_root
        if mode == "release"
        else explicit_root or project_data
    ).resolve()

    if test_runner_requested() and mode != "test":
        raise RuntimeError(
            "Test runner detected without AMADEUS_RUNTIME_MODE=test. "
            "Use tools/run_tests_isolated.py so tests cannot touch real user data."
        )
    if mode == "test":
        if not explicit_root:
            raise RuntimeError("Test runtime requires an explicit AMADEUS_DATA_DIR.")
        if root == project_data:
            raise RuntimeError("Test runtime is forbidden from using the project data directory.")
        if not (root / TEST_DATA_SENTINEL).is_file():
            raise RuntimeError(
                f"Test data directory is missing safety sentinel {TEST_DATA_SENTINEL}."
            )

    return RuntimePaths(
        mode=mode,
        root=root,
        conversation_context=root / "conversation_context.json",
        conversation_archive=root / "conversation_archive.jsonl",
        state_database=root / "amadeus_state.sqlite3",
        diagnostics_dir=root / "diagnostics",
        cache_dir=root / "cache",
    )
