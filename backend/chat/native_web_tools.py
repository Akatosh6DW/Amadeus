"""Adapt provider function calls to the existing conversation tool executor."""
from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlsplit


WEB_NAMES = {"web_search", "read_web_page"}


def native_web_enabled(settings: Mapping[str, Any], body: Mapping[str, Any], purpose: str) -> bool:
    # This provider's function calling is verified by the live comparison probe.
    # Other endpoints keep their existing JSON tool protocol.
    plan = body.get("_turnExecutionPlan") or {}
    required_web = bool(set(plan.get("requiredCapabilities") or []) & WEB_NAMES)
    web_in_progress = any(
        isinstance(item, Mapping) and item.get("sourceType") == "tool"
        and not item.get("historical")
        and (item.get("name") or (item.get("receipt") or {}).get("name")) in WEB_NAMES
        for item in (body.get("_cognitiveEvidence") or {}).values()
    )
    return (
        bool(settings.get("permissionWebSearch"))
        and (required_web or web_in_progress)
        and urlsplit(str(settings.get("apiEndpoint") or "")).hostname == "api.deepseek.com"
        and plan.get("researchPolicy") != "forbidden"
        and (purpose in {"chat_primary", "chat_tool_followup", "chat_fast_stream", "chat_fast_utterance_stream"}
             or purpose.startswith("chat_tool_query_loop_round_"))
    )


def prepare_native_web_payload(payload: Mapping[str, Any], body: Mapping[str, Any]) -> dict[str, Any]:
    active = dict(payload)
    definitions = [
        ("web_search", "Search public web information; returns titles, URLs, dates and snippets.",
         {"query": {"type": "string"}, "provider": {"type": "string", "enum": ["duckduckgo", "yahoo", "wikipedia", "brave"]},
          "freshness": {"type": "string", "enum": ["pd", "pw", "pm", "py"]},
          "locale": {"type": "string"}}, "query"),
        ("read_web_page", "Read a public webpage or linked document to answer the requested question.",
         {"url": {"type": "string"}, "researchQuery": {"type": "string"}}, "url"),
    ]
    active["tools"] = [{"type": "function", "function": {
        "name": name, "description": description, "parameters": {
            "type": "object", "properties": properties, "required": [required], "additionalProperties": False,
        },
    }} for name, description, properties, required in definitions]
    plan = body.get("_turnExecutionPlan") or {}
    evidence = body.get("_cognitiveEvidence") or {}
    attempted = any(
        isinstance(item, Mapping) and item.get("sourceType") == "tool"
        and not item.get("historical")
        and (item.get("name") or (item.get("receipt") or {}).get("name")) in WEB_NAMES
        and item.get("executionStatus") != "not_attempted"
        for item in evidence.values()
    )
    required = bool(set(plan.get("requiredCapabilities") or []) & WEB_NAMES)
    active["tool_choice"] = "required" if required and not attempted else "auto"
    if active["tool_choice"] == "required":
        # DeepSeek's documented forced-tool mode excludes thinking mode.
        active["thinking"] = {"type": "disabled"}
        active.pop("reasoning_effort", None)
    # Native function arguments have their own schema. Final speech still uses
    # the conversation JSON contract in the prompt and its existing validator.
    active.pop("response_format", None)
    active["stream"] = False
    active["messages"] = [*active.get("messages", []), {"role": "system", "content":
        "本轮提供原生联网函数。搜索和读网页直接使用这些函数；函数结果会作为工具证据返回。"
        "其他工具继续使用原来的 kind=tool_use/calls。资料够用后按原有对话 JSON 容器给出最终回答。"}]
    return active


def bind_native_web_context(content: str, body: Mapping[str, Any]) -> str:
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return content
    if not isinstance(value, dict) or not value.pop("_nativeWebToolCalls", False):
        return content
    snapshot = body.get("_turnContextSnapshot") or {}
    message = snapshot.get("currentUserMessage") or {}
    request = str(message.get("text") or body.get("userMessage") or "")
    if not isinstance(value.get("cognitive"), dict):
        # Native function calls need no fabricated state edits. Bind the real
        # request and let the existing executor register these read-only steps.
        value["cognitive"] = {"interactionAct": "CHAT", "understoodGoal": request, "patches": []}
    return json.dumps(value, ensure_ascii=False)
