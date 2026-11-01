"""DeepSeek's paid Search/Fetch tools adapter.

The provider exposes only two fixed endpoints.  Keep the API key in the
transport header, never in a result or exception, and cache successful
responses before projecting page windows so ``find``/continuation reads do
not issue another paid Fetch request for the same URL.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import re
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from backend.research.planning import safe_outbound_web_query
from tools.safe_web import (
    SafeWebError,
    UnsafeUrlError,
    ResponseLimitError,
    canonicalize_public_url,
    prompt_injection_markers,
    safe_http_request,
    search_result_date_metadata,
    validate_public_url,
)


DEEPSEEK_HOST = "api.deepseek.com"
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
DEEPSEEK_FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
DEEPSEEK_QUERY_MAX_CHARS = 128
DEEPSEEK_COUNT_MAX = 50
DEEPSEEK_PAGE_MAX_CHARS = 14_000
_CACHE_TTL_SECONDS = 120.0
_FRESHNESS_DAYS = {
    "pd": 1,
    "pw": 7,
    "pm": 30,
    "py": 365,
}
_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE: dict[tuple[str, str, int, bool, int, int, str], tuple[float, dict[str, Any]]] = {}
_FETCH_CACHE: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


class DeepSeekSearchError(SafeWebError):
    """Safe, user-facing error for the paid DeepSeek web endpoints."""


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]


def api_key_for_settings(settings: Mapping[str, Any]) -> str:
    """Select the key without sending a credential to the wrong provider."""

    endpoint = urlsplit(str(settings.get("apiEndpoint") or "").strip())
    official_endpoint = bool(endpoint.hostname and endpoint.hostname.casefold() == DEEPSEEK_HOST)
    main_key = str(settings.get("apiKey") or "").strip()
    independent_key = str(settings.get("deepseekSearchApiKey") or "").strip()
    # The legacy webSearchApiKey is also used by Brave.  Only accept it for
    # DeepSeek when a caller explicitly records that ownership; never route a
    # key merely because the provider selector changed.
    if not independent_key and str(settings.get("webSearchApiKeyProvider") or "").strip().lower() == "deepseek":
        independent_key = str(settings.get("webSearchApiKey") or "").strip()
    key = main_key if official_endpoint and main_key else independent_key
    if not key:
        if official_endpoint:
            raise DeepSeekSearchError("DeepSeek 搜索需要当前模型的 API Key")
        raise DeepSeekSearchError("当前模型不是官方 DeepSeek 端点，请填写独立的 DeepSeek 搜索 Key")
    return key


def _query_text(value: Any) -> str:
    # regex-purpose: format
    raw_query = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(raw_query) > DEEPSEEK_QUERY_MAX_CHARS:
        raise DeepSeekSearchError("DeepSeek 搜索词不能超过 128 个字符")
    query = safe_outbound_web_query(raw_query, max_length=DEEPSEEK_QUERY_MAX_CHARS)
    if not query:
        raise DeepSeekSearchError("DeepSeek 搜索词为空或包含不允许的内容")
    return query


def _domains(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple)):
        raise DeepSeekSearchError("DeepSeek 搜索域名限定格式无效")
    result: list[str] = []
    for item in raw:
        domain = str(item or "").strip().lower().lstrip("*.").rstrip(".")
        # regex-purpose: format
        if not domain or len(domain) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain):
            raise DeepSeekSearchError("DeepSeek 搜索域名限定格式无效")
        if domain not in result:
            result.append(domain)
    if len(result) > 20:
        raise DeepSeekSearchError("DeepSeek 搜索域名限定最多 20 个")
    return result


def _explicit_language(value: Any) -> str:
    """Convert an explicitly requested locale to a safe ``lang:`` term.

    ``uiLanguage`` is intentionally not accepted here: a Chinese interface
    should not silently constrain an English/source-oriented query.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    # regex-purpose: format
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?", raw):
        raise DeepSeekSearchError("DeepSeek 搜索语言格式无效")
    return raw.split("-", 1)[0].split("_", 1)[0].lower()


def _freshness_after(value: str, *, today: date | None = None) -> str:
    """Translate the legacy freshness code to DeepSeek's date operator."""

    days = _FRESHNESS_DAYS[value]
    current = today or datetime.now(timezone.utc).date()
    return (current - timedelta(days=days)).isoformat()


def _provider_query(
    query: Any,
    *,
    domains: Any = None,
    freshness: str = "",
    locale: Any = None,
) -> tuple[str, str, dict[str, Any]]:
    original = _query_text(query)
    normalized_freshness = str(freshness or "").strip().lower()
    if normalized_freshness and normalized_freshness not in _FRESHNESS_DAYS:
        raise DeepSeekSearchError("DeepSeek 搜索暂不支持该时间范围，请切换搜索服务")
    requested_domains = _domains(domains)
    language = _explicit_language(locale)
    if len(requested_domains) == 1:
        terms = [f"site:{requested_domains[0]}"]
    elif requested_domains:
        # Several allowed domains are alternatives; joining site clauses with
        # AND would make a result satisfy every host at once.
        terms = ["(" + " OR ".join(f"site:{domain}" for domain in requested_domains) + ")"]
    else:
        terms = []
    after_date = ""
    if normalized_freshness:
        # DeepSeek's query syntax supports an ``after:YYYY-MM-DD`` operator.
        # Use it instead of natural-language wording that would merely ask the
        # ranking model for recent results without applying a date boundary.
        after_date = _freshness_after(normalized_freshness)
        terms.append(f"after:{after_date}")
    if language:
        # DeepSeek has no separate language field in this API.  Add a query
        # operator only when the caller explicitly supplied ``locale``.
        terms.append(f"lang:{language}")
    provider_query = " ".join([original, *terms]).strip()
    if len(provider_query) > DEEPSEEK_QUERY_MAX_CHARS:
        raise DeepSeekSearchError("DeepSeek 搜索词（含域名和时间条件）不能超过 128 个字符")
    return original, provider_query, {
        "requestedFreshness": normalized_freshness,
        "appliedFreshness": normalized_freshness,
        "domains": requested_domains,
        "locale": language,
        "after": after_date,
        "dateFilterSource": "deepseek.after" if after_date else "",
    }


def _count(value: Any) -> int:
    if isinstance(value, bool):
        raise DeepSeekSearchError("DeepSeek 搜索数量必须是 1–50 的整数")
    try:
        count = int(value)
    except (TypeError, ValueError) as error:
        raise DeepSeekSearchError("DeepSeek 搜索数量必须是 1–50 的整数") from error
    if not 1 <= count <= DEEPSEEK_COUNT_MAX:
        raise DeepSeekSearchError("DeepSeek 搜索数量必须是 1–50 的整数")
    return count


def _post_json(url: str, api_key: str, payload: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
    try:
        response = safe_http_request(
            url,
            method="POST",
            body=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=timeout,
        )
        value = json.loads(response["body"].decode("utf-8"))
    except Exception as error:
        # Do not pass provider response bodies or transport details through the
        # ordinary web-tool error/log path.  In particular, an upstream error
        # must never echo the Authorization header or a proxy URL.
        if isinstance(error, DeepSeekSearchError):
            raise
        if isinstance(error, UnsafeUrlError):
            reason = "目的地址的 DNS 或公网地址检查未通过"
        elif isinstance(error, ResponseLimitError):
            reason = "响应超出读取上限"
        elif isinstance(error, (TimeoutError, OSError, SafeWebError)):
            reason = "网络连接、代理或上游 HTTP 请求失败"
        elif isinstance(error, (ValueError, UnicodeError)):
            reason = "服务返回内容无法解析"
        else:
            reason = "请求处理异常"
        raise DeepSeekSearchError(f"DeepSeek 搜索服务请求失败：{reason}（{type(error).__name__}）") from None
    if not isinstance(value, dict):
        raise DeepSeekSearchError("DeepSeek 搜索服务返回格式无效")
    code = value.get("code")
    if value.get("error") or (isinstance(code, str) and code.strip().lower() in {"error", "failed"}):
        raise DeepSeekSearchError("DeepSeek 搜索服务拒绝了请求")
    return value


def _cache_get(cache: dict[Any, tuple[float, Any]], key: Any) -> Any | None:
    now = time.monotonic()
    with _CACHE_LOCK:
        item = cache.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= now:
            cache.pop(key, None)
            return None
        return deepcopy(value)


def _cache_put(cache: dict[Any, tuple[float, Any]], key: Any, value: Any) -> None:
    with _CACHE_LOCK:
        cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, deepcopy(value))


def _date_metadata(item: Mapping[str, Any]) -> dict[str, str]:
    provider_date = str(item.get("date") or "").strip()[:160]
    date_hint, timestamp = search_result_date_metadata(
        provider_date,
        item.get("title"),
        item.get("snippet"),
    )
    if provider_date:
        return {
            "publishedAt": provider_date[:80],
            "publishedTimestamp": timestamp[:80],
            "dateEvidence": provider_date[:120],
            "dateHint": date_hint[:80],
            "dateSource": "provider_metadata",
            "providerDate": provider_date[:80],
            "providerDateSource": "deepseek.date",
        }
    return {
        "publishedAt": "",
        "publishedTimestamp": "",
        "dateEvidence": date_hint[:120],
        "dateHint": date_hint[:80],
        "dateSource": "search_title_or_snippet" if date_hint else "",
        "providerDate": "",
        "providerDateSource": "",
    }


def _source_from_result(item: Mapping[str, Any], index: int, *, body_allowed: bool) -> dict[str, Any] | None:
    url = str(item.get("url") or "").strip()
    try:
        parsed, _ = validate_public_url(url)
    except SafeWebError:
        return None
    canonical_url = canonicalize_public_url(parsed._replace(fragment="").geturl())
    # regex-purpose: projection
    title = re.sub(r"\s+", " ", html.unescape(str(item.get("title") or ""))).strip()[:300]
    # regex-purpose: projection
    snippet = re.sub(
        r"\s+",
        " ",
        html.unescape(str(item.get("snippet") or item.get("summary") or "")),
    ).strip()[:800]
    full_content = str(item.get("content") or "")
    content = full_content[:DEEPSEEK_PAGE_MAX_CHARS] if body_allowed else ""
    body_has_more = bool(content) and len(full_content) > len(content)
    body_next_start = len(content) if body_has_more else None
    date_metadata = _date_metadata(item)
    source: dict[str, Any] = {
        "id": f"S{index}",
        "title": title or parsed.hostname,
        "url": canonical_url,
        "domain": parsed.hostname,
        "snippet": snippet,
        "summary": str(item.get("summary") or "")[:900],
        "body": content,
        **date_metadata,
        "searchProvider": "deepseek",
        "searchBackend": "deepseek",
        "fetchStatus": "success" if content else "not_fetched",
        "bodyCharLimit": DEEPSEEK_PAGE_MAX_CHARS,
        "hasMore": body_has_more,
        "nextStartChar": body_next_start,
        "continuationStartChar": body_next_start,
        "continuationMaxChars": DEEPSEEK_PAGE_MAX_CHARS if body_has_more else None,
        "requestedFreshness": "",
        "appliedFreshness": "",
        "dateFilterSupported": True,
    }
    if "rerank_score" in item:
        source["rerankScore"] = item.get("rerank_score")
    if "authority" in item:
        source["authority"] = item.get("authority")
    return source


def search(
    query: Any,
    *,
    api_key: str,
    count: int = 5,
    need_content: bool = True,
    timeout: float = 12,
    domains: Any = None,
    freshness: str = "",
    body_limit: int | None = None,
    offset: int = 0,
    page_cursor: str = "",
    locale: Any = None,
) -> dict[str, Any]:
    """Call DeepSeek Search once and map its response to Amadeus sources."""

    key = str(api_key or "").strip()
    if not key:
        raise DeepSeekSearchError("DeepSeek 搜索需要 API Key")
    original, provider_query, details = _provider_query(
        query, domains=domains, freshness=freshness, locale=locale,
    )
    result_count = _count(count)
    include_content = bool(need_content)
    if body_limit is None:
        requested_body_limit = result_count if include_content else 0
    else:
        if isinstance(body_limit, bool):
            raise DeepSeekSearchError("DeepSeek 搜索正文数量必须是非负整数")
        try:
            requested_body_limit = int(body_limit)
        except (TypeError, ValueError) as error:
            raise DeepSeekSearchError("DeepSeek 搜索正文数量必须是非负整数") from error
        if requested_body_limit < 0:
            raise DeepSeekSearchError("DeepSeek 搜索正文数量必须是非负整数")
        requested_body_limit = min(requested_body_limit, result_count)
    if not include_content:
        requested_body_limit = 0
    try:
        requested_offset = int(offset)
    except (TypeError, ValueError) as error:
        raise DeepSeekSearchError("DeepSeek 搜索 offset 必须是非负整数") from error
    if requested_offset < 0:
        raise DeepSeekSearchError("DeepSeek 搜索 offset 必须是非负整数")
    requested_page_cursor = str(page_cursor or "").strip()[:512]
    cache_key = (
        _key_fingerprint(key), provider_query, result_count, include_content,
        requested_body_limit, requested_offset, requested_page_cursor,
    )
    cached = _cache_get(_SEARCH_CACHE, cache_key)
    if cached is not None:
        cached["cacheHit"] = True
        return cached
    value = _post_json(
        DEEPSEEK_SEARCH_URL,
        key,
        {"query": provider_query, "count": result_count, "need_content": include_content},
        timeout=timeout,
    )
    raw_results = value.get("results")
    if not isinstance(raw_results, list):
        raise DeepSeekSearchError("DeepSeek 搜索服务没有返回结果列表")
    sources: list[dict[str, Any]] = []
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        source = _source_from_result(
            raw,
            len(sources) + 1,
            body_allowed=len(sources) < requested_body_limit,
        )
        if source is None:
            continue
        full_content = raw.get("content")
        if isinstance(full_content, str) and full_content.strip():
            _cache_put(
                _FETCH_CACHE,
                (_key_fingerprint(key), source["url"]),
                {"title": source["title"], "content": full_content},
            )
        source["requestedFreshness"] = details["requestedFreshness"]
        source["appliedFreshness"] = details["appliedFreshness"]
        sources.append(source)
        if len(sources) >= result_count:
            break
    response = {
        "query": original,
        "providerQuery": provider_query,
        "provider": "deepseek",
        "requestedProvider": "deepseek",
        "providerAttempts": ["deepseek"],
        "providerErrors": [],
        "searchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "maxResults": result_count,
        "needContent": include_content,
        "dateFilter": {
            "requestedFreshness": details["requestedFreshness"],
            "appliedFreshness": details["appliedFreshness"],
            "supported": True,
            "provider": "deepseek",
            "parameter": "after" if details["after"] else "",
            "source": details["dateFilterSource"],
            "after": details["after"],
            "fallbackUnfiltered": False,
        },
        "locale": details["locale"],
        "domains": {"requested": details["domains"], "supported": True,
                    "applied": bool(details["domains"]), "matchedResults": len(sources),
                    "candidateLimit": result_count},
        "pagination": {
            "requestedOffset": requested_offset,
            "requestedPageCursor": requested_page_cursor,
            "appliedOffset": 0,
            "appliedPageCursor": None,
            "offsetSupported": False,
            "pageCursorSupported": False,
            "supported": False,
            "applied": False,
            "hasMore": None,
            "nextOffset": None,
            "nextPageCursor": None,
            "reason": (
                "deepseek_search_does_not_expose_offset_or_page_cursor"
                if requested_offset or requested_page_cursor else ""
            ),
        },
        "cacheHit": False,
    }
    _cache_put(_SEARCH_CACHE, cache_key, response)
    return deepcopy(response)


def _project_page(content: str, *, url: str, title: str, query: str = "", find: str = "",
                  start_char: int | None = None, max_chars: int | None = None,
                  cache_hit: bool = False) -> dict[str, Any]:
    if start_char is not None:
        try:
            requested_start = int(start_char)
        except (TypeError, ValueError) as error:
            raise DeepSeekSearchError("DeepSeek 网页 startChar 必须是非负整数") from error
        if requested_start < 0:
            raise DeepSeekSearchError("DeepSeek 网页 startChar 必须是非负整数")
    else:
        requested_start = 0
    if max_chars is None:
        window = DEEPSEEK_PAGE_MAX_CHARS
    else:
        try:
            window = int(max_chars)
        except (TypeError, ValueError) as error:
            raise DeepSeekSearchError("DeepSeek 网页 maxChars 必须是正整数") from error
        if window <= 0:
            raise DeepSeekSearchError("DeepSeek 网页 maxChars 必须是正整数")
        window = min(window, DEEPSEEK_PAGE_MAX_CHARS)
    match_positions = []
    if find:
        # regex-purpose: projection
        # regex-boundary: re.escape keeps the find term literal; the bounded HTTP response is searched once.
        match = re.search(re.escape(str(find)), content, flags=re.IGNORECASE)
        if match and start_char is None:
            requested_start = match.start()
        if match:
            match_positions.append({"startChar": match.start(), "endChar": match.end(), "text": match.group(0)})
    effective_start = min(requested_start, len(content))
    text = content[effective_start:effective_start + window]
    end_char = effective_start + len(text)
    has_more = end_char < len(content)
    parsed = urlsplit(url)
    return {
        "requestedUrl": url,
        "url": url,
        "finalUrl": url,
        "domain": parsed.hostname or "",
        "title": title or parsed.hostname or url,
        "text": text,
        "links": [],
        "accessedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mimeType": "text/plain",
        "fetchStatus": "success",
        "extractedChars": len(content),
        "truncated": has_more,
        "promptInjectionMarkers": prompt_injection_markers(text),
        "cacheHit": cache_hit,
        "totalChars": len(content),
        "range": {"startChar": effective_start, "endChar": end_char},
        "rangeExact": True,
        "projectionMode": "window",
        "startChar": effective_start,
        "endChar": end_char,
        "nextStartChar": end_char if has_more else None,
        "continuationStartChar": end_char if has_more else None,
        "continuationMaxChars": window if has_more else None,
        "continuation": ({"mode": "sequential", "startChar": end_char, "maxChars": window}
                         if has_more else None),
        "hasMore": has_more,
        "find": str(find or ""),
        "matchCount": len(match_positions),
        "matches": match_positions,
        "matchPositions": match_positions,
        "findMatches": match_positions,
        "researchQuery": str(query or "")[:500],
    }


def fetch(
    url: Any,
    *,
    api_key: str,
    timeout: float = 12,
    query: str = "",
    find: str = "",
    start_char: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Fetch once through DeepSeek and serve all later page windows locally."""

    key = str(api_key or "").strip()
    if not key:
        raise DeepSeekSearchError("DeepSeek Fetch 需要 API Key")
    requested_url = str(url or "").strip()
    try:
        parsed, _ = validate_public_url(requested_url)
    except Exception:
        raise DeepSeekSearchError("DeepSeek Fetch 只允许读取公网 HTTP(S) 页面") from None
    canonical_url = canonicalize_public_url(parsed._replace(fragment="").geturl())
    cache_key = (_key_fingerprint(key), canonical_url)
    cached = _cache_get(_FETCH_CACHE, cache_key)
    cache_hit = cached is not None
    if cached is None:
        value = _post_json(DEEPSEEK_FETCH_URL, key, {"url": canonical_url}, timeout=timeout)
        title = str(value.get("title") or "").strip()[:300]
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekSearchError("DeepSeek Fetch 没有返回可读取正文")
        cached = {"title": title, "content": content}
        _cache_put(_FETCH_CACHE, cache_key, cached)
    page = _project_page(cached["content"], url=canonical_url, title=cached["title"],
                         query=query, find=find, start_char=start_char,
                         max_chars=max_chars, cache_hit=cache_hit)
    page["requestedUrl"] = requested_url
    return page


__all__ = [
    "DEEPSEEK_COUNT_MAX",
    "DEEPSEEK_FETCH_URL",
    "DEEPSEEK_QUERY_MAX_CHARS",
    "DEEPSEEK_SEARCH_URL",
    "DeepSeekSearchError",
    "api_key_for_settings",
    "fetch",
    "search",
]
