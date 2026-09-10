"""Legacy fallback for explicit proactive-outreach controls.

This module owns a deliberately narrow command surface: whether AMADEUS may
start a new conversation while the user is idle.  It does not classify general
dialogue, facts, tools, or topics.  A structured model directive is authoritative;
these narrow patterns are consulted only when an older model response omitted it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal


ProactiveDirective = Literal[
    "unchanged",
    "pause_until_next_user_message",
    "resume",
]


_CONTROL_VOCATIVE = re.compile(
    r"^(?:你|助手|阿万音|红莉栖|克里斯蒂娜|amadeus)|"
    r"(?:助手|阿万音|红莉栖|克里斯蒂娜|amadeus)$",
    re.I,
)
_PAUSE_DIRECT_COMMAND = re.compile(
    r"^(?:嗯|好|那个|麻烦|请)?(?:先|再)?(?:"
    r"稍等(?:一)?下|"
    r"等(?:一)?下|等一会儿?|等会儿?|等等|等着|"
    r"等我(?:一会儿?|一下|会儿?)?|"
    r"等(?:一?下|一?会儿?)我|"
    r"安静(?:一会儿?)?"
    r")(?:吧|啊|呀|哦)?$",
    re.I,
)
_PAUSE_OUTREACH_COMMAND = re.compile(
    r"(?:先|暂时|这会儿|一会儿|目前)?"
    r"(?:别|不要|不用|暂停)"
    r"(?:再|先)?(?:主动)?(?:说话|开口|找我|联系我|提醒我|催我|打扰我|发消息|叫我)",
    re.I,
)
_PAUSE_RETURN_LATER = re.compile(
    r"(?:我(?:先|要|去)?(?:忙|处理|弄|看|做).{0,18}(?:完|好).{0,8}再(?:回来)?(?:找|叫|联系)?你|"
    r"我(?:一会儿|等会儿|稍后|待会儿)(?:再)?(?:回来|找你|叫你|联系你|跟你说)|"
    r"等我.{0,18}(?:回来|弄完|忙完|处理完|做好)(?:再说|再聊)?)",
    re.I,
)
_RESUME_COMMAND = re.compile(
    r"^(?:嗯|好|现在)?(?:不用|不必|别)(?:再)?等(?:我)?了|"
    r"^(?:嗯|好|现在)?(?:可以|继续|恢复)(?:主动)?(?:说话|开口|找我|联系我|提醒我|聊(?:天)?)(?:了|吧)?$|"
    r"^(?:我)?回来(?:了|啦|咯)$",
    re.I,
)
_PAUSE_ENGLISH = re.compile(
    r"^(?:please\s+)?(?:wait(?:\s+(?:a\s+)?(?:moment|minute|bit))?|hold\s+on(?:\s+(?:a\s+)?(?:moment|minute|bit))?|be\s+quiet)(?:\s+amadeus)?$|"
    r"(?:do\s+not|don't)\s+(?:message|contact|remind|interrupt)\s+me",
    re.I,
)
_RESUME_ENGLISH = re.compile(
    r"^(?:you\s+can\s+)?(?:continue|talk|message\s+me|resume)(?:\s+now)?$|^i(?:'m|\s+am)\s+back$",
    re.I,
)


def _compact_control_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[\s，。！？!?、；;：:…~～,.]+", "", normalized)


def _without_control_vocative(value: str) -> str:
    """Remove a role address without making its position part of the command grammar."""

    compact = str(value or "")
    previous = None
    while compact and compact != previous:
        previous = compact
        compact = _CONTROL_VOCATIVE.sub("", compact)
    return compact


def infer_explicit_proactive_directive(text: str) -> ProactiveDirective:
    """Return a high-confidence outreach state command from the current turn.

    Ambiguous mentions of waiting (for example "我在等下载") deliberately
    remain ``unchanged``.  The model still controls conversational wording; this
    function only supplies a conservative legacy fallback when the structured
    model response omitted ``proactiveDirective``.
    """

    raw = unicodedata.normalize("NFKC", str(text or "")).strip().casefold()
    compact = _compact_control_text(raw)
    if not compact:
        return "unchanged"
    direct_command = _without_control_vocative(compact)
    if _RESUME_COMMAND.search(direct_command) or _RESUME_ENGLISH.search(raw):
        return "resume"
    if (
        _PAUSE_DIRECT_COMMAND.fullmatch(direct_command)
        or _PAUSE_OUTREACH_COMMAND.search(compact)
        or _PAUSE_RETURN_LATER.search(compact)
        or _PAUSE_ENGLISH.search(raw)
    ):
        return "pause_until_next_user_message"
    return "unchanged"
