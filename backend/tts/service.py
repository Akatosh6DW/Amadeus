"""GPT-SoVITS 本地服务生命周期；唯一持有本进程启动的子进程与签名。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable
import urllib.request

from agent.fingerprints import runtime_fingerprint
from backend.common.values import clamp_int


@dataclass(frozen=True)
class GptSovitsServiceConfig:
    root: Path
    data_dir: Path
    config_dir: Path
    model_cache_root: Path
    script_path: Path
    log_path: Path
    rhubarb_exe_path: Path
    release_mode: bool
    reserved_ports: frozenset[int]


@dataclass(frozen=True)
class GptSovitsSettingsPort:
    load: Callable[[], dict[str, Any]]
    save: Callable[[dict[str, Any]], dict[str, Any]]
    normalize: Callable[[dict[str, Any]], dict[str, Any]]
    effective_reference_map: Callable[
        [dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]
    ]


class GptSovitsService:
    """进程、签名、启动锁的单一所有者；设置和网络入口由应用装配。"""

    def __init__(
        self,
        config: GptSovitsServiceConfig,
        settings: GptSovitsSettingsPort,
        *,
        features: Callable[[], Any],
        open_request: Callable[..., Any],
        process_factory: Callable[..., Any] | None = None,
        clock: Any = time,
        now_iso: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.settings = settings
        self.features = features
        self.open_request = open_request
        self.process_factory = process_factory or subprocess.Popen
        self.clock = clock
        self.now_iso = now_iso or (
            lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        )
        self.process: Any | None = None
        self.active_signature: tuple[str, ...] | None = None
        self.start_lock = threading.Lock()

    def local_path(self, value: Any) -> Path:
        path = Path(str(value or "").strip())
        return path if path.is_absolute() else (self.config.root / path).resolve()

    def port_error(self, settings: dict[str, Any]) -> str:
        port = clamp_int(settings.get("gptSovitsPort"), 1024, 65535, 9881)
        if port in self.config.reserved_ports:
            return f"端口 {port} 保留给 Amadeus System 主服务，不能用于语音服务"
        return ""

    def model_route_arguments(self, settings: dict[str, Any]) -> list[str]:
        """Build the child-process arguments for optional GPT model routes."""
        arguments: list[str] = []
        raw_routes = settings.get("gptSovitsGptModelRoutes")
        if isinstance(raw_routes, dict):
            for route, model_path in sorted(raw_routes.items()):
                arguments.extend(["--gpt-model-route", f"{route}={self.local_path(model_path)}"])
        arguments.extend([
            "--gpt-route-short-max-chars",
            str(settings.get("gptSovitsGptRouteShortMaxChars") or 32),
            "--gpt-route-long-min-chars",
            str(settings.get("gptSovitsGptRouteLongMinChars") or 60),
        ])
        return arguments


    def endpoint(self, settings: dict[str, Any]) -> str:
        port = clamp_int(settings.get("gptSovitsPort"), 1024, 65535, 9881)
        return f"http://127.0.0.1:{port}/tts"


    def health_url(self, settings: dict[str, Any]) -> str:
        port = clamp_int(settings.get("gptSovitsPort"), 1024, 65535, 9881)
        return f"http://127.0.0.1:{port}/health"


    def is_process_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


    def read_health(self, settings: dict[str, Any], timeout: float = 0.7) -> dict[str, Any] | None:
        """无代理读取本地健康状态；健康响应只接受 ok 的对象。"""
        request = urllib.request.Request(self.health_url(settings), method="GET")
        try:
            with self.open_request(request, timeout=timeout) as response:
                if response.status != 200:
                    return None
                payload = json.loads(response.read().decode("utf-8"))
                return payload if isinstance(payload, dict) and payload.get("ok") else None
        except Exception:
            return None


    def expected_capabilities(self) -> dict[str, Any]:
        """每次健康握手都读取当前重试契约，避免运行中配置变更被旧快照遮蔽。"""

        retry_contract = {
            "maxNormalAttempts": 3,
            "maxConservativeAttempts": 0,
        }
        contract_path = self.config.config_dir / "tts_runtime_contract.json"
        try:
            raw_contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_contract = {}
        if isinstance(raw_contract, dict):
            for key, default in tuple(retry_contract.items()):
                try:
                    retry_contract[key] = max(0, int(raw_contract.get(key, default)))
                except (TypeError, ValueError):
                    retry_contract[key] = default
        retry_contract["maxNormalAttempts"] = max(
            1,
            retry_contract["maxNormalAttempts"],
        )

        return {
            "resegmentOnFailure": bool(
                self.features().tts_resegment_on_failure_enabled
            ),
            "adaptiveRetryShadow": bool(
                self.features().tts_adaptive_retry_shadow_enabled
            ),
            "boundedRetry": bool(
                self.features().tts_bounded_retry_enabled
            ),
            "highConfidenceResegment": bool(
                self.features().tts_high_confidence_resegment_enabled
            ),
            "coherentReferenceFallback": True,
            "criticalRouteWarmup": True,
            **retry_contract,
        }


    def capability_mismatches(self, payload: dict[str, Any] | None) -> list[str]:
        capabilities = (
            payload.get("capabilities")
            if isinstance(payload, dict) and isinstance(payload.get("capabilities"), dict)
            else {}
        )
        return [
            key
            for key, expected in self.expected_capabilities().items()
            if capabilities.get(key) != expected
        ]


    def is_running(self, settings: dict[str, Any], timeout: float = 0.7) -> bool:
        payload = self.read_health(settings, timeout)
        return bool(
            payload
            and payload.get("instanceSignature") == self.signature_token(settings)
            and not self.capability_mismatches(payload)
        )


    def stop_conflicting(self, settings: dict[str, Any]) -> bool:
        """维持旧协议：有不同签名/能力的健康实例可按其签名请求关闭。

        这可能是别的进程启动的本地实例；迁移只保留旧条件与请求参数。
        """
        payload = self.read_health(settings, timeout=0.9)
        if not payload:
            return False
        same_signature = (
            payload.get("instanceSignature") == self.signature_token(settings)
        )
        if same_signature and not self.capability_mismatches(payload):
            return False
        observed = str(payload.get("instanceSignature") or "")
        if not observed:
            return False
        endpoint = self.health_url(settings).rsplit("/", 1)[0] + "/shutdown"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps({"instanceSignature": observed}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.open_request(request, timeout=2.0) as response:
                if response.status != 200:
                    return False
            deadline = self.clock.monotonic() + 8.0
            while self.clock.monotonic() < deadline:
                if self.read_health(settings, timeout=0.4) is None:
                    return True
                self.clock.sleep(0.2)
        except Exception:
            return False
        return False


    def required_paths(self, settings: dict[str, Any]) -> dict[str, Path]:
        env_path = self.local_path(settings.get("gptSovitsCondaEnv"))
        paths = {
            "Python 环境": env_path / "python.exe",
            "GPT-SoVITS 根目录": self.local_path(settings.get("gptSovitsRoot")),
            "GPT 权重": self.local_path(settings.get("gptSovitsGptModel")),
            "SoVITS 权重": self.local_path(settings.get("gptSovitsSovitsModel")),
            "启动 GPT 权重": self.local_path(settings.get("gptSovitsBootstrapGptModel")),
            "启动 SoVITS 权重": self.local_path(settings.get("gptSovitsBootstrapSovitsModel")),
            "参考音频": self.local_path(settings.get("gptSovitsRefAudio")),
            "参考文本": self.local_path(settings.get("gptSovitsRefText")),
            "TTS 启动脚本": self.config.script_path,
        }
        if str(settings.get("gptSovitsStyleMap") or "").strip():
            paths["情绪参考映射"] = self.local_path(settings.get("gptSovitsStyleMap"))
        if not self.config.release_mode:
            for route, model_path in sorted(
                (settings.get("gptSovitsGptModelRoutes") or {}).items()
            ):
                paths[f"GPT {route} 路由权重"] = self.local_path(model_path)
        return paths


    def missing_paths(self, settings: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for label, path in self.required_paths(settings).items():
            if not path.exists():
                missing.append(f"{label}: {path}")
        return missing


    def status(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = self.settings.load() if settings is None else self.settings.normalize(settings)
        health = self.read_health(settings)
        capability_mismatches = self.capability_mismatches(health)
        running = bool(
            health
            and health.get("instanceSignature") == self.signature_token(settings)
            and not capability_mismatches
        )
        owned = self.is_process_alive()
        return {
            "running": running,
            "starting": owned and not running,
            "owned": owned,
            "pid": self.process.pid if owned else None,
            "endpoint": self.endpoint(settings),
            "logPath": str(self.config.log_path),
            "missing": self.missing_paths(settings),
            "capabilities": (
                health.get("capabilities")
                if isinstance(health, dict)
                else {}
            ),
            "criticalWarmup": (
                health.get("criticalWarmup")
                if isinstance(health, dict)
                and isinstance(health.get("criticalWarmup"), dict)
                else {"ready": False, "routes": [], "elapsedMs": 0}
            ),
            "expectedCapabilities": self.expected_capabilities(),
            "capabilityMismatches": capability_mismatches,
        }


    def start(self, raw_settings: dict[str, Any] | None = None, wait_seconds: float = 0) -> dict[str, Any]:
        """启动或复用服务；进程句柄和签名仅在此对象内更新。

        原顺序是先处理设置与参考音频，再验路径/签名、关闭冲突、启动并等健康。
        空字典按旧逻辑读取设置，非空字典才先保存。
        """
        configured_settings = self.settings.save(raw_settings) if raw_settings else self.settings.load()
        settings, candidate_data = self.settings.effective_reference_map(configured_settings)
        endpoint = self.endpoint(settings)
        port_error = self.port_error(settings)
        if port_error:
            return {
                **self.status(settings),
                "running": False,
                "starting": False,
                "error": "tts_port_conflict",
                "message": port_error,
            }
        missing = self.missing_paths(settings)
        if missing:
            return {
                "running": False,
                "endpoint": endpoint,
                "error": "GPT-SoVITS 配置缺少文件",
                "missing": missing,
            }

        signature = self.signature(settings)
        if self.is_process_alive() and self.active_signature and self.active_signature != signature:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=4)
            self.process = None
            self.active_signature = None
        self.stop_conflicting(settings)
        if self.is_running(settings) and (not self.is_process_alive() or self.active_signature == signature):
            settings = self.settings.save({
                "ttsProvider": "gpt-sovits",
                "ttsEndpoint": endpoint,
            })
            return {**self.status(settings), "message": "GPT-SoVITS 已在线"}

        if self.process is not None and self.process.poll() is not None:
            self.process = None

        if self.process is None:
            paths = self.required_paths(settings)
            env = os.environ.copy()
            if self.config.release_mode:
                # Diagnostic stage WAV capture is a source-only inspection feature;
                # a frozen build must not inherit an external path that re-enables it.
                env.pop("AMADEUS_TTS_DIAGNOSTIC_DIR", None)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["AMADEUS_TTS_RESEGMENT_ON_FAILURE"] = (
                "1"
                if self.features().tts_resegment_on_failure_enabled
                else "0"
            )
            env["AMADEUS_TTS_ADAPTIVE_RETRY_SHADOW"] = (
                "1"
                if self.features().tts_adaptive_retry_shadow_enabled
                else "0"
            )
            env["AMADEUS_TTS_BOUNDED_RETRY"] = (
                "1"
                if self.features().tts_bounded_retry_enabled
                else "0"
            )
            env["AMADEUS_TTS_HIGH_CONFIDENCE_RESEGMENT"] = (
                "1"
                if self.features().tts_high_confidence_resegment_enabled
                else "0"
            )
            env["HF_HOME"] = str(self.config.model_cache_root / "huggingface")
            env["TORCH_HOME"] = str(self.config.model_cache_root / "torch")
            # Numba-backed Librosa functions require a writable cache locator at
            # import time. A portable release may live in a read-only directory,
            # and staged __pycache__ directories are intentionally omitted.
            numba_cache = self.config.data_dir / "cache" / "numba"
            numba_cache.mkdir(parents=True, exist_ok=True)
            env["NUMBA_CACHE_DIR"] = str(numba_cache)
            weight_state = self.config.data_dir / "cache" / "gpt-sovits" / "weight.json"
            weight_state.parent.mkdir(parents=True, exist_ok=True)
            env["AMADEUS_GPT_SOVITS_WEIGHT_JSON"] = str(weight_state)
            gpt_root = paths["GPT-SoVITS 根目录"]
            python_path_parts = [str(gpt_root), str(gpt_root / "GPT_SoVITS")]
            if env.get("PYTHONPATH"):
                python_path_parts.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
            command = [
                str(paths["Python 环境"]),
                "-X",
                "utf8",
                str(self.config.script_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(settings["gptSovitsPort"]),
                "--gpt-sovits-root",
                str(gpt_root),
                "--gpt-model",
                str(paths["GPT 权重"]),
                "--sovits-model",
                str(paths["SoVITS 权重"]),
                "--bootstrap-gpt-model",
                str(paths["启动 GPT 权重"]),
                "--bootstrap-sovits-model",
                str(paths["启动 SoVITS 权重"]),
                "--instance-signature",
                self.signature_token(settings),
                "--default-ref-audio",
                str(paths["参考音频"]),
                "--default-ref-text",
                str(paths["参考文本"]),
                "--ref-language",
                "日文",
                "--target-language",
                str(settings.get("gptSovitsTargetLanguage") or "日文"),
                "--rhubarb-exe",
                str(self.config.rhubarb_exe_path),
            ]
            if str(settings.get("gptSovitsStyleMap") or "").strip():
                command.extend(["--style-map", str(self.local_path(settings.get("gptSovitsStyleMap")))])
            command.extend(self.model_route_arguments(settings))
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = self.config.log_path.open("ab")
            try:
                log_file.write(f"\n\n[{self.now_iso()}] starting GPT-SoVITS TTS\n".encode("utf-8"))
                if candidate_data.get("enabled"):
                    log_file.write(
                        (
                            f"[{self.now_iso()}] developer reference candidates: "
                            f"{','.join(candidate_data.get('appliedMappings') or [])}\n"
                        ).encode("utf-8")
                    )
                log_file.flush()
                self.process = self.process_factory(
                    command,
                    cwd=str(self.config.root),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
                self.active_signature = signature
            finally:
                # 子进程已继承输出句柄；父进程无论启动成败都应关闭自己的副本。
                log_file.close()

        if wait_seconds <= 0:
            settings = self.settings.save({
                "ttsProvider": "gpt-sovits",
                "ttsEndpoint": endpoint,
            })
            return {**self.status(settings), "message": "GPT-SoVITS 正在后台启动"}

        deadline = self.clock.time() + wait_seconds
        while self.clock.time() < deadline:
            if self.process and self.process.poll() is not None:
                code = self.process.returncode
                self.process = None
                return {
                    "running": False,
                    "endpoint": endpoint,
                    "error": f"GPT-SoVITS 启动进程已退出，退出码 {code}",
                    "missing": [],
                }
            if self.is_running(settings, timeout=1.2):
                settings = self.settings.save({
                    "ttsProvider": "gpt-sovits",
                    "ttsEndpoint": endpoint,
                })
                return {**self.status(settings), "message": "GPT-SoVITS 已启动"}
            observed_health = self.read_health(settings, timeout=0.4)
            capability_mismatches = self.capability_mismatches(observed_health)
            if (
                observed_health
                and observed_health.get("instanceSignature")
                == self.signature_token(settings)
                and capability_mismatches
            ):
                return {
                    **self.status(settings),
                    "running": False,
                    "error": "tts_capability_handshake_mismatch",
                    "message": (
                        "GPT-SoVITS 启动配置与主服务不一致："
                        + ", ".join(capability_mismatches)
                    ),
                }
            self.clock.sleep(1.0)

        return {
            **self.status(settings),
            "error": f"GPT-SoVITS 正在启动，但 {wait_seconds:g} 秒内还没有返回健康状态",
        }


    def stop(self) -> dict[str, Any]:
        """只终止本运行时拥有的 Popen；外部健康实例不属于 stop 的对象。"""
        settings = self.settings.load()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=4)
        self.process = None
        self.active_signature = None
        return self.status(settings)


    def ensure_available(
        self,
        settings: dict[str, Any],
        wait_seconds: float = 28,
        *,
        metrics: dict[str, Any] | None = None,
        is_running: Callable[..., bool] | None = None,
        start: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        # 入口传入晚绑定方法，既保留 server 的 patch 点，也避免第二份进程状态。
        # 锁内再次查健康是并发请求的屏障；三项时长各自覆盖原等待范围。
        is_running = is_running or self.is_running
        start = start or self.start
        observation = metrics if metrics is not None else {}
        observation.update({"initiallyReady": False, "startLockWaitMs": 0, "startupWaitMs": 0})
        started = self.clock.perf_counter()
        try:
            if is_running(settings, timeout=1.0):
                observation["initiallyReady"] = True
                return
            if not settings.get("autoStartGptSovits", True):
                raise RuntimeError("GPT-SoVITS 当前离线，并且自动启动已关闭")
            lock_started = self.clock.perf_counter()
            with self.start_lock:
                observation["startLockWaitMs"] = round((self.clock.perf_counter() - lock_started) * 1000)
                if is_running(settings, timeout=1.0):
                    return
                startup_started = self.clock.perf_counter()
                try:
                    status = start(wait_seconds=wait_seconds)
                    if not status.get("running"):
                        raise RuntimeError(status.get("error") or "GPT-SoVITS 自动启动失败")
                finally:
                    observation["startupWaitMs"] = round((self.clock.perf_counter() - startup_started) * 1000)
        finally:
            observation["readinessWaitMs"] = round((self.clock.perf_counter() - started) * 1000)


    def signature(self, settings: dict[str, Any]) -> tuple[str, ...]:
        effective_settings, _candidate_data = self.settings.effective_reference_map(settings)
        style_map_value = str(effective_settings.get("gptSovitsStyleMap") or "").strip()
        style_map_path = self.local_path(style_map_value) if style_map_value else None
        style_map_hash = ""
        if style_map_path and style_map_path.is_file():
            try:
                style_map_hash = runtime_fingerprint(style_map_path.read_bytes(), length=32)
            except OSError:
                style_map_hash = ""
        return (
            str(self.local_path(effective_settings.get("gptSovitsGptModel"))),
            str(self.local_path(effective_settings.get("gptSovitsSovitsModel"))),
            str(self.local_path(effective_settings.get("gptSovitsBootstrapGptModel"))),
            str(self.local_path(effective_settings.get("gptSovitsBootstrapSovitsModel"))),
            str(self.local_path(effective_settings.get("gptSovitsRefAudio"))),
            str(self.local_path(effective_settings.get("gptSovitsRefText"))),
            str(style_map_path) if style_map_path else "",
            style_map_hash,
            json.dumps(
                {
                    route: str(self.local_path(path))
                    for route, path in sorted(
                        (effective_settings.get("gptSovitsGptModelRoutes") or {}).items()
                    )
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            str(effective_settings.get("gptSovitsGptRouteShortMaxChars") or 32),
            str(effective_settings.get("gptSovitsGptRouteLongMinChars") or 60),
            str(effective_settings.get("gptSovitsTargetLanguage") or "日文"),
            str(effective_settings.get("gptSovitsPort") or 9881),
        )


    def signature_token(self, settings: dict[str, Any]) -> str:
        normalized = "\0".join(item.casefold() for item in self.signature(settings))
        return runtime_fingerprint(normalized, length=32)
