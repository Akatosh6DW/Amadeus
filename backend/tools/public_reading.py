"""Read-only adapters for public Bluesky, Bangumi, arXiv and RSS sources.

The adapters deliberately use the existing ``safe_http_request`` boundary and
the existing RSS parser.  They do not create sessions, accept credentials or
expose any write endpoint.  Provider responses are normalised to a small
bounded ``items``/``sources`` shape so the same tool can be used by ordinary
chat and autonomous life.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

from agent.fingerprints import runtime_fingerprint
from backend.life.reading import read_feed
from backend.research.sources import stable_source_uid
from tools.safe_web import SafeWebError, safe_http_request


_BLUESKY_API = "https://public.api.bsky.app/xrpc"
# The cached public AppView returns 403 for search; its standard AppView
# exposes the anonymous search route. Author feeds and threads use the cache.
_BLUESKY_SEARCH_API = "https://api.bsky.app/xrpc"
_BANGUMI_API = "https://api.bgm.tv"
_ARXIV_API = "https://export.arxiv.org/api/query"
_JSON_TYPES = frozenset({"application/json", "text/json"})
_XML_TYPES = frozenset({"application/atom+xml", "application/rss+xml", "application/xml", "text/xml", "text/plain"})
_MAX_BODY = 2 * 1024 * 1024
_MAX_QUERY = 300
_MAX_CURSOR = 300
_MAX_ID = 240


def _text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _bounded_int(value: Any, *, default: int = 10, minimum: int = 1, maximum: int = 20) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _timeout(settings: Mapping[str, Any]) -> float:
    try:
        return max(3.0, min(float(settings.get("webRequestTimeout") or 12), 30.0))
    except (TypeError, ValueError):
        return 12.0


def _tag(value: Any) -> str:
    return str(value or "").rsplit("}", 1)[-1]


def _json_request(
    url: str,
    settings: Mapping[str, Any],
    *,
    method: str = "GET",
    body: bytes | None = None,
    limit: int = _MAX_BODY,
) -> dict[str, Any]:
    response = safe_http_request(
        url,
        method=method,
        body=body,
        timeout=_timeout(settings),
        headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})},
        accepted_content_types=_JSON_TYPES,
    )
    body = response.get("body", b"")
    if len(body) > limit:
        raise ValueError("公共阅读响应超过 2 MiB")
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("公共阅读响应不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("公共阅读响应不是对象")
    return {"url": response.get("url", url), "payload": payload}


def _xml_request(url: str, settings: Mapping[str, Any], *, limit: int = _MAX_BODY) -> dict[str, Any]:
    response = safe_http_request(
        url,
        timeout=_timeout(settings),
        headers={"Accept": "application/atom+xml,application/xml,text/xml,text/plain"},
        accepted_content_types=_XML_TYPES,
    )
    body = response.get("body", b"")
    if len(body) > limit:
        raise ValueError("公共阅读响应超过 2 MiB")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise ValueError("公共阅读响应不是有效 Atom/XML") from error
    return {"url": response.get("url", url), "root": root}


def _at_uri_url(uri: str, handle: str) -> str:
    """Turn a Bluesky ``at://.../app.bsky.feed.post/...`` URI into a public URL."""

    parts = str(uri or "").split("/")
    if len(parts) >= 5 and parts[0] == "at:" and parts[-2] == "app.bsky.feed.post" and parts[-1]:
        actor = _text(handle, 120) or parts[2]
        return f"https://bsky.app/profile/{quote(actor, safe=':@.-_~')}/post/{quote(parts[-1], safe='._~-')}"
    return ""


def _bluesky_post(post: Mapping[str, Any]) -> dict[str, Any]:
    record = post.get("record") if isinstance(post.get("record"), Mapping) else {}
    author = post.get("author") if isinstance(post.get("author"), Mapping) else {}
    handle = _text(author.get("handle"), 120)
    display_name = _text(author.get("displayName"), 160)
    uri = _text(post.get("uri"), 300)
    text = _text(record.get("text"), 4000)
    title = display_name or handle or _text(author.get("did"), 160) or "Bluesky post"
    item = {
        "id": uri,
        "title": title,
        "author": handle or _text(author.get("did"), 160),
        "text": text,
        "body": text,
        "createdAt": _text(record.get("createdAt") or post.get("indexedAt"), 80),
        "indexedAt": _text(post.get("indexedAt"), 80),
        "likeCount": post.get("likeCount") if isinstance(post.get("likeCount"), int) else 0,
        "replyCount": post.get("replyCount") if isinstance(post.get("replyCount"), int) else 0,
        "repostCount": post.get("repostCount") if isinstance(post.get("repostCount"), int) else 0,
    }
    url = _at_uri_url(uri, handle)
    if url:
        item["url"] = url
    return item


def _bluesky_items(payload: Mapping[str, Any], *, limit: int) -> list[dict[str, Any]]:
    posts = payload.get("posts")
    if not isinstance(posts, list):
        feed = payload.get("feed")
        posts = [entry.get("post") for entry in feed if isinstance(entry, Mapping)] if isinstance(feed, list) else []
    return [_bluesky_post(post) for post in posts[:limit] if isinstance(post, Mapping)]


def _bluesky_thread_node(node: Mapping[str, Any], result: list[dict[str, Any]], *, limit: int, depth: int = 0) -> None:
    if len(result) >= limit or depth > 2:
        return
    if node.get("post") and isinstance(node.get("post"), Mapping):
        result.append(_bluesky_post(node["post"]))
    replies = node.get("replies")
    if isinstance(replies, list):
        for reply in replies:
            if isinstance(reply, Mapping):
                _bluesky_thread_node(reply, result, limit=limit, depth=depth + 1)
            if len(result) >= limit:
                break


def _bangumi_item(item: Mapping[str, Any], *, detail: bool = False) -> dict[str, Any]:
    subject_id = item.get("id")
    try:
        subject_id = int(subject_id)
    except (TypeError, ValueError):
        subject_id = _text(subject_id, 80)
    title = _text(item.get("name_cn") or item.get("name"), 300)
    result = {
        "id": subject_id,
        "title": title,
        "originalTitle": _text(item.get("name"), 300),
        "type": item.get("type") if isinstance(item.get("type"), int) else _text(item.get("type"), 80),
        "date": _text(item.get("date"), 80),
        "platform": _text(item.get("platform"), 120),
        "score": item.get("score"),
        "summary": _text(item.get("summary"), 4000),
        "body": _text(item.get("summary"), 4000),
        "url": f"https://bgm.tv/subject/{quote(str(subject_id), safe='0123456789')}" if subject_id else "",
    }
    images = item.get("images")
    if isinstance(images, Mapping):
        result["images"] = {key: _text(images.get(key), 500) for key in ("large", "common", "medium", "small", "grid") if images.get(key)}
    if detail:
        for key in ("eps", "volumes", "sites", "infobox", "tags", "rating", "collection"):
            if key in item:
                result[key] = item[key]
    return result


def _arxiv_id(value: Any) -> str:
    raw = _text(value, _MAX_ID).strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower().endswith("arxiv.org"):
        raw = parsed.path.strip("/").split("/")[-1]
    if raw.lower().startswith("arxiv:"):
        raw = raw[6:]
    return raw[:_MAX_ID]


def _arxiv_search_query(query: str) -> str:
    """Preserve arXiv's documented field syntax while defaulting to all fields."""

    value = _text(query, _MAX_QUERY).strip()
    prefixes = ("all:", "cat:", "ti:", "au:", "abs:", "co:", "id:", "jr:", "rn:", "submittedDate:")
    return value if value.lower().startswith(prefixes) else f"all:{value}"


def _arxiv_entry(entry: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    authors: list[str] = []
    categories: list[str] = []
    for child in entry:
        name = _tag(child.tag)
        if name == "author":
            author_name = next((_text(grandchild.text, 180) for grandchild in child if _tag(grandchild.tag) == "name"), "")
            if author_name:
                authors.append(author_name)
        elif name == "category":
            term = _text(child.attrib.get("term"), 80)
            if term:
                categories.append(term)
        elif name in {"id", "title", "summary", "published", "updated", "doi", "journal_ref"}:
            fields[name] = _text("".join(child.itertext()), 6000 if name in {"summary", "title"} else 300)
    item_id = _text(fields.get("id"), 300)
    title = _text(fields.get("title"), 500)
    summary = _text(fields.get("summary"), 5000)
    return {
        "id": item_id,
        "title": title,
        "authors": authors[:20],
        "summary": summary,
        "body": summary,
        "publishedAt": _text(fields.get("published"), 80),
        "updatedAt": _text(fields.get("updated"), 80),
        "categories": categories[:20],
        "doi": _text(fields.get("doi"), 300),
        "journalRef": _text(fields.get("journal_ref"), 300),
        "url": item_id if item_id.startswith(("http://", "https://")) else "",
    }


def _arxiv_entries(root: Any, *, limit: int) -> list[dict[str, Any]]:
    return [_arxiv_entry(entry) for entry in root if _tag(entry.tag) == "entry"][:limit]


def _source_rows(items: list[dict[str, Any]], endpoint: str) -> list[dict[str, Any]]:
    rows = []
    for index, item in enumerate(items[:20], start=1):
        if not isinstance(item, Mapping):
            continue
        item_url = _text(item.get("url"), 1000)
        url = item_url or endpoint
        title = _text(item.get("title"), 400)
        body = _text(item.get("body") or item.get("summary") or item.get("text"), 6000)
        # A provider endpoint is often shared by every returned item.  Stable
        # per-item identities keep evidence rows distinct even when an item has
        # no deep link (for example a feed entry without ``link``).
        item_identity = _text(item.get("id"), 400) or item_url or str(index)
        source_uid = (
            stable_source_uid(item_url)
            if item_url
            else "src_" + runtime_fingerprint(f"{endpoint}#{item_identity}#{body}", length=32)
        )
        rows.append({
            "id": f"S{index}",
            "sourceUid": source_uid,
            "sourceOrigin": "public_read",
            "fetchStatus": "success",
            "title": title,
            "url": url,
            "snippet": body[:1600],
            "body": body,
        })
    return rows


@dataclass
class PublicReadingTools:
    """Provider adapter shared by ordinary tools and autonomous life."""

    write_log: Callable[..., Any] | None = None

    def _result(self, settings: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        source = _text(args.get("source"), 40).lower()
        operation = _text(args.get("operation"), 40).lower()
        if source == "bsky":
            source = "bluesky"
        if source == "atom":
            source = "rss"
        if source not in {"bluesky", "bangumi", "arxiv", "rss"}:
            return {"name": "public_read", "ok": False, "error": "unsupported_public_source"}
        if source == "rss":
            if operation not in {"feed", "read"}:
                return {"name": "public_read", "ok": False, "error": "unsupported_rss_operation", "source": source, "operation": operation}
            url = _text(args.get("url"), 2000)
            if not url:
                return {"name": "public_read", "ok": False, "error": "url_required", "source": source, "operation": operation}
            result = dict(read_feed(url, timeout=_timeout(settings)))
            items = [dict(item) for item in result.get("items", []) if isinstance(item, Mapping)][: _bounded_int(args.get("limit"), default=12)]
            result.update(name="public_read", source=source, operation="feed", endpoint=result.get("source") or url,
                          items=items, sources=_source_rows(items, str(result.get("source") or url)))
            return result

        limit = _bounded_int(args.get("limit", args.get("maxResults", 10)), maximum=20)
        query = _text(args.get("query"), _MAX_QUERY)
        cursor = _text(args.get("cursor"), _MAX_CURSOR)
        if source == "bluesky":
            if operation in {"search", "posts", "account", "author"}:
                if operation == "search" and not query:
                    return {"name": "public_read", "ok": False, "error": "query_required", "source": source, "operation": operation}
                actor = _text(args.get("actor") or args.get("account") or args.get("handle"), 180)
                if operation != "search" and not actor:
                    return {"name": "public_read", "ok": False, "error": "actor_required", "source": source, "operation": operation}
                params = {"limit": str(limit)}
                if cursor:
                    params["cursor"] = cursor
                if operation == "search":
                    params["q"] = query
                    endpoint = f"{_BLUESKY_SEARCH_API}/app.bsky.feed.searchPosts?{urlencode(params)}"
                else:
                    params["actor"] = actor
                    endpoint = f"{_BLUESKY_API}/app.bsky.feed.getAuthorFeed?{urlencode(params)}"
                response = _json_request(endpoint, settings)
                payload = response["payload"]
                items = _bluesky_items(payload, limit=limit)
                return {"name": "public_read", "ok": True, "source": source, "operation": "search" if operation == "search" else "posts",
                        "endpoint": response["url"], "query": query or actor, "items": items,
                        "nextCursor": _text(payload.get("cursor"), _MAX_CURSOR), "sources": _source_rows(items, response["url"])}
            if operation in {"thread", "post"}:
                uri = _text(args.get("uri") or args.get("id"), 500)
                if not uri:
                    return {"name": "public_read", "ok": False, "error": "uri_required", "source": source, "operation": operation}
                params = urlencode({"uri": uri, "depth": str(min(limit, 10))})
                response = _json_request(f"{_BLUESKY_API}/app.bsky.feed.getPostThread?{params}", settings)
                items: list[dict[str, Any]] = []
                _bluesky_thread_node(response["payload"].get("thread", {}), items, limit=limit)
                return {"name": "public_read", "ok": True, "source": source, "operation": "thread",
                        "endpoint": response["url"], "query": uri, "items": items,
                        "sources": _source_rows(items, response["url"])}
            return {"name": "public_read", "ok": False, "error": "unsupported_bluesky_operation", "source": source, "operation": operation}

        if source == "bangumi":
            if operation in {"search", "subjects"}:
                if not query:
                    return {"name": "public_read", "ok": False, "error": "query_required", "source": source, "operation": operation}
                endpoint = f"{_BANGUMI_API}/v0/search/subjects?{urlencode({'limit': str(limit), 'offset': '0'})}"
                response = _json_request(endpoint, settings, method="POST",
                                          body=json.dumps({"keyword": query}, ensure_ascii=False).encode("utf-8"))
                payload = response["payload"]
                raw_items = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("list")
                raw_items = raw_items if isinstance(raw_items, list) else payload.get("results")
                raw_items = raw_items if isinstance(raw_items, list) else []
                items = [_bangumi_item(item) for item in raw_items[:limit] if isinstance(item, Mapping)]
                return {"name": "public_read", "ok": True, "source": source, "operation": "search",
                        "endpoint": response["url"], "query": query, "items": items,
                        "sources": _source_rows(items, response["url"])}
            if operation in {"subject", "detail"}:
                subject_id = _text(args.get("id") or args.get("subjectId"), 80)
                if not subject_id or not subject_id.isdigit():
                    return {"name": "public_read", "ok": False, "error": "subject_id_required", "source": source, "operation": operation}
                endpoint = f"{_BANGUMI_API}/v0/subjects/{subject_id}"
                response = _json_request(endpoint, settings)
                item = _bangumi_item(response["payload"], detail=True)
                return {"name": "public_read", "ok": True, "source": source, "operation": "subject",
                        "endpoint": response["url"], "query": subject_id, "items": [item],
                        "sources": _source_rows([item], response["url"])}
            return {"name": "public_read", "ok": False, "error": "unsupported_bangumi_operation", "source": source, "operation": operation}

        if operation not in {"search", "abstract", "detail"}:
            return {"name": "public_read", "ok": False, "error": "unsupported_arxiv_operation", "source": source, "operation": operation}
        identifier = ""
        if operation == "search":
            if not query:
                return {"name": "public_read", "ok": False, "error": "query_required", "source": source, "operation": operation}
            search_query = _arxiv_search_query(query)
            params = {"search_query": search_query, "start": "0", "max_results": str(limit)}
        else:
            identifier = _arxiv_id(args.get("id") or args.get("arxivId") or query)
            if not identifier:
                return {"name": "public_read", "ok": False, "error": "arxiv_id_required", "source": source, "operation": operation}
            search_query = ""
            params = {"id_list": identifier, "max_results": str(limit)}
        response = _xml_request(f"{_ARXIV_API}?{urlencode(params)}", settings)
        items = _arxiv_entries(response["root"], limit=limit)
        return {"name": "public_read", "ok": True, "source": source, "operation": "search" if operation == "search" else "abstract",
                "endpoint": response["url"], "query": query or search_query or identifier, "items": items,
                "sources": _source_rows(items, response["url"])}

    def tool_public_read(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        if not settings.get("permissionWebSearch"):
            return {"name": "public_read", "ok": False, "error": "permission_web_search_denied"}
        try:
            result = self._result(settings, args if isinstance(args, Mapping) else {})
        except (SafeWebError, ValueError, TypeError, KeyError) as error:
            result = {"name": "public_read", "ok": False, "error": "public_read_failed",
                      "source": _text(args.get("source"), 40), "operation": _text(args.get("operation"), 40),
                      "detail": _text(error, 500)}
        if self.write_log:
            self.write_log("public_read_completed" if result.get("ok") else "public_read_failed",
                           source=str(result.get("source") or "")[:40],
                           operation=str(result.get("operation") or "")[:40],
                           itemCount=len(result.get("items") or []) if isinstance(result.get("items"), list) else 0,
                           error=str(result.get("error") or "")[:120])
        return result


def tool_public_read(settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Convenience entry point for callers that do not need a logger."""

    return PublicReadingTools().tool_public_read(settings, args)
