"""User-owned capability scope. No content or intent classification."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_CONFIG = {
    "activityFrequency": 100,
    # Retained for old clients/config files; automatic wakeups use activityFrequency.
    "enabled": False, "intervalMinutes": 60, "dailyRunLimit": 8, "maxSteps": 6,
    "activeStartHour": 8, "activeEndHour": 23,
    "instructions": "按自己的兴趣阅读、思考，保存值得继续探索的问题。有实际发现再写日记，不必每次发言。",
    "directories": [], "sources": [], "allowWeb": True, "allowQqRead": False,
    "allowQqChat": False, "qqChatOwner": "", "qqChatIntervalMinutes": 180,
    "publishMode": "draft", "publishAccount": "", "publishVisibility": "friends",
    "dailyPublishLimit": 1,
}


def normalize_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("自主生活配置必须是对象")
    unknown = set(raw) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError("未知自主生活配置项：" + ", ".join(sorted(unknown)))
    result = {**DEFAULT_CONFIG, **raw}
    for key in ("enabled", "allowWeb", "allowQqRead", "allowQqChat"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} 必须是开关")
    for key, low, high in (
        ("activityFrequency", 25, 200),
        ("intervalMinutes", 5, 1440), ("dailyRunLimit", 1, 48), ("maxSteps", 2, 12),
        ("activeStartHour", 0, 23), ("activeEndHour", 0, 23), ("dailyPublishLimit", 1, 5),
        ("qqChatIntervalMinutes", 30, 10080),
    ):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} 必须是 {low}–{high} 的整数")
    for key, maximum in (("instructions", 4000), ("publishAccount", 20), ("qqChatOwner", 20)):
        if not isinstance(result[key], str) or len(result[key]) > maximum:
            raise ValueError(f"{key} 必须是至多 {maximum} 字的文本")
        result[key] = result[key].strip()
    account = result["publishAccount"]
    if account and not (account.isascii() and account.isdigit() and 5 <= len(account) <= 20):
        raise ValueError("请输入完整 QQ 账号")
    owner = result["qqChatOwner"]
    if owner and not (owner.isascii() and owner.isdigit() and 5 <= len(owner) <= 20):
        raise ValueError("请选择完整的主人 QQ 号")
    if result["allowQqChat"] and (not account or not owner):
        raise ValueError("主动闲聊需要绑定代理账号，并选择 QQ 页中已配置的主人")
    if not isinstance(result["publishMode"], str) or result["publishMode"] not in {"off", "draft", "automatic"}:
        raise ValueError("发布模式必须是 off、draft 或 automatic")
    if result["publishMode"] == "automatic" and not account:
        raise ValueError("自动发布需要绑定 QQ 账号：请先在 QQ 页配置代理账号，或在此填写完整 QQ 号")
    if not isinstance(result["publishVisibility"], str) or result["publishVisibility"] not in {"friends", "private", "public"}:
        raise ValueError("请选择有效的动态可见范围")
    for key in ("directories", "sources"):
        values = result[key]
        if not isinstance(values, list) or len(values) > 30:
            raise ValueError(f"{key} 最多填写 30 项")
        cleaned = []
        for value in values:
            if not isinstance(value, str) or len(value) > 2048 or not value.strip():
                raise ValueError(f"{key} 中有无效条目")
            value = value.strip()
            if key == "directories":
                path = Path(value).expanduser()
                if not path.is_absolute():
                    raise ValueError("阅读目录必须是绝对路径")
                value = str(path.resolve())
            else:
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("来源必须是不含登录凭证的 HTTP(S) 网址")
            if value not in cleaned:
                cleaned.append(value)
        result[key] = cleaned
    return result


def active_hour(config: dict, hour: int) -> bool:
    start, end = config["activeStartHour"], config["activeEndHour"]
    return start == end or (start <= hour < end if start < end else hour >= start or hour < end)
