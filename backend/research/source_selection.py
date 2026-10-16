"""Deterministic selection of web sources for a bounded answer context.

The selector is deliberately independent of retrieval and model code.  It ranks
already collected source records by readable evidence, task relevance, source
authority, and domain diversity without changing source IDs or evidence policy.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from tools.web_source_registry import known_official_web_domain


_READABLE_FETCH_STATUSES = frozenset({"success", "partial", "ok", "fetched"})
_TIER_SCORE = {"A": 3, "B": 2, "C": 1, "D": 0}
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _text_tokens(value: Any) -> set[str]:
    """Return bounded language-neutral tokens for light task relevance."""

    text = str(value or "").casefold()
    tokens = {match.group(0) for match in _LATIN_TOKEN_RE.finditer(text)}
    for run_match in _CJK_RUN_RE.finditer(text):
        run = "".join(_CJK_RE.findall(run_match.group(0)))
        if len(run) <= 2:
            if run:
                tokens.add(run)
            continue
        tokens.update(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


def _natural_id(value: Any, original_index: int) -> tuple[Any, ...]:
    """Sort S2 before S10 while retaining stable order for nonstandard IDs."""

    text = str(value or "").strip().casefold()
    match = re.fullmatch(r"([a-z_-]*?)(\d+)", text)
    if match:
        return (0, match.group(1), int(match.group(2)), text, original_index)
    return (1, text, original_index)


def _domain(source: Mapping[str, Any]) -> str:
    value = str(source.get("domain") or "").strip().casefold().removeprefix("www.")
    if value:
        return value
    url = str(source.get("url") or source.get("canonicalUrl") or "").strip()
    try:
        return (urllib.parse.urlsplit(url).hostname or "").casefold().removeprefix("www.")
    except (TypeError, ValueError):
        return ""


def _body_text(source: Mapping[str, Any]) -> str:
    """Use the projected body too; query chunks may intentionally blank ``body``."""

    parts = [
        str(source.get("body") or ""),
        str(source.get("bodyExcerpt") or ""),
    ]
    chunks = source.get("evidenceChunks")
    if isinstance(chunks, Sequence) and not isinstance(chunks, (str, bytes)):
        parts.extend(
            str(item.get("text") or "")
            for item in chunks
            if isinstance(item, Mapping)
        )
    return "\n".join(part[:4_000] for part in parts if part.strip())[:12_000]


def source_has_readable_body(source: Mapping[str, Any]) -> bool:
    """Whether a source carries fetched, substantive body evidence."""

    status = str(source.get("fetchStatus") or "").strip().casefold()
    body_length = len(_body_text(source).strip())
    return (
        status in _READABLE_FETCH_STATUSES
        or (not status and body_length >= 120)
    ) and body_length >= 120


def _authority_score(source: Mapping[str, Any]) -> int:
    tier = str(source.get("sourceTier") or "").strip().upper()
    score = _TIER_SCORE.get(tier, 0)
    if not tier and known_official_web_domain(_domain(source)):
        return _TIER_SCORE["A"]
    return score


def _relevance_score(source: Mapping[str, Any], query: str) -> int:
    query_tokens = _text_tokens(query)
    if not query_tokens:
        return 0
    title_tokens = _text_tokens(source.get("title"))
    query_field_tokens = _text_tokens(
        " ".join((
            str(source.get("researchQuery") or ""),
            str(source.get("researchPurpose") or ""),
        ))
    )
    discovery_tokens = _text_tokens(source.get("snippet") or source.get("excerpt"))
    body_tokens = _text_tokens(_body_text(source))
    try:
        provider_score = int(float(source.get("relevanceScore") or 0))
    except (TypeError, ValueError, OverflowError):
        provider_score = 0
    return (
        7 * len(query_tokens & title_tokens)
        + 4 * len(query_tokens & query_field_tokens)
        + 3 * len(query_tokens & body_tokens)
        + len(query_tokens & discovery_tokens)
        + min(20, max(0, provider_score))
    )


def _quality_key(source: Mapping[str, Any], query: str) -> tuple[int, int, int, int]:
    readable = source_has_readable_body(source)
    fetch_status = str(source.get("fetchStatus") or "").strip().casefold()
    # Readable正文 is the first boundary.  Authority then gets a real
    # precedence over relevance so a readable official page is not displaced
    # by a more keyword-heavy secondary page.  Relevance still orders sources
    # within the same authority tier.
    fetch_rank = 2 if readable else (1 if fetch_status in _READABLE_FETCH_STATUSES else 0)
    return (
        -int(readable),
        -_authority_score(source),
        -_relevance_score(source, query),
        -fetch_rank,
    )


def _rank_key(source: Mapping[str, Any], query: str, original_index: int) -> tuple[Any, ...]:
    return (*_quality_key(source, query), _natural_id(source.get("id"), original_index))


def rank_web_sources(
    sources: Sequence[Mapping[str, Any]],
    query: str = "",
) -> list[dict[str, Any]]:
    """Return sources in deterministic evidence-first order without truncating."""

    indexed = [
        (index, dict(source))
        for index, source in enumerate(sources)
        if isinstance(source, Mapping)
    ]
    indexed.sort(key=lambda item: _rank_key(item[1], query, item[0]))
    return [source for _index, source in indexed]


def select_web_sources(
    sources: Sequence[Mapping[str, Any]],
    query: str = "",
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Select a bounded, evidence-first source window with soft diversity.

    The helper only changes presentation order and window membership.  It does
    not infer answer eligibility and therefore preserves ``answerSourceIds``
    semantics in callers that filter that field before selection.
    """

    cap = max(0, int(limit))
    ranked = rank_web_sources(sources, query)
    if cap == 0 or not ranked:
        return []

    selected: list[dict[str, Any]] = []
    selected_domains: set[str] = set()
    remaining = list(ranked)
    # Preserve the rank window first.  Only exact quality ties may use a new
    # domain as a deterministic tie-breaker; a better official or relevant
    # page always stays ahead of diversity.
    while remaining and len(selected) < cap:
        best_quality = _quality_key(remaining[0], query)
        tie_indexes = [
            index
            for index, source in enumerate(remaining)
            if _quality_key(source, query) == best_quality
        ]
        candidate_index = tie_indexes[0]
        for index in tie_indexes:
            domain = _domain(remaining[index])
            if domain and domain not in selected_domains:
                candidate_index = index
                break
        candidate = remaining.pop(candidate_index)
        selected.append(candidate)
        domain = _domain(candidate)
        if domain:
            selected_domains.add(domain)
    return selected


__all__ = ["rank_web_sources", "select_web_sources", "source_has_readable_body"]
