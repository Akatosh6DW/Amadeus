"""QQ transport for Amadeus's existing speech contract and TTS service.

No QQ API, desktop playback, voice selection or new synthesis engine lives here.
Only the gateway's explicitly enabled voice mode may invoke this adapter.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
import re
import wave


SPEECH_FIELDS = frozenset({
    'textZh', 'spokenTextJa', 'voiceStyle', 'voiceTone', 'voiceIntensity',
    'emotionIntensity', 'speechSpeed', 'speechPace', 'unitId',
})


def speech_plan(result: dict) -> dict:
    """Keep the finalized speech track, not cognition/history or arbitrary audio URLs."""
    units = result.get('speechUnits') or []
    return {'speechLanguage': result.get('speechLanguage'), 'speechUnits': [
        {key: value for key, value in unit.items() if key in SPEECH_FIELDS}
        for unit in units if isinstance(unit, dict) and unit.get('deliveryRequired') is not False
    ]}


def audio_path(root: Path, draft_id: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{32}', draft_id):
        raise ValueError('无效语音草稿标识')
    return root / 'qq-audio' / (draft_id + '.wav')


def _number(value, default=1.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def synthesize(server, root: Path, draft_id: str, text: str, plan: dict) -> dict:
    """Called in the same scoped worker as chat; saves audio, never sends it."""
    from backend.chat.presentation import speech_language_profile
    settings = server.load_settings()
    language = speech_language_profile(settings)
    units = speech_plan(plan).get('speechUnits', [])
    if not units or plan.get('speechLanguage') != language['speechLanguage']:
        # A literal owner-authored send has no model speech track. Use the same
        # frozen-text translation and speech-unit builder as desktop delivery.
        snapshot = server.build_persona_context_snapshot(text, [], {})
        raw = server.normalize_model_reply({'reply': text, 'spokenTextJa': ''}, settings,
                                           persona_snapshot=snapshot)
        raw['reply'] = text
        raw['speechLanguage'] = language['speechLanguage']
        if language['speechLanguage'] == 'ja-JP':
            raw = server.align_spoken_text_ja(settings, raw, persona_snapshot=snapshot)
        prepared = server.speech_unit_contract_for_result(raw, request_id='qq-voice-' + draft_id)
        units = speech_plan(prepared)['speechUnits']
    if not units:
        raise ValueError('没有可用的语音稿，未发送语音或替代文字')
    chunks = []
    for index, unit in enumerate(units):
        spoken = str(unit.get('textZh' if language['speechLanguage'] == 'zh-CN' else 'spokenTextJa') or '').strip()
        if not spoken:
            raise ValueError('语音稿不完整，未发送')
        body = {
            'text': spoken, 'voiceStyle': unit.get('voiceStyle') or 'calm',
            'voiceTone': unit.get('voiceTone') or 'neutral',
            'voiceIntensity': unit.get('voiceIntensity') or 'normal',
            'emotionIntensity': min(2.5, max(.2, _number(unit.get('emotionIntensity')))),
            'speechSpeed': min(1.45, max(.62, _number(unit.get('speechSpeed')) * _number(settings.get('speechRate')))),
            'speechPace': unit.get('speechPace') or 'normal',
            'speechProsody': settings.get('speechProsody'), **language,
            'segmentMode': 'joined', 'semanticUnits': [spoken],
            'requestId': 'qq-voice-' + draft_id, 'utteranceId': unit.get('unitId') or f'{draft_id}-{index}',
            'segmentIndex': index, 'segmentTotal': len(units),
        }
        if settings.get('ttsProvider', 'gpt-sovits') == 'gpt-sovits':
            prepared = server.prepare_tts_with_deferred_alignment(body)
            if prepared.get('ok') is False or not prepared.get('audioBase64'):
                raise ValueError('原有语音合成服务未返回音频')
            audio = base64.b64decode(prepared['audioBase64'], validate=True)
        else:
            _, audio = server.synthesize_tts(body)
        chunks.append(audio)
    # One QQ voice message, retaining the existing semantic units and all audio.
    destination = audio_path(root, draft_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        expected = None
        for chunk in chunks:
            with wave.open(io.BytesIO(chunk), 'rb') as source:
                params = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if expected is None:
                    expected = params
                    output.setnchannels(params[0])
                    output.setsampwidth(params[1])
                    output.setframerate(params[2])
                elif params != expected:
                    raise ValueError('合成音频格式不一致，未发送不完整语音')
                frames = source.readframes(source.getnframes())
                if not frames:
                    raise ValueError('合成音频为空')
                output.writeframes(frames)
    destination.write_bytes(buffer.getvalue())
    return {'ok': True, 'audio_path': str(destination), 'reply_format': 'voice'}
