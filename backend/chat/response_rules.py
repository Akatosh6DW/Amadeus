"""已生成回复的本地语气修正与空回复提示；不调用模型、不读写用户状态。"""
from __future__ import annotations
from typing import Any
from agent.fingerprints import runtime_digest

def local_empty_reply(text: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    variants = (
        (
            "我好像没太理解你具体想说什么，可以再解释一下吗？",
            "ごめん、少し意味をつかみきれなかったわ。もう少し説明してくれる？",
        ),
        (
            "我可能没理解对。你能换个说法再跟我讲一下吗？",
            "私の受け取り方が違ったかもしれない。別の言い方でもう一度教えてくれる？",
        ),
        (
            "我这次好像没听懂你的重点。可以再多告诉我一点吗？",
            "今回は大事なところをうまくつかめなかったみたい。もう少し教えてくれる？",
        ),
    )
    digest = runtime_digest(text)
    reply, spoken = variants[digest[0] % len(variants)]
    return {
        "reply": reply,
        "spokenTextJa": spoken,
        "emotion": "concerned",
        "gesture": "soften",
        "voiceStyle": "soft",
        "voiceTone": "gentle",
        "speechSpeed": 1.0,
        "memoryUpdates": [],
        "source": "local-empty-recovery",
        "emptyReply": False,
        "repairedEmptyReply": True,
        "emptyReplyRecoveryReason": "upstream_returned_no_usable_reply",
        "requiresUserResponse": True,
        "expectedResponseType": "free_text",
    }
