"""网页来源裁剪、引用选择与 source batch 投影。

本模块只处理已经返回的来源值；真正的 batch 持久化由窄端口提供，
不会访问 server 或主动发起网络请求。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import urllib.parse
from typing import Any, Callable

from backend.research.sources import source_has_readable_evidence, stable_source_uid
from tools.safe_web import canonicalize_public_url


def preferred_web_citation_id(results: list[dict[str, Any]]) -> str:
    valid_ids: set[str] = set()
    for item in results:
        if not item.get("ok") or item.get("name") not in {"web_search", "read_web_page"}:
            continue
        status = item.get("evidenceStatus") if isinstance(item.get("evidenceStatus"), dict) else {}
        if not status.get("sufficient"):
            continue
        answer_ids = {str(value) for value in status.get("answerSourceIds", []) if str(value)}
        if not answer_ids:
            # Some providers return the deterministic sufficiency verdict but
            # omit its redundant answerSourceIds projection. In that case a
            # readable source from the same sufficient result is still a
            # verified citation target.
            answer_ids = {
                str(source.get("id") or "")
                for source in (
                    item.get("sources")
                    if isinstance(item.get("sources"), list)
                    else []
                )
                if isinstance(source, dict)
                and re.fullmatch(r"S\d+", str(source.get("id") or ""), re.I)
                and source_has_readable_evidence(source)
            }
        valid_ids.update(answer_ids)
    for item in results:
        status = item.get("evidenceStatus") if isinstance(item.get("evidenceStatus"), dict) else {}
        priority_ids = [
            *status.get("responsibilitySourceIds", []),
            *status.get("disciplineSourceIds", []),
            *status.get("reportSourceIds", []),
            *status.get("scoreSourceIds", []),
            *status.get("relevantSourceIds", []),
            *status.get("substantiveSourceIds", []),
        ]
        for source_id in priority_ids:
            if str(source_id) in valid_ids:
                return str(source_id)
    return sorted(valid_ids, key=lambda value: int(value[1:]) if value[1:].isdigit() else 999)[0] if valid_ids else ""

def ensure_verified_web_citation(value: Any, results: list[dict[str, Any]]) -> Any:
    """Preserve model citations without authoring an unbound fallback.

    The final source batch is assembled later by ``attach_verified_web_sources``.
    Adding the first numerically valid source here cannot establish which page
    supports the model's claims, especially when several tool results reuse
    local ``S1``/``S2`` identifiers.  Leave the model value byte-for-byte
    intact and let the final attachment stage keep only explicit citations that
    map to the visible source batch.
    """
    return value

def public_web_sources(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in results:
        if not item.get("ok") or item.get("name") not in {"web_search", "read_web_page"}:
            continue
        raw_sources = item.get("sources") if isinstance(item.get("sources"), list) else []
        status = item.get("evidenceStatus") if isinstance(item.get("evidenceStatus"), dict) else {}
        relevant_ids = {
            str(source_id)
            for source_id in (
                status.get("answerSourceIds")
                if isinstance(status.get("answerSourceIds"), list)
                else status.get("relevantSourceIds", [])
            )
            if str(source_id)
        }
        has_relevance_filter = (
            "answerSourceIds" in status
            or "relevantSourceIds" in status
        )
        for source in raw_sources:
            if not isinstance(source, dict):
                continue
            if has_relevance_filter and str(source.get("id") or "") not in relevant_ids:
                continue
            url = canonicalize_public_url(str(source.get("url") or "").strip())
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or url in seen_urls:
                continue
            seen_urls.add(url)
            excerpt = str(source.get("snippet") or source.get("body") or "")
            excerpt = re.sub(r"\s+", " ", excerpt).strip()[:500]
            sources.append({
                "id": f"S{len(sources) + 1}",
                "title": re.sub(r"\s+", " ", str(source.get("title") or parsed.hostname)).strip()[:300],
                "url": url[:4096],
                "domain": str(source.get("domain") or parsed.hostname)[:255],
                "sourceUid": str(
                    source.get("sourceUid")
                    or stable_source_uid(str(source.get("url") or ""))
                )[:80],
                "sourceTier": str(source.get("sourceTier") or "")[:8],
                "sourceType": str(source.get("sourceType") or "")[:60],
                "fetchStatus": str(source.get("fetchStatus") or "")[:40],
                "qualityFlags": [
                    str(flag)[:80]
                    for flag in (
                        source.get("qualityFlags")
                        if isinstance(source.get("qualityFlags"), list)
                        else []
                    )
                ][:8],
                "excerpt": excerpt,
                "publishedAt": str(source.get("publishedAt") or "")[:80],
                "accessedAt": str(source.get("accessedAt") or "")[:80],
            })
            if len(sources) >= 8:
                return sources
    return sources


def readable_public_source_uids(
    results: list[dict[str, Any]],
) -> dict[str, str]:
    """Map final visible S-ids to stable UIDs only for actually read bodies."""

    readable_identity_by_public_uid = {
        str(
            source.get("sourceUid")
            or stable_source_uid(str(source.get("url") or ""))
        ): str(
            source.get("sourceUid")
            or source.get("canonicalUrl")
            or source.get("url")
            or ""
        )
        for item in results
        if isinstance(item, dict)
        and item.get("ok")
        and item.get("name") in {"web_search", "read_web_page"}
        for source in (
            item.get("sources") if isinstance(item.get("sources"), list) else []
        )
        if isinstance(source, dict) and source_has_readable_evidence(source)
    }
    return {
        str(source.get("id") or "").upper(): readable_identity_by_public_uid[
            str(source.get("sourceUid") or "")
        ]
        for source in public_web_sources(results)
        if str(source.get("id") or "")
        and str(source.get("sourceUid") or "") in readable_identity_by_public_uid
    }

@dataclass(frozen=True)
class CitationPorts:
    """来源 batch 的唯一持久化端口。"""

    freeze_source_batch: Callable[..., dict[str, Any]]


class VerifiedWebCitationService:
    """Attach bounded, verified public sources to a normalized reply."""

    def __init__(self, ports: CitationPorts) -> None:
        self.ports = ports

    def attach_verified_web_sources(self, result: dict[str, Any], tool_results: list[dict[str, Any]]) -> dict[str, Any]:
        planned_statuses = [
            item.get("evidenceStatus")
            for item in tool_results
            if isinstance(item, dict)
            and item.get("ok")
            and item.get("name") in {"web_search", "read_web_page"}
            and isinstance(item.get("evidenceStatus"), dict)
        ]
        evidence_sufficient = (
            not planned_statuses
            or any(status.get("sufficient") for status in planned_statuses)
        )
        sources = public_web_sources(tool_results)
        if not sources:
            return result
        valid_ids = {source["id"] for source in sources}
        readable_ids = set(readable_public_source_uids(tool_results))
        reply = str(result.get("reply") or "").strip()
        reply = re.sub(
            r"\[(S\d+)\]",
            lambda match: (
                f"[{match.group(1).upper()}]"
                if match.group(1).upper() in valid_ids
                and (evidence_sufficient or match.group(1).upper() in readable_ids)
                else ""
            ),
            reply,
            flags=re.I,
        )
        if not evidence_sufficient:
            # A globally partial receipt can still contain a readable source for
            # one local claim. Keep only citations bound to those actual bodies;
            # never auto-cite an unresolved part of the answer.
            reply = reply.rstrip()
        source_batch = self.ports.freeze_source_batch(
            sources,
            request_id=str(result.get("requestId") or ""),
            topic_key=next(
                (
                    str(item.get("query") or "")
                    for item in tool_results
                    if isinstance(item, dict) and item.get("name") == "web_search"
                ),
                "",
            ),
        )
        result["reply"] = reply
        result["sourceBatch"] = source_batch
        result["sources"] = [
            {
                **source,
                "id": str(source.get("displayId") or source.get("id") or ""),
                "batchId": str(source_batch.get("batchId") or ""),
            }
            for source in source_batch.get("sources", [])
            if isinstance(source, dict)
        ]
        return result
