"""QQ-only projection of verified web citations.

The desktop/web response keeps its internal ``[S1]`` references and the full
source batch.  QQ is a transport boundary, however: it should show a compact
source note only for references that are both present in the reply and bound to
an actual public source, while the spoken track must remain answer-only.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterable, Mapping
import urllib.parse


_CITATION = re.compile(r"\[(S\d+)\]", re.IGNORECASE)
_SOURCE_ID = re.compile(r"S\d+\Z", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"'，。！？；;、]+", re.IGNORECASE)
_MAX_SOURCES = 8
_MAX_TITLE = 300
_MAX_URL = 4096


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _source_rows(value: Any) -> list[dict[str, str]]:
    """Return only bounded public source identity fields.

    ``sourceBatch.sources`` uses ``displayId`` while the direct result uses
    ``id``.  Accepting both is important for the owner loopback, but the
    projection never forwards excerpts, bodies, or arbitrary provider fields.
    """

    if isinstance(value, Mapping):
        value = value.get("sources")
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value[: _MAX_SOURCES * 2]:
        if not isinstance(item, Mapping):
            continue
        ident = _text(item.get("displayId") or item.get("id"), 32).upper()
        if not _SOURCE_ID.fullmatch(ident) or ident in seen:
            continue
        raw_url = _text(item.get("url") or item.get("canonicalUrl"), _MAX_URL)
        try:
            parsed = urllib.parse.urlsplit(raw_url)
        except ValueError:
            continue
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            continue
        # Do not expose URLs carrying credentials.  Valid public source URLs
        # normally have neither; the URL itself remains byte-stable for the
        # user.
        if parsed.username or parsed.password:
            continue
        title = _text(item.get("title") or item.get("name") or parsed.hostname, _MAX_TITLE)
        if not title:
            title = parsed.hostname
        rows.append({"id": ident, "title": title, "url": raw_url})
        seen.add(ident)
        if len(rows) >= _MAX_SOURCES:
            break
    return rows


def safe_citation_sources(value: Any) -> list[dict[str, str]]:
    """Public, bounded source records safe to carry across the QQ bridge."""

    return _source_rows(value)


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Find simple backtick code spans so a literal ``[S1]`` is untouched."""

    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        start = index
        while index < len(text) and text[index] == "`":
            index += 1
        width = index - start
        closing = text.find("`" * width, index)
        if closing < 0:
            # An unmatched backtick is treated as ordinary text.  It is safer
            # to remove a protocol marker than to hide the rest of a reply.
            continue
        spans.append((start, closing + width))
        index = closing + width
    return spans


def _in_span(position: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def project_citation_text(text: Any, sources: Any) -> tuple[str, list[dict[str, str]]]:
    """Remove protocol markers and return only actually bound sources.

    A citation-like marker is an internal transport token even when the
    current turn has no source row (for example, a model reuses a prior
    answer's marker).  Unknown markers are therefore removed without
    inventing a source note.  Literal markers in code spans and URLs remain
    ordinary user text; a bare ``S1`` technical term is not affected.
    """

    value = str(text or "")
    rows = _source_rows(sources)
    by_id = {item["id"]: item for item in rows}
    spans = [*_code_spans(value), *[(item.start(), item.end()) for item in _URL.finditer(value)]]
    referenced: list[dict[str, str]] = []
    seen: set[str] = set()
    output: list[str] = []
    cursor = 0
    for match in _CITATION.finditer(value):
        ident = match.group(1).upper()
        if _in_span(match.start(), spans):
            continue
        output.append(value[cursor : match.start()])
        cursor = match.end()
        if ident in by_id and ident not in seen:
            referenced.append(by_id[ident])
            seen.add(ident)
    output.append(value[cursor:])
    return "".join(output).strip(), referenced


def _source_parts(sources: list[dict[str, str]]) -> list[str]:
    if not sources:
        return []
    lines = ["参考来源："]
    lines.extend(f"- {item['title']}\n  {item['url']}" for item in sources)
    return ["\n".join(lines)]


def _speech_plan(value: Any, sources: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = deepcopy(dict(value))
    raw_units = result.get("speechUnits")
    if not isinstance(raw_units, list):
        return result
    units: list[dict[str, Any]] = []
    for raw in raw_units:
        if not isinstance(raw, Mapping):
            continue
        unit = dict(raw)
        for key in ("textZh", "spokenTextJa", "speechText", "text"):
            if isinstance(unit.get(key), str):
                unit[key], _ = project_citation_text(unit[key], sources)
        units.append(unit)
    result["speechUnits"] = units
    return result


def project_qq_result(
    result: Mapping[str, Any] | None,
    *,
    include_source_parts: bool = False,
    voice: bool = False,
) -> dict[str, Any]:
    """Project a backend result for QQ without changing the backend result.

    The first projection (the short-lived QQ worker) may already remove the
    markers.  ``citationSources`` is retained so the gateway can still add the
    independent source notes on its second, delivery-aware projection.
    """

    projected = dict(result) if isinstance(result, Mapping) else {}
    raw_sources: Any = projected.get("sources")
    if not isinstance(raw_sources, list):
        raw_sources = projected.get("sourceBatch")
    rows = _source_rows(raw_sources)
    previous = _source_rows(projected.get("citationSources"))
    reply, referenced = project_citation_text(projected.get("reply"), rows)
    if not referenced and previous:
        by_id = {item["id"]: item for item in previous}
        referenced = [by_id[item["id"]] for item in previous if item["id"] in by_id]
    projected["reply"] = reply
    projected["citationSources"] = referenced
    projected["sources"] = referenced

    raw_parts = projected.get("replyParts")
    if isinstance(raw_parts, list):
        parts: list[str] = []
        for item in raw_parts:
            if isinstance(item, str):
                value = item
            elif isinstance(item, Mapping):
                value = next(
                    (item[key] for key in ("textZh", "zh", "reply", "text")
                     if isinstance(item.get(key), str)),
                    "",
                )
            else:
                continue
            clean, _ = project_citation_text(value, rows or previous)
            if clean:
                parts.append(clean)
        if parts:
            projected["replyParts"] = parts
        else:
            projected.pop("replyParts", None)

    speech = projected.get("speech")
    if isinstance(speech, Mapping):
        projected["speech"] = _speech_plan(speech, rows or previous)
    if isinstance(projected.get("speechUnits"), list):
        projected["speechUnits"] = _speech_plan(
            {"speechUnits": projected["speechUnits"]}, rows or previous
        ).get("speechUnits", [])
    if include_source_parts:
        projected["sourceParts"] = _source_parts(referenced)
    else:
        projected.pop("sourceParts", None)
    return projected


def citation_source_parts(result: Mapping[str, Any] | None) -> list[str]:
    """Read already-projected source notes for delivery as separate bubbles."""

    value = result.get("sourceParts") if isinstance(result, Mapping) else None
    return [item for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


__all__ = [
    "citation_source_parts",
    "project_citation_text",
    "project_qq_result",
    "safe_citation_sources",
]
