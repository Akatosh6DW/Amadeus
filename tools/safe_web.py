#!/usr/bin/env python3
"""Bounded web search and page reading for AMADEUS.

The network boundary in this module deliberately does less than a browser: it
accepts public HTTP(S) text resources, pins DNS results for each connection,
revalidates every redirect, and never executes active content.
"""

from __future__ import annotations

import base64
import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
import zlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any


USER_AGENT = "AMADEUS-Research/1.0 (+local desktop assistant)"
MAX_WIRE_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_PAGE_CHARS = 14_000
MAX_REDIRECTS = 3
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "spm",
}
ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/json",
}
BLOCKED_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".home",
    ".lan",
)
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
SUPPORTED_FRESHNESS = frozenset({"pd", "pw", "pm", "py"})
DUCKDUCKGO_DATE_FILTER = {"pd": "d", "pw": "w", "pm": "m", "py": "y"}


class SafeWebError(RuntimeError):
    """A user-displayable error raised by the bounded network layer."""


class UnsafeUrlError(SafeWebError):
    """The requested URL could reach a non-public or unsupported target."""


class ResponseLimitError(SafeWebError):
    """The remote response exceeded a configured safety bound."""


def canonicalize_public_url(url: str) -> str:
    """Return a stable public-page identity without common tracking fields."""

    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return str(url or "").strip()
    port = parsed.port
    netloc = host
    if port and port not in {80, 443}:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query_items = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
        and not key.lower().startswith("utm_")
    ]
    query_items.sort(key=lambda item: (item[0].casefold(), item[1]))
    query = urllib.parse.urlencode(query_items, doseq=True)
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), netloc, path, query, "")
    )


def page_access_status(text: str) -> str:
    """Classify common non-content pages that may still return HTTP 200."""

    compact = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if re.search(r"enable javascript|javascript (?:is )?required|请启用\s*javascript", compact):
        return "js_required"
    if re.search(r"captcha|robot check|verify you are human|人机验证|验证码", compact):
        return "captcha"
    if re.search(r"access denied|request blocked|forbidden|访问被拒绝|拒绝访问", compact):
        return "blocked"
    if re.search(r"please log in|sign in to continue|登录后(?:查看|继续)|请先登录", compact):
        return "login_required"
    if re.search(r"subscribe to (?:read|continue)|paywall|付费后阅读|订阅后阅读", compact):
        return "paywalled"
    return "success"


def prompt_injection_markers(text: str) -> list[str]:
    """Flag web prose that tries to control the model or tool runtime."""

    patterns = {
        "override_instructions": r"ignore (?:all |the )?(?:previous|prior|above) instructions|忽略(?:此前|之前|以上).{0,8}(?:指令|要求)",
        "system_prompt_request": r"system prompt|developer message|系统提示词|开发者消息",
        "tool_execution_request": r"(?:call|invoke|use).{0,20}(?:tool|browser)|调用.{0,12}(?:工具|浏览器)",
        "secret_request": r"(?:reveal|print|return).{0,20}(?:secret|api key|token)|(?:泄露|输出|显示).{0,12}(?:密钥|令牌|API\s*Key)",
    }
    return [
        label
        for label, pattern in patterns.items()
        if re.search(pattern, str(text or ""), re.I)
    ]


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(address.is_global)


def validate_public_url(url: str) -> tuple[urllib.parse.SplitResult, list[str]]:
    """Validate a URL and resolve it to public IP addresses.

    Every returned address is public. Callers must connect to one of these
    addresses directly instead of resolving the hostname again.
    """

    raw = str(url or "").strip()
    if not raw or len(raw) > 4096:
        raise UnsafeUrlError("URL 为空或过长")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise UnsafeUrlError("URL 格式无效") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("只允许 HTTP 或 HTTPS 网页")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URL 不允许携带用户名或密码")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname or hostname == "localhost" or hostname.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeUrlError("不允许访问本机或内部网络地址")
    if port is not None and port not in {80, 443}:
        raise UnsafeUrlError("只允许网页标准端口 80/443")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeUrlError("不允许访问本机、局域网或保留地址")
        return parsed, [str(literal)]
    if "." not in hostname:
        raise UnsafeUrlError("不允许访问单标签内部主机名")
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
        records = socket.getaddrinfo(ascii_host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (UnicodeError, socket.gaierror, OSError) as error:
        raise SafeWebError(f"域名解析失败：{hostname}") from error
    addresses = list(dict.fromkeys(str(record[4][0]).split("%", 1)[0] for record in records))
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeUrlError("域名解析到了非公网地址，已拒绝访问")
    return parsed, addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _socket_permission_denied(error: BaseException | None) -> bool:
    """Return whether Windows/network policy rejected a direct socket."""

    current = error
    seen: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return True
        if getattr(current, "winerror", None) == 10013:
            return True
        if getattr(current, "errno", None) in {13, 10013}:
            return True
        current = current.__cause__ or current.__context__
    return False


def _validated_hostname_socket(
    host: str,
    port: int,
    timeout: float,
    allowed_addresses: list[str] | tuple[str, ...],
) -> socket.socket:
    """Connect through the hostname while preserving the DNS safety boundary.

    Some Windows network policies reject applications that connect to a
    numeric address even though the same public HTTPS host is permitted. This
    compatibility path lets the OS resolve/connect by hostname, then rejects
    the connection unless the actual peer is one of the public addresses that
    ``validate_public_url`` approved immediately beforehand.
    """

    allowed = {
        str(ipaddress.ip_address(str(value).split("%", 1)[0]))
        for value in allowed_addresses
        if _is_public_ip(str(value))
    }
    if not allowed:
        raise UnsafeUrlError("域名没有可用的已验证公网地址")
    raw = socket.create_connection((host, port), timeout)
    try:
        peer = str(ipaddress.ip_address(str(raw.getpeername()[0]).split("%", 1)[0]))
        if peer not in allowed or not _is_public_ip(peer):
            raise UnsafeUrlError("主机名连接到了未经验证的地址，已拒绝访问")
        return raw
    except BaseException:
        raw.close()
        raise


class _ValidatedHostHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        host: str,
        addresses: list[str],
        port: int,
        timeout: float,
    ):
        super().__init__(host, port=port, timeout=timeout)
        self._addresses = tuple(addresses)

    def connect(self) -> None:
        self.sock = _validated_hostname_socket(
            self.host,
            self.port,
            self.timeout,
            self._addresses,
        )


class _ValidatedHostHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        addresses: list[str],
        port: int,
        timeout: float,
    ):
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._addresses = tuple(addresses)

    def connect(self) -> None:
        raw = _validated_hostname_socket(
            self.host,
            self.port,
            self.timeout,
            self._addresses,
        )
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _read_connection_response(
    connection: http.client.HTTPConnection,
    *,
    method: str,
    path: str,
    body: bytes | None,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(65_536, MAX_WIRE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_WIRE_BYTES:
            raise ResponseLimitError("网页响应超过 2 MB 限制")
    return (
        response.status,
        response_headers,
        _decode_bounded(
            b"".join(chunks),
            response_headers.get("content-encoding", ""),
        ),
    )


def _decode_bounded(payload: bytes, encoding: str) -> bytes:
    normalized = str(encoding or "").strip().lower()
    if normalized in {"", "identity"}:
        decoded = payload
    elif normalized in {"gzip", "x-gzip"}:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(payload, MAX_TEXT_BYTES + 1)
        if len(decoded) > MAX_TEXT_BYTES:
            raise ResponseLimitError("网页解压后超过 2 MB 限制")
        decoded += decoder.flush(MAX_TEXT_BYTES + 1 - len(decoded))
    elif normalized == "deflate":
        try:
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(payload, MAX_TEXT_BYTES + 1)
        except zlib.error:
            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            decoded = decoder.decompress(payload, MAX_TEXT_BYTES + 1)
    else:
        raise SafeWebError(f"不支持网页压缩格式：{normalized}")
    if len(decoded) > MAX_TEXT_BYTES:
        raise ResponseLimitError("网页解压后超过 2 MB 限制")
    return decoded


def _request_once(
    url: str,
    *,
    method: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    parsed, addresses = validate_public_url(url)
    host = (parsed.hostname or "").encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Search engines may return an IRI whose path contains Chinese/Japanese
    # characters. ``http.client`` requires an ASCII request target, so encode
    # the path/query while preserving existing escapes and URI delimiters.
    encoded_path = urllib.parse.quote(
        parsed.path or "/",
        safe="/:@-._~!$&'()*+,;=%",
    )
    encoded_query = urllib.parse.quote(
        parsed.query,
        safe="=&?/:;+,%@-._~!$'()*",
    )
    path = urllib.parse.urlunsplit(("", "", encoded_path, encoded_query, ""))
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.8",
        "Accept-Encoding": "identity",
        "Connection": "close",
        **headers,
    }
    last_error: Exception | None = None
    permission_denied = False
    for address in addresses:
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHTTPSConnection(host, address, port, timeout)
        else:
            connection = _PinnedHTTPConnection(host, address, port, timeout)
        try:
            return _read_connection_response(
                connection,
                method=method,
                path=path,
                body=body,
                headers=request_headers,
            )
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            last_error = error
            permission_denied = permission_denied or _socket_permission_denied(error)
        finally:
            connection.close()
    if permission_denied:
        if parsed.scheme == "https":
            hostname_connection: http.client.HTTPConnection = (
                _ValidatedHostHTTPSConnection(host, addresses, port, timeout)
            )
        else:
            hostname_connection = _ValidatedHostHTTPConnection(
                host,
                addresses,
                port,
                timeout,
            )
        try:
            return _read_connection_response(
                hostname_connection,
                method=method,
                path=path,
                body=body,
                headers=request_headers,
            )
        except UnsafeUrlError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            last_error = error
        finally:
            hostname_connection.close()
    raise SafeWebError(f"网页连接失败：{last_error or '所有公网地址均不可用'}")


def safe_http_request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 12,
) -> dict[str, Any]:
    """Fetch a bounded public text response with redirect revalidation."""

    current = str(url or "").strip()
    current_method = method.upper()
    current_body = body
    for redirect_count in range(MAX_REDIRECTS + 1):
        status, response_headers, payload = _request_once(
            current,
            method=current_method,
            body=current_body,
            headers=dict(headers or {}),
            timeout=max(2.0, min(float(timeout), 30.0)),
        )
        if status in {301, 302, 303, 307, 308}:
            if redirect_count >= MAX_REDIRECTS:
                raise SafeWebError("网页重定向次数过多")
            location = response_headers.get("location", "").strip()
            if not location:
                raise SafeWebError("网页返回了无目标重定向")
            redirected = urllib.parse.urljoin(current, location)
            if urllib.parse.urlsplit(current).scheme == "https" and urllib.parse.urlsplit(redirected).scheme == "http":
                raise UnsafeUrlError("拒绝从 HTTPS 降级重定向到 HTTP")
            current = redirected
            validate_public_url(current)
            if status == 303 or (status in {301, 302} and current_method == "POST"):
                current_method, current_body = "GET", None
            continue
        if status < 200 or status >= 300:
            raise SafeWebError(f"网页返回 HTTP {status}")
        content_type = response_headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise SafeWebError(f"不读取此内容类型：{content_type or 'unknown'}")
        return {
            "url": current,
            "status": status,
            "headers": response_headers,
            "body": payload,
            "contentType": content_type,
        }
    raise SafeWebError("网页重定向处理失败")


class _ReadableHtmlParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "article", "section", "main", "li", "h1", "h2", "h3", "h4", "blockquote", "br"}
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        if self._in_title:
            self.title_parts.append(cleaned)
        self.text_parts.append(cleaned + " ")


def extract_readable_text(payload: bytes, content_type: str, charset_hint: str = "") -> tuple[str, str]:
    encodings = [charset_hint, "utf-8", "gb18030", "shift_jis", "latin-1"]
    text = ""
    for encoding in dict.fromkeys(item.strip() for item in encodings if item.strip()):
        try:
            text = payload.decode(encoding)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    if not text:
        text = payload.decode("utf-8", errors="replace")
    if content_type == "application/json":
        try:
            value = json.loads(text)
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
        return "", text[:MAX_PAGE_CHARS]
    if content_type in {"text/plain", "text/markdown"}:
        return "", re.sub(r"\s+", " ", text).strip()[:MAX_PAGE_CHARS]
    parser = _ReadableHtmlParser()
    parser.feed(text)
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()[:300]
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.text_parts).splitlines()]
    readable = "\n".join(line for line in lines if len(line) >= 2)
    return title, html.unescape(readable)[:MAX_PAGE_CHARS]


def read_web_page(url: str, *, timeout: float = 12) -> dict[str, Any]:
    response = safe_http_request(url, timeout=timeout)
    content_header = response["headers"].get("content-type", "")
    charset_match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_header, re.I)
    title, text = extract_readable_text(
        response["body"], response["contentType"], charset_match.group(1) if charset_match else ""
    )
    if len(text) < 40:
        raise SafeWebError("网页没有提取出足够的可读正文")
    fetch_status = page_access_status(text)
    if fetch_status != "success":
        raise SafeWebError(f"网页正文不可用：{fetch_status}")
    parsed = urllib.parse.urlsplit(response["url"])
    return {
        "requestedUrl": str(url or "").strip(),
        "url": canonicalize_public_url(response["url"]),
        "finalUrl": response["url"],
        "domain": parsed.hostname or "",
        "title": title or parsed.hostname or response["url"],
        "text": text,
        "accessedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mimeType": response["contentType"],
        "fetchStatus": "success",
        "truncated": len(text) >= MAX_PAGE_CHARS,
        "promptInjectionMarkers": prompt_injection_markers(text),
    }


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and classes & {"result__a", "result-link"}:
            self._finish_current()
            href = values.get("href") or ""
            if href.startswith("//"):
                href = "https:" + href
            parsed = urllib.parse.urlsplit(href)
            query = urllib.parse.parse_qs(parsed.query)
            target = query.get("uddg", [href])[0]
            self._current = {"url": urllib.parse.unquote(target), "title": "", "snippet": ""}
            self._capture = "title"
        elif self._current is not None and classes & {"result__snippet", "result-snippet"}:
            self._capture = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None and self._capture == "title":
            self._capture = ""
        if tag in {"div", "article"} and self._current is not None and self._current.get("title"):
            self._finish_current()

    def handle_data(self, data: str) -> None:
        if self._current is not None and self._capture:
            cleaned = re.sub(r"\s+", " ", data).strip()
            if cleaned:
                key = self._capture
                self._current[key] = (self._current.get(key, "") + " " + cleaned).strip()

    def close(self) -> None:
        super().close()
        self._finish_current()

    def _finish_current(self) -> None:
        if self._current is not None and self._current.get("title"):
            hint, published_timestamp = search_result_date_metadata(
                self._current.get("title", ""),
                self._current.get("snippet", ""),
            )
            if hint:
                self._current["publishedAt"] = hint
                self._current["dateEvidence"] = hint
            if published_timestamp:
                self._current["publishedTimestamp"] = published_timestamp
            self.results.append(self._current)
        self._current = None
        self._capture = ""


_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)
_SEARCH_DATE_PATTERNS = (
    re.compile(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b"),
    re.compile(r"(?:19|20)\d{2}年\s*\d{1,2}月\s*\d{1,2}日?"),
    re.compile(rf"\b(?:{_MONTH_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{{2}}\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_PATTERN})\s+(?:19|20)\d{{2}}\b", re.I),
    re.compile(r"\b(?:today|yesterday|\d+\s+(?:minutes?|hours?|days?|weeks?|months?)\s+ago)\b", re.I),
    re.compile(r"(?:刚刚|今天|昨日|昨天|\d+\s*(?:分钟|小时|天|周|个月)前)"),
    re.compile(r"\b\d{1,2}月\s*\d{1,2}日\b"),
)


def search_result_date_metadata(*values: Any) -> tuple[str, str]:
    """Return a provider-visible date clue and a normalized UTC timestamp."""

    text = re.sub(r"\s+", " ", " ".join(str(value or "") for value in values)).strip()
    hint = ""
    for pattern in _SEARCH_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            hint = match.group(0).strip()
            break
    if not hint:
        return "", ""
    now = datetime.now(timezone.utc)
    normalized = re.sub(r"(?i)(\d)(?:st|nd|rd|th)\b", r"\1", hint).strip()
    parsed: datetime | None = None
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%B %d, %Y", "%B %d %Y",
        "%b %d, %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
    ):
        try:
            parsed = datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if parsed is None:
        chinese = re.fullmatch(r"((?:19|20)\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日?", normalized)
        month_day = re.fullmatch(r"(\d{1,2})月\s*(\d{1,2})日", normalized)
        if chinese:
            try:
                parsed = datetime(
                    int(chinese.group(1)), int(chinese.group(2)), int(chinese.group(3)),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                parsed = None
        elif month_day:
            try:
                parsed = datetime(now.year, int(month_day.group(1)), int(month_day.group(2)), tzinfo=timezone.utc)
                if parsed > now + timedelta(days=2):
                    parsed = parsed.replace(year=now.year - 1)
            except ValueError:
                parsed = None
    if parsed is None:
        relative = re.fullmatch(
            r"(\d+)\s*(minutes?|hours?|days?|weeks?|months?)\s+ago",
            normalized,
            re.I,
        )
        relative_zh = re.fullmatch(r"(\d+)\s*(分钟|小时|天|周|个月)前", normalized)
        if normalized.casefold() in {"today", "今天", "刚刚"}:
            parsed = now
        elif normalized.casefold() in {"yesterday", "昨日", "昨天"}:
            parsed = now - timedelta(days=1)
        elif relative:
            amount = int(relative.group(1))
            unit = relative.group(2).lower()
            days = amount * (30 if unit.startswith("month") else 7 if unit.startswith("week") else 1)
            parsed = now - (timedelta(days=days) if unit.startswith(("day", "week", "month")) else timedelta(
                hours=amount if unit.startswith("hour") else 0,
                minutes=amount if unit.startswith("minute") else 0,
            ))
        elif relative_zh:
            amount = int(relative_zh.group(1))
            unit = relative_zh.group(2)
            parsed = now - (
                timedelta(days=amount * (30 if unit == "个月" else 7 if unit == "周" else 1))
                if unit in {"天", "周", "个月"}
                else timedelta(hours=amount if unit == "小时" else 0, minutes=amount if unit == "分钟" else 0)
            )
    return hint, parsed.isoformat(timespec="seconds") if parsed is not None else ""


def _search_duckduckgo(
    query: str,
    max_results: int,
    timeout: float,
    freshness: str = "",
) -> list[dict[str, str]]:
    params = {"q": query, "kl": "wt-wt"}
    date_filter = DUCKDUCKGO_DATE_FILTER.get(str(freshness or "").strip().lower())
    if date_filter:
        params["df"] = date_filter
    url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode(params)
    response = safe_http_request(url, timeout=timeout)
    parser = _DuckDuckGoParser()
    content = response["body"].decode("utf-8", errors="replace")
    parser.feed(content)
    parser.close()
    if not parser.results and ("anomaly.js" in content or "anomaly-modal" in content):
        raise SafeWebError("搜索服务要求验证码（captcha），尚未取得搜索结果")
    return parser.results[:max_results]


class _BingParser(HTMLParser):
    """Extract ordinary Bing result cards without executing page scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._in_result = False
        self._h2_depth = 0
        self._capture = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "li" and "b_algo" in classes:
            self._finish_current()
            self._current = {"url": "", "title": "", "snippet": ""}
            self._in_result = True
            return
        if not self._in_result or self._current is None:
            return
        if tag == "h2":
            self._h2_depth += 1
        elif tag == "a" and self._h2_depth and not self._current.get("url"):
            href = str(values.get("href") or "")
            if href.startswith(("http://", "https://")):
                self._current["url"] = _decode_bing_target(href)
                self._capture = "title"
        elif tag == "p" and self._current.get("url"):
            self._capture = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if not self._in_result:
            return
        if tag == "a" and self._capture == "title":
            self._capture = ""
        elif tag == "p" and self._capture == "snippet":
            self._capture = ""
        elif tag == "h2" and self._h2_depth:
            self._h2_depth -= 1
        elif tag == "li":
            self._finish_current()

    def handle_data(self, data: str) -> None:
        if self._current is None or not self._capture:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if cleaned:
            key = self._capture
            self._current[key] = (self._current.get(key, "") + " " + cleaned).strip()

    def _finish_current(self) -> None:
        if self._current and self._current.get("url") and self._current.get("title"):
            self.results.append(self._current)
        self._current = None
        self._in_result = False
        self._h2_depth = 0
        self._capture = ""

    def close(self) -> None:
        super().close()
        self._finish_current()


def _search_bing(query: str, max_results: int, timeout: float) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({
        "q": query,
        "count": max_results,
        "setlang": "zh-Hans",
    })
    response = safe_http_request(url, timeout=timeout)
    parser = _BingParser()
    parser.feed(response["body"].decode("utf-8", errors="replace"))
    parser.close()
    return parser.results[:max_results]


class _BaiduParser(HTMLParser):
    """Extract Baidu's ordinary result headings for Chinese-query fallback."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._h3_depth = 0
        self._capture = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "h3" and (
            "t" in classes
            or "c-title" in classes
            or "c-title-en" in classes
            or any(name.startswith("c-title") for name in classes)
        ):
            self._current = {"url": "", "title": "", "snippet": ""}
            self._h3_depth = 1
            return
        if self._current is None:
            return
        if tag == "h3":
            self._h3_depth += 1
        elif tag == "a" and not self._current.get("url"):
            href = str(values.get("href") or "")
            if href.startswith(("http://", "https://")):
                self._current["url"] = href
                self._capture = True

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag == "a":
            self._capture = False
        elif tag == "h3":
            self._h3_depth -= 1
            if self._h3_depth <= 0:
                if self._current.get("url") and self._current.get("title"):
                    self.results.append(self._current)
                self._current = None
                self._capture = False
                self._h3_depth = 0

    def handle_data(self, data: str) -> None:
        if self._current is None or not self._capture:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if cleaned:
            self._current["title"] = (
                self._current.get("title", "") + " " + cleaned
            ).strip()


def _search_baidu(query: str, max_results: int, timeout: float) -> list[dict[str, str]]:
    url = "https://www.baidu.com/s?" + urllib.parse.urlencode({"wd": query, "rn": max_results})
    response = safe_http_request(url, timeout=timeout)
    parser = _BaiduParser()
    parser.feed(response["body"].decode("utf-8", errors="replace"))
    parser.close()
    return parser.results[:max_results]


class _YahooParser(HTMLParser):
    """Extract Yahoo result titles and their direct destination links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._pending_url = ""
        self._current: dict[str, str] | None = None
        self._h3_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a":
            href = str(values.get("href") or "")
            if href.startswith(("http://", "https://")):
                self._pending_url = href
        elif tag == "h3" and "title" in classes and self._pending_url:
            self._current = {"url": self._pending_url, "title": "", "snippet": ""}
            self._h3_depth = 1
        elif tag == "h3" and self._current is not None:
            self._h3_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._current is not None:
            self._h3_depth -= 1
            if self._h3_depth <= 0:
                if self._current.get("title"):
                    self.results.append(self._current)
                self._current = None
                self._h3_depth = 0
        elif tag == "a" and self._current is None:
            self._pending_url = ""

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if cleaned:
            self._current["title"] = (
                self._current.get("title", "") + " " + cleaned
            ).strip()


def _search_yahoo(query: str, max_results: int, timeout: float) -> list[dict[str, str]]:
    url = "https://search.yahoo.com/search?" + urllib.parse.urlencode({
        "p": query,
        "n": max_results,
    })
    response = safe_http_request(url, timeout=timeout)
    parser = _YahooParser()
    parser.feed(response["body"].decode("utf-8", errors="replace"))
    parser.close()
    return parser.results[:max_results]


def _decode_bing_target(url: str) -> str:
    """Turn Bing's public click wrapper back into the cited destination URL."""

    parsed = urllib.parse.urlsplit(str(url or ""))
    if (parsed.hostname or "").lower() not in {"bing.com", "www.bing.com"}:
        return url
    encoded = urllib.parse.parse_qs(parsed.query).get("u", [""])[0]
    if not encoded.startswith("a1"):
        return url
    payload = encoded[2:]
    try:
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return url
    return decoded if decoded.startswith(("http://", "https://")) else url


def _search_wikipedia(query: str, max_results: int, timeout: float, locale: str) -> list[dict[str, str]]:
    language = str(locale or "en").strip().lower().replace("_", "-").split("-")[0]
    if not re.fullmatch(r"[a-z]{2,3}", language):
        raise SafeWebError("百科检索 locale 必须使用语言代码，例如 zh、ja、en")
    origin = f"https://{language}.wikipedia.org"
    params = {"action": "query", "list": "search", "srsearch": query,
              "srlimit": max_results, "srnamespace": 0, "format": "json"}
    response = safe_http_request(
        origin + "/w/api.php?" + urllib.parse.urlencode(params), timeout=timeout,
    )
    data = json.loads(response["body"])
    if not isinstance(data, dict) or data.get("error"):
        raise SafeWebError("百科检索接口返回错误，尚未取得搜索结果")
    results = []
    for item in data.get("query", {}).get("search", [])[:max_results]:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
            continue
        results.append({
            "url": origin + "/wiki/" + urllib.parse.quote(item["title"].replace(" ", "_"), safe=""),
            "title": item["title"],
            "snippet": html.unescape(re.sub(r"<[^>]+>", "", str(item.get("snippet") or ""))),
        })
    return results


def _search_brave(query: str, api_key: str, max_results: int, timeout: float, freshness: str) -> list[dict[str, str]]:
    if not api_key:
        raise SafeWebError("Brave Search API Key 未配置")
    params: dict[str, Any] = {"q": query, "count": max_results, "safesearch": "moderate", "text_decorations": "false"}
    if freshness in {"pd", "pw", "pm", "py"}:
        params["freshness"] = freshness
    response = safe_http_request(
        "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params),
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        timeout=timeout,
    )
    data = json.loads(response["body"].decode("utf-8"))
    raw_results = data.get("web", {}).get("results", []) if isinstance(data, dict) else []
    return [
        {
            "url": str(item.get("url") or ""),
            "title": str(item.get("title") or ""),
            "snippet": re.sub(r"<[^>]+>", "", str(item.get("description") or "")),
            "publishedAt": str(item.get("page_age") or ""),
        }
        for item in raw_results[:max_results]
        if isinstance(item, dict) and item.get("url")
    ]


def _page_read_priority(
    source: dict[str, Any],
    query: str,
    position: int,
    freshness: str = "",
) -> float:
    domain = str(source.get("domain") or "").lower()
    title = str(source.get("title") or "").lower()
    score = -position * 0.05
    latin_terms = {item.lower() for item in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", query)}
    if any(term in domain for term in latin_terms):
        score += 3.0
    if re.search(r"(?:^|\.)(?:docs?|documentation|developer|developers|api-docs)[.-]", domain):
        score += 2.0
    if re.search(r"官方|official|documentation|docs", title, re.I):
        score += 0.8
    if any(host in domain for host in ("blog.csdn.net", "zhuanlan.zhihu.com", "tieba.baidu.com")):
        score -= 1.0
    requested_freshness = str(freshness or "").strip().lower()
    if requested_freshness in SUPPORTED_FRESHNESS:
        _, published_timestamp = search_result_date_metadata(
            source.get("publishedTimestamp"),
            source.get("publishedAt"),
            source.get("dateEvidence"),
            source.get("title"),
            source.get("snippet"),
        )
        try:
            published = datetime.fromisoformat(published_timestamp)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 86_400)
            window_days = {"pd": 1.5, "pw": 8.0, "pm": 32.0, "py": 370.0}[requested_freshness]
            if age_days <= window_days:
                score += 0.5 + 2.3 * (1.0 - min(1.0, age_days / window_days))
            else:
                score -= 3.2
        except (TypeError, ValueError):
            # An unparsed date clue is useful to the reviewer but cannot be
            # treated as proof that the page falls inside the requested window.
            pass
    return score


def web_search(
    query: str,
    *,
    provider: str = "duckduckgo",
    api_key: str = "",
    max_results: int = 5,
    read_top_pages: int = 2,
    timeout: float = 12,
    freshness: str = "",
    locale: str = "",
    region: str = "",
    source_policy_id: str = "",
) -> dict[str, Any]:
    clean_query = re.sub(r"\s+", " ", str(query or "")).strip()[:500]
    if len(clean_query) < 2:
        raise SafeWebError("搜索词过短")
    provider = str(provider or "duckduckgo").strip().lower()
    max_results = max(1, min(int(max_results), 8))
    read_top_pages = max(0, min(int(read_top_pages), 3, max_results))
    cache_key = json.dumps(
        [
            provider,
            clean_query,
            max_results,
            read_top_pages,
            freshness,
            locale,
            region,
            source_policy_id,
            USER_AGENT,
        ],
        ensure_ascii=False,
    )
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            cached_result = json.loads(json.dumps(cached[1], ensure_ascii=False))
            cached_result["cacheHit"] = True
            return cached_result
    provider_attempts: list[str] = []
    provider_errors: list[dict[str, str]] = []
    effective_provider = provider
    requested_freshness = str(freshness or "").strip().lower()
    if provider == "wikipedia":
        provider_attempts.append("wikipedia")
        raw_results = _search_wikipedia(clean_query, max_results, timeout, locale)
    elif provider == "brave":
        provider_attempts.append("brave")
        raw_results = _search_brave(clean_query, api_key, max_results, timeout, requested_freshness)
    elif provider == "yahoo":
        provider_attempts.append("yahoo")
        raw_results = _search_yahoo(clean_query, max_results, timeout)
    elif provider == "duckduckgo":
        # The free release must work on networks where one public search host is
        # blocked. Keep the total search-stage deadline close to the configured
        # timeout and fail over to an independent endpoint before giving up.
        raw_results: list[dict[str, str]] = []
        providers = [("duckduckgo", _search_duckduckgo)]
        if re.search(r"[\u3400-\u9fff]", clean_query):
            providers.append(("baidu", _search_baidu))
            providers.append(("yahoo", _search_yahoo))
        providers.append(("bing", _search_bing))
        per_provider_timeout = max(
            3.0,
            min(7.0, float(timeout) / max(1, len(providers))),
        )
        for candidate, search_function in providers:
            provider_attempts.append(candidate)
            try:
                candidate_results = (
                    search_function(clean_query, max_results, per_provider_timeout, requested_freshness)
                    if candidate == "duckduckgo"
                    else search_function(clean_query, max_results, per_provider_timeout)
                )
                if candidate_results:
                    raw_results = candidate_results
                    effective_provider = candidate
                    break
                provider_errors.append({
                    "provider": candidate,
                    "error": "搜索服务没有返回结果",
                })
            except (SafeWebError, OSError, ValueError, json.JSONDecodeError) as error:
                provider_errors.append({
                    "provider": candidate,
                    "error": str(error)[:240],
                })
        if not raw_results:
            detail = "；".join(
                f"{item['provider']}：{item['error']}" for item in provider_errors
            )
            raise SafeWebError(f"所有搜索线路均失败（{detail or '没有可用结果'}）")
    else:
        raise SafeWebError("暂不支持该搜索服务；请选择 duckduckgo、yahoo、brave 或 wikipedia")
    date_filter_supported = effective_provider in {"duckduckgo", "brave"}
    applied_freshness = (
        requested_freshness
        if date_filter_supported and requested_freshness in SUPPORTED_FRESHNESS
        else ""
    )
    date_filter = {
        "requestedFreshness": requested_freshness,
        "appliedFreshness": applied_freshness,
        "supported": date_filter_supported,
        "provider": effective_provider,
        "parameter": (
            "df" if effective_provider == "duckduckgo"
            else "freshness" if effective_provider == "brave"
            else ""
        ),
        "fallbackUnfiltered": bool(requested_freshness and not applied_freshness),
    }
    sources: list[dict[str, Any]] = []
    for item in raw_results:
        try:
            parsed, _ = validate_public_url(str(item.get("url") or ""))
        except SafeWebError:
            continue
        date_hint, published_timestamp = search_result_date_metadata(
            item.get("publishedAt"),
            item.get("dateEvidence"),
            item.get("title"),
            item.get("snippet"),
        )
        source = {
            "id": f"S{len(sources) + 1}",
            "title": re.sub(r"\s+", " ", html.unescape(str(item.get("title") or parsed.hostname or "网页"))).strip()[:300],
            "url": canonicalize_public_url(
                urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
            ),
            "domain": parsed.hostname or "",
            "snippet": re.sub(r"\s+", " ", html.unescape(str(item.get("snippet") or ""))).strip()[:800],
            "publishedAt": str(item.get("publishedAt") or date_hint)[:80],
            "publishedTimestamp": str(item.get("publishedTimestamp") or published_timestamp)[:80],
            "dateEvidence": str(item.get("dateEvidence") or date_hint)[:120],
            "searchProvider": effective_provider,
            "requestedFreshness": requested_freshness,
            "appliedFreshness": applied_freshness,
            "dateFilterSupported": date_filter_supported,
            "fetchStatus": "not_fetched",
        }
        sources.append(source)
        if len(sources) >= max_results:
            break
    if not sources:
        raise SafeWebError("搜索服务没有返回可验证的公网来源")
    read_candidates = sorted(
        enumerate(sources),
        key=lambda pair: _page_read_priority(pair[1], clean_query, pair[0], requested_freshness),
        reverse=True,
    )
    page_timeout = max(3.0, min(float(timeout), 8.0))
    read_deadline = time.monotonic() + read_top_pages * page_timeout
    readable_pages = 0
    for _, source in read_candidates:
        remaining = read_deadline - time.monotonic()
        if readable_pages >= read_top_pages or remaining <= 0:
            break
        try:
            # A fast 403/empty page is an attempt, not a readable result. Try
            # another returned source within the same total reading budget.
            page = read_web_page(source["url"], timeout=min(page_timeout, remaining))
            source["url"] = page["url"]
            source["domain"] = page["domain"]
            source["title"] = page["title"] or source["title"]
            source["body"] = page["text"]
            source["accessedAt"] = page["accessedAt"]
            source["requestedUrl"] = str(page.get("requestedUrl") or source["url"])
            source["finalUrl"] = str(page.get("finalUrl") or page["url"])
            source["mimeType"] = str(page.get("mimeType") or "text/html")
            source["fetchStatus"] = str(page.get("fetchStatus") or "success")
            source["truncated"] = bool(page.get("truncated"))
            source["promptInjectionMarkers"] = list(page.get("promptInjectionMarkers") or [])
            readable_pages += 1
        except SafeWebError as error:
            source["bodyError"] = str(error)[:240]
            status_match = re.search(
                r"(js_required|captcha|blocked|login_required|paywalled)",
                str(error),
            )
            source["fetchStatus"] = status_match.group(1) if status_match else "fetch_error"
    result = {
        "query": clean_query,
        "provider": effective_provider,
        "requestedProvider": provider,
        "providerAttempts": provider_attempts,
        "providerErrors": provider_errors,
        "dateFilter": date_filter,
        "sources": sources,
        "searchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cacheHit": False,
    }
    with _CACHE_LOCK:
        _CACHE[cache_key] = (
            time.monotonic() + (20 if freshness in {"pd", "pw"} else 90),
            result,
        )
    return json.loads(json.dumps(result, ensure_ascii=False))
