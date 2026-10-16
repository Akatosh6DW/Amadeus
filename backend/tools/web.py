"""联网工具的最小适配器：权限、查询清理和安全网页传输。"""
from __future__ import annotations

from typing import Any, Callable

from backend.research.planning import safe_outbound_web_query
from tools.safe_web import SafeWebError, read_web_page, web_search


class WebTools:
    def __init__(self, *, write_log: Callable[..., Any]) -> None:
        self.write_log = write_log

    @staticmethod
    def _request_id(args: dict[str, Any]) -> str:
        return str(args.get("requestId") or "")[:120]

    def tool_web_search(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionWebSearch"):
            return {"name": "web_search", "ok": False, "error": "permission_web_search_denied"}
        query = safe_outbound_web_query(args.get("query"), max_length=500)
        if not query:
            return {
                "name": "web_search",
                "ok": False,
                "error": "query_has_insufficient_or_sensitive_context",
            }
        try:
            options = {
                "provider": str(args.get("provider") or settings.get("webSearchProvider") or "duckduckgo"),
                "api_key": str(settings.get("webSearchApiKey") or ""),
                "max_results": int(settings.get("webSearchMaxResults") or 5),
                "read_top_pages": 0,
                "timeout": float(settings.get("webRequestTimeout") or 12),
                "freshness": str(args.get("freshness") or ""),
                "locale": str(args.get("locale") or settings.get("uiLanguage") or "zh-CN"),
                "region": str(args.get("region") or ""),
            }
            result = web_search(query, **options)
            self.write_log(
                "web_search_tool_completed",
                requestId=self._request_id(args),
                query=query[:500],
                provider=str(result.get("provider") or options["provider"])[:40],
            )
            return {"name": "web_search", "ok": True, **result}
        except (SafeWebError, ValueError, TypeError) as error:
            self.write_log(
                "web_search_tool_failed",
                requestId=self._request_id(args),
                query=query[:500],
                errorType=type(error).__name__,
                error=str(error)[:1000],
            )
            return {
                "name": "web_search",
                "ok": False,
                "error": str(error)[:500],
                "query": query[:500],
            }

    def tool_read_web_page(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionWebSearch"):
            return {"name": "read_web_page", "ok": False, "error": "permission_web_search_denied"}
        url = str(args.get("url") or "").strip()
        try:
            page = dict(read_web_page(
                url,
                timeout=float(settings.get("webRequestTimeout") or 12),
                query=str(args.get("researchQuery") or args.get("query") or ""),
            ))
            page["body"] = page.pop("text", page.get("body", ""))
            self.write_log(
                "read_web_page_tool_completed",
                requestId=self._request_id(args),
                url=str(page.get("url") or url)[:500],
                fetchStatus=str(page.get("fetchStatus") or "success")[:40],
            )
            return {"name": "read_web_page", "ok": True,
                    "researchQuery": str(args.get("researchQuery") or args.get("query") or ""),
                    "sources": [{"id": "S1", **page}]}
        except (SafeWebError, ValueError, TypeError) as error:
            self.write_log(
                "read_web_page_tool_failed",
                requestId=self._request_id(args),
                url=url[:500],
                errorType=type(error).__name__,
                error=str(error)[:1000],
            )
            return {
                "name": "read_web_page",
                "ok": False,
                "error": str(error)[:500],
                "url": url[:500],
            }
