"""Run the existing Amadeus conversation pipeline in a QQ-specific data scope.

Each conversation is serialized and owns a persistent data directory. A short
lived subprocess owns the server module's global state, so no desktop session
or other QQ conversation is ever switched underneath a running request.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
from .media import segment_description
from .identity import mention_text, mentions, event_sender
from .references import reply_text
from .dialogue import profile_path
from .triggers import local_group_match, quoted_message_id, trigger_record


ROOT = Path(__file__).resolve().parents[2]
MODEL_KEYS = frozenset({
    "apiEndpoint", "apiModel", "apiKey", "apiNetworkMode", "apiProxyUrl",
    "apiContextLimit", "apiContextCompactThreshold", "useResponseFormat",
    "contextSemanticRouterEnabled", "cognitiveRepairModel", "webSearchProvider", "webSearchFallbackProvider",
    "webSearchApiKey", "deepseekSearchApiKey", "webSearchMaxResults", "webReadTopPages", "webRequestTimeout",
    "webResearchBudgets", "permissionWebSearch",
    "visionEnabled", "visionModel", "visionApiKey", "visionEndpoint",
    "ttsProvider", "ttsEndpoint", "ttsSpeaker", "speechRate", "speechLanguage", "speechProsody",
    "ttsDeliveryPostProcessingEnabled", "gptSovitsModelPreset", "alignSpokenJa",
    "gptSovitsPort", "autoStartGptSovits",
    "voicePresetRevision", "gptSovitsCondaEnv", "gptSovitsRoot", "gptSovitsGptModel",
    "gptSovitsSovitsModel", "gptSovitsBootstrapGptModel", "gptSovitsBootstrapSovitsModel",
    "gptSovitsGptModelRoutes", "gptSovitsGptRouteShortMaxChars", "gptSovitsGptRouteLongMinChars",
    "gptSovitsRefAudio", "gptSovitsRefText", "gptSovitsStyleMap", "gptSovitsTargetLanguage",
})
QQ_TOOLS = frozenset({"get_system_time", "local_compute", "web_search", "read_web_page", "qq_history", "qq_manage", "qq_identity", "qq_remember_alias", "qq_person_memory", "qq_media"})
MAX_QQ_TEXT_CHARS = 6000


def scope_key(account: str, kind: str, target: str) -> str:
    if kind not in {"private", "group"} or not account.isascii() or not target.isascii() or not account.isdigit() or not target.isdigit():
        raise ValueError("无效 QQ 会话身份")
    return f"{account}-{kind}-{target}"


def reply_request_id(account: str, kind: str, target: str, message_id) -> str:
    key = scope_key(account, kind, target)
    return 'qq-' + hashlib.sha256((key + ':' + str(message_id)).encode()).hexdigest()


def _message_text(event: dict) -> str:
    """Extract content independently of whether this event should wake a reply."""
    segments = event.get("message")
    if not isinstance(segments, list):
        return ""
    return "".join(str((s.get("data") or {}).get("text", "")) if s.get('type') == 'text' else mention_text(s) if s.get('type') == 'at' else reply_text({'message': [s]}) if s.get('type') == 'reply' else segment_description(s)
                   for s in segments if isinstance(s, dict)).strip()


def message_text_length(event: dict) -> int:
    """Return the assembled QQ text length without applying the model limit."""
    return len(_message_text(event))


def message_text(event: dict) -> str:
    """Extract content independently of whether this event should wake a reply."""
    text = _message_text(event)
    if len(text) > MAX_QQ_TEXT_CHARS:
        raise ValueError("QQ 文字消息超过 6000 字，未送入模型")
    return text


def reply_trigger(event: dict, account: str, policy: dict | None = None) -> bool:
    """Cheap candidate filter; quoted messages must be resolved outside the WS reader."""
    if event.get('message_type') == 'group':
        return bool(local_group_match(event, account, policy or {}) or quoted_message_id(event, policy or {}))
    return event.get('message_type') == 'private' and bool(message_text(event))


def session_settings(model: dict, root: Path, *, voice: bool = False, speech_language: str = 'inherit') -> dict:
    settings = {k: v for k, v in model.items() if k in MODEL_KEYS}
    if speech_language not in {'inherit', 'zh-CN', 'ja-JP'}:
        raise ValueError('无效 QQ 语音语言')
    if speech_language != 'inherit':
        from backend.chat.presentation import speech_language_profile
        profile = speech_language_profile({'speechLanguage': speech_language})
        settings.update(speechLanguage=speech_language, voiceLocale=profile['locale'],
                        gptSovitsTargetLanguage=profile['targetLanguage'])
    settings.update({
        "memoryFile": str(root / "memory.json"), "agentFileWorkspace": str(root / "agent-workspace"),
        "permissionReadScreen": False, "permissionReadCamera": False,
        "permissionFileRead": False, "permissionFileWrite": False,
        "permissionOpenBrowser": False, "permissionQqControl": False,
        "permissionReadSystemTime": True, "voiceEnabled": False,
        "alignSpokenJa": bool(voice and model.get("alignSpokenJa", True)),
        "autoStartGptSovits": bool(voice and model.get("autoStartGptSovits", True)),
        # QQ workers receive the same public model settings as the desktop
        # chat.  Older/sanitized model files may contain only the endpoint,
        # model and key; keep the desktop default (vision on) in that case.
        # An explicit false remains a user choice and still disables both the
        # attachment reader and native image input.
        "visionEnabled": bool(model.get("visionEnabled", True)),
        "proactiveInteractionsEnabled": False, "proactiveScreenObservationEnabled": False,
    })
    return settings


class AmadeusRunner:
    def __init__(self, runtime: Path, *, python: str | None = None, timeout: float = 180, frozen: bool = False):
        self.runtime = runtime.resolve()
        desktop_python = ROOT / ".conda" / "amadeus-desktop" / ("python.exe" if os.name == "nt" else "bin/python")
        self.python = python or (str(desktop_python) if desktop_python.exists() else sys.executable)
        self.timeout = timeout
        self.frozen = frozen
        self.slots = asyncio.Semaphore(2)
        self.locks: dict[str, asyncio.Lock] = {}

    @property
    def model_path(self):
        return self.runtime / "amadeus-model.json"

    def configured(self) -> bool:
        try:
            model = json.loads(self.model_path.read_text(encoding="utf-8"))
            endpoint = urlparse(str(model.get("apiEndpoint", "")))
            needs_key = (endpoint.scheme == "https" and endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
                         and not (endpoint.hostname or "").endswith(".local"))
            return bool(model.get("apiEndpoint") and model.get("apiModel") and (model.get("apiKey") or not needs_key))
        except (OSError, ValueError, AttributeError):
            return False

    async def reply(self, *, account: str, kind: str, target: str, event: dict,
                    delivery_history: list[dict] | None = None, conversation_context: dict | None = None,
                    reply_instructions: str = "", images: list | None = None,
                    media_notes: list | None = None, compact_reply: bool = True,
                    reply_identity: str = "assistant", owner_instruction: str = "", owner_tasks: list | None = None,
                    trigger: dict | None = None, reply_format: str = "text", speech_language: str = 'inherit',
                    owner_ids: list[str] | None = None, account_role: str = 'assistant',
                    identity_context: dict | None = None, identity_scope: dict | None = None,
                    message_context: dict | None = None, reply_style: str = 'game') -> dict:
        key = scope_key(account, kind, target)
        if (not owner_instruction and event.get('user_id') is not None
                and isinstance(event.get('sender'), dict) and event['sender'].get('user_id') is not None
                and not event_sender(event)):
            raise ValueError('QQ 发言者标识冲突，未送入模型')
        if not owner_instruction and trigger is None and not reply_trigger(event, account):
            return {"reply": "", "skipped": True}
        text = owner_instruction or message_text(event)
        if not self.configured():
            raise ValueError("请先配置 data/qq-agent/amadeus-model.json 中的模型")
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock, self.slots:
            root = profile_path(self.runtime, key, reply_identity, reply_style)
            root.mkdir(parents=True, exist_ok=True)
            request_id = reply_request_id(account, kind, target, event['message_id'])
            payload = {"action": "chat", "request_id": request_id, "text": text,
                       "account": account, "kind": kind, "target": target,
                       "sender": event_sender(event), "delivery_history": delivery_history or []}
            payload['owner_ids'] = list(owner_ids or [])
            payload.update(account_role=account_role, mentions=mentions(event),
                           identity_context=identity_context or {}, identity_scope=identity_scope,
                           message_context=message_context or {})
            if not owner_instruction and kind == 'group':
                body = message_text({**event, 'message': [s for s in event.get('message', []) if isinstance(s, dict) and s.get('type') not in {'at', 'reply'}]})
                payload['trigger'] = {**(trigger or trigger_record(event, 'mention')), 'hasBody': bool(body)}
            payload["conversation_context"] = conversation_context or {}
            payload["reply_instructions"] = reply_instructions[:2000]
            payload['reply_style'] = reply_style
            payload.update(images=images or [], media_notes=media_notes or [], compact_reply=compact_reply,
                           reply_identity=reply_identity, owner_instruction=owner_instruction, owner_tasks=owner_tasks or [],
                           reply_format=reply_format, speech_language=speech_language)
            return await self._invoke(root, payload)

    async def synthesize(self, *, account: str, kind: str, target: str,
                         reply_identity: str, draft_id: str, text: str, speech: dict,
                         speech_language: str = 'inherit', reply_style: str = 'game') -> dict:
        key = scope_key(account, kind, target)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock, self.slots:
            root = profile_path(self.runtime, key, reply_identity, reply_style)
            root.mkdir(parents=True, exist_ok=True)
            return await self._invoke(root, {
                'action': 'voice', 'account': account, 'kind': kind, 'target': target,
                'reply_identity': reply_identity, 'reply_style': reply_style, 'draft_id': draft_id,
                'text': text, 'speech': speech, 'reply_format': 'voice', 'speech_language': speech_language,
            })

    async def _invoke(self, root: Path, payload: dict) -> dict:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
        prefix = [self.python, '--qq-helper', 'worker'] if self.frozen else [self.python, '-X', 'utf8', '-m', 'integrations.qq.amadeus_worker']
        command = [*prefix,
                   "--runtime-root", str(self.runtime), "--data-root", str(root), "--model-config", str(self.model_path)]
        # Windows CREATE_NO_WINDOW keeps the per-turn worker out of the desktop.
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryDirectory(prefix='request-', dir=root) as temporary, (root / "worker.stderr.log").open("ab") as errors:
            wire = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            result_path = Path(temporary) / 'output.json'
            if self.frozen:
                request_path = Path(temporary) / 'input.json'
                request_path.write_bytes(wire)
                command += ['--input-file', str(request_path), '--output-file', str(result_path)]
            process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=errors, creationflags=flags)
            try:
                output, _ = await asyncio.wait_for(process.communicate(None if self.frozen else wire), self.timeout)
                if self.frozen and process.returncode == 0:
                    output = result_path.read_bytes()
            except (TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
        if process.returncode:
            raise RuntimeError("Amadeus QQ 对话进程失败；详见该会话的本机日志")
        result = json.loads(output)
        if not result.get("ok"):
            raise ValueError(str(result.get("error") or "Amadeus 没有生成可发送的回复"))
        return result
