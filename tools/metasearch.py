"""Small adapter around DDGS's maintained search-engine implementations.

Only search queries go to this client. Result URLs are still fetched through
safe_web's public-URL and redirect checks. Engines are selected explicitly so
DDGS's automatic encyclopedia preference cannot replace a web search.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, unquote_plus, urlsplit

try:
    from ddgs import DDGS
    from ddgs.engines import ENGINES
except ImportError:  # Older installations can still use the legacy transport.
    DDGS = None  # type: ignore[assignment]
    ENGINES = {}


def available() -> bool:
    return DDGS is not None


_CHALLENGE_MARKERS = (
    "captcha",
    "challenge-form",
    "cf-chl-",
    "anomaly.js",
    "verify you are human",
    "unusual traffic",
    "robot check",
    "anomaly-modal",
    "access denied",
)

_YAHOO_TEMPORARY_ERROR_TEXT = "temporary problems searching for web pages"


def _has_yahoo_temporary_error_page(body: str) -> bool:
    """Recognize Yahoo's HTTP-200 transient error page as a provider error.

    Yahoo uses a zero-result-looking ``zrp`` container for this page, but the
    visible message says that the provider had a temporary search failure.
    Depending on the response layout, the container is under either ``#web``
    or ``#algo-bd``. Restrict the check to those result areas and the
    provider's structural marker so a normal answer, disambiguation card, or
    result snippet containing the same words stays valid.
    """

    sample = str(body or "")[:200_000]
    result_area_matches = re.finditer(
        r"<(?:div|section)\b[^>]*\bid\s*=\s*['\"](?:web|algo-bd)['\"][^>]*>",
        sample,
        flags=re.I,
    )
    for result_area_match in result_area_matches:
        result_area = sample[result_area_match.end():]
        class_values = re.findall(
            r"\bclass\s*=\s*(['\"])(.*?)\1", result_area,
            flags=re.I | re.S,
        )
        has_zero_result_marker = any(
            re.search(r"(?<!\w)zrp_(?:yst|msg)(?!\w)", value, flags=re.I)
            or re.search(r"(?<!\w)zrpmsg(?!\w)", value, flags=re.I)
            for _, value in class_values
        )
        if not has_zero_result_marker:
            continue
        visible_text = re.sub(
            r"<(?:script|style)\b[^>]*>.*?</(?:script|style)\s*>",
            " ",
            result_area,
            flags=re.I | re.S,
        )
        visible_text = re.sub(r"<[^>]+>", " ", visible_text)
        visible_text = re.sub(r"\s+", " ", visible_text).casefold()
        if _YAHOO_TEMPORARY_ERROR_TEXT in visible_text:
            return True
    return False


@dataclass
class _ResponseCapture:
    """Response metadata captured from one DDGS engine instance."""

    engine: Any
    http_client: Any
    original_request: Any
    status_code: int | None = None
    body: str = ""

    def restore(self) -> None:
        # The DDGS client is local to one search call. Restoring the instance
        # still keeps this instrumentation from leaking into a cached client
        # if a caller supplies a custom DDGS implementation in a test.
        try:
            self.http_client.request = self.original_request
        except Exception:
            pass


def _capture_engine_response(client: Any, engine_name: str) -> _ResponseCapture | None:
    """Record the response already made by DDGS without issuing another one."""
    get_engines = getattr(client, "_get_engines", None)
    if not callable(get_engines):
        return None
    try:
        engines = get_engines("text", engine_name)
        engine = engines[0] if engines else None
        http_client = getattr(engine, "http_client", None)
        original_request = getattr(http_client, "request", None)
        if engine is None or not callable(original_request):
            return None
    except Exception:
        # A small test double or an older DDGS release may not expose the
        # private cache. The normal DDGS path remains usable without capture.
        return None

    capture = _ResponseCapture(engine, http_client, original_request)

    def request(*args: Any, **kwargs: Any) -> Any:
        response = original_request(*args, **kwargs)
        capture.status_code = getattr(response, "status_code", None)
        capture.body = str(getattr(response, "text", "") or "")
        return response

    try:
        # This is an instance-local wrapper. It does not alter DDGS classes or
        # any process-global HTTP function.
        http_client.request = request
    except Exception:
        return None
    return capture


def _is_no_results_error(error: BaseException) -> bool:
    return type(error).__name__ == "DDGSException" and str(error) == "No results found."


def _has_challenge(body: str) -> bool:
    sample = body[:200_000].casefold()
    return any(marker in sample for marker in _CHALLENGE_MARKERS)


def _provider_failure(engine: str, capture: _ResponseCapture | None) -> str | None:
    if capture is None:
        return None
    status = capture.status_code
    if status is not None and status != 200:
        suffix = " challenge" if _has_challenge(capture.body) else ""
        return f"{engine} search failed (HTTP {status}{suffix})"
    if (
        status == 200
        and engine == "yahoo"
        and _has_yahoo_temporary_error_page(capture.body)
    ):
        return f"{engine} search failed (temporary provider error)"
    if status == 200 and _has_challenge(capture.body):
        return f"{engine} search failed (challenge page)"
    return None


def _decode_wrapped_url(value: str) -> str:
    """Decode a result URL wrapped by DuckDuckGo or Yahoo."""
    current = value
    for _ in range(2):
        parsed = urlsplit(current)
        host = (parsed.hostname or "").casefold()
        candidate = None
        if host in {"duckduckgo.com", "www.duckduckgo.com"}:
            params = parse_qs(parsed.query)
            for key in ("uddg", "u", "url"):
                if params.get(key):
                    candidate = unquote(params[key][0])
                    break
        elif host == "yahoo.com" or host.endswith(".yahoo.com"):
            params = parse_qs(parsed.query)
            for key in ("u", "url", "target"):
                if params.get(key):
                    candidate = unquote(params[key][0])
                    break
            path_match = re.search(r"/ru=(.*)", parsed.path, flags=re.I)
            if path_match:
                encoded = re.split(r"/(?:rk|rs)=", path_match.group(1), maxsplit=1, flags=re.I)[0]
                candidate = unquote_plus(encoded)
        if not candidate or not candidate.casefold().startswith(("http://", "https://")):
            break
        current = candidate
    return current


def _source_dedupe_key(value: str) -> tuple[Any, ...] | str:
    """Deduplicate a URL while preserving case-sensitive path/query components."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return value
    if not host:
        return value
    return (
        parsed.scheme,
        host.casefold(),
        parsed.username,
        parsed.password,
        port,
        parsed.path,
        parsed.query,
        parsed.fragment,
    )


def _has_class_token(node: Any, token: str) -> bool:
    classes = f" {str(node.get('class') or '').casefold()} "
    return f" {token.casefold()} " in classes


def _has_yahoo_ad_marker(node: Any) -> bool:
    """Reject ad containers without relying on a result/domain allow-list."""
    current = node
    while current is not None:
        if any(_has_class_token(current, token) for token in ("ad", "ads", "sponsored")):
            return True
        role = str(current.get("role") or "").casefold()
        if role in {"advertisement", "ad"}:
            return True
        current = current.getparent()
    return False


def _is_yahoo_source_url(value: str) -> bool:
    """Return whether a decoded answer-card href can be a source record."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    if parsed.scheme.casefold() not in {"http", "https"} or not host:
        return False
    # Inline SVG namespace URLs can appear as href-like attributes in the
    # captured DOM; they are not navigable source records. Match the namespace
    # path rather than allowing or denying any particular result domain.
    namespace_uri = f"{parsed.scheme.casefold()}://{host}{parsed.path}"
    if (
        namespace_uri in {"http://www.w3.org/2000/svg", "https://www.w3.org/2000/svg"}
        and not parsed.query
        and not parsed.fragment
    ):
        return False
    # Only Yahoo's search endpoint is navigation. Other Yahoo hosts such as
    # news.yahoo.com remain valid source pages.
    search_path = parsed.path.casefold()
    if host == "search.yahoo.com" and (
        search_path == "/search"
        or search_path.startswith(("/search;", "/search/"))
    ):
        return False
    return True


def _is_yahoo_navigation_link(link: Any) -> bool:
    """Reject complete answer-module navigation labels without broad title matching."""
    label = re.sub(r"\s+", " ", _visible_text(link)).strip().casefold()
    return bool(re.fullmatch(
        r"(?:see|view|show)\s+(?:all\s+)?results(?:\s+for\s+(?:this|the)\s+question)?|"
        r"(?:next|previous)(?:\s+(?:results?|page))?|back",
        label,
    ))


def _visible_text(node: Any) -> str:
    return " ".join(" ".join(str(part).split()) for part in node.xpath(".//text()")).strip()


def _yahoo_answer_snippet(comp_title: Any, container: Any) -> str:
    """Read only the answer-card snippet paired with a source title."""
    for ancestor in comp_title.iterancestors():
        if ancestor is container:
            break
        if _has_class_token(ancestor, "itm"):
            snippet_nodes = ancestor.xpath(
                "(.//*[contains(concat(' ', normalize-space(@class), ' '), ' compTextList ')])[1]"
            )
            return _visible_text(snippet_nodes[0]) if snippet_nodes else ""
    return ""


def _extract_yahoo_answer_sources(
    tree: Any, seen_urls: set[Any]
) -> list[dict[str, str]]:
    """Extract external source cards from Yahoo's current answer layout."""
    extracted: list[dict[str, str]] = []
    containers = tree.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), ' compContainerExp ')]"
    )
    for container in containers:
        title_nodes = container.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '), ' compTitle ')]"
        )
        for comp_title in title_nodes:
            if _has_yahoo_ad_marker(comp_title):
                continue
            links = comp_title.xpath(".//a[@href]")
            chosen_url = ""
            chosen_link = None
            for link in links:
                if _is_yahoo_navigation_link(link):
                    continue
                candidate = _decode_wrapped_url(str(link.get("href") or ""))
                if _is_yahoo_source_url(candidate):
                    chosen_url = candidate
                    chosen_link = link
                    break
            if not chosen_url or chosen_link is None:
                continue
            key = _source_dedupe_key(chosen_url)
            if key in seen_urls:
                continue
            title = _visible_text(chosen_link)
            if not title:
                title = str(chosen_link.get("aria-label") or "").strip()
            if not title:
                continue
            seen_urls.add(key)
            extracted.append({
                "href": chosen_url,
                "title": title,
                # The answer text is a discovery snippet only. It is not
                # treated as fetched source body or as an authority signal.
                "body": _yahoo_answer_snippet(comp_title, container),
            })
    return extracted


def _extract_yahoo_fallback(engine: Any, body: str) -> list[dict[str, str]]:
    """Extract current Yahoo cards while retaining DDGS's legacy parser."""
    if not body or engine is None:
        return []
    try:
        tree = engine.extract_tree(body)
        nodes = tree.xpath(
            "//*[self::section or self::div]"
            "[contains(concat(' ', normalize-space(@class), ' '), ' algo-sr ')]"
        )
        if not nodes:
            nodes = tree.xpath(
                "//div[contains(concat(' ', normalize-space(@class), ' '), ' relsrch ')]"
            )
    except Exception:
        return []

    extracted: list[dict[str, str]] = []
    seen_urls: set[Any] = set()
    for node in nodes:
        classes = f" {str(node.get('class') or '').casefold()} "
        if any(token in classes for token in (" ad ", " ads ", " sponsored ")):
            continue
        hrefs = node.xpath(".//h3//a[@href]/@href")
        if not hrefs:
            hrefs = node.xpath(
                ".//*[contains(@class, 'compTitle') or "
                "contains(concat(' ', normalize-space(@class), ' '), ' title ')]"
                "//a[@href]/@href"
            )
        if not hrefs:
            continue
        url = _decode_wrapped_url(str(hrefs[0]))
        key = _source_dedupe_key(url)
        if not url or key in seen_urls:
            continue
        title_nodes = node.xpath(".//h3//a[@href]")
        if not title_nodes:
            title_nodes = node.xpath(
                ".//*[contains(@class, 'compTitle') or "
                "contains(concat(' ', normalize-space(@class), ' '), ' title ')]//a[@href]"
            )
        title_parts: list[str] = []
        if title_nodes:
            title_node = title_nodes[0]
            label = str(title_node.get("aria-label") or "").strip()
            if label:
                title_parts = [label]
            else:
                title_parts = title_node.xpath("./text()")
                if not title_parts:
                    title_parts = title_node.xpath(
                        ".//text()[not(ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' title-url ')])]"
                    )
        body_nodes = node.xpath(
            "(.//*[contains(@class, 's-desc') or contains(@class, 'compText') "
            "or contains(@class, 'Text') or contains(@class, 'abstract')])[1]"
        )
        body_parts = body_nodes[0].xpath(".//text()") if body_nodes else node.xpath(".//p//text()")
        title = " ".join(" ".join(str(part).split()) for part in title_parts).strip()
        snippet = " ".join(" ".join(str(part).split()) for part in body_parts).strip()
        if not title:
            continue
        seen_urls.add(key)
        extracted.append({"href": url, "title": title, "body": snippet})
    extracted.extend(_extract_yahoo_answer_sources(tree, seen_urls))
    return extracted


def _result_has_url_and_title(item: Any) -> bool:
    if isinstance(item, dict):
        return bool((item.get("href") or item.get("url")) and item.get("title"))
    return bool(
        (getattr(item, "href", "") or getattr(item, "url", ""))
        and getattr(item, "title", "")
    )


def _ddgs_text(
    client: Any, *, query: str, engine: str, max_results: int,
    freshness: str, region: str, capture: _ResponseCapture | None,
) -> list[dict[str, Any]]:
    try:
        results = client.text(
            query, backend=engine, max_results=max_results,
            region=region,
            timelimit={"pd": "d", "pw": "w", "pm": "m", "py": "y"}.get(freshness),
        )
    except Exception as error:
        failure = _provider_failure(engine, capture)
        if failure:
            raise RuntimeError(failure) from None
        if _is_no_results_error(error):
            if capture is not None and engine == "yahoo" and capture.status_code == 200:
                fallback = _extract_yahoo_fallback(capture.engine, capture.body)
                if fallback:
                    return fallback
            return []
        raise RuntimeError(f"{engine} search failed ({type(error).__name__})") from None

    failure = _provider_failure(engine, capture)
    # A valid page can include challenge-related script names. Keep parsed
    # results in that case, but never hide a non-200 response.
    if failure and capture is not None and capture.status_code != 200:
        raise RuntimeError(failure) from None
    if (
        capture is not None
        and engine == "yahoo"
        and capture.status_code == 200
        and not any(_result_has_url_and_title(item) for item in results or [])
    ):
        results = _extract_yahoo_fallback(capture.engine, capture.body)
    if failure and not any(_result_has_url_and_title(item) for item in results or []):
        raise RuntimeError(failure) from None
    return results or []


def search(
    query: str, *, engine: str, max_results: int, timeout: float,
    proxy: str | None = None, freshness: str = "",
) -> list[dict[str, Any]]:
    if DDGS is None:
        raise RuntimeError("DDGS search dependency is not installed")
    if engine not in {"duckduckgo", "yahoo", "google", "brave"}:
        raise ValueError("Unsupported metasearch engine")
    if engine not in ENGINES.get("text", {}):
        # DDGS silently switches an unknown/disabled backend to 'auto', which
        # can return encyclopedia matches and mislabel the actual provider.
        raise RuntimeError(f"{engine} search backend is unavailable")
    # Preserve explicit domain constraints even when an upstream engine ignores
    # its own site: syntax. This filters discovery results, never user answers.
    domains = re.findall(r"(?<![-\w])site:([\w.-]+)", query, flags=re.I)
    client = DDGS(proxy=proxy, timeout=max(0.1, timeout))
    capture = _capture_engine_response(client, engine)
    try:
        results = _ddgs_text(
            client,
            query=query,
            engine=engine,
            max_results=max_results,
            freshness=freshness,
            region="cn-zh" if re.search(r"[\u3400-\u9fff]", query) else "us-en",
            capture=capture,
        )
    finally:
        if capture is not None:
            capture.restore()
    normalized = []
    for item in results or []:
        if isinstance(item, dict):
            raw_url = item.get("href") or item.get("url") or ""
            title = item.get("title") or ""
            snippet = item.get("body") or item.get("description") or ""
        else:
            raw_url = getattr(item, "href", "") or getattr(item, "url", "") or ""
            title = getattr(item, "title", "") or ""
            snippet = getattr(item, "body", "") or getattr(item, "description", "") or ""
        url = _decode_wrapped_url(str(raw_url))
        host = (urlsplit(url).hostname or "").lower()
        if domains and not any(host == d.lower() or host.endswith("." + d.lower()) for d in domains):
            continue
        normalized.append({
            "url": url, "title": str(title), "snippet": str(snippet),
            "searchBackend": f"ddgs:{engine}",
        })
    return normalized[:max_results]
