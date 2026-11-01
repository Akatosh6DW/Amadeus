"""Bounded, query-aware projections of untrusted web text.

This module deliberately has no backend, agent, server, filesystem, or network
dependencies.  Fetchers and research callers can share the same projection
without importing one another.  The returned text is still untrusted evidence;
the helper only chooses bounded text and never changes source identity or fetch
metadata.
"""

from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from hashlib import sha256
import math
import re
from typing import Any, Mapping


_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        # regex-purpose: projection; regex-boundary: known override phrases only.
        re.compile(
            r"ignore (?:all |previous )?instructions|"
            r"忽略(?:以上|之前|此前)(?:指令|要求)",
            re.IGNORECASE,
        ),
    ),
    (
        "secret_exfiltration",
        # regex-purpose: projection; regex-boundary: explicit secret-output phrases only.
        re.compile(
            r"输出(?:密钥|密码)|(?:reveal|output) (?:the )?(?:secret|password|token)",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_reference",
        # regex-purpose: projection; regex-boundary: prompt-reference phrases only.
        re.compile(r"system prompt|developer message|系统提示词|开发者消息", re.IGNORECASE),
    ),
    (
        "tool_execution_request",
        # regex-purpose: projection; regex-boundary: explicit tool-request phrases only.
        re.compile(r"(?:call|invoke|use).{0,20}(?:tool|browser)|调用.{0,12}(?:工具|浏览器)", re.IGNORECASE),
    ),
)

_PROMPT_MARKER_NAMES = {
    "instruction_override": "override_instructions",
    "secret_exfiltration": "secret_request",
    "system_prompt_reference": "system_prompt_request",
    "tool_execution_request": "tool_execution_request",
}

_SECTION_HEADING_PATTERN = re.compile(
    r"^(?:§\s*)?(?:(?:\d+(?:\.\d+)+\.?)|(?:\d+\.))\s*(?:\S.*)?$|"
    r"^(?:success\s+criterion|guideline|section|chapter|appendix)\s+"
    r"\d+(?:\.\d+)*\b",
    re.IGNORECASE,
)

def _clean_body(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _body_segments(body: str) -> list[str]:
    """Split prose without deleting any source text.

    Risky passages remain ordinary source segments.  Callers receive risk
    labels through :func:`web_text_risks`; no segment is removed based on its
    wording.
    """

    normalized = re.sub(r"\r\n?", "\n", str(body or "")).strip()
    if not normalized:
        return []
    raw_segments = re.split(
        r"\n{2,}|(?<=[。！？])\s*|(?<=[.!?])\s+",
        normalized,
    )
    return [cleaned for raw in raw_segments if (cleaned := _clean_body(raw))]


def web_text_risks(body: str) -> list[dict[str, Any]]:
    """Return side-channel risk labels while preserving the complete body.

    Labels contain only pattern kinds and segment indexes.  They deliberately
    do not replace, redact, or repeat the source passage.
    """

    segments = _body_segments(body)
    markers: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        signals = [kind for kind, pattern in _RISK_PATTERNS if pattern.search(segment)]
        if signals:
            markers.append({"segmentIndex": index, "signals": signals})
    return markers


# Descriptive alias for callers that prefer a verb-first name.
detect_web_text_risks = web_text_risks


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


READING_QUOTE_MIN_CHARS = 20
READING_QUOTE_MAX_CHARS = 600
READING_PROJECTION_MAX_CHARS = 2_800


def source_body_hash(source: Mapping[str, Any]) -> str:
    """Hash the unprojected body so persisted excerpts cannot go stale."""

    body = str(source.get("body") or "")
    return sha256(body.encode("utf-8", errors="replace")).hexdigest()


def verify_source_excerpt(
    source: Mapping[str, Any],
    excerpt: Mapping[str, Any] | str,
) -> dict[str, Any] | None:
    """Validate an excerpt against the current page body and provenance."""

    body = str(source.get("body") or "")
    if not body or source.get("ok") is False:
        return None
    if isinstance(excerpt, str):
        text = excerpt
        declared_hash = ""
        declared_start: int | None = None
        declared_end: int | None = None
        excerpt_uid = ""
        excerpt_url = ""
    elif isinstance(excerpt, Mapping):
        text = str(
            excerpt.get("text")
            or excerpt.get("quote")
            or excerpt.get("excerpt")
            or excerpt.get("sourceExcerpt")
            or ""
        )
        declared_hash = str(
            excerpt.get("sourceHash")
            or excerpt.get("bodyHash")
            or excerpt.get("hash")
            or ""
        ).strip()
        excerpt_uid = str(excerpt.get("sourceUid") or "").strip()
        excerpt_url = str(excerpt.get("url") or "").strip()
        raw_start = excerpt.get("startChar")
        raw_end = excerpt.get("endChar")
        try:
            declared_start = int(raw_start) if raw_start is not None else None
            declared_end = int(raw_end) if raw_end is not None else None
        except (TypeError, ValueError):
            return None
    else:
        declared_end = None
        return None
    if not text or len(text) < READING_QUOTE_MIN_CHARS or len(text) > READING_QUOTE_MAX_CHARS:
        return None
    body_hash = source_body_hash(source)
    if declared_hash and declared_hash != body_hash:
        return None
    source_uid = str(source.get("sourceUid") or source.get("canonicalUrl") or source.get("url") or "")
    source_url = str(source.get("url") or source.get("canonicalUrl") or "")
    if excerpt_uid and source_uid and excerpt_uid != source_uid:
        return None
    if excerpt_url and source_url and excerpt_url != source_url:
        return None
    if declared_start is None:
        start = body.find(text)
    else:
        start = declared_start
        if start < 0 or body[start:start + len(text)] != text:
            return None
    if start < 0:
        return None
    end = start + len(text)
    if declared_end is not None and declared_end != end:
        return None
    return {
        "text": body[start:end],
        "startChar": start,
        "endChar": end,
        "sourceHash": body_hash,
        "sourceUid": source_uid,
        "url": source_url,
        "verified": True,
        "verification": "exact_source_match",
    }


def verified_source_excerpts(source: Mapping[str, Any], *, limit: int = 4) -> list[dict[str, Any]]:
    """Validate and deduplicate reading excerpts persisted with one source."""

    summary = source.get("readingSummary")
    if not isinstance(summary, Mapping):
        return []
    raw = summary.get("verifiedExcerpts") or summary.get("excerpts") or []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for item in raw:
        verified = verify_source_excerpt(source, item)
        if verified is None:
            continue
        key = (verified["startChar"], verified["endChar"], verified["text"])
        if key in seen:
            continue
        seen.add(key)
        result.append(verified)
        if len(result) >= max(1, int(limit)):
            break
    return result


def rebind_reading_summary(source: Mapping[str, Any], *, source_uid: str, url: str) -> dict[str, Any] | None:
    """Rebind a derived summary only after validating against the old source.

    Fetch merging and observation windows can assign a new internal identity
    to the same body. Return a detached view; never modify the raw receipt.
    """
    reading = source.get("readingSummary")
    if not isinstance(reading, Mapping):
        return None
    result = deepcopy(dict(reading))
    result.update(sourceUid=source_uid, url=url)
    result["verifiedExcerpts"] = [
        {**item, "sourceUid": source_uid, "url": url}
        for item in verified_source_excerpts(source)
    ]
    return result


def _reading_summary_matches_query(summary: Mapping[str, Any], query: str) -> bool:
    """Avoid promoting a prior-turn excerpt for an unrelated current query."""

    recorded = str(summary.get("query") or "").strip()
    current = str(query or "").strip()
    if not recorded or not current:
        return False
    recorded_terms = set(_query_terms(recorded))
    current_terms = set(_query_terms(current))
    return bool(recorded_terms and current_terms and recorded_terms.intersection(current_terms))


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
    # Evidence chunks retain source text too.  Risk labels are carried beside
    # the projection so downstream policy can gate side effects without
    # deleting or rewriting the quoted passage.
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
    risk_markers = web_text_risks(raw_body)
    prompt_markers = [
        _PROMPT_MARKER_NAMES[signal]
        for marker in risk_markers
        for signal in marker.get("signals", [])
        if signal in _PROMPT_MARKER_NAMES
    ]
    source_prompt_markers = source.get("promptInjectionMarkers")
    if isinstance(source_prompt_markers, (list, tuple)):
        prompt_markers.extend(str(item) for item in source_prompt_markers if str(item).strip())
    risk_fields = {
        "riskMarkers": risk_markers,
        "promptInjectionMarkers": list(dict.fromkeys(prompt_markers)),
        "riskLevel": "high" if risk_markers or prompt_markers else "none",
    }
    reading = source.get("readingSummary")
    reading_mapping = reading if isinstance(reading, Mapping) else {}
    reading_status = str(reading_mapping.get("status") or "")
    reading_matches = (
        reading_mapping.get("sourceHash") == source_body_hash(source)
        and _reading_summary_matches_query(reading_mapping, query)
    )
    verified = verified_source_excerpts(source)
    if verified and not _reading_summary_matches_query(reading_mapping, query):
        # A source can be reused by a later turn. Keep the raw body available,
        # but do not let a summary quote from the previous question become the
        # first context window for this one.
        verified = []
    reading_fields = {
        "verifiedExcerpts": verified,
        # A derived summary is deliberately kept separate from body evidence.
        # Callers may display it with an explicit derived-summary label, but it
        # must never replace or upgrade ``bodyExcerpt``'s source provenance.
        "derivedSummary": str(reading_mapping.get("summary") or "")[:900] if reading_matches else "",
        "derivedSummaryKind": str(reading_mapping.get("kind") or "derived_summary"),
        "derivedSummaryStatus": str(reading_mapping.get("status") or "none"),
        "readingCoverage": dict(reading_mapping.get("coverage") or {}) if reading_matches else {},
        "sourceBodyHash": source_body_hash(source) if raw_body else "",
    }
    if not raw_body.strip() or source.get("ok") is False:
        return {
            "bodyExcerpt": "",
            "evidenceChunks": [],
            "bodyTruncated": False,
            "projectionMode": "empty",
            **risk_fields,
            **reading_fields,
        }
    budget = max(120, int(max_chars))
    if reading_status in {"complete", "fallback"}:
        # The isolated summary is a compact reading aid. Do not append it to
        # an otherwise full 8k source projection and thereby recreate the
        # context pressure this service is meant to reduce.
        budget = min(budget, READING_PROJECTION_MAX_CHARS)
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
        projection = {
            "bodyExcerpt": body_excerpt,
            "evidenceChunks": chunks,
            "bodyTruncated": bool(
                clipped or len(body_excerpt) < len(raw_body)
            ),
            "projectionMode": "query_chunks_with_context" if body_excerpt else "empty",
        }
    else:
        body_excerpt, truncated = _lead_tail_projection(
            raw_body,
            budget,
            anchor_indices=projection_anchors,
        )
        projection = {
            "bodyExcerpt": body_excerpt,
            "evidenceChunks": [],
            "bodyTruncated": truncated,
            "projectionMode": "lead_tail" if body_excerpt else "empty",
        }
    if verified:
        original_excerpt = str(projection.get("bodyExcerpt") or "")
        # Verified source quotes receive the first allocation in the projection
        # budget. They are safe to surface as excerpts because each one was
        # hash- and exact-match checked against this page body above.
        ordered_parts = [item["text"] for item in verified]
        if original_excerpt:
            ordered_parts.append(original_excerpt)
        body_excerpt, clipped = _join_bounded(ordered_parts, budget)
        projection["bodyExcerpt"] = body_excerpt
        projection["bodyTruncated"] = bool(
            projection.get("bodyTruncated") or clipped or len(body_excerpt) < len(raw_body)
        )
        projection["projectionMode"] = "verified_reading_excerpts"
        verified_texts = {item["text"] for item in verified}
        projection["evidenceChunks"] = [
            chunk for chunk in projection.get("evidenceChunks", [])
            if not any(
                text == str(chunk.get("text") or "")
                or text in str(chunk.get("text") or "")
                for text in verified_texts
            )
        ][:4]
    projection.update(reading_fields)
    projection.update(risk_fields)
    return projection


__all__ = [
    "extract_query_evidence_chunks",
    "project_web_text",
    "source_body_hash",
    "verify_source_excerpt",
    "verified_source_excerpts",
    "web_text_risks",
    "detect_web_text_risks",
]
