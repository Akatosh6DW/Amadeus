"""构建 TTS 请求并通过本地 HTTP 交付语音。

入口显式提供设置、请求规则、传输和日志回调。子进程启停与启动锁仍由入口的
就绪回调负责；本模块不持有进程状态。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Mapping
import urllib.parse
import urllib.request


@dataclass(frozen=True)
class TtsRequestRules:
    """构建请求体时所需的产品归一化规则。"""

    language_profile: Callable[[dict[str, Any], dict[str, Any]], dict[str, str]]
    voice_intensity: Callable[[Any], str]
    speech_pace: Callable[[Any, Any], str]
    speech_prosody: Callable[[Any], str]
    clamp_float: Callable[[Any, float, float, float], float]
    clamp_int: Callable[[Any, int, int, int], int]
    pace_speed: Callable[[str], float]
    validate_preflight: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class TtsTransport:
    """本地 HTTP 传输及入口持有的服务就绪边界。"""

    gpt_endpoint: Callable[[dict[str, Any]], str]
    ensure_available: Callable[..., None]
    open_local: Callable[..., Any]
    open_direct: Callable[..., Any]


class TtsClient:
    """按当前设置构建请求，并交付给配置的语音提供器。

    回调在调用时解析，以保留 ``server`` 的旧测试 patch 点。JSON 请求、
    音频回退和延迟对齐读取也由调用者逐次传入。
    """

    def __init__(
        self,
        *,
        load_settings: Callable[[], dict[str, Any]],
        default_provider: Callable[[], str],
        rules: TtsRequestRules,
        transport: TtsTransport,
        log_event: Callable[..., None],
        fingerprint: Callable[..., str],
        normalize_request_id: Callable[[Any], str],
        perf_counter: Callable[[], float],
        synthesis_error_type: type[Exception],
    ) -> None:
        self._load_settings = load_settings
        self._default_provider = default_provider
        self._rules = rules
        self._transport = transport
        self._log_event = log_event
        self._fingerprint = fingerprint
        self._normalize_request_id = normalize_request_id
        self._perf_counter = perf_counter
        self._synthesis_error_type = synthesis_error_type

    def _speech_fields(
        self,
        body: dict[str, Any],
        settings: dict[str, Any],
        text: str,
        *,
        language_profile: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        language_profile = language_profile or self._rules.language_profile(body, settings)
        speech_pace = self._rules.speech_pace(body.get("speechPace"), body.get("speechSpeed"))
        return {
            "text": text,
            "voiceStyle": str(body.get("voiceStyle") or "calm"),
            "voiceTone": str(body.get("voiceTone") or "neutral"),
            "voiceIntensity": self._rules.voice_intensity(body.get("voiceIntensity")),
            "emotionIntensity": self._rules.clamp_float(body.get("emotionIntensity"), 0.2, 2.5, 1.0),
            "speechPace": speech_pace,
            "speechSpeed": self._rules.clamp_float(
                body.get("speechSpeed"), 0.62, 1.45, self._rules.pace_speed(speech_pace),
            ),
            "speaker": settings.get("ttsSpeaker") or "1",
            "locale": language_profile["locale"],
            "speechLanguage": language_profile["speechLanguage"],
            "speechProsody": self._rules.speech_prosody(body.get("speechProsody")),
        }

    @staticmethod
    def _json_post(url: str, body: Mapping[str, Any]) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def synthesize(self, body: dict[str, Any]) -> tuple[str, bytes]:
        settings = self._load_settings()
        endpoint = settings.get("ttsEndpoint", "").strip().rstrip("/")
        provider = settings.get("ttsProvider", self._default_provider()).strip().lower()
        if provider == "gpt-sovits" and not endpoint:
            endpoint = self._transport.gpt_endpoint(settings)
        text = str(body.get("text") or "").strip()
        # 校验先于就绪检查：无效请求不应启动服务，也不应争用启动锁。
        if not text:
            raise ValueError("TTS text is empty.")
        if not endpoint or provider in {"", "system"}:
            raise ValueError("Local TTS endpoint is not configured.")
        if provider == "gpt-sovits":
            self._rules.validate_preflight(body)
            self._transport.ensure_available(settings)
        language_profile = self._rules.language_profile(body, settings)

        if provider == "voicevox":
            speaker = urllib.parse.quote(settings.get("ttsSpeaker") or "1")
            query_url = f"{endpoint}/audio_query?text={urllib.parse.quote(text)}&speaker={speaker}"
            query_request = urllib.request.Request(query_url, data=b"", method="POST")
            with self._transport.open_direct(query_request, timeout=45) as response:
                query = response.read()
            synth_url = f"{endpoint}/synthesis?speaker={speaker}"
            synth_request = urllib.request.Request(
                synth_url,
                data=query,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self._transport.open_direct(synth_request, timeout=90) as response:
                return response.headers.get("Content-Type", "audio/wav"), response.read()

        payload = self._speech_fields(body, settings, text, language_profile=language_profile)
        payload.update({
            "segmentMode": str(body.get("segmentMode") or "auto"),
            "semanticUnits": body.get("semanticUnits"),
        })
        if provider == "gpt-sovits":
            payload["deliveryPostProcessingEnabled"] = bool(
                settings.get("ttsDeliveryPostProcessingEnabled", False)
            )
        request = self._json_post(endpoint, payload)
        request_opener = (
            self._transport.open_local if provider == "gpt-sovits" else self._transport.open_direct
        )
        with request_opener(request, timeout=90) as response:
            return response.headers.get("Content-Type", "audio/wav"), response.read()

    def synthesize_timed(
        self, body: dict[str, Any], *, synthesize: Callable[[dict[str, Any]], tuple[str, bytes]],
    ) -> dict[str, Any]:
        settings = self._load_settings()
        provider = settings.get("ttsProvider", self._default_provider()).strip().lower()
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("TTS text is empty.")

        if provider == "gpt-sovits":
            self._rules.validate_preflight(body)
            self._transport.ensure_available(settings)
            endpoint = settings.get("ttsEndpoint", "").strip().rstrip("/") or self._transport.gpt_endpoint(settings)
            timed_endpoint = endpoint.rsplit("/", 1)[0] + "/tts_timed"
            payload = self._speech_fields(body, settings, text)
            payload.update({
                "alignModel": str(body.get("alignModel") or "fast"),
                "segmentMode": str(body.get("segmentMode") or "auto"),
                "semanticUnits": body.get("semanticUnits"),
                "deliveryPostProcessingEnabled": bool(
                    settings.get("ttsDeliveryPostProcessingEnabled", False)
                ),
            })
            request = self._json_post(timed_endpoint, payload)
            with self._transport.open_local(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))

        content_type, audio = synthesize(body)
        return {
            "ok": True,
            "contentType": content_type,
            "audioBase64": base64.b64encode(audio).decode("ascii"),
            "alignment": {
                "source": "none", "requestedText": text, "asrText": "", "duration": 0,
                "words": [], "items": [],
            },
        }

    def gpt_json_request(
        self, path: str, body: dict[str, Any], timeout: int = 180,
    ) -> dict[str, Any]:
        """记录设置、就绪和下游 HTTP 各阶段耗时；默认下游超时 180 秒。"""
        started = self._perf_counter()
        timings: dict[str, Any] = {"downstreamHttpMs": None}
        stage = "settings"
        failure = ""
        try:
            settings = self._load_settings()
            timings["settingsLoadMs"] = round((self._perf_counter() - started) * 1000)
            stage = "readiness"
            # 注入的回调拥有进程启动锁；就绪、HTTP 和引擎耗时须分别记录。
            self._transport.ensure_available(settings, metrics=timings)
            endpoint = settings.get("ttsEndpoint", "").strip().rstrip("/") or self._transport.gpt_endpoint(settings)
            base_endpoint = endpoint.rsplit("/", 1)[0]
            request = self._json_post(base_endpoint + path, body)
            stage = "http"
            http_started = self._perf_counter()
            try:
                with self._transport.open_local(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("GPT-SoVITS returned an invalid JSON payload.")
            finally:
                timings["downstreamHttpMs"] = round((self._perf_counter() - http_started) * 1000)
            stage = "completed"
            payload["transportMetrics"] = timings
            return payload
        except Exception as error:
            failure = type(error).__name__
            raise
        finally:
            timings["transportTotalMs"] = round((self._perf_counter() - started) * 1000)
            self._log_event(
                "tts_transport_completed", requestId=str(body.get("requestId") or "")[:120],
                utteranceId=str(body.get("utteranceId") or "")[:120], endpointPath=path,
                stage=stage, errorType=failure, **timings,
            )

    def cancel(self, request_id: str) -> bool:
        """尽力取消，直连 HTTP 最多等待 2 秒，不启动已停止的服务。

        取消可能与请求或进程启动并发，不能等待就绪检查所用的启动锁。
        """
        normalized = self._normalize_request_id(request_id)
        if not normalized:
            return False
        try:
            settings = self._load_settings()
            if str(settings.get("ttsProvider") or "").strip().lower() != "gpt-sovits":
                return False
            endpoint = str(settings.get("ttsEndpoint") or "").strip().rstrip("/")
            if not endpoint:
                endpoint = self._transport.gpt_endpoint(settings)
            base_endpoint = endpoint.rsplit("/", 1)[0]
            request = self._json_post(base_endpoint + "/tts_cancel", {"requestId": normalized})
            with self._transport.open_direct(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return bool(isinstance(payload, dict) and payload.get("cancelled"))
        except Exception:
            return False

    def prepare_with_deferred_alignment(
        self,
        body: dict[str, Any],
        *,
        json_request: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        """先交付音频，稍后独立读取对齐信息。

        下游准备请求沿用 JSON 请求默认的 180 秒超时；对齐读取限时 15 秒。
        """
        settings = self._load_settings()
        if settings.get("ttsProvider", self._default_provider()).strip().lower() != "gpt-sovits":
            raise ValueError("Deferred alignment is only available for GPT-SoVITS.")
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("TTS text is empty.")
        self._rules.validate_preflight(body)
        speech_fields = self._speech_fields(body, settings, text)
        request_id = str(body.get("requestId") or "").strip()[:120]
        utterance_id = str(body.get("utteranceId") or "").strip()[:120]
        segment_index = self._rules.clamp_int(body.get("segmentIndex"), 1, 64, 1)
        segment_total = self._rules.clamp_int(body.get("segmentTotal"), 1, 64, 1)
        supplied_semantic_units = body.get("semanticUnits")
        compact_contract_text = lambda value: re.sub(r"\s+", "", str(value or ""))
        semantic_units_joined = (
            "".join(str(item or "") for item in supplied_semantic_units)
            if isinstance(supplied_semantic_units, list) else ""
        )
        started = self._perf_counter()
        self._log_event(
            "tts_utterance_prepare_started",
            requestId=request_id, utteranceId=utterance_id,
            segmentIndex=segment_index, segmentTotal=segment_total,
            textCharacters=len(text),
            semanticUnitCount=(len(supplied_semantic_units) if isinstance(supplied_semantic_units, list) else 0),
            textFingerprint=self._fingerprint(text, length=16),
            semanticUnitsFingerprint=(
                self._fingerprint(semantic_units_joined, length=16)
                if isinstance(supplied_semantic_units, list) else ""
            ),
            semanticUnitsCoverText=(
                compact_contract_text(semantic_units_joined) == compact_contract_text(text)
                if isinstance(supplied_semantic_units, list) else None
            ),
        )
        downstream_body = speech_fields
        downstream_body.update({
            "alignModel": str(body.get("alignModel") or "fast"),
            "segmentMode": str(body.get("segmentMode") or "auto"),
            "semanticUnits": supplied_semantic_units,
            "requestId": request_id,
            "utteranceId": utterance_id,
            "segmentIndex": segment_index,
            "segmentTotal": segment_total,
            "deliveryPostProcessingEnabled": bool(
                settings.get("ttsDeliveryPostProcessingEnabled", False)
            ),
        })
        try:
            payload = json_request("/tts_prepare", downstream_body)
        except Exception as error:
            failure_metrics = (
                error.diagnostics
                if isinstance(error, self._synthesis_error_type)
                and isinstance(error.diagnostics, dict) else {}
            )
            self._log_event(
                "tts_utterance_prepare_failed",
                requestId=request_id, utteranceId=utterance_id,
                segmentIndex=segment_index, segmentTotal=segment_total,
                elapsedMs=round((self._perf_counter() - started) * 1000),
                errorType=type(error).__name__, error=str(error)[:1000],
                plannedInternalSegments=int(failure_metrics.get("plannedSegments") or failure_metrics.get("segments") or 0),
                attemptCount=int(failure_metrics.get("attemptCount") or 0),
                retryCount=int(failure_metrics.get("retryCount") or 0),
                failedInferenceMs=int(failure_metrics.get("failedInferenceMs") or 0),
                resegmentationEnabled=bool(failure_metrics.get("resegmentationEnabled")),
                failureReasons=(
                    list(failure_metrics.get("failureReasons"))
                    if isinstance(failure_metrics.get("failureReasons"), list) else []
                ),
                referenceAudio=str(failure_metrics.get("referenceAudio") or ""),
                primaryFailedSegment=int(failure_metrics.get("failedSegment") or 0),
                primaryReferenceAudio=str(
                    failure_metrics.get("primaryReferenceAudio") or failure_metrics.get("referenceAudio") or ""
                ),
                fallbackFailedSegment=int(failure_metrics.get("fallbackFailedSegment") or 0),
                fallbackReferenceAudio=str(failure_metrics.get("fallbackReferenceAudio") or ""),
                fallbackFailureReasons=(
                    list(failure_metrics.get("fallbackFailureReasons"))
                    if isinstance(failure_metrics.get("fallbackFailureReasons"), list) else []
                ),
            )
            raise
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        transport = payload.get("transportMetrics") if isinstance(payload.get("transportMetrics"), dict) else {}
        self._log_event(
            "tts_utterance_prepare_completed",
            requestId=request_id, utteranceId=utterance_id,
            segmentIndex=segment_index, segmentTotal=segment_total,
            elapsedMs=round((self._perf_counter() - started) * 1000),
            audioPresent=bool(payload.get("audioBase64")),
            synthesisMs=int(metrics.get("synthesisMs") or 0),
            readinessWaitMs=transport.get("readinessWaitMs"),
            downstreamHttpMs=transport.get("downstreamHttpMs"),
            synthesisLockWaitMs=metrics.get("synthesisLockWaitMs"),
            plannedInternalSegments=int(metrics.get("plannedSegments") or 0),
            deliveredInternalSegments=int(metrics.get("segments") or 0),
            attemptCount=int(metrics.get("attemptCount") or 0),
            retryCount=int(metrics.get("retryCount") or 0),
            inferenceGenerationMs=int(metrics.get("inferenceGenerationMs") or 0),
            failedInferenceMs=int(metrics.get("failedInferenceMs") or 0),
            resegmentationEnabled=bool(metrics.get("resegmentationEnabled")),
            resegmentedSegments=int(metrics.get("resegmentedSegments") or 0),
            referenceAudio=str(metrics.get("referenceAudio") or ""),
            failureReasons=(
                list(metrics.get("failureReasons"))
                if isinstance(metrics.get("failureReasons"), list) else []
            ),
        )
        return payload

    @staticmethod
    def read_deferred_alignment(
        body: dict[str, Any], *, json_request: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        """验证对齐 ID 后，以 15 秒下游超时读取已生成的对齐信息。"""
        alignment_id = str(body.get("alignmentId") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", alignment_id):
            raise ValueError("Invalid alignment id.")
        return json_request("/tts_alignment", {"alignmentId": alignment_id}, timeout=15)
