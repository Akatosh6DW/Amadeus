"""Independent opportunities, scaled by one user-owned activity level."""
from datetime import datetime, timedelta

from .config import active_hour


ACTIVITIES = {
    "explore": {"label": "阅读探索", "minutes": 120, "firstMinutes": 0},
    "chat": {"label": "找你聊天", "minutes": 180, "firstMinutes": 15},
    "reflect": {"label": "整理想法", "minutes": 360, "firstMinutes": 60},
    "share": {"label": "空间分享", "minutes": 720, "firstMinutes": 120},
}

FOCUS = {
    "explore": "这次主要顺着自己的兴趣阅读、浏览或继续探索。不需要同时完成聊天、整理和发动态。",
    "chat": "这次是单独留给与主人联系的时间。结合最近的交流、自己的兴趣和实际经历，考虑现在有没有想和主人聊的话。已有内容足够时就自然聊，不用先做完新一轮研究；没有想说的也可以安静结束，在 decision 留下这次的选择。话题和说法由你自己决定。",
    "reflect": "这次主要回顾共同经历和自己的想法：有什么值得接着想，什么已经聊完或可以放下。按需要更新笔记和 thoughts，不必为整理再开一轮搜索，也不用写成待办清单。",
    "share": "这次主要考虑有没有适合放进 QQ 空间的想法。结合实际读到的内容、共同经历与已有动态，自行决定分享或暂时不发；不需要重新做一整轮研究，也不必把私聊复述成动态。",
    "manual": "这是主人发起的一次试运行，按当前兴趣自由选择活动。",
}


def interval_minutes(config: dict, kind: str) -> int:
    return int(ACTIVITIES[kind]["minutes"] * 100 / config["activityFrequency"] + 0.5)


def configured_activity(config: dict, kind: str) -> bool:
    if kind == "explore":
        return bool(config["allowWeb"] or config["directories"] or config["allowQqRead"])
    if kind == "chat":
        return bool(config["allowQqChat"] and config["publishAccount"] and config["qqChatOwner"])
    if kind == "share":
        return config["publishMode"] != "off"
    return True


def next_active_time(when: datetime, config: dict) -> datetime:
    if active_hour(config, when.hour):
        return when
    start = when.replace(hour=config["activeStartHour"], minute=0, second=0, microsecond=0)
    return start if start > when else start + timedelta(days=1)
