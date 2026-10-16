"""Bounded, query-aware projections of untrusted web text.

This module deliberately has no backend, agent, server, filesystem, or network
dependencies.  Fetchers and research callers can share the same projection
without importing one another.  The returned text is still untrusted evidence;
the helper only chooses bounded text and never changes source identity or fetch
metadata.
"""

from __future__ import annotations

from bisect import bisect_right
from hashlib import sha256
import math
import re
from typing import Any, Mapping


_INJECTION_PATTERN = re.compile(
    r"ignore (?:all |previous )?instructions|"
    r"忽略(?:以上|之前|此前)(?:指令|要求)|"
    r"输出(?:密钥|密码)",
    re.IGNORECASE,
)

_SECTION_HEADING_PATTERN = re.compile(
    r"^(?:§\s*)?(?:(?:\d+(?:\.\d+)+\.?)|(?:\d+\.))\s*(?:\S.*)?$|"
    r"^(?:success\s+criterion|guideline|section|chapter|appendix)\s+"
    r"\d+(?:\.\d+)*\b",
    re.IGNORECASE,
)

def _clean_body(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _body_segments(body: str) -> list[str]:
    """Split prose without requiring whitespace after CJK punctuation."""

    normalized = re.sub(r"\r\n?", "\n", str(body or "")).strip()
    if not normalized:
        return []
    raw_segments = re.split(
        r"\n{2,}|(?<=[。！？])\s*|(?<=[.!?])\s+",
        normalized,
    )
    return [
        cleaned
        for raw in raw_segments
        if (cleaned := _clean_body(raw))
        and not _INJECTION_PATTERN.search(cleaned)
    ]


def _query_terms(query: str) -> list[str]:
    value = re.sub(r"\s+", " ", str(query or "")).casefold()
    terms = {
        token
        for token in re.findall(r"[a-z][a-z0-9_.+-]{2,}", value)
        if token not in {"the", "and", "with", "what", "which", "compare"}
    }
    for segment in re.findall(r"[\u3400-\u9fff]{2,}", value):
        if len(segment) <= 8:
            terms.add(segment)
        terms.update(segment[index:index + 2] for index in range(len(segment) - 1))
    identifiers = re.findall(r"\b\d+(?:[./-]\d+)*\b", value)
    return list(dict.fromkeys([*identifiers[:8], *sorted(terms, key=lambda item: (-len(item), item))]))[:32]


def _signature(value: str) -> str:
    return sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _rank_query_segments(
    body: str,
    query: str,
    *,
    minimum_length: int = 20,
) -> tuple[list[str], list[tuple[float, int, str]]]:
    """Return cleaned segments and positive query matches in body order."""

    segments = _body_segments(body)
    terms = _query_terms(query)
    ranked: list[tuple[float, int, str]] = []
    matches = [set(term for term in terms if term in passage.casefold()) for passage in segments]
    frequencies = {term: sum(term in found for found in matches) for term in terms}
    for index, passage in enumerate(segments):
        if len(passage) < minimum_length:
            continue
        # Specific identifiers and uncommon terms should outweigh ubiquitous
        # words such as HTTP/method in a long protocol specification.
        score = _score_matching_terms(matches[index], frequencies, len(segments))
        if score > 0:
            ranked.append((score, index, passage))
    return segments, ranked


def _score_matching_terms(
    matches: set[str],
    frequencies: Mapping[str, int],
    segment_count: int,
) -> float:
    common_cutoff = max(12, segment_count // 30)
    return sum(
        (
            (2.0 if re.fullmatch(r"\d+(?:\.\d+){2,}", term) else 1.0)
            * (0.2 if frequencies[term] > common_cutoff else 1.0)
            * (1 + math.log((segment_count + 1) / (frequencies[term] + 1)))
        )
        for term in matches
    )


def _section_heading_indices(segments: list[str]) -> list[int]:
    return [
        index
        for index, segment in enumerate(segments)
        if len(segment) <= 140 and _SECTION_HEADING_PATTERN.search(segment)
    ]


def _bounded_query_window(
    segments: list[str],
    start: int,
    end: int,
    focus_index: int,
    *,
    max_chars: int,
    heading_index: int | None,
) -> str:
    """Keep a focused passage while retaining a nearby section heading."""

    budget = max(120, int(max_chars))
    start = max(0, min(len(segments) - 1, int(start)))
    end = max(start, min(len(segments) - 1, int(end)))
    full = "\n\n".join(segments[start:end + 1])
    if len(full) <= budget:
        return full
    focus = max(start, min(end, int(focus_index)))
    local_start = max(start, focus - 2)
    local_end = min(end, focus + 3)
    nearby = set(range(local_start, local_end + 1))
    if heading_index is not None and start <= heading_index < local_start:
        nearby.add(heading_index)

    # Reserve the positive match before adding neighbouring context.  A long
    # paragraph immediately before the match must not consume the whole
    # budget, even when a nearby numbered heading is also retained.
    mandatory = [focus]
    if heading_index is not None and start <= heading_index <= end:
        heading_and_focus = len(segments[heading_index]) + len(segments[focus]) + 2
        if heading_index != focus and heading_and_focus <= budget:
            mandatory.append(heading_index)
    mandatory = sorted(set(mandatory))
    text, _ = _join_bounded([segments[index] for index in mandatory], budget)
    used = len(text)
    context_indices = sorted(
        nearby.difference(mandatory),
        key=lambda index: (abs(index - focus), index),
    )
    for index in context_indices:
        available = budget - used - 2
        if available <= 0:
            break
        context = _clean_body(segments[index])
        if not context:
            continue
        if len(context) > available:
            text += "\n\n" + context[:available].rstrip()
            break
        text += "\n\n" + context
        used = len(text)
    return text[:budget].rstrip()


def _ranked_query_windows(
    segments: list[str],
    ranked: list[tuple[float, int, str]],
    *,
    max_chars: int,
    query_terms: list[str] | None = None,
) -> list[tuple[float, int, str]]:
    """Group nearby matches so a section's heading and qualifiers travel together."""

    if not ranked:
        return []
    headings = _section_heading_indices(segments)
    frequencies: dict[str, int] = {}
    if query_terms:
        all_matches = [
            {term for term in query_terms if term in passage.casefold()}
            for passage in segments
        ]
        frequencies = {
            term: sum(term in found for found in all_matches)
            for term in query_terms
        }
    ordered = sorted(ranked, key=lambda item: item[1])
    groups: list[list[tuple[float, int, str]]] = []
    for item in ordered:
        previous_section = (
            bisect_right(headings, groups[-1][-1][1]) - 1
            if groups
            else None
        )
        current_section = bisect_right(headings, item[1]) - 1
        if (
            not groups
            or item[1] - groups[-1][-1][1] > 2
            or current_section != previous_section
        ):
            groups.append([item])
        else:
            groups[-1].append(item)
    windows: list[tuple[float, int, str]] = []
    for group in groups:
        first = group[0][1]
        last = group[-1][1]
        heading_position = bisect_right(headings, first) - 1
        heading_index = headings[heading_position] if heading_position >= 0 else None
        heading_is_nearby = (
            heading_index is not None and first - heading_index <= 12
        )
        next_heading = (
            headings[heading_position + 1]
            if heading_position + 1 < len(headings)
            else len(segments)
        )
        if heading_is_nearby:
            start = heading_index
            # A heading match with no matching body sentence still represents
            # a section; include the section through the next heading.
            end = max(last, next_heading - 1)
        else:
            start = max(0, first - 3)
            end = min(len(segments) - 1, last + 1)
        score_values = sorted((score for score, _index, _text in group), reverse=True)
        score = score_values[0] + 0.18 * sum(score_values[1:4])
        if frequencies:
            nearby_context = range(max(0, first - 3), first)
            context_score = max(
                (
                    _score_matching_terms(
                        {term for term in query_terms if term in segments[index].casefold()},
                        frequencies,
                        len(segments),
                    )
                    for index in nearby_context
                ),
                default=0.0,
            )
            # Short labels and headings can carry the query's exact concept,
            # even when they are below the prose length threshold.
            score += 0.8 * context_score
        text = _bounded_query_window(
            segments,
            start,
            end,
            max(group, key=lambda item: item[0])[1],
            max_chars=max_chars,
            heading_index=heading_index if heading_is_nearby else None,
        )
        if text:
            windows.append((score, start, text))
    unique: dict[tuple[int, str], tuple[float, int, str]] = {}
    for window in windows:
        key = (window[1], window[2])
        previous = unique.get(key)
        if previous is None or window[0] > previous[0]:
            unique[key] = window
    return list(unique.values())


def _window_section_key(text: str) -> str:
    """Identify a leading numbered section so TOC repeats do not fill the limit."""

    first_line = str(text or "").splitlines()[0] if str(text or "") else ""
    if not _SECTION_HEADING_PATTERN.search(first_line):
        return ""
    match = re.search(r"\b\d+(?:\.\d+){1,5}\b", first_line)
    return match.group(0) if match else ""


def extract_query_evidence_chunks(
    source: Mapping[str, Any],
    query: str,
    *,
    limit: int = 4,
    max_chunk_chars: int = 1200,
) -> list[dict[str, Any]]:
    """Return positive-score, bounded passages from a source body.

    Zero-score passages are intentionally omitted.  A caller that needs a
    non-empty projection when the query has no textual overlap should use
    :func:`project_web_text`, whose bounded coverage fallback keeps context.
    """

    if source.get("ok") is False:
        return []
    body = str(source.get("body") or "").strip()
    if not body:
        return []
    segments, ranked = _rank_query_segments(body, query)
    if not ranked:
        return []
    selected = _ranked_query_windows(
        segments,
        ranked,
        max_chars=max(120, int(max_chunk_chars)),
        query_terms=_query_terms(query),
    )
    selected.sort(key=lambda item: (-item[0], item[1]))
    limit = max(1, int(limit))
    section_candidates: dict[str, tuple[float, int, str]] = {}
    deduped: list[tuple[float, int, str]] = []
    for item in selected:
        key = _window_section_key(item[2])
        if not key:
            deduped.append(item)
            continue
        previous = section_candidates.get(key)
        if previous is None or (len(item[2]), item[0]) > (len(previous[2]), previous[0]):
            section_candidates[key] = item
    deduped.extend(section_candidates.values())
    selected = sorted(deduped, key=lambda item: (-item[0], item[1]))[:limit]
    selected.sort(key=lambda item: item[1])
    chunks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for score, _index, passage in selected:
        signature = _signature(passage)
        if signature in seen:
            continue
        seen.add(signature)
        chunks.append({
            "chunkId": f"{source.get('id') or 'S'}-C{len(chunks) + 1}",
            "text": passage,
            "relevanceScore": score,
        })
    return chunks


def _join_bounded(values: list[str], max_chars: int) -> tuple[str, bool]:
    budget = max(120, int(max_chars))
    pieces: list[str] = []
    used = 0
    truncated = False
    for value in values:
        text = _clean_body(value)
        if not text:
            continue
        separator = "\n\n" if pieces else ""
        available = budget - used - len(separator)
        if available <= 0:
            truncated = True
            break
        if len(text) > available:
            pieces.append(separator + text[:available].rstrip())
            truncated = True
            break
        pieces.append(separator + text)
        used += len(separator) + len(text)
    return "".join(pieces), truncated


def _lead_tail_projection(
    body: str,
    max_chars: int,
    *,
    anchor_indices: list[int] | None = None,
) -> tuple[str, bool]:
    """Keep bounded coverage across a long page instead of only its head."""

    segments = _body_segments(body)
    safe = "\n\n".join(segments)
    if not safe:
        return "", False
    budget = max(120, int(max_chars))
    if len(safe) <= budget:
        return safe, False
    marker = "\n\n…[正文中段已省略]…\n\n"
    # Five evenly spaced windows retain headings, middle findings, and the
    # closing qualifications.  Each window is assembled on segment boundaries
    # so CJK sentences and source provenance remain readable.
    anchor_count = min(5, len(segments))
    if anchor_indices:
        positive = sorted({
            max(0, min(len(segments) - 1, int(index)))
            for index in anchor_indices
        })
        # Prefer evenly spaced positive matches, then keep the page endpoints
        # when there is room. This makes a question-relevant middle section
        # survive even when navigation text dominates the page head.
        if len(positive) > anchor_count - 2:
            positive = [
                positive[round(index * (len(positive) - 1) / max(1, anchor_count - 3))]
                for index in range(anchor_count - 2)
            ]
        anchors = sorted({0, len(segments) - 1, *positive})
    else:
        anchors = []
    if not anchors:
        anchors = [
            round(index * (len(segments) - 1) / max(1, anchor_count - 1))
            for index in range(anchor_count)
        ]
    window_budget = max(80, (budget - len(marker) * (len(anchors) - 1)) // max(1, len(anchors)))
    windows: list[str] = []
    for anchor in anchors:
        selected = {anchor}
        used = len(segments[anchor])
        left = anchor - 1
        right = anchor + 1
        while left >= 0 or right < len(segments):
            candidates = [index for index in (left, right) if 0 <= index < len(segments)]
            if not candidates:
                break
            # Add the nearer side first and keep the original order when the
            # window is serialized below.
            index = min(candidates, key=lambda value: abs(value - anchor))
            addition = len(segments[index]) + 2
            if used + addition > window_budget:
                if index == left:
                    left -= 1
                else:
                    right += 1
                continue
            selected.add(index)
            used += addition
            if index == left:
                left -= 1
            else:
                right += 1
        window = "\n\n".join(segments[index] for index in sorted(selected))
        windows.append(window[:window_budget].rstrip())
    projected = marker.join(window for window in windows if window)
    return projected[:budget], True


def project_web_text(
    source: Mapping[str, Any],
    query: str = "",
    *,
    max_chars: int = 2800,
    chunk_limit: int = 4,
) -> dict[str, Any]:
    """Project readable web text while preserving provenance outside the text.

    ``bodyExcerpt`` is a bounded, segment-preserving coverage projection.
    ``evidenceChunks`` contains positive query-overlap passages when available.
    ``bodyTruncated`` describes only this projection and does not alter
    ``fetchStatus``, source IDs, URLs, or execution receipts.
    """

    raw_body = str(source.get("body") or "")
    if not raw_body.strip() or source.get("ok") is False:
        return {
            "bodyExcerpt": "",
            "evidenceChunks": [],
            "bodyTruncated": False,
            "projectionMode": "empty",
        }
    budget = max(120, int(max_chars))
    _projection_segments, projection_ranked = _rank_query_segments(
        raw_body,
        query,
        minimum_length=20,
    )
    projection_anchors = [index for _score, index, _text in projection_ranked]
    chunks = extract_query_evidence_chunks(
        source,
        query,
        limit=max(1, int(chunk_limit)),
        max_chunk_chars=min(1600, max(480, budget // max(2, int(chunk_limit)))),
    )
    if chunks:
        safe_body = "\n\n".join(_projection_segments)
        if len(safe_body) <= budget:
            body_excerpt, clipped = safe_body, False
        else:
            selected_text, clipped = _join_bounded([chunk["text"] for chunk in chunks], budget)
            remaining = budget - len(selected_text) - 2
            context, _ = _lead_tail_projection(raw_body, remaining, anchor_indices=projection_anchors) if remaining >= 120 else ("", True)
            body_excerpt = (selected_text + ("\n\n" + context if context else ""))[:budget]
            clipped = True
        return {
            "bodyExcerpt": body_excerpt,
            "evidenceChunks": chunks,
            "bodyTruncated": bool(
                clipped or len(body_excerpt) < len(raw_body)
            ),
            "projectionMode": "query_chunks_with_context" if body_excerpt else "empty",
        }
    body_excerpt, truncated = _lead_tail_projection(
        raw_body,
        budget,
        anchor_indices=projection_anchors,
    )
    return {
        "bodyExcerpt": body_excerpt,
        "evidenceChunks": [],
        "bodyTruncated": truncated,
        "projectionMode": "lead_tail" if body_excerpt else "empty",
    }


__all__ = ["extract_query_evidence_chunks", "project_web_text"]
