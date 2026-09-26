"""当前回复的连接词频度提示；保留原文风约束，不触发模型请求。"""
from __future__ import annotations
from typing import Any

TRANSITION_MARKERS = (
    "不过", "但是", "然而", "可是", "只是", "倒是", "反倒", "话虽如此", "虽说", "虽然", "但至少", "而是",
)


def transition_style_prompt(messages: list[dict[str, Any]]) -> str:
    assistant_text = "\n".join(
        str(item.get("content") or "")
        for item in messages[-8:]
        if isinstance(item, dict) and item.get("role") == "assistant"
    )
    recent_hits = sum(assistant_text.count(marker) for marker in TRANSITION_MARKERS)
    if recent_hits >= 2:
        instruction = "最近几轮转折结构偏密。本轮优先直接陈述；只有红莉栖式纠正、嘴硬掩饰或必要逻辑对照时才保留一个。"
    elif recent_hits == 1:
        instruction = "最近回复已经用过一次转折。本轮避免重复同一个连接词；语气确实需要时仍可自然使用一个。"
    else:
        instruction = "本轮可以自然使用一次转折，不要连续叠加，也不要把每个情绪反应都写成前后对照。"
    return "\n".join([
        "[本轮中文文风控制]",
        instruction,
        "用户自然更换话题时通常直接接住新内容，不要把‘换题很突然／跨度很大’当作固定吐槽素材。",
        "只有话题切换本身对理解当前问题确实有意义，或用户主动谈到这种切换时，才偶尔自然提一句；禁止用同一种话题跳跃评语反复充当开场。",
        "最大程度保持牧濑红莉栖的节奏：短促纠正、停顿、自我收束、科学类比、把关心藏在结论后面。转折只是其中一种手段。",
        "需要补充时可以直接另起一句；需要纠正时先说结论，再给原因。避免通用客服腔。",
        "自然示例：‘现在十二点。先去吃饭，实验数据不会因为你饿着就更可靠。’",
        "避免示例：‘虽然已经十二点了，不过实验还可以继续，但是你还是要吃饭。’",
    ])
