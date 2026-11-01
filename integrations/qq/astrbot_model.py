"""Map the existing OpenAI-compatible connection into AstrBot v4 provider config."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def apply_model(config: dict, model: dict, *, api_base: str | None = None, key: str | None = None):
    endpoint = urlsplit(str(model.get("apiEndpoint", "")))
    if api_base is None:
        suffix = "/chat/completions"
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc or not endpoint.path.endswith(suffix) or endpoint.query:
            raise ValueError("自动导入仅支持标准 chat/completions 端点；其他服务请在 AstrBot 面板配置")
        api_base = urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path[:-len(suffix)], "", ""))
    if not str(model.get("apiModel", "")).strip():
        raise ValueError("未选择聊天模型")
    ident = "amadeus-qq-openai"
    config["provider"] = [{"id":ident, "provider":"openai", "type":"openai_chat_completion",
        "provider_type":"chat_completion", "enable":True, "key":[key if key is not None else model.get("apiKey", "")],
        "api_base":api_base, "model":model["apiModel"], "timeout":120, "proxy":"", "custom_headers":{}}]
    config["provider_sources"] = []
    settings = config.setdefault("provider_settings", {})
    settings.update(enable=True, streaming_response=False, web_search=False, computer_use_runtime="none")
    settings.setdefault("file_extract", {})["enable"] = False
    settings.setdefault("proactive_capability", {})["add_cron_tools"] = False
    runner = config.setdefault("agent_runner", {})
    runner["runner_type"] = "local"
    runner.setdefault("config", {}).setdefault("model", {}).update(provider_id=ident, fallback_provider_ids=[])
