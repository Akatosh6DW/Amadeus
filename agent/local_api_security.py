"""Trust boundary for the desktop loopback HTTP API, before any business work."""
from __future__ import annotations

import hmac
import ipaddress
from typing import Any
from urllib.parse import urlsplit

SESSION_HEADER = "X-Amadeus-Session"
BOOTSTRAP_HEADER = "X-Amadeus-Client"
SESSION_PATH = "/api/local-session"


def _authority(value: str) -> tuple[str, int] | None:
    if not value or any(char in value for char in "/\\?#@,% \t\r\n"):
        return None
    try:
        parsed = urlsplit("//" + value)
        if not parsed.hostname:
            return None
        return parsed.hostname.lower(), parsed.port or 80
    except ValueError:
        return None


def _origin(value: str, *, referer: bool = False) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "http" or (not referer and (parsed.path or parsed.query or parsed.fragment)):
            return None
        return _authority(parsed.netloc)
    except ValueError:
        return None


def request_rejection(headers: Any, *, server_address: tuple, client_address: tuple,
                      method: str, path: str, session_token: str) -> tuple[int, str] | None:
    """Header facts establish origin; a non-simple secret header gates writes.

    Native loopback clients may omit browser headers, but still need the startup
    token for POST. An Origin or Fetch Metadata declaration can never be waived
    merely because the requester also supplied the token.
    """
    try:
        if not ipaddress.ip_address(client_address[0]).is_loopback:
            return 403, "local_client_required"
    except (ValueError, IndexError):
        return 403, "local_client_required"
    for name in ("Host", "Origin", "Referer", "Sec-Fetch-Site", "Content-Type", SESSION_HEADER, BOOTSTRAP_HEADER):
        values = headers.get_all(name, []) if hasattr(headers, "get_all") else ([headers[name]] if name in headers else [])
        if len(values) > 1:
            return 400, "ambiguous_request_headers"
    host = _authority(headers.get("Host", ""))
    bound_host, port = str(server_address[0]).lower(), int(server_address[1])
    allowed_hosts = {bound_host}
    if bound_host in {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}:
        allowed_hosts = {"127.0.0.1", "::1", "localhost"}
    if host is None or host[0] not in allowed_hosts or host[1] != port:
        return 403, "untrusted_host"
    if not path.startswith("/") or path.startswith("//"):
        return 400, "invalid_request_target"
    origin = headers.get("Origin")
    if origin is not None and _origin(origin) != host:
        return 403, "untrusted_origin"
    referer = headers.get("Referer")
    if origin is None and referer is not None and _origin(referer, referer=True) != host:
        return 403, "untrusted_referer"
    fetch_site = headers.get("Sec-Fetch-Site", "").lower()
    if fetch_site not in {"", "none", "same-origin"}:
        return 403, "cross_origin_request"
    if method == "OPTIONS":
        return 403, "cross_origin_preflight_denied"
    if method == "POST":
        if headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            return 415, "json_content_type_required"
        if headers.get("Transfer-Encoding"):
            return 400, "unsupported_transfer_encoding"
        supplied = headers.get(SESSION_HEADER, "")
        if not session_token or not hmac.compare_digest(supplied.encode("utf-8"), session_token.encode("utf-8")):
            return 403, "local_session_required"
    if method == "GET" and urlsplit(path).path == SESSION_PATH:
        if headers.get(BOOTSTRAP_HEADER) != "desktop":
            return 403, "local_session_bootstrap_required"
    return None
