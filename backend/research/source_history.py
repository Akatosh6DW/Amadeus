"""从持久会话来源恢复邻接网页引用，并给新搜索提供有界词语提示。

用户资料按调用读取；助手普通文字不能变成可信 URL 或已验证的网页来源。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import re
from tools.safe_web import canonicalize_public_url
from backend.tools.browser_rules import browser_references_previous_source

@dataclass(frozen=True)
class SourceHistoryPorts:
    read_context: Callable[[], dict[str, Any]]
    pending_action: Callable[[], dict[str, Any]]
    preferred_source: Callable[..., Any]

class SourceHistory:
    def __init__(self, ports: SourceHistoryPorts) -> None:
        self.ports = ports

    def recent_message_web_sources(
        self,
        messages: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, str]], str]:
        """Return the nearest assistant source set and its visible answer text."""

        candidates: list[dict[str, Any]] = []
        # A restored or older UI may omit message metadata even though the server
        # archive retained it. Keep server-owned history as the authoritative
        # fallback instead of degrading an "open that source" request to a search.
        context = self.ports.read_context()
        candidates.extend({
            "content": str(turn.get("assistant") or ""),
            "sources": turn.get("sources"),
        } for turn in context.get("recentTurns", [])
          if isinstance(turn, dict) and str(turn.get("assistant") or "").strip())
        pending = self.ports.pending_action()
        pending_batch_id = str(pending.get("batchId") or "")
        batches = [
            item for item in context.get("sourceBatches", [])
            if isinstance(item, dict) and isinstance(item.get("sources"), list)
        ]
        for batch in batches:
            batch_sources = [
                {
                    **source,
                    "id": str(source.get("displayId") or source.get("id") or ""),
                }
                for source in batch.get("sources", [])
                if isinstance(source, dict)
            ]
            candidates.append({
                "content": " ".join(str(item.get("title") or "") for item in batch_sources),
                "sources": batch_sources,
                "_pending": str(batch.get("batchId") or "") == pending_batch_id,
            })
        candidates.extend(
            item
            for item in (messages or [])
            if isinstance(item, dict) and item.get("role") == "assistant"
        )
        candidates.sort(key=lambda item: bool(item.get("_pending")))
        for item in reversed(candidates):
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            raw_sources = (
                meta.get("sources")
                if isinstance(meta.get("sources"), list)
                else item.get("sources")
                if isinstance(item.get("sources"), list)
                else []
            )
            sources: list[dict[str, str]] = []
            for source in raw_sources[:8]:
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "").strip()
                if not re.match(r"^https?://", url, re.I):
                    continue
                sources.append({
                    "id": str(source.get("displayId") or source.get("id") or "")[:20],
                    "sourceUid": str(source.get("sourceUid") or "")[:80],
                    "batchId": str(source.get("batchId") or "")[:100],
                    "title": str(source.get("title") or "")[:500],
                    "url": canonicalize_public_url(url)[:2000],
                    "domain": str(source.get("domain") or "")[:200],
                    "sourceTier": str(source.get("sourceTier") or "")[:8],
                    "sourceType": str(source.get("sourceType") or "")[:60],
                })
            if sources:
                return sources, str(item.get("content") or "")[:4000]
        return [], ""


    def recent_assistant_web_subject(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None,
    ) -> str:
        """Return only a subject backed by a persisted source, never model prose."""

        if not browser_references_previous_source(user_message):
            return ""
        selected = self.ports.preferred_source(user_message, messages)
        if selected:
            return str(selected.get("title") or selected.get("domain") or "")[:240]
        # Assistant prose is not trusted as URL provenance. It may, however, supply
        # one bounded lexical hint for a fresh resolver search when the UI did not
        # preserve source metadata. Never inherit a URL, numeric paper id, or the
        # assistant's whole sentence.
        for item in reversed(messages or []):
            if not isinstance(item, dict) or item.get("role") != "assistant":
                continue
            content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            acronym = re.search(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b", content)
            if acronym:
                suffix = " arXiv paper" if re.search(r"论文|paper|arxiv", content, re.I) else ""
                return f"{acronym.group(0)}{suffix}"[:240]
            quoted = re.search(r"[“\"《]([^”\"》]{2,120})[”\"》]", content)
            if quoted:
                return quoted.group(1).strip()[:240]
        return ""
