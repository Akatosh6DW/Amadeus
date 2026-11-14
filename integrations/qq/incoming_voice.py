"""Incoming OneBot voice handling for the QQ gateway.

This adapter is intentionally separate from permissions and reply routing.  It
turns only records that NapCat has already received into text.  The caller
still decides whether the resulting event is allowed to trigger a reply; the
transcribed words are never interpreted as authorization.
"""

from __future__ import annotations

import asyncio
import copy
import os
from pathlib import Path
from typing import Any, Mapping

from .asr_worker import (
    MAX_AUDIO_BYTES,
    MAX_SEGMENT_SECONDS,
    VoiceInputError,
    VoiceTranscriber,
    _local_path,
    audio_info,
)


MAX_RECORD_SEGMENTS = 5


def record_segments(event: object) -> list[dict[str, Any]]:
    """Return valid OneBot ``record`` segments from an event, in order.

    OneBot events carry ``message`` as a segment list.  A malformed record is
    not treated as an audio input because it cannot be handed to NapCat's
    ``get_record`` action safely.
    """

    if not isinstance(event, Mapping):
        return []
    message = event.get("message")
    if not isinstance(message, list):
        return []
    records: list[dict[str, Any]] = []
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "record":
            continue
        data = segment.get("data")
        if not isinstance(data, Mapping):
            continue
        file_value = data.get("file")
        if not isinstance(file_value, (str, os.PathLike)) or not str(os.fspath(file_value)).strip():
            # NapCat also accepts the OneBot ``file_id`` alias.  Keep the
            # segment untouched; the gateway call below still sends it as the
            # official ``file`` argument.
            file_value = data.get("file_id")
        if not isinstance(file_value, (str, os.PathLike)):
            continue
        if not str(os.fspath(file_value)).strip():
            continue
        records.append(copy.deepcopy(segment))
    return records


def _record_result_file(response: object) -> Path | None:
    """Extract only a local file returned by NapCat's ``get_record``."""

    if not isinstance(response, Mapping):
        return None
    status = response.get("status")
    if status is not None and str(status).strip().lower() not in {"ok", "success"}:
        return None
    retcode = response.get("retcode")
    if retcode not in (None, 0, "0"):
        return None
    if response.get("ok") is False:
        return None
    data = response.get("data")
    value: object = None
    if isinstance(data, Mapping):
        value = data.get("file")
    return _local_path(value)


async def _get_record_file(call, record_file: str) -> Path:
    try:
        response = await call("get_record", {"file": record_file, "out_format": "wav"})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise VoiceInputError("voice_record_failed", "语音文件暂时无法读取，请稍后重试。") from exc
    path = _record_result_file(response)
    if path is None:
        raise VoiceInputError("voice_record_failed", "语音文件暂时无法读取，请稍后重试。")
    return path


async def transcribe_event(call, event: dict, transcriber: VoiceTranscriber) -> dict:
    """Replace incoming record segments with text in an event copy.

    Every record is downloaded/converted by NapCat first.  The event's
    ``record.data.file`` value is an opaque OneBot identifier and is never
    opened as a local path by this function.  Segment order and all unrelated
    identity, mention, and reply fields are retained.
    """

    records = record_segments(event)
    if not records:
        return event
    if len(records) > MAX_RECORD_SEGMENTS:
        raise VoiceInputError("voice_too_many_segments", "一条消息最多识别五段语音，请分开发送。")

    message = event.get("message") if isinstance(event, Mapping) else None
    if not isinstance(message, list):
        return event
    result = copy.deepcopy(event)
    prepared_message = result.get("message")
    if not isinstance(prepared_message, list):
        return event

    total_bytes = 0
    transcripts: list[dict[str, Any]] = []
    for index, segment in enumerate(message):
        if not isinstance(segment, Mapping) or segment.get("type") != "record":
            continue
        data = segment.get("data")
        if not isinstance(data, Mapping):
            continue
        file_value = data.get("file")
        if not isinstance(file_value, (str, os.PathLike)) or not str(os.fspath(file_value)).strip():
            file_value = data.get("file_id")
        if not isinstance(file_value, (str, os.PathLike)):
            continue
        record_file = str(os.fspath(file_value)).strip()
        if not record_file:
            continue
        local_file = await _get_record_file(call, record_file)
        size, _duration = audio_info(local_file)
        total_bytes += size
        if total_bytes > MAX_AUDIO_BYTES:
            raise VoiceInputError("voice_audio_too_large", "这条消息的语音总大小超过 20 MB，请分开发送。")
        try:
            text = await transcriber.transcribe(local_file)
        except asyncio.CancelledError:
            raise
        except VoiceInputError:
            raise
        except Exception as exc:
            raise VoiceInputError("voice_asr_failed", "本地语音识别失败，请稍后重试。") from exc
        if not isinstance(text, str) or not text.strip():
            raise VoiceInputError("voice_asr_empty", "这条语音没听清，能再说一遍吗？")
        text = text.strip()
        prepared_message[index] = {"type": "text", "data": {"text": text}}
        transcripts.append({"index": index, "text": text, "source": "napcat:get_record"})

    if not transcripts:
        return event
    result["_voice_transcripts"] = transcripts
    return result


__all__ = [
    "MAX_AUDIO_BYTES",
    "MAX_RECORD_SEGMENTS",
    "MAX_SEGMENT_SECONDS",
    "VoiceInputError",
    "VoiceTranscriber",
    "record_segments",
    "transcribe_event",
]
