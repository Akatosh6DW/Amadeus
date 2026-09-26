"""导航目标的无 I/O 形态规则；不把模型给出的任意网址当成授权。"""
from __future__ import annotations
import re
import unicodedata
import urllib.parse
from typing import Any, Mapping

_COMMON_COUNTRY_SECOND_LEVEL_SUFFIXES = frozenset({
    "ac", "co", "com", "edu", "gov", "net", "org",
})


def browser_url_site_identity(value: Any) -> str:
    """Return the registrable-looking host label without doing network I/O."""

    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").rstrip(".").casefold()
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or not host
    ):
        return ""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return ""
    identity_index = -2
    if (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in _COMMON_COUNTRY_SECOND_LEVEL_SUFFIXES
    ):
        identity_index = -3
    identity = labels[identity_index]
    return re.sub(r"[^a-z0-9]+", "", identity)


def semantic_task_mentions_site_identity(text: str, site_identity: str) -> bool:
    """Match a host identity in an already-resolved task without parsing prose.

    The semantic resolver may return a full sentence such as ``用户确认访问
    Five Guys 官网``. Re-running the browser command regex over that sentence
    recreates the same competing-parser problem. Instead, compare the host to
    short adjacent ASCII name tokens from the resolved task.
    """

    identity = str(site_identity or "").casefold()
    if not identity:
        return False
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    tokens: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isascii() and character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    for start in range(len(tokens)):
        combined = ""
        for token in tokens[start:start + 4]:
            combined += token
            if combined == identity:
                return True
            if len(combined) >= len(identity):
                break
    return False


def browser_explicit_search_query(user_message: str) -> str:
    """Extract a browser-visible search only when the user asked for one."""

    value = re.sub(r"\s+", " ", str(user_message or "")).strip()
    match = re.search(
        r"(?:用|在)?(?:默认)?浏览器(?:里|中)?"
        r"(?:搜索|查找|搜一下|搜)\s*(.+)$",
        value,
        re.I,
    )
    if not match:
        return ""
    return match.group(1).strip(" ，。！？!?")[:500]


def browser_references_previous_source(user_message: str) -> bool:
    value = re.sub(r"\s+", " ", str(user_message or "")).strip()
    return bool(re.search(
        r"\bS\d+\b|"
        r"对应的?|刚才的?|刚刚的?|上面的?|前面的?|你(?:刚才|刚刚|上面|前面)?(?:说|提|推荐|找到|查到)的?|"
        r"那个|那篇|那条|这个|这篇|这条|其中(?:的)?|第[一二三四五六七八九十\d]+(?:个|篇|条)?|"
        r"(?:打开|访问|进入)(?:一下)?(?:这个|那个|刚才的?|刚刚的?)?"
        r"(?:链接|页面|网页)(?:一下)?(?:吧|好吗|可以吗)?[。！？!?]*$",
        value,
        re.I,
    ))


def browser_request_resolution_rank(request: Mapping[str, Any]) -> int:
    """Rank a browser request by how close it is to an executable destination."""

    args = request.get("args") if isinstance(request.get("args"), Mapping) else {}
    if str(args.get("url") or "").strip():
        return 3
    if str(args.get("query") or "").strip() and str(
        args.get("browserIntent") or ""
    ).strip().lower() == "search":
        return 2
    if str(args.get("resolveQuery") or "").strip() or args.get("deferToResearch"):
        return 1
    return 0
