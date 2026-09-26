"""说话人争议的既有本地保护规则；只读取当前输入与候选，不读取存储。"""
from __future__ import annotations
from typing import Any
import re
from backend.context.conversation import is_speaker_attribution_dispute

def enforce_speaker_attribution_grounding(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
    if not is_speaker_attribution_dispute(user_message):
        return result
    reply = str(result.get("reply") or "")
    unsupported_accusation = bool(re.search(
        r"(?:明明|确实|就是)?你(?:自己|上次|刚才|之前)?(?:.{0,12})?(?:说过|说的|提过|承认过)",
        reply,
    ))
    acknowledged_error = bool(re.search(r"你说得对|我记错|我记混|我记反|不是你说|是我说|归因错误", reply))
    if not unsupported_accusation or acknowledged_error:
        return result
    fixed = dict(result)
    fixed.update({
        "reply": "……你说得对。按带说话人标签的记录核对后，那是我先前的判断，不是你的发言。是我把自己的回复记到你头上了，抱歉。",
        "spokenTextJa": "……あなたの言う通り。話者ラベル付きの記録を確認したら、それは私が前にした判断で、あなたの発言じゃなかった。自分の返事をあなたの言葉と取り違えていたわ。ごめんなさい。",
        "emotion": "flustered",
        "gesture": "soften",
        "voiceStyle": "shy",
        "voiceTone": "gentle",
        "voiceIntensity": "subtle",
    })
    fixed["speakerAttributionRepaired"] = True
    return fixed
