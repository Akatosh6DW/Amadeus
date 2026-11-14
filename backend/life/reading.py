"""Reuse the existing public network and workspace boundaries for life reads."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from backend.tools.files import WorkspaceFileTools
from tools.safe_web import safe_http_request


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value: str) -> str:
    parser = _Text()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())[:2000]


def read_feed(url: str, *, timeout: float = 20) -> dict:
    try:
        timeout = max(3.0, min(float(timeout), 30.0))
    except (TypeError, ValueError):
        timeout = 20.0
    response = safe_http_request(url, timeout=timeout, headers={"Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml"},
                                 accepted_content_types=frozenset({"application/atom+xml", "application/rss+xml", "application/xml", "text/xml", "text/plain"}))
    if len(response["body"]) > 2 * 1024 * 1024:
        raise ValueError("订阅文件超过 2 MiB")
    root = ElementTree.fromstring(response["body"])
    items = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
            continue
        fields = {}
        for child in entry:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "link":
                if child.attrib.get("rel", "alternate") == "alternate":
                    fields["url"] = urljoin(response["url"], child.attrib.get("href") or child.text or "")
            elif tag in {"title", "summary", "description", "content", "encoded", "published", "pubDate", "updated", "id", "guid"}:
                fields[tag] = plain("".join(child.itertext()))
        link = fields.get("url", "")
        items.append({"title": fields.get("title", ""),
                      "url": link if urlsplit(link).scheme in {"https", "http"} else "",
                      "id": fields.get("id") or fields.get("guid") or link,
                      "summary": fields.get("summary") or fields.get("description") or fields.get("content") or fields.get("encoded", ""),
                      "publishedAt": fields.get("published") or fields.get("pubDate") or fields.get("updated", "")})
        if len(items) >= 12:
            break
    return {"ok": True, "source": response["url"], "items": items, "readAtSource": "feed summaries; follow links for full text"}


def read_local(config: dict, settings: dict, args: dict, *, listing: bool, data_dir: Path) -> dict:
    if not settings.get("permissionFileRead"):
        return {"ok": False, "error": "permissionFileRead_disabled"}
    roots = [Path(value).resolve() for value in config["directories"]]
    if not roots:
        return {"ok": False, "error": "no_reading_directories"}
    target = Path(args.get("path") or roots[0]).expanduser()
    if not target.is_absolute():
        return {"ok": False, "error": "absolute_path_required"}
    target = target.resolve()
    root = next((root for root in roots if target == root or root in target.parents), None)
    if root is None:
        return {"ok": False, "error": "path_outside_reading_directories"}
    tools = WorkspaceFileTools(data_dir=lambda: data_dir, defaults=lambda: {},
                               max_file_bytes=lambda: 128 * 1024, max_directory_entries=lambda: 60)
    scoped = {"permissionFileRead": True, "agentFileWorkspace": str(root)}
    result = (tools.tool_list_directory if listing else tools.tool_read_text_file)(scoped, {"path": str(target)})
    result["root"] = str(root)
    result["source"] = str(target)
    if isinstance(result.get("content"), str):
        result["truncated"] = len(result["content"]) > 12000
        result["content"] = result["content"][:12000]
    return result
