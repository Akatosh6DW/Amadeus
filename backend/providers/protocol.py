"""上游模型响应和 SSE 行的纯协议解析规则。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from backend.providers.errors import UpstreamProtocolError


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


_NATIVE_WEB_TOOL_NAMES = {"web_search", "read_web_page"}


def _native_web_tool_container(message: Mapping[str, Any]) -> str:
    """Convert one native Chat Completions tool message to our call envelope.

    The transport layer intentionally keeps returning text.  A native tool
    response therefore becomes one complete semantic JSON value and continues
    through the existing cognitive/tool validation path.  Only the two public
    web tools are accepted here; arbitrary provider functions must not become
    executable application calls by accident.
    """

    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        raise UpstreamProtocolError("upstream tool_calls must be a non-empty list")

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
        if name not in _NATIVE_WEB_TOOL_NAMES:
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

        required_argument = "query" if name == "web_search" else "url"
        required_value = arguments.get(required_argument)
        if not isinstance(required_value, str) or not required_value.strip():
            raise UpstreamProtocolError(
                f"upstream tool {name} requires a non-empty {required_argument}"
            )

        call: dict[str, Any] = {"name": name, "args": arguments}
        native_id = raw_call.get("id")
        if native_id is not None and str(native_id).strip():
            call["toolCallId"] = str(native_id)
        calls.append(call)

    result: dict[str, Any] = {
        "kind": "tool_use",
        "_nativeWebToolCalls": True,
    }
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        try:
            content_value = json.loads(content)
        except json.JSONDecodeError:
            content_value = None
        if isinstance(content_value, Mapping) and isinstance(content_value.get("cognitive"), Mapping):
            result["cognitive"] = dict(content_value["cognitive"])
    result["calls"] = calls
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def extract_upstream_chat_text(data: Any) -> str:
    """接受兼容的文本结构，并拒绝没有可用消息的 HTTP 200 响应。"""

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
                    return _native_web_tool_container(message)
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    text_parts = [
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") in {None, "text", "output_text"}
                    ]
                    combined = "".join(text_parts).strip()
                    if combined:
                        return combined
            text = choice.get("text")
            if isinstance(text, str) and text.strip():
                return text
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    error = data.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or "provider_error")[:80]
        message = str(error.get("message") or "upstream returned an error object")[:300]
        raise UpstreamProtocolError(f"{code}: {message}")
    raise UpstreamProtocolError("upstream returned no usable assistant message")


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
