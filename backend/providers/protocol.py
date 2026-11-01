"""上游模型响应和 SSE 行的纯协议解析规则。"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit
from collections.abc import Callable, Mapping
from typing import Any, Sequence

from backend.providers.errors import (
    UpstreamProtocolError,
    classify_provider_response_body,
)


def prepare_provider_payload(settings: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt wire constraints without changing instruction roles or user history.

    Gemini's compatibility endpoint retains only the last system message.
    Consolidate all instruction messages, in order, before crossing that API
    boundary so persona, output contract and validation feedback survive.
    Gemini extracts system/developer messages into systemInstruction. An
    assistant draft followed by our system feedback therefore still ends in a
    model turn, which its Chat Completions adapter rejects. Keep that feedback
    authoritative and add a neutral transport continuation only when needed.
    This projection is never persisted as a user utterance.
    """
    active = dict(payload)
    # Module provenance is host metadata, never a provider message field.
    if isinstance(active.get("messages"), list):
        from backend.context.prompt_assembly import wire_messages
        active["messages"] = wire_messages(active["messages"])
    json_tools_available = active.pop('_amadeusJsonToolFallback', False) is True
    host = urlsplit(str(settings.get("apiEndpoint") or "")).hostname
    if host == 'api.deepseek.com' and json_tools_available and active.get('tools'):
        thinking = active.get('thinking')
        thinking_enabled = not isinstance(thinking, Mapping) or thinking.get('type') != 'disabled'
        messages = list(active.get('messages') or [])
        missing_reasoning = any(isinstance(item, Mapping) and item.get('role') == 'assistant'
                                and not isinstance(item.get('reasoning_content'), str) for item in messages)
        native_history = any(isinstance(item, Mapping) and (item.get('role') == 'tool' or item.get('tool_calls'))
                             for item in messages)
        if thinking_enabled and missing_reasoning and not native_history:
            # Synthetic cognitive candidates and compacted history do not have
            # original provider reasoning. Never invent it or discard thinking.
            # Amadeus already executes these same tools via its JSON protocol.
            active.pop('tools', None)
            active.pop('tool_choice', None)
            active.pop('parallel_tool_calls', None)
            active['messages'] = [*messages, {'role': 'system', 'content':
                '[工具传输兼容] 本次续写使用既有 JSON 工具协议，替代前文的原生函数传输说明。'
                'web_search/read_web_page 以及其他已授权工具仍可用：需要调用时返回原协议 kind=tool_use 和 calls，'
                '保留 cognitive 等必填字段。已有工具证据继续有效，资料不足就继续查询，最终回答仍使用原有对话 JSON 容器。'}]
    if host != "generativelanguage.googleapis.com":
        return active
    # DeepSeek's top-level thinking field is rejected by Google's compatibility
    # endpoint. Translate the fast-response intent only where Gemini supports
    # it; never overwrite an explicit provider-native reasoning preference.
    thinking = active.pop("thinking", None)
    model = str(active.get("model") or settings.get("apiModel") or "").lower()
    if isinstance(thinking, Mapping) and thinking.get("type") == "disabled" and model.startswith("gemini-3"):
        active.setdefault("reasoning_effort", "low")
    messages = list(active.get("messages") or [])
    instructions = [item for item in messages if isinstance(item, Mapping)
                    and item.get("role") in {"system", "developer"}]
    if instructions:
        blocks = []
        for item in instructions:
            content = item.get("content")
            if isinstance(content, str):
                blocks.append(content)
            elif isinstance(content, list):
                blocks.extend(str(part.get("text") or "") for part in content
                              if isinstance(part, Mapping) and part.get("type") == "text")
        messages = [{"role": "system", "content": "\n\n".join(blocks)},
                    *[item for item in messages if not isinstance(item, Mapping)
                      or item.get("role") not in {"system", "developer"}]]
    turns = [item for item in messages if isinstance(item, Mapping)
             and item.get("role") not in {"system", "developer"}]
    if not turns or turns[-1].get("role") == "assistant":
        messages.append({"role": "user", "content":
                         "Continue the current request under the system instructions and validation feedback. "
                         "This is an application continuation, not a new user request or authorization."})
    active["messages"] = messages
    return active


def upstream_response_shape(data: Any) -> dict[str, Any]:
    """仅输出响应结构和长度，避免把模型正文写入诊断日志。"""
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    choices = data.get("choices")
    choice_shapes: list[dict[str, Any]] = []
    if isinstance(choices, list):
        for choice in choices[:3]:
            if not isinstance(choice, dict):
                choice_shapes.append({"type": type(choice).__name__})
                continue
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            reasoning_content = message.get("reasoning_content") if isinstance(message, dict) else None
            choice_shapes.append({
                "type": "dict",
                "finishReason": str(choice.get("finish_reason") or "")[:80],
                "messageType": type(message).__name__,
                "messageKeys": sorted(str(key) for key in message.keys())[:20] if isinstance(message, dict) else [],
                "contentType": type(content).__name__ if isinstance(message, dict) else "missing",
                "contentChars": len(content) if isinstance(content, str) else 0,
                "contentNonWhitespaceChars": (
                    len(re.sub(r"\s+", "", content))
                    if isinstance(content, str) else 0
                ),
                "reasoningContentType": type(reasoning_content).__name__ if isinstance(message, dict) else "missing",
                "reasoningChars": len(reasoning_content) if isinstance(reasoning_content, str) else 0,
            })
    return {
        "type": "dict",
        "keys": sorted(str(key) for key in data.keys())[:20],
        "choicesType": type(choices).__name__,
        "choicesCount": len(choices) if isinstance(choices, list) else 0,
        "choices": choice_shapes,
        "hasError": bool(data.get("error")),
    }


def upstream_finish_reason(data: Any) -> str:
    """提取第一个可用的完成原因。"""
    choices = data.get("choices") if isinstance(data, dict) else []
    if not isinstance(choices, list):
        return ""
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        reason = str(choice.get("finish_reason") or "").strip()
        if reason:
            return reason[:80]
    return ""


_NATIVE_WEB_TOOL_NAMES = frozenset({"web_search", "read_web_page"})
_NATIVE_SELECTION_FIELDS = ("toolAuthorization", "targetDomain")


def _registered_native_tool_names(
    allowed_tool_names: Sequence[str] | None = None,
) -> frozenset[str]:
    """Return registered names accepted by the provider-native adapter.

    The provider response is only a transport representation.  The registry
    remains the trust boundary here; the normal tool planner still performs
    permission, argument, task and receipt validation after this conversion.
    An optional per-turn allowlist can narrow the registry without making the
    protocol layer maintain a second list of product tools.
    """

    # Import lazily so the low-level provider parser remains usable by tests
    # and provider error handling even while the application registry loads.
    from agent.tool_architecture import DEFAULT_TOOL_REGISTRY

    registered = frozenset(item.tool_id for item in DEFAULT_TOOL_REGISTRY.all())
    if allowed_tool_names is None:
        return registered
    if isinstance(allowed_tool_names, str):
        allowed_tool_names = (allowed_tool_names,)
    return frozenset(
        str(name).strip()
        for name in allowed_tool_names
        if str(name).strip() in registered
    )


def _native_web_tool_container(
    message: Mapping[str, Any],
    *,
    allowed_tool_names: Sequence[str] | None = None,
) -> str:
    """Convert native Chat Completions function calls to our call envelope.

    The transport layer intentionally keeps returning text.  A native tool
    response therefore becomes one complete semantic JSON value and continues
    through the existing cognitive/tool validation path.  Only names present
    in the application registry (and, when supplied, the current-turn
    allowlist) are accepted; arbitrary provider functions must not become
    executable application calls by accident.
    """

    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        raise UpstreamProtocolError("upstream tool_calls must be a non-empty list")

    accepted_names = _registered_native_tool_names(allowed_tool_names)
    content_value: Mapping[str, Any] | None = None
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        try:
            parsed_content = json.loads(content)
        except json.JSONDecodeError:
            parsed_content = None
        if isinstance(parsed_content, Mapping):
            content_value = parsed_content
    root_selection = {
        field: source[field]
        for source in (message, content_value or {})
        for field in _NATIVE_SELECTION_FIELDS
        if field in source
    }
    calls: list[dict[str, Any]] = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, Mapping):
            raise UpstreamProtocolError(f"upstream tool_calls[{index}] must be an object")
        call_type = raw_call.get("type")
        if call_type is not None and str(call_type).strip() not in {"", "function"}:
            raise UpstreamProtocolError(
                f"unsupported upstream tool call type at index {index}"
            )
        function = raw_call.get("function")
        if not isinstance(function, Mapping):
            raise UpstreamProtocolError(
                f"upstream tool_calls[{index}].function must be an object"
            )
        name = str(function.get("name") or "").strip()
        if name not in accepted_names:
            raise UpstreamProtocolError(f"unsupported upstream tool function: {name[:80]}")

        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, Mapping):
            arguments = dict(raw_arguments)
        elif isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as error:
                raise UpstreamProtocolError(
                    f"invalid JSON arguments for upstream tool {name}"
                ) from error
        else:
            raise UpstreamProtocolError(
                f"missing arguments for upstream tool {name}"
            )
        if not isinstance(arguments, dict):
            raise UpstreamProtocolError(
                f"arguments for upstream tool {name} must be a JSON object"
            )

        selection = dict(root_selection)
        for field in _NATIVE_SELECTION_FIELDS:
            if field in arguments:
                selection[field] = arguments.pop(field)
        if name == "web_search":
            required_argument = "query"
            required_value = arguments.get(required_argument)
            if not isinstance(required_value, str) or not required_value.strip():
                raise UpstreamProtocolError(
                    f"upstream tool {name} requires a non-empty {required_argument}"
                )
        elif name == "read_web_page":
            required_argument = "url"
            required_value = arguments.get(required_argument)
            if not isinstance(required_value, str) or not required_value.strip():
                raise UpstreamProtocolError(
                    f"upstream tool {name} requires a non-empty {required_argument}"
                )

        call: dict[str, Any] = {"name": name, "args": arguments}
        native_id = raw_call.get("id")
        if native_id is not None and str(native_id).strip():
            call["toolCallId"] = str(native_id)
        if selection:
            call["selection"] = selection
        calls.append(call)

    all_web_calls = all(call["name"] in _NATIVE_WEB_TOOL_NAMES for call in calls)
    result: dict[str, Any] = {
        "kind": "tool_use",
        ("_nativeWebToolCalls" if all_web_calls else "_nativeToolCalls"): True,
    }
    if isinstance(content_value, Mapping) and isinstance(content_value.get("cognitive"), Mapping):
        result["cognitive"] = dict(content_value["cognitive"])
    result["calls"] = calls
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def extract_upstream_chat_text(
    data: Any,
    *,
    allowed_tool_names: Sequence[str] | None = None,
) -> str:
    """接受兼容的文本结构，并拒绝没有可用消息的 HTTP 200 响应。

    ``allowed_tool_names`` is an optional narrow per-turn boundary.  Callers
    that do not have one still get the single registry boundary rather than a
    provider-specific hard-coded allowlist.
    """

    if not isinstance(data, dict):
        raise UpstreamProtocolError(f"upstream response is {type(data).__name__}, expected object")
    choices = data.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                if message.get("tool_calls"):
                    return _native_web_tool_container(
                        message,
                        allowed_tool_names=allowed_tool_names,
                    )
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    classification = classify_provider_response_body(content, http_status=200)
                    if classification.is_error:
                        raise UpstreamProtocolError(
                            "provider returned an HTTP-200 error body",
                            category=classification.category,
                            code=classification.category,
                            status=200,
                        )
                    return content
                if isinstance(content, list):
                    text_parts = [
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") in {None, "text", "output_text"}
                    ]
                    combined = "".join(text_parts).strip()
                    if combined:
                        classification = classify_provider_response_body(combined, http_status=200)
                        if classification.is_error:
                            raise UpstreamProtocolError(
                                "provider returned an HTTP-200 error body",
                                category=classification.category,
                                code=classification.category,
                                status=200,
                            )
                        return combined
            text = choice.get("text")
            if isinstance(text, str) and text.strip():
                classification = classify_provider_response_body(text, http_status=200)
                if classification.is_error:
                    raise UpstreamProtocolError(
                        "provider returned an HTTP-200 error body",
                        category=classification.category,
                        code=classification.category,
                        status=200,
                    )
                return text
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        classification = classify_provider_response_body(output_text, http_status=200)
        if classification.is_error:
            raise UpstreamProtocolError(
                "provider returned an HTTP-200 error body",
                category=classification.category,
                code=classification.category,
                status=200,
            )
        return output_text
    error = data.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or "provider_error")[:80]
        message = str(error.get("message") or "upstream returned an error object")[:300]
        raise UpstreamProtocolError(
            f"{code}: {message}",
            category="error_object",
            code=code,
            status=200,
        )
    raise UpstreamProtocolError(
        "upstream returned no usable assistant message",
        category="protocol_invalid",
        code="no_usable_assistant_message",
        status=200,
    )


def json_mode_fallback_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """关闭提供器 JSON 模式，同时把原输出协议保留在提示词里。"""
    active = dict(payload)
    output_format = active.pop("response_format", None) or {}
    instruction = (
        "[输出格式兼容恢复] 本次使用普通传输模式，前文规定的 JSON 输出协议保持不变。"
        "请严格按前文当前阶段的协议返回一个完整 JSON 对象，保留所有必填字段、工具调用和证据引用。"
        "自然语言只放在协议指定的文本字段里；不输出对象之外的说明、Markdown 围栏或推理过程。"
    )
    if isinstance(output_format, Mapping) and output_format.get("json_schema"):
        instruction += "\n原输出结构：" + json.dumps(output_format["json_schema"], ensure_ascii=False)
    active["messages"] = [*payload.get("messages", []), {"role": "system", "content": instruction}]
    return active


def prepare_upstream_json_mode(
    settings: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    fallback: Callable[[Mapping[str, Any]], dict[str, Any]] = json_mode_fallback_payload,
) -> tuple[dict[str, Any], dict | None, str]:
    """读取单轮、单 endpoint/model 的 JSON 模式降级状态。"""
    active = dict(payload)
    state = active.pop("_cognitiveJsonModeState", None)
    state = state if isinstance(state, dict) else None
    key = json.dumps([str(settings.get("apiEndpoint") or ""),
                      str(active.get("model") or settings.get("apiModel") or "")])
    if state is not None and state.get(key) and active.get("response_format"):
        active = fallback(active)
    return active, state, key


def extract_upstream_stream_delta(data: Any) -> str:
    """从兼容的 SSE 事件中提取一个可交付文本增量。"""

    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list):
        return ""
    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") in {None, "text", "output_text"}
                )
        elif isinstance(choice.get("text"), str):
            parts.append(str(choice["text"]))
    return "".join(parts)


def parse_sse_data_line(raw_line: bytes) -> tuple[str, Any]:
    """按原有单行 SSE 约定识别元数据、完成标记和 JSON 正文。

    返回 ``skip``、``done``、``malformed`` 或 ``json``；无效 JSON 由
    调用方计数，严格 UTF-8 解码错误仍交给传输层处理。
    """

    line = raw_line.decode("utf-8", errors="strict").strip()
    if not line or line.startswith(("event:", "id:", "retry:", ":")):
        return "skip", None
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line:
        return "skip", None
    if line == "[DONE]":
        return "done", None
    try:
        return "json", json.loads(line)
    except json.JSONDecodeError:
        return "malformed", None
