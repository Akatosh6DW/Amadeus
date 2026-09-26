"""已读网页来源批次的规范化、冻结和会话内保存。

来源编号在同一批次固定；所有批次更新复用原会话锁与作用域存储，不另建缓存。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import re
import urllib.parse
import uuid
from tools.safe_web import canonicalize_public_url
from backend.research.sources import stable_source_uid

@dataclass(frozen=True)
class SourceRegistryPorts:
    lock: Callable[[], Any]
    read_context: Callable[[], dict[str, Any]]
    write_context: Callable[..., Any]
    now: Callable[[], str]
    history_limit: Callable[[], int]

class SourceRegistry:
    def __init__(self, ports: SourceRegistryPorts) -> None:
        self.ports = ports

    def normalized_registry_source(
        self,
        source: dict[str, Any],
        *,
        display_id: str = "",
        batch_id: str = "",
    ) -> dict[str, Any] | None:
        url = str(source.get("url") or source.get("canonicalUrl") or "").strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        canonical = canonicalize_public_url(url)
        return {
            "sourceUid": str(source.get("sourceUid") or stable_source_uid(canonical))[:80],
            "displayId": str(display_id or source.get("displayId") or source.get("id") or "")[:20],
            "batchId": str(batch_id or source.get("batchId") or "")[:100],
            "title": re.sub(r"\s+", " ", str(source.get("title") or parsed.hostname)).strip()[:500],
            "url": canonical[:4096],
            "canonicalUrl": canonical[:4096],
            "domain": str(source.get("domain") or parsed.hostname)[:255],
            "sourceTier": str(source.get("sourceTier") or "")[:8],
            "sourceType": str(source.get("sourceType") or "")[:60],
            "publishedAt": str(source.get("publishedAt") or "")[:80],
            "verifiedAt": str(source.get("accessedAt") or source.get("verifiedAt") or self.ports.now())[:80],
        }


    def freeze_source_batch(
        self,
        sources: list[dict[str, Any]],
        *,
        request_id: str = "",
        topic_key: str = "",
        batch_id: str = "",
    ) -> dict[str, Any]:
        frozen_id = str(batch_id or f"batch_{uuid.uuid4().hex}")[:100]
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in sources[:12]:
            if not isinstance(source, dict):
                continue
            item = self.normalized_registry_source(
                source,
                display_id=f"S{len(normalized) + 1}",
                batch_id=frozen_id,
            )
            if not item or item["sourceUid"] in seen:
                continue
            seen.add(item["sourceUid"])
            normalized.append(item)
        return {
            "batchId": frozen_id,
            "requestId": str(request_id)[:120],
            "topicKey": re.sub(r"\s+", " ", str(topic_key or "")).strip()[:300],
            "createdAt": self.ports.now(),
            "sources": normalized,
            "frozen": True,
        }


    def persist_source_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(batch, dict) or not batch.get("batchId"):
            return {}
        normalized = self.freeze_source_batch(
            batch.get("sources") if isinstance(batch.get("sources"), list) else [],
            request_id=str(batch.get("requestId") or ""),
            topic_key=str(batch.get("topicKey") or ""),
            batch_id=str(batch.get("batchId") or ""),
        )
        if not normalized["sources"]:
            return {}
        normalized["createdAt"] = str(batch.get("createdAt") or normalized["createdAt"])[:80]
        with self.ports.lock():
            context = self.ports.read_context()
            batches = [
                item for item in context.get("sourceBatches", [])
                if isinstance(item, dict) and item.get("batchId") != normalized["batchId"]
            ]
            batches.append(normalized)
            context["sourceBatches"] = batches[-self.ports.history_limit():]
            self.ports.write_context(context)
        return normalized
