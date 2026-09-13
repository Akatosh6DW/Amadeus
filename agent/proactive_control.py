"""Shared model-owned outreach contract; no local natural-language classifier."""

from __future__ import annotations

from typing import Literal


ProactiveDirective = Literal["unchanged", "pause", "pause_until_next_user_message", "resume"]

# The old spelling stays readable for older replies, without automatic resume.
PROACTIVE_DIRECTIVES = {"unchanged", "pause", "pause_until_next_user_message", "resume"}

PROACTIVE_DIRECTIVE_INSTRUCTION = (
    "proactiveDirective 由你结合当前用户整句意图、最近对话和 proactiveControl 决定，"
    "只能输出 pause、resume 或 unchanged。用户希望你安静、暂时别主动搭话时用 pause；"
    "根据上下文确认用户希望恢复主动互动或原先安静的情境已结束时用 resume；"
    "没有改变原状态的意思时用 unchanged。不要按单个关键词判断，也不要因为用户又发了一句话就自动恢复。"
    "unchanged 会保留已生效的静默；用户补充要求、换个问题或与你直接交谈不一定表示恢复主动搭话。"
    "paused 只限制你主动发起的闲聊、问候和追问，当前用户主动提问仍可正常回答。"
    "用户已安排的待办提醒优先于普通静默，提醒可以照常发出且不解除静默；"
    "需要取消或推迟某条提醒时，使用对应待办操作，不能以 pause 代替。"
    "轻声说话是 voiceStyle/voiceTone 的表达要求，不等同于停止主动互动。"
    "旧值 pause_until_next_user_message 与 pause 等价，不再表示下一句话自动解禁。"
)
