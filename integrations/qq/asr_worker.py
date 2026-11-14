"""Bounded local ASR worker used by the QQ incoming voice adapter.

The parent process owns the OneBot connection and event routing.  This module
only owns a small, line based JSON protocol to a local ASR process.  A worker
keeps its model warm for serialized requests and is terminated by
``VoiceTranscriber.close`` when the gateway goes idle or shuts down.

No audio is downloaded here.  The caller must first ask NapCat for a local
WAV file and only then pass that returned path to this worker.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import wave
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_LINE_LIMIT = 256 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_SEGMENT_SECONDS = 120.0
ASR_TIMEOUT_SECONDS = 180.0
IDLE_RELEASE_SECONDS = 120.0


class VoiceInputError(Exception):
    """A user-facing failure while preparing or transcribing an input voice."""

    def __init__(self, code: str, user_message: str) -> None:
        self.code = str(code or "voice_error").strip() or "voice_error"
        self.user_message = str(user_message or "语音暂时无法识别，请稍后重试。").strip()
        if not self.user_message:
            self.user_message = "语音暂时无法识别，请稍后重试。"
        super().__init__(self.user_message)


def _voice_error(code: str, message: str) -> VoiceInputError:
    return VoiceInputError(code, message)


class _ASRWorkerFailure(Exception):
    """Internal worker failure whose code is safe to send over the protocol."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _local_path(value: object) -> Path | None:
    """Convert a trusted NapCat result field into a local path.

    URLs are deliberately not accepted as audio inputs.  NapCat's ``file``
    result is normally an absolute Windows path; ``file://`` is accepted for
    transports that serialize the same local path as a URI.  This function is
    not used on the incoming event's record field.
    """

    if isinstance(value, Path):
        return value
    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = os.fspath(value)
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    raw = str(raw).strip()
    if not raw:
        return None
    # ``urlsplit`` regards ``C:/...`` as a URI with scheme ``c``.  Check the
    # Windows drive form before interpreting URI schemes.
    is_windows_drive = len(raw) >= 3 and raw[1] == ":" and raw[2] in {"/", "\\"}
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https", "ws", "wss"}:
        return None
    if scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            return None
        raw = unquote(parsed.path)
        # A Windows file URI carries the drive after an initial slash.
        if os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
    elif parsed.scheme and not is_windows_drive:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def audio_info(path: Path) -> tuple[int, float]:
    """Return byte size and WAV duration, enforcing the local ASR limits."""

    try:
        resolved = Path(path).resolve()
        stat = resolved.stat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _voice_error("voice_audio_missing", "语音文件暂时不可用，请重新发送。") from exc
    if not resolved.is_file():
        raise _voice_error("voice_audio_missing", "语音文件暂时不可用，请重新发送。")
    if stat.st_size <= 0:
        raise _voice_error("voice_audio_empty", "这条语音没听清，能再说一遍吗？")
    if stat.st_size > MAX_AUDIO_BYTES:
        raise _voice_error("voice_audio_too_large", "这段语音文件太大了，请分段或压缩后再发。")
    try:
        with wave.open(str(resolved), "rb") as source:
            rate = int(source.getframerate())
            frames = int(source.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise _voice_error("voice_audio_invalid", "语音格式暂时无法读取，请重新发送。") from exc
    if rate <= 0 or frames <= 0:
        raise _voice_error("voice_audio_empty", "这条语音没听清，能再说一遍吗？")
    duration = frames / rate
    if duration > MAX_SEGMENT_SECONDS:
        raise _voice_error("voice_audio_too_long", "单段语音不能超过两分钟，请分段发送。")
    return int(stat.st_size), float(duration)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _configured_path(runtime: Path, keys: Iterable[str]) -> Path | None:
    settings = _read_json_object(runtime / "paths.json")
    for key in keys:
        value = settings.get(key)
        if not isinstance(value, (str, os.PathLike)):
            continue
        candidate = Path(os.fspath(value)).expanduser()
        if not candidate.is_absolute():
            candidate = runtime / candidate
        try:
            return candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def discover_python(root: Path, runtime: Path) -> str:
    """Find an already-installed interpreter without downloading anything."""

    configured = os.environ.get("AMADEUS_QQ_ASR_PYTHON") or os.environ.get("AMADEUS_ASR_PYTHON")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    configured_path = _configured_path(runtime, ("asr_python", "asrPython", "python_gpt_sovits", "pythonGptSovits"))
    if configured_path is not None:
        candidates.append(configured_path)
    for base in (root, root / 'runtime', runtime, PROJECT_ROOT):
        candidates.extend([
            base / ".conda" / "gpt-sovits" / ("python.exe" if os.name == "nt" else "bin/python"),
            base / "python-gpt-sovits" / ("python.exe" if os.name == "nt" else "bin/python"),
        ])
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    return sys.executable


def discover_model(root: Path, runtime: Path) -> Path | None:
    """Return a local faster-whisper model directory, if one is installed."""

    configured = os.environ.get("AMADEUS_QQ_ASR_MODEL") or os.environ.get("AMADEUS_ASR_MODEL")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    configured_path = _configured_path(runtime, ("asr_model", "asrModel", "whisper_model", "whisperModel"))
    if configured_path is not None:
        candidates.append(configured_path)
    for base in (runtime, root, root / 'runtime', PROJECT_ROOT):
        candidates.extend([
            base / "asr" / "faster-whisper-small",
            base / "voice" / "asr" / "faster-whisper-small",
        ])
    cache_roots: list[Path] = []
    for value in (
        os.environ.get("HF_HOME"),
        os.environ.get("HUGGINGFACE_HUB_CACHE"),
    ):
        if value:
            cache_roots.append(Path(value).expanduser())
    for base in (runtime, root, root / 'runtime', PROJECT_ROOT):
        cache_roots.extend([
            base / "cache" / "huggingface" / "hub",
            base / ".cache" / "huggingface" / "hub",
        ])
    for cache_root in cache_roots:
        candidates.append(cache_root / "models--Systran--faster-whisper-small")
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.is_dir():
            continue
        # Hugging Face stores the actual model under a content-addressed
        # snapshot.  Resolve the newest complete snapshot without accepting a
        # cache index directory as a model itself.
        if candidate.name == "models--Systran--faster-whisper-small":
            snapshots = candidate / "snapshots"
            try:
                snapshot_dirs = sorted(
                    (item for item in snapshots.iterdir() if item.is_dir()),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                snapshot_dirs = []
            for snapshot in snapshot_dirs:
                try:
                    names = {item.name for item in snapshot.iterdir()}
                except OSError:
                    continue
                if "config.json" in names and "model.bin" in names:
                    return snapshot
            continue
        return candidate
    return None


_MODEL: Any = None
_MODEL_KEY: tuple[str, str, str] | None = None


def _load_model(root: Path, runtime: Path) -> Any:
    global _MODEL, _MODEL_KEY
    model_path = discover_model(root, runtime)
    if model_path is None:
        raise _ASRWorkerFailure("voice_asr_model_missing")
    device = str(os.environ.get("AMADEUS_QQ_ASR_DEVICE") or "cuda").strip().lower()
    if device not in {"cuda", "cpu"}:
        device = "cuda"
    configured_compute = os.environ.get("AMADEUS_QQ_ASR_COMPUTE_TYPE")
    compute = str(configured_compute or ("int8_float16" if device == "cuda" else "int8"))
    if _MODEL is not None and _MODEL_KEY is not None and _MODEL_KEY[0] == str(model_path):
        return _MODEL
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from faster_whisper import WhisperModel
    except Exception as exc:
        raise _ASRWorkerFailure("voice_asr_runtime_missing") from exc
    attempts = [(device, compute)]
    if device == "cuda":
        # A portable install may carry the model but not CUDA.  A CPU retry
        # stays local and avoids making availability depend on a GPU driver.
        if not configured_compute:
            attempts.append(("cuda", "float16"))
        attempts.append(("cpu", "int8"))
    last_error: Exception | None = None
    for attempt_device, attempt_compute in attempts:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                model = WhisperModel(
                    str(model_path),
                    device=attempt_device,
                    compute_type=attempt_compute,
                    local_files_only=True,
                )
            _MODEL = model
            _MODEL_KEY = (str(model_path), attempt_device, attempt_compute)
            return model
        except Exception as exc:
            last_error = exc
    raise _ASRWorkerFailure("voice_asr_model_unavailable") from last_error


def transcribe_audio(path: Path, root: Path, runtime: Path) -> str:
    """Run one local transcription and return only its text."""

    audio_info(path)
    model = _load_model(root, runtime)
    try:
        language = str(os.environ.get("AMADEUS_QQ_ASR_LANGUAGE") or "zh").strip().lower()
        options: dict[str, Any] = {
            "beam_size": 5,
            "vad_filter": True,
            "condition_on_previous_text": False,
        }
        if language and language not in {"auto", "detect"}:
            options["language"] = language
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = model.transcribe(
                str(Path(path).resolve()),
                **options,
            )
    except Exception as exc:
        raise RuntimeError("local ASR inference failed") from exc
    segments: Any = result[0] if isinstance(result, tuple) else result
    if isinstance(segments, str):
        return segments.strip()
    texts: list[str] = []
    try:
        iterator = iter(segments)
    except TypeError:
        return ""
    for segment in iterator:
        value = getattr(segment, "text", "")
        if isinstance(value, str):
            texts.append(value)
    return "".join(texts).strip()


class VoiceTranscriber:
    """Lazy, serialized client for the local JSON ASR worker."""

    def __init__(
        self,
        root: Path,
        runtime: Path,
        *,
        python: str | Path | None = None,
        timeout: float = ASR_TIMEOUT_SECONDS,
        idle_seconds: float = IDLE_RELEASE_SECONDS,
    ) -> None:
        self.root = Path(root).resolve()
        self.runtime = Path(runtime).resolve()
        self.python = str(Path(python).resolve()) if python else discover_python(self.root, self.runtime)
        self.timeout = max(0.01, float(timeout))
        self.idle_seconds = max(0.01, float(idle_seconds))
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._request_id = 0
        self._idle_task: asyncio.Task[None] | None = None

    async def _start(self) -> asyncio.subprocess.Process:
        if self._closed:
            raise _voice_error("voice_asr_closed", "语音服务已停止，请稍后重试。")
        process = self._process
        if process is not None and process.returncode is None:
            return process
        self._process = None
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        command = [
            self.python,
            "-X",
            "utf8",
            "-u",
            str(Path(__file__).resolve()),
            "--serve",
            "--root",
            str(self.root),
            "--runtime",
            str(self.runtime),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=WORKER_LINE_LIMIT,
                creationflags=flags,
            )
        except (OSError, ValueError) as exc:
            raise _voice_error("voice_asr_unavailable", "本地语音识别服务不可用，请稍后重试。") from exc
        if self._closed:
            await self._terminate_process(process)
            raise _voice_error("voice_asr_closed", "语音服务已停止，请稍后重试。")
        self._process = process
        return process

    async def _terminate_process(self, process: asyncio.subprocess.Process | None = None) -> None:
        process = process or self._process
        if process is None:
            return
        if process is self._process:
            self._process = None
        if process.returncode is None:
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                pass
            try:
                await asyncio.wait_for(process.wait(), 2.0)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
                with contextlib.suppress(Exception):
                    await process.wait()
        else:
            with contextlib.suppress(Exception):
                await process.wait()

    def _cancel_idle_release(self) -> None:
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_idle_release(self, process: asyncio.subprocess.Process) -> None:
        self._cancel_idle_release()
        if self._closed or process.returncode is not None or self._process is not process:
            return
        self._idle_task = asyncio.create_task(self._release_when_idle(process))

    async def _release_when_idle(self, process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.sleep(self.idle_seconds)
            async with self._lock:
                if not self._closed and self._process is process and process.returncode is None:
                    await self._terminate_process(process)
        except asyncio.CancelledError:
            return

    async def transcribe(self, path: Path) -> str:
        if self._closed:
            raise _voice_error("voice_asr_closed", "语音服务已停止，请稍后重试。")
        resolved = Path(path).resolve()
        audio_info(resolved)
        async with self._lock:
            if self._closed:
                raise _voice_error("voice_asr_closed", "语音服务已停止，请稍后重试。")
            self._cancel_idle_release()
            process = await self._start()
            if process.stdin is None or process.stdout is None:
                await self._terminate_process(process)
                raise _voice_error("voice_asr_protocol", "本地语音识别服务响应异常，请稍后重试。")
            try:
                self._request_id += 1
                request_id = self._request_id
                wire = json.dumps(
                    {"id": request_id, "op": "transcribe", "path": str(resolved)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                try:
                    process.stdin.write(wire)
                    await process.stdin.drain()
                    line = await asyncio.wait_for(process.stdout.readline(), self.timeout)
                except asyncio.CancelledError:
                    await self._terminate_process(process)
                    raise
                except asyncio.TimeoutError as exc:
                    await self._terminate_process(process)
                    raise _voice_error("voice_asr_timeout", "语音识别超时，请稍后重试。") from exc
                except (BrokenPipeError, ConnectionError, OSError) as exc:
                    await self._terminate_process(process)
                    raise _voice_error("voice_asr_failed", "本地语音识别失败，请稍后重试。") from exc
                if not line:
                    await self._terminate_process(process)
                    raise _voice_error("voice_asr_failed", "本地语音识别失败，请稍后重试。")
                try:
                    response = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    await self._terminate_process(process)
                    raise _voice_error("voice_asr_protocol", "本地语音识别服务响应异常，请稍后重试。") from exc
                if not isinstance(response, dict) or response.get("id") != request_id:
                    await self._terminate_process(process)
                    raise _voice_error("voice_asr_protocol", "本地语音识别服务响应异常，请稍后重试。")
                if response.get("ok") is not True:
                    code = response.get("code")
                    error_messages = {
                        "voice_asr_empty": "这条语音没听清，能再说一遍吗？",
                        "voice_asr_model_missing": "本地语音识别模型未安装，请先准备本机语音资源。",
                        "voice_asr_runtime_missing": "本地语音识别运行环境不可用，请检查语音环境。",
                        "voice_asr_model_unavailable": "本地语音识别模型无法加载，请检查本机语音资源。",
                    }
                    if code in error_messages:
                        raise _voice_error(str(code), error_messages[code])
                    raise _voice_error("voice_asr_failed", "本地语音识别失败，请稍后重试。")
                text = response.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise _voice_error("voice_asr_empty", "这条语音没听清，能再说一遍吗？")
                return text.strip()
            finally:
                self._schedule_idle_release(process)

    async def close(self) -> None:
        self._closed = True
        task = self._idle_task
        self._idle_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._terminate_process()


def _write_response(response: dict[str, Any]) -> None:
    try:
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        raise SystemExit(0)


def _serve(root: Path, runtime: Path) -> int:
    for line in sys.stdin.buffer:
        request_id: object = None
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError
            request_id = request.get("id")
            operation = request.get("op")
            if operation == "shutdown":
                _write_response({"id": request_id, "ok": True})
                return 0
            if operation != "transcribe":
                _write_response({"id": request_id, "ok": False, "code": "voice_asr_protocol"})
                continue
            path = _local_path(request.get("path"))
            if path is None:
                raise _voice_error("voice_audio_missing", "语音文件暂时不可用，请重新发送。")
            text = transcribe_audio(path, root, runtime)
            if not text:
                _write_response({"id": request_id, "ok": False, "code": "voice_asr_empty"})
            else:
                _write_response({"id": request_id, "ok": True, "text": text})
        except VoiceInputError as exc:
            _write_response({"id": request_id, "ok": False, "code": exc.code})
        except _ASRWorkerFailure as exc:
            _write_response({"id": request_id, "ok": False, "code": exc.code})
        except Exception:
            # Error details and third party output stay out of both protocol
            # streams.  The parent gets only a stable, user-safe code.
            _write_response({"id": request_id, "ok": False, "code": "voice_asr_failed"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--runtime", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    if not args.serve:
        return 0
    return _serve(args.root.resolve(), args.runtime.resolve())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ASR_TIMEOUT_SECONDS",
    "IDLE_RELEASE_SECONDS",
    "MAX_AUDIO_BYTES",
    "MAX_SEGMENT_SECONDS",
    "VoiceInputError",
    "VoiceTranscriber",
    "audio_info",
    "discover_model",
    "discover_python",
    "transcribe_audio",
]
