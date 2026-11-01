"""Protect actual private content, never topic words such as 'system prompt'."""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import os
import secrets
from threading import RLock
import unicodedata


def normalize_for_match(value: str) -> str:
    """Matching projection only; callers keep canonical visible text intact."""
    return "".join(unicodedata.normalize("NFKC", value).split())


class PrivateContentGuard:
    def __init__(self, *, canary: str | None = None):
        self.canary = canary or "AMADEUS_PRIVATE_" + secrets.token_hex(18)
        self._spans: set[str] = set()
        self._secrets: set[str] = set()
        self._lock = RLock()

    def register_prompt(self, text: str) -> None:
        normalized = normalize_for_match(text)
        spans = {sha256(normalized[i:i + 24].encode("utf-8")).hexdigest()
                 for i in range(0, max(0, len(normalized) - 23), 6)}
        with self._lock:
            self._spans.update(spans)

    def register_secret(self, value: str) -> None:
        if isinstance(value, str) and len(value) >= 8:
            with self._lock:
                self._secrets.add(value)

    def configure(self, settings: Mapping | None) -> None:
        for key, value in (settings or {}).items():
            if isinstance(value, str) and str(key).casefold().endswith(("apikey", "api_key", "token", "secret")):
                self.register_secret(value)
        for name in ("AMADEUS_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            self.register_secret(os.environ.get(name, ""))

    def reason(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        if self.canary in text:
            return "canary"
        with self._lock:
            secret_values, spans = tuple(self._secrets), frozenset(self._spans)
        if any(secret in text for secret in secret_values):
            return "secret_value"
        if spans:
            value = normalize_for_match(text)
            if any(sha256(value[i:i + 24].encode("utf-8")).hexdigest() in spans
                   for i in range(max(0, len(value) - 23))):
                return "private_prompt_span"
        return ""


DEFAULT_GUARD = PrivateContentGuard()


def _explicit_static_prompt_values(payload: Mapping | None, messages: list) -> list[str]:
    """Return host-declared prompt templates eligible for span registration.

    A request's first system message is not an ownership boundary: QQ task
    prompts and cognitive projections can occupy that slot while containing
    owner text or external evidence. Callers that own a static prompt may
    provide it through the private transport field, or mark a system message
    with ``_amadeusPromptModule.registerPrivate``.
    """
    result: list[str] = []
    raw = payload or {}
    for key in ("_privatePromptTemplates", "_staticPromptTemplates"):
        values = raw.get(key)
        if isinstance(values, str):
            values = [values]
        if isinstance(values, (list, tuple, set)):
            result.extend(value for value in values if isinstance(value, str))
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") != "system":
            continue
        module = message.get("_amadeusPromptModule")
        if not isinstance(module, Mapping) or module.get("registerPrivate") is not True:
            continue
        content = message.get("content")
        if isinstance(content, str):
            result.append(content)
    return result


def protect_model_payload(payload: dict, settings: Mapping | None = None) -> dict:
    """Register only explicitly declared static host prompts.

    Task context, quoted messages, schemas and web evidence are carried in
    system-role messages by adapters. Position or role alone cannot prove
    private ownership, so dynamic messages are never registered implicitly.
    Canary and secret checks remain independent of prompt-span registration.
    """
    DEFAULT_GUARD.configure(settings)
    messages = list(payload.get("messages") or [])
    for text in _explicit_static_prompt_values(payload, messages):
        DEFAULT_GUARD.register_prompt(text)
    marker = {"role": "system", "content": (
        "[宿主私有标记，仅用于出站边界，不得出现在回复或工具参数]\n" + DEFAULT_GUARD.canary
    )}
    if not any(isinstance(m, Mapping) and m.get("content") == marker["content"] for m in messages):
        messages.insert(1 if messages else 0, marker)
    result = {key: value for key, value in payload.items()
              if key not in {"_privatePromptTemplates", "_staticPromptTemplates"}}
    return {**result, "messages": messages}
