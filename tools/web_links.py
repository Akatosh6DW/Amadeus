"""Bounded link metadata extracted from untrusted web documents.

The page reader may expose destination links so a later research step can
follow a detail page that is only named in the fetched document.  This module
only normalizes obvious URL shape and local-address hazards; opening a link
still goes through :func:`tools.safe_web.validate_public_url` and its full DNS
and redirect checks.
"""

from __future__ import annotations

import html
import ipaddress
import re
import urllib.parse
from typing import Any, Mapping


MAX_WEB_LINKS = 24
MAX_WEB_LINK_CANDIDATES = MAX_WEB_LINKS * 4
MAX_WEB_LINK_TEXT = 240
MAX_WEB_LINK_URL = 2048
BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".home", ".lan")


def normalize_web_link(raw_url: Any, *, base_url: str = "") -> str:
    """Resolve one document link without performing DNS or an HTTP request."""

    raw = str(raw_url or "").strip()
    if not raw or raw.startswith("#"):
        return ""
    try:
        candidate = urllib.parse.urljoin(str(base_url or "").strip(), raw)
        parsed = urllib.parse.urlsplit(candidate)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname or hostname == "localhost" or hostname.endswith(BLOCKED_HOST_SUFFIXES):
            return ""
        try:
            port = parsed.port
        except ValueError:
            return ""
        if port is not None and port not in {80, 443}:
            return ""
        try:
            literal = ipaddress.ip_address(hostname.split("%", 1)[0])
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            return ""
        if literal is None and "." not in hostname:
            return ""
        netloc = hostname
        if literal is not None and literal.version == 6:
            netloc = f"[{hostname}]"
        default_port = 80 if scheme == "http" else 443
        if port is not None and port != default_port:
            netloc = f"{netloc}:{port}"
        normalized = urllib.parse.urlunsplit((
            scheme,
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        ))
        return normalized if len(normalized) <= MAX_WEB_LINK_URL else ""
    except (TypeError, ValueError):
        return ""


def _link_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()[:MAX_WEB_LINK_TEXT]


def compact_web_links(
    value: Any,
    *,
    base_url: str = "",
    max_links: int = MAX_WEB_LINKS,
    max_url_chars: int = MAX_WEB_LINK_URL,
) -> list[dict[str, str]]:
    """Return small ``{text,url}`` records in document order.

    Link labels and URLs are untrusted page data.  Empty labels are omitted so
    navigation/image anchors do not fill the bounded list, and URLs are
    de-duplicated after relative resolution.
    """

    if not isinstance(value, (list, tuple)):
        return []
    limit = max(0, min(int(max_links), MAX_WEB_LINKS))
    if limit == 0:
        return []
    url_limit = max(32, min(int(max_url_chars), MAX_WEB_LINK_URL))
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, Mapping):
            raw_url = item.get("url") or item.get("href")
            label = item.get("text") or item.get("label") or item.get("title")
        else:
            raw_url = item
            label = ""
        url = normalize_web_link(raw_url, base_url=base_url)
        if not url:
            continue
        if len(url) > url_limit:
            continue
        text = _link_text(label)
        if not text or url in seen:
            continue
        seen.add(url)
        links.append({"text": text, "url": url})
        if len(links) >= limit:
            break
    return links


_MARKDOWN_INLINE_LINK = re.compile(
    r"(?<!\!)\[([^\]\n]{1,400})\]\(\s*(?:<([^>\n]{1,4096})>|([^\s)]+))"
    r"(?:\s+(?:\"[^\n\"]*\"|'[^\n']*'))?\s*\)",
)
_MARKDOWN_REFERENCE_DEF = re.compile(
    r"(?m)^\s{0,3}\[([^\]\n]{1,400})\]:\s*(?:<([^>\n]{1,4096})>|([^\s]+))"
)
_MARKDOWN_REFERENCE_LINK = re.compile(
    r"(?<!\!)\[([^\]\n]{1,400})\]\[([^\]\n]{0,400})\]"
)
_MARKDOWN_AUTOLINK = re.compile(r"<((?:https?://)[^>\s]{1,4096})>", re.I)


def extract_markdown_links(markdown: str) -> list[dict[str, str]]:
    """Extract inline and autolinks while leaving Markdown body untouched."""

    links: list[dict[str, str]] = []
    for match in _MARKDOWN_INLINE_LINK.finditer(str(markdown or "")):
        if len(links) >= MAX_WEB_LINK_CANDIDATES:
            break
        links.append({
            "text": _link_text(match.group(1)),
            "url": match.group(2) or match.group(3) or "",
        })
    definitions: dict[str, str] = {}
    for match in _MARKDOWN_REFERENCE_DEF.finditer(str(markdown or "")):
        key = re.sub(r"\s+", " ", match.group(1)).strip().casefold()
        if key:
            definitions[key] = match.group(2) or match.group(3) or ""
    for match in _MARKDOWN_REFERENCE_LINK.finditer(str(markdown or "")):
        if len(links) >= MAX_WEB_LINK_CANDIDATES:
            break
        label = _link_text(match.group(1))
        key = re.sub(r"\s+", " ", match.group(2) or match.group(1)).strip().casefold()
        if label and key in definitions:
            links.append({"text": label, "url": definitions[key]})
    for match in _MARKDOWN_AUTOLINK.finditer(str(markdown or "")):
        if len(links) >= MAX_WEB_LINK_CANDIDATES:
            break
        links.append({"text": match.group(1), "url": match.group(1)})
    return links


__all__ = [
    "MAX_WEB_LINKS",
    "MAX_WEB_LINK_CANDIDATES",
    "MAX_WEB_LINK_TEXT",
    "MAX_WEB_LINK_URL",
    "compact_web_links",
    "extract_markdown_links",
    "normalize_web_link",
]
