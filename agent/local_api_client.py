"""Session bootstrap for native clients of the local desktop service."""
from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlsplit

from agent.local_api_security import BOOTSTRAP_HEADER, SESSION_HEADER, SESSION_PATH


def local_api_headers(base_url: str, *, timeout: float = 5, opener=None) -> dict[str, str]:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("local API credentials require a loopback HTTP origin")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    request = urllib.request.Request(origin + SESSION_PATH, headers={BOOTSTRAP_HEADER: "desktop"})
    open_request = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    with open_request(request, timeout=timeout) as response:
        payload = json.load(response)
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("local API session token missing")
    return {SESSION_HEADER: token}
