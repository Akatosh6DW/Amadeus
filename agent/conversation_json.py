"""Lossless decoding and actionable diagnostics for one conversation container."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping


JSON_CONTAINER_INSTRUCTION = """[本次响应的 JSON 容器格式]
返回且仅返回一个完整 JSON 对象，以 { 开始，以 } 结束。字段和允许的 kind 由当前阶段协议决定；图片输入不会改变该协议。
不要输出 JSON 数组、多个对象、逐行事件、Markdown 围栏或对象之外的解释。不要把整个对象再次编码成字符串。
回答内容放在当前协议的文本字段内。字符串中的换行、双引号和反斜杠必须按 JSON 转义；数组或对象最后一项后不要加逗号。
先完成内部结构再发送完整对象；不要遗漏当前阶段要求的认知、回答、工具或结果字段。保持当前阶段，不把格式整理变成新的任务或重新执行工具。
"""


def format_recovery_payload(payload: Mapping[str, Any], *, disable_thinking: bool) -> dict[str, Any]:
    result = dict(payload)
    # Preserve the caller's JSON-mode setting, including an explicit omission.
    result["max_tokens"] = max(4096, int(result.get("max_tokens") or 0))
    if disable_thinking:
        result["thinking"] = {"type": "disabled"}
        result.pop("reasoning_effort", None)
    return result


def _outer_spans(text: str) -> tuple[list[str], bool]:
    """Never search inside a broken outer container for a usable inner object."""
    spans: list[str] = []
    start = -1
    stack: list[str] = []
    quoted = escaped = False
    for index, char in enumerate(text):
        if start < 0:
            if char in "{[":
                start = index
                stack.append(char)
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack.pop() != ("{" if char == "}" else "["):
                return spans, False
            if not stack:
                spans.append(text[start:index + 1])
                start = -1
    return spans, start < 0


def parse_conversation_json(text: str, *, normalize: Callable[[str], str]) -> tuple[dict | None, dict]:
    """Recover wrappers/syntax, never invent missing values or combine decisions."""
    candidate = str(text or "").strip().lstrip("\ufeff").strip()
    repairs: list[str] = []
    if not candidate:
        return None, {"reason": "empty_output"}
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.S | re.I)
    if fenced:
        candidate = fenced.group(1).strip()
        repairs.append("markdown_fence")
    error: json.JSONDecodeError | None = None
    for _ in range(4):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as failure:
            error = failure
            normalized = normalize(candidate)
            if normalized != candidate:
                candidate = normalized
                repairs.append("json_syntax")
                continue
            break
        if isinstance(value, dict):
            return value, {"repairs": repairs}
        if isinstance(value, str) and value.strip() != candidate:
            candidate = value.strip()
            repairs.append("encoded_object")
            continue
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            return value[0], {"repairs": [*repairs, "single_object_array"]}
        return None, {"reason": "object_required", "topLevelType": type(value).__name__}
    spans, complete = _outer_spans(candidate)
    if len(spans) > 1:
        return None, {"reason": "multiple_containers", "containerCount": len(spans),
                      "repair": "Combine the intended response into one complete object; do not emit JSONL or multiple decisions."}
    # A broken object/array must not be truncated to a valid leading portion.
    # Prose/fence wrappers around exactly one complete object are unambiguous.
    wrapped = (not candidate.startswith(("{", "[", '"'))
               or (candidate.startswith("{") and error is not None and error.msg == "Extra data"))
    if complete and len(spans) == 1 and wrapped:
        start = candidate.find(spans[0])
        for extra in (candidate[:start].strip(), candidate[start + len(spans[0]):].strip()):
            if not extra:
                continue
            try:
                json.loads(extra)
            except json.JSONDecodeError:
                continue
            return None, {"reason": "multiple_containers", "containerCount": 2}
        value, diagnostic = parse_conversation_json(spans[0], normalize=normalize)
        if value is not None:
            return value, {"repairs": [*repairs, "surrounding_text", *diagnostic.get("repairs", [])]}
    diagnostic = {"reason": "invalid_json" if spans or candidate.startswith(("{", "[", '"')) else "plain_text",
                  "containerComplete": complete}
    if error is not None:
        diagnostic.update(jsonError=error.msg, line=error.lineno, column=error.colno, offset=error.pos)
    return None, diagnostic
