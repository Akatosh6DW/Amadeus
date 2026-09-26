"""已生成回复的本地语气修正与空回复提示；不调用模型、不读写用户状态。"""
from __future__ import annotations
from typing import Any
import re
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


def enforce_direct_social_request(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
    """Keep persona banter from replacing a direct request for recognition."""

    if not re.search(r"(?:快|请|好好)?夸(?:夸)?我|表扬我|称赞我", str(user_message or "")):
        return result
    reply = str(result.get("reply") or "").strip()
    if re.search(r"做得|厉害|不错|漂亮|值得|恭喜|了不起|优秀|真棒|有两下子", reply):
        return result
    variants = (
        "行，这次确实做得漂亮。",
        "能把困扰这么久的问题解决掉，值得表扬。",
        "好吧，这回我承认，你做得很不错。",
    )
    index = sum(ord(character) for character in str(user_message or "")) % len(variants)
    repaired = dict(result)
    repaired["reply"] = variants[index] + reply
    if str(repaired.get("emotion") or "") not in {"excited", "proud", "amused", "affectionate"}:
        repaired["emotion"] = "proud"
    repaired["gesture"] = "perk"
    repaired["directSocialRequestRepaired"] = True
    return repaired


def enforce_explicit_supportive_tone(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
    """Honor an explicit request for gentle reassurance when the model labels it neutral."""

    text = str(user_message or "")
    supportive = bool(re.search(r"已经做得够多|今天就到这里|该休息|休息一下|别太累|安慰(?:我)?", text))
    gentle_request = bool(re.search(r"轻声|温柔(?:地)?|柔和(?:地)?|慢慢告诉", text))
    if not supportive or not gentle_request:
        return result
    if str(result.get("emotion") or "") not in {"neutral", "thinking", "focused"}:
        return result
    repaired = dict(result)
    repaired["emotion"] = "concerned"
    repaired["gesture"] = "soften"
    repaired["voiceStyle"] = "soft"
    repaired["voiceTone"] = "gentle"
    repaired["voiceIntensity"] = "subtle"
    repaired["explicitSupportiveToneRepaired"] = True
    return repaired


def enforce_distress_support_tone(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
    """Keep explicit self-doubt in a supportive expression instead of annoyance."""

    text = re.sub(r"\s+", "", str(user_message or ""))
    self_doubt = bool(re.search(
        r"(?:怀疑|觉得|是不是).{0,8}(?:自己|我).{0,10}(?:根本)?(?:不适合|不行|没用|做不好)|"
        r"(?:我|自己).{0,8}(?:根本)?(?:不适合|不配|没能力).{0,8}(?:研究|工作|继续)|"
        r"一次失败.{0,10}(?:否定|怀疑).{0,8}(?:自己|我)",
        text,
    ))
    if not self_doubt or str(result.get("emotion") or "") not in {
        "annoyed", "neutral", "teasing", "amused", "skeptical",
    }:
        return result
    repaired = dict(result)
    repaired["emotion"] = "concerned"
    repaired["gesture"] = "soften"
    repaired["voiceStyle"] = "soft"
    if str(repaired.get("voiceTone") or "") not in {"comforting", "gentle", "warm"}:
        repaired["voiceTone"] = "comforting"
    repaired["voiceIntensity"] = "subtle"
    repaired["distressSupportToneRepaired"] = True
    return repaired


def enforce_explicit_laugh_request(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
    """Make the dedicated laughing expression reliable for an explicit laugh request."""

    if not re.search(r"放开(?:地)?大笑|大笑一次|笑出声|哈哈大笑", str(user_message or "")):
        return result
    repaired = dict(result)
    reply = str(repaired.get("reply") or "").strip()
    if re.search(r"(?:要我|让我).{0,12}(?:大笑|放声(?:大)?笑).{0,18}(?:不像|不行|别想)", reply):
        reply = "哈哈……！好吧，这次算你赢，确实有点离谱。"
    elif not re.search(r"哈哈|呵呵|笑出声", reply):
        reply = "哈哈……！" + reply
    repaired["reply"] = reply
    repaired["emotion"] = "laughing"
    repaired["gesture"] = "laugh"
    repaired["voiceStyle"] = "energetic"
    repaired["voiceTone"] = "laughing"
    repaired["voiceIntensity"] = "strong"
    repaired["explicitLaughRequestRepaired"] = True
    return repaired


def enforce_explicit_choice_grounding(user_message: str, result: dict[str, Any]) -> dict[str, Any]:
    """Retain user-provided A/B conditions when the reply omits both labels."""

    text = str(user_message or "").strip()
    if not (re.search(r"方案\s*A", text, re.I) and re.search(r"方案\s*B", text, re.I)):
        return result
    reply = str(result.get("reply") or "").strip()
    if re.search(r"方案\s*[AB]|(?:^|[^A-Za-z])[AB](?:[^A-Za-z]|$)", reply, re.I):
        return result
    clauses = [item.strip(" ，。") for item in re.split(r"[；;\n]", text) if item.strip()]
    grounded = [item for item in clauses if re.search(r"方案\s*[AB]", item, re.I)]
    if len(grounded) < 2:
        return result
    repaired = dict(result)
    repaired["reply"] = "明白：" + "；".join(grounded[:2]) + "。" + reply
    repaired["explicitChoiceGroundingRepaired"] = True
    return repaired
