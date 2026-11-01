"""联网工具的最小适配器：权限、查询清理和安全网页传输。"""
from __future__ import annotations

from typing import Any, Callable
import time
import re

from backend.research.deepseek_search import (
    api_key_for_settings,
    fetch as deepseek_fetch,
    search as deepseek_search,
)
from backend.research.planning import safe_outbound_web_query
from tools.safe_web import SafeWebError, read_web_page, web_search, _normalize_domains, _domain_matches_url


class WebTools:
    def __init__(self, *, write_log: Callable[..., Any]) -> None:
        self.write_log = write_log

    @staticmethod
    def _request_id(args: dict[str, Any]) -> str:
        return str(args.get("requestId") or "")[:120]

    @staticmethod
    def _read_top_pages(settings: dict[str, Any]) -> int:
        """Return the bounded page-read setting without losing an explicit zero."""

        value = settings.get("webReadTopPages", 2)
        try:
            return max(0, min(int(value), 3))
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _readable_body_count(result: dict[str, Any]) -> int:
        sources = result.get("sources")
        if not isinstance(sources, list):
            return 0
        return sum(
            1
            for source in sources
            if isinstance(source, dict) and str(source.get("body") or "").strip()
        )

    @staticmethod
    def _provider(settings: dict[str, Any], args: dict[str, Any] | None = None) -> str:
        """Return the saved provider; model tool arguments cannot override it."""

        configured = str(settings.get("webSearchProvider") or "deepseek").strip().lower()
        if configured not in {"deepseek", "duckduckgo", "bing", "brave"}:
            configured = "deepseek"
        return configured

    def tool_web_search(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        # A provider may ignore site: entirely (observed with regional Bing).
        # Preserve the explicit query constraint before treating hits as evidence.
        try:
            requested_domains = _normalize_domains(args.get('domains'))
            query = str(args.get('query') or '')
            # regex-purpose: format
            sites = re.findall(r'(?:^|\s)site:([a-zA-Z0-9.-]+)(?:/\S*)?(?=\s|$)', query, re.I)
            # regex-purpose: format
            if not requested_domains and len(sites) == 1 and not re.search(r'\bOR\b|[|()]', query):
                requested_domains = _normalize_domains(sites)
        except SafeWebError as error:
            return {'name': 'web_search', 'ok': False, 'error': str(error)}
        primary = self._provider(settings)
        fallback = str(settings.get("webSearchFallbackProvider", "bing") or "none").lower()
        providers = [primary]
        if fallback in {"bing", "duckduckgo"} and fallback != primary:
            providers.append(fallback)
        attempts: list[str] = []
        failures: list[dict[str, Any]] = []
        timeout = float(settings.get('webRequestTimeout') or 12)
        deadline = time.monotonic() + min(30, timeout * len(providers))
        result = {'name': 'web_search', 'ok': False, 'error': 'search_timeout', 'sources': []}
        for provider in providers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = self._tool_web_search_once(
                {**settings, 'webRequestTimeout': min(timeout, remaining)}, args, provider=provider)
            attempts.extend(result.get("providerAttempts") or [provider])
            if result.get("error") in {"permission_web_search_denied", "query_has_insufficient_or_sensitive_context"}:
                return result
            if requested_domains and result.get('ok'):
                raw_sources = result.get('sources') or []
                result['sources'] = [source for source in raw_sources
                                     if _domain_matches_url(source.get('url', ''), requested_domains)]
                result['domains'] = {'requested': list(requested_domains), 'supported': True,
                                     'applied': True, 'matchedResults': len(result['sources']),
                                     'discardedResults': len(raw_sources) - len(result['sources'])}
                if raw_sources and not result['sources']:
                    result['error'] = 'requested_domain_not_found'
                    result['ok'] = False
            sources = result.get("sources") or []
            if result.get("ok") and sources:
                break
            failures.append({"provider": provider, "reason": result.get("error") or "empty_results"})
            if provider != providers[-1]:
                self.write_log("web_search_fallback", requestId=self._request_id(args),
                               primaryProvider=primary, fallbackProvider=providers[-1],
                               reason=failures[-1]["reason"])
        result.update(primaryProvider=primary, providerAttempts=attempts,
                      fallbackUsed=any(item != primary for item in attempts),
                      queryLocale=str(args.get('locale') or ''),
                      providerFailures=failures)
        for source in result.get('sources') or []:
            source['searchProvider'] = result.get('provider') or provider
        return result

    def _tool_web_search_once(
        self, settings: dict[str, Any], args: dict[str, Any], *, provider: str,
    ) -> dict[str, Any]:
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
            max_results = int(
                args.get("maxResults", args.get("max_results", settings.get("webSearchMaxResults") or 5))
            )
            read_top_pages = self._read_top_pages(settings)
            timeout = float(settings.get("webRequestTimeout") or 12)
            if provider == "deepseek":
                deepseek_options: dict[str, Any] = {
                    "api_key": api_key_for_settings(settings),
                    "count": max_results,
                    "need_content": read_top_pages > 0,
                    "timeout": timeout,
                    "domains": args.get("domains"),
                    "freshness": str(args.get("freshness") or ""),
                    "body_limit": read_top_pages,
                    "offset": args.get("offset", args.get("startOffset", 0)),
                    "page_cursor": args.get("pageCursor", args.get("page_cursor", "")),
                }
                explicit_locale = str(args.get("locale") or "").strip()
                if explicit_locale:
                    deepseek_options["locale"] = explicit_locale
                result = deepseek_search(
                    query,
                    **deepseek_options,
                )
                self.write_log(
                    "web_search_tool_completed",
                    requestId=self._request_id(args),
                    query=query[:128],
                    provider="deepseek",
                    readTopPages=read_top_pages,
                    bodyCount=self._readable_body_count(result),
                )
                return {"name": "web_search", "ok": True, **result}
            options = {
                "provider": provider,
                "api_key": str(settings.get("webSearchApiKey") or "") if provider == 'brave' else '',
                "max_results": max_results,
                "read_top_pages": read_top_pages,
                "timeout": timeout,
                "freshness": str(args.get("freshness") or ""),
                "locale": str(args.get("locale") or settings.get("uiLanguage") or "zh-CN"),
                "region": str(args.get("region") or ""),
            }
            # Optional controls are omitted when absent to preserve the
            # established adapter call contract for existing clients.
            if "domains" in args:
                options["domains"] = args.get("domains")
            if "offset" in args or "startOffset" in args:
                options["offset"] = args.get("offset", args.get("startOffset", 0))
            if "pageCursor" in args or "page_cursor" in args:
                options["page_cursor"] = args.get("pageCursor", args.get("page_cursor", ""))
            result = web_search(query, **options)
            self.write_log(
                "web_search_tool_completed",
                requestId=self._request_id(args),
                query=query[:500],
                provider=str(result.get("provider") or options["provider"])[:40],
                readTopPages=options["read_top_pages"],
                bodyCount=self._readable_body_count(result),
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
        # A browser search result is fetched directly, without an unnecessary
        # DeepSeek Fetch attempt on an unrelated retrieval route.
        if args.get('searchProvider') in {'bing', 'duckduckgo', 'brave', 'yahoo', 'wikipedia'}:
            settings = {**settings, 'webSearchProvider': 'bing'}
        result = self._tool_read_web_page_once(settings, args)
        fallback = str(settings.get("webSearchFallbackProvider", "bing") or "none").lower()
        if (
            settings.get("permissionWebSearch") and self._provider(settings) == "deepseek"
            and fallback in {"bing", "duckduckgo"}
            and (not result.get("ok") or not self._readable_body_count(result))
        ):
            reason = result.get("error") or "empty_content"
            result = self._tool_read_web_page_once({**settings, "webSearchProvider": fallback}, args)
            result.update(fallbackUsed=True, providerAttempts=["deepseek", "direct_web"], primaryError=reason)
        return result

    def _tool_read_web_page_once(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionWebSearch"):
            return {"name": "read_web_page", "ok": False, "error": "permission_web_search_denied"}
        url = str(args.get("url") or "").strip()
        try:
            provider = self._provider(settings)
            query = str(args.get("researchQuery") or args.get("query") or "")
            timeout = float(settings.get("webRequestTimeout") or 12)
            read_options: dict[str, Any] = {"timeout": timeout, "query": query}
            if "find" in args:
                read_options["find"] = args.get("find")
            if "startChar" in args or "start_char" in args:
                read_options["start_char"] = args.get("startChar", args.get("start_char", 0))
            if "maxChars" in args or "max_chars" in args:
                read_options["max_chars"] = args.get("maxChars", args.get("max_chars"))
            if provider == "deepseek":
                page = dict(deepseek_fetch(
                    url,
                    api_key=api_key_for_settings(settings),
                    timeout=timeout,
                    query=query,
                    find=str(read_options.get("find") or ""),
                    start_char=read_options.get("start_char"),
                    max_chars=read_options.get("max_chars"),
                ))
            else:
                page = dict(read_web_page(url, **read_options))
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
