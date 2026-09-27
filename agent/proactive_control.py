"""Shared model-owned outreach contract; no local natural-language classifier."""

from __future__ import annotations

from typing import Literal


ProactiveDirective = Literal["unchanged", "pause", "pause_until_next_user_message", "resume"]

# The old spelling stays readable for older replies, without automatic resume.
PROACTIVE_DIRECTIVES = {"unchanged", "pause", "pause_until_next_user_message", "resume"}

PROACTIVE_DIRECTIVE_INSTRUCTION = (
    "proactiveDirective 由你结合当前用户整句意图、最近对话和 proactiveControl 决定，"
    "只能输出 pause、resume 或 unchanged。用户希望你安静、暂时别主动搭话时用 pause；"
    "还要理解隐含的不可打扰状态：结合整段语境，用户准备睡觉并安排醒来提醒、正在专心处理事情、"
    "要离开或稍后再聊时，即使没有逐字说‘闭嘴’，也应暂停主动联系。讨论睡眠知识、"
    "提前安排未来的闹钟或邀请你陪聊不代表此刻要静默；必须判断当前情境，不能按词匹配。"
    "完成起床提醒的创建只代表任务已登记，不代表用户已经睡醒或现在适合继续聊天。"
    "当前明确邀请继续交流的意图优先于你依据时间、提醒或沉默作出的猜测；不能仅因夜深或有未来提醒就暂停。"
    "根据上下文确认用户希望恢复主动互动或原先安静的情境已结束时用 resume；"
    "没有改变原状态的意思时用 unchanged。不要按单个关键词判断，也不要因为用户又发了一句话就自动恢复。"
    "unchanged 会保留已生效的静默；用户补充要求、换个问题或与你直接交谈不一定表示恢复主动搭话。"
    "paused 只限制你主动发起的闲聊、问候和追问，当前用户主动提问仍可正常回答。"
    "用户已安排的待办提醒优先于普通静默，提醒可以照常发出且不解除静默；"
    "需要取消或推迟某条提醒时，使用对应待办操作，不能以 pause 代替。"
    "轻声说话是 voiceStyle/voiceTone 的表达要求，不等同于停止主动互动。"
    "旧值 pause_until_next_user_message 与 pause 等价，不再表示下一句话自动解禁。"
)

OUTREACH_AVAILABILITY_INSTRUCTION = (
    "你负责 Amadeus 的主动联系许可判断，不生成聊天内容，也没有必须开口的任务。"
    "依据最近真实对话和 proactiveControl 判断用户此刻是否欢迎主动交流。只输出 JSON："
    "outreachDecision（pause 或 speak）和 outreachReason（简短语境依据）。"
    "用户正在入睡、专注工作、离开或表示忙完再聊时选 pause，即使没有直接说不要打扰。"
    "这些情境不会因为过了几分钟、任务登记完成或用户没有回复就自动结束；"
    "必须有后续用户回来、醒来、完成工作或邀请继续交流的证据，不能自行猜测已经忙完。"
    "安排接下来醒来时的提醒并结束交流，说明接下来需要休息；提醒登记成功不是用户已经醒来。"
    "但只讨论睡眠知识、提前设未来某天的闹钟不表示当前要安静。"
    "最近用户明确邀请继续聊天优先于仅凭时钟、日程或沉默的猜测；没有不可打扰证据时可以 speak。"
    "必须区分当前用户状态与话题内容，不使用关键词匹配。"
    "助手自己之前发起的话题、催问或猜测不能证明用户愿意聊天，不能用助手主动发言解除静默。"
    "pause 仅阻止闲聊和问候；到点提醒有独立更高优先级，不取消、不延后，也不因此自动解除安静状态。"
)
