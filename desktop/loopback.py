"""不经系统代理访问桌面后端，保留本地 API 的请求鉴权。"""

from __future__ import annotations

import urllib.parse
import urllib.request


def open_loopback_request(request: str | urllib.request.Request, timeout: float):
    """本地控制请求直连；POST /api/ 仍按原有规则附加鉴权头。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if isinstance(request, urllib.request.Request) and request.get_method() == "POST":
        from agent.local_api_client import local_api_headers

        parsed = urllib.parse.urlsplit(request.full_url)
        if parsed.path.startswith("/api/"):
            for name, value in local_api_headers(request.full_url, timeout=timeout, opener=opener.open).items():
                request.add_header(name, value)
    return opener.open(request, timeout=timeout)
