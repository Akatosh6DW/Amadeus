"""Split a finished QQ text reply into a small number of safe bubbles.

The splitter is deliberately conservative.  It prefers complete sentences and
clauses, never cuts inside URLs or backtick code spans, and lets an individual
unbreakable token exceed the soft character limit.  The character limit is a
delivery preference rather than a request to rewrite model output.
"""
from __future__ import annotations

import re


DEFAULT_REPLY_BUBBLE_CHARS = 60
MAX_REPLY_BUBBLE_CHARS = 240
MAX_REPLY_BUBBLES = 8
# Owner-control results are sent to a private owner chat.  Keep a smaller
# ceiling than ordinary drafts so an unexpectedly verbose desktop result does
# not become a burst of bubbles.  ``split_reply`` coalesces the tail and never
# discards text when this ceiling is reached.
MAX_OWNER_CONTROL_BUBBLES = 4

_SENTENCE_END = frozenset("。！？!?")
_CLAUSE_END = frozenset("；;")
_SOFT_BREAK = frozenset("，,、；;：:")
_CLOSERS = frozenset(['"', "'", '”', '’', '）', ')', '】', ']', '》', '〉', '〕', '」', '』'])
_URL = re.compile(r"https?://[^\s<>\"'，。！？；;、]+", re.IGNORECASE)
# Keep long numeric tokens intact as well. QQ IDs, dates and decimal values
# are common in replies and splitting one in the middle changes its meaning.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_ABBREVIATIONS = frozenset({"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "etc", "e.g", "i.e"})


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    ranges = [(match.start(), match.end()) for match in _URL.finditer(text)]
    ranges.extend((match.start(), match.end()) for match in _NUMBER.finditer(text))
    index = 0
    while index < len(text):
        if text.startswith("```", index):
            end = text.find("```", index + 3)
            end = len(text) if end < 0 else end + 3
            ranges.append((index, end))
            index = end
        elif text[index] == "`":
            end = text.find("`", index + 1)
            end = len(text) if end < 0 else end + 1
            ranges.append((index, end))
            index = end
        else:
            index += 1
    return ranges


def _protected(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def _period_boundary(text: str, index: int) -> bool:
    next_index = index + 1
    if next_index < len(text) and text[next_index] == ".":
        return False
    previous = text[index - 1] if index else ""
    following = text[next_index] if next_index < len(text) else ""
    if previous.isdigit() and following.isdigit():
        return False
    if following and not following.isspace() and following not in _CLOSERS:
        return False
    word = re.search(r"([A-Za-z]+)$", text[:index])
    return not word or word.group(1).lower() not in _ABBREVIATIONS


def _sentence_chunks(text: str, ranges: list[tuple[int, int]]) -> list[str]:
    chunks: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        if _protected(index, ranges):
            index += 1
            continue
        char = text[index]
        boundary = char in _SENTENCE_END or char in _CLAUSE_END or char in "\r\n"
        if char == ".":
            boundary = _period_boundary(text, index)
        if boundary:
            end = index + 1
            while end < len(text) and text[end] in _CLOSERS:
                end += 1
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start, index = end, end
            continue
        index += 1
    tail = text[start:].strip()
    if tail:
        chunks.append(tail)
    return chunks or [text.strip()]


def _safe_cut(text: str, position: int, ranges: list[tuple[int, int]]) -> bool:
    if position <= 0 or position >= len(text):
        return False
    return not _protected(position - 1, ranges) and not _protected(position, ranges)


def _split_long(text: str, max_chars: int, ranges: list[tuple[int, int]]) -> list[str]:
    parts: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break
        limit = min(max_chars, len(remaining) - 1)
        preferred: list[int] = []
        fallback: list[int] = []
        for position in range(1, len(remaining)):
            if not _safe_cut(remaining, position, ranges):
                continue
            before = remaining[position - 1]
            after = remaining[position]
            if before.isspace() or before in _SOFT_BREAK:
                (preferred if position <= limit else fallback).append(position)
            elif position <= limit and after.isspace():
                preferred.append(position)
        cut = preferred[-1] if preferred else (fallback[0] if fallback else len(remaining))
        piece = remaining[:cut].strip()
        if not piece:
            # This only occurs around leading whitespace; consume it without
            # manufacturing an empty message.
            remaining = remaining[cut + 1:]
            continue
        parts.append(piece)
        remaining = remaining[cut:].strip()
        ranges = _protected_ranges(remaining)
    return parts


def _merge_join(left: str, right: str) -> str:
    if not left or not right:
        return left + right
    return " " if left[-1].isascii() and right[0].isascii() else ""


def split_reply(text: str, max_chars: int = DEFAULT_REPLY_BUBBLE_CHARS,
                *, max_parts: int = MAX_REPLY_BUBBLES) -> list[str]:
    """Return at most ``max_parts`` complete text bubbles.

    ``max_chars=0`` explicitly disables splitting.  The limit is soft for a
    URL, code span, or another token for which no safe boundary exists.
    """
    if not isinstance(text, str):
        raise TypeError("QQ 回复必须为文字")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 0 <= max_chars <= MAX_REPLY_BUBBLE_CHARS:
        raise ValueError(f"QQ 气泡字数必须为 0–{MAX_REPLY_BUBBLE_CHARS} 的整数")
    if isinstance(max_parts, bool) or not isinstance(max_parts, int) or max_parts < 1:
        raise ValueError("QQ 气泡数量上限必须为正整数")
    text = text.strip()
    if not text or max_chars == 0:
        return [text] if text else []
    ranges = _protected_ranges(text)
    pieces: list[str] = []
    for sentence in _sentence_chunks(text, ranges):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
        else:
            pieces.extend(_split_long(sentence, max_chars, _protected_ranges(sentence)))
    if len(pieces) <= max_parts:
        return pieces
    # Keep the earliest complete bubbles intact and coalesce the tail.  This
    # preserves order and meaning while enforcing a hard upper bound.
    result = pieces[:max_parts - 1]
    tail = pieces[max_parts - 1]
    for piece in pieces[max_parts:]:
        tail += _merge_join(tail, piece) + piece
    result.append(tail)
    return result


__all__ = [
    "DEFAULT_REPLY_BUBBLE_CHARS",
    "MAX_REPLY_BUBBLE_CHARS",
    "MAX_REPLY_BUBBLES",
    "MAX_OWNER_CONTROL_BUBBLES",
    "split_reply",
]
