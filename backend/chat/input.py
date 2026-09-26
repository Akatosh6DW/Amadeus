"""提取当前用户消息与对话列表；不推断身份、不修改原始消息。"""
from typing import Any


def streaming_user_message(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    successive = [
        str(item).strip()
        for item in (body.get("userMessages") if isinstance(body.get("userMessages"), list) else [])
        if str(item).strip()
    ]
    return (
        "\n".join(successive) if successive else str(body.get("userMessage") or "").strip(),
        [item for item in messages if isinstance(item, dict)],
    )
