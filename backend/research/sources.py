"""研究来源身份、分级和证据门禁的纯值规则。

本模块只根据传入值计算来源元数据，不读取文件、不访问网络，也不依赖
server 入口；查询策略、去重和 URL 规范化由明确的下层模块提供。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Sequence

from agent.fingerprints import runtime_fingerprint
from agent.research_architecture import extract_latin_entities
from backend.common.text import unique_strings
from backend.research.query import research_source_policy_id
from tools.safe_web import canonicalize_public_url
from tools.web_source_registry import (
    KNOWN_OFFICIAL_WEB_ROOTS,
    known_official_web_domain,
)


_READABLE_FETCH_STATUSES = frozenset({"success", "partial", "ok", "fetched"})
_FAILED_FETCH_STATUSES = frozenset({
    "captcha", "blocked", "login_required", "paywalled",
    "js_required", "non_content", "failed", "fetch_error",
})
_PENDING_FETCH_STATUSES = frozenset({"", "not_fetched", "pending"})


def stable_source_uid(url: str) -> str:
    """按规范化 URL 生成稳定的来源身份。"""
    canonical = canonicalize_public_url(url)
    return "src_" + runtime_fingerprint(canonical)


def official_entity_search_terms(
    query: str,
    entity_terms: Sequence[str] = (),
) -> list[str]:
    """从查询和实体词中提取用于官网匹配的拉丁实体。"""
    values = [
        *[str(item).strip() for item in entity_terms if str(item).strip()],
        *extract_latin_entities(query),
    ]
    return unique_strings(
        value
        for value in values
        if len(re.sub(r"[\W_]+", "", value, flags=re.UNICODE)) >= 4
        and value.upper() not in {
            "HTTP", "HTTPS", "HTML", "JSON", "API", "SDK", "TTS",
            "PYTHON", "JAVASCRIPT", "WINDOWS", "LINUX",
        }
    )


def source_fetch_state(source: dict[str, Any]) -> str:
    """将来源抓取状态归一为 pending、success、failed 或 unknown。"""
    fetch_status = str(source.get("fetchStatus") or "").strip().lower()
    if fetch_status in _READABLE_FETCH_STATUSES:
        return "success"
    if fetch_status in _FAILED_FETCH_STATUSES:
        return "failed"
    if fetch_status in _PENDING_FETCH_STATUSES:
        # A body without an explicit status is the shape used by older callers
        # for an already-read page, so retain its readable evidence state.
        if not fetch_status and len(str(source.get("body") or "").strip()) >= 120:
            return "success"
        return "pending"
    return "unknown"


def classify_web_source(
    source: dict[str, Any],
    query: str = "",
    *,
    source_policy_id: str = "",
    entity_terms: Sequence[str] = (),
) -> dict[str, Any]:
    """按来源身份、策略和抓取状态计算分级，不信任网页自称。"""

    item = dict(source)
    raw_url = str(item.get("url") or "").strip()
    canonical_url = canonicalize_public_url(raw_url) if raw_url else ""
    parsed = urllib.parse.urlsplit(canonical_url or raw_url)
    domain = str(item.get("domain") or parsed.hostname or "").lower().removeprefix("www.")
    identity = f"{domain} {item.get('title') or ''} {canonical_url}".casefold()
    source_type = "web_page"
    # An unknown page is not authoritative merely because the search provider
    # returned it.  It must earn A/B through an identified source class.
    tier = "C"
    quality_flags: list[str] = []
    policy_id = str(source_policy_id or research_source_policy_id(query))
    entity_candidates = official_entity_search_terms(query, entity_terms)
    domain_identity = re.sub(r"[\W_]+", "", domain, flags=re.UNICODE).casefold()
    title_identity = re.sub(
        r"[\W_]+",
        "",
        str(item.get("title") or ""),
        flags=re.UNICODE,
    ).casefold()
    entity_domain_match = any(
        re.sub(r"[\W_]+", "", term, flags=re.UNICODE).casefold() in domain_identity
        for term in entity_candidates
    )
    entity_title_match = any(
        re.sub(r"[\W_]+", "", term, flags=re.UNICODE).casefold() in title_identity
        for term in entity_candidates
    )
    official_domain = known_official_web_domain(domain)
    domain_labels = [item for item in domain.split(".") if item]
    registrable_label = domain_labels[-2] if len(domain_labels) >= 2 else domain
    normalized_registrable_label = re.sub(
        r"[\W_]+", "", registrable_label, flags=re.UNICODE
    ).casefold()
    exact_entity_domain_match = any(
        re.sub(r"[\W_]+", "", term, flags=re.UNICODE).casefold()
        == normalized_registrable_label
        for term in entity_candidates
    )
    conventional_entity_tld = bool(
        domain_labels
        and domain_labels[-1] in {"com", "org", "net", "io", "ai", "dev", "app"}
    )
    entity_home_path = str(parsed.path or "/").strip("/") in {
        "", "about", "about-us", "about_us"
    }
    if re.search(
        r"arxiv\.org|openreview\.net|doi\.org|aclanthology\.org|"
        r"proceedings\.mlr\.press|pubmed\.ncbi\.nlm\.nih\.gov",
        domain,
        re.I,
    ):
        source_type, tier = "academic_primary", "A"
    elif re.search(r"(?:^|\.)gov(?:\.|$)|(?:^|\.)gov\.[a-z]{2}$|\.edu(?:\.|$)", domain, re.I):
        source_type, tier = "official_institution", "A"
    elif domain in {"github.com", "gitlab.com"}:
        source_type, tier = "official_repository", "A"
    elif re.search(
        r"reddit|quora|zhihu|知乎|weibo|微博|tieba|贴吧|"
        r"bilibili|哔哩哔哩|douyin|抖音|youtube|forum|论坛|"
        r"jianshu|简书|meipian|美篇|csdn|wenku\.baidu|百度文库",
        identity,
        re.I,
    ):
        source_type, tier = "community_or_social", "C"
        quality_flags.append("user_generated")
    elif official_domain:
        source_type, tier = "official_documentation", "A"
    elif (
        policy_id == "official_entity"
        and exact_entity_domain_match
        and conventional_entity_tld
        and entity_home_path
    ):
        # Unknown organizations cannot all live in a static allowlist.  An
        # exact-name root homepage on a conventional public suffix is accepted
        # as a B-tier official candidate only after its body is fetched; it is
        # deliberately weaker than a known official root.
        source_type, tier = "official_entity_homepage", "B"
        quality_flags.append("heuristic_official_domain")
    elif (
        policy_id == "official_entity"
        and (entity_domain_match or entity_title_match)
    ):
        # A brand-like domain or an "official" page title is only a candidate;
        # lookalike domains are common and cannot promote themselves to A-tier.
        source_type, tier = "candidate_entity_homepage", "C"
        quality_flags.append("unverified_official_domain")
    elif re.search(r"百科|wikipedia|新闻|news|媒体|magazine", identity, re.I):
        source_type, tier = "secondary_reference", "B"
    fetch_status = str(item.get("fetchStatus") or "").strip().lower()
    if fetch_status in _FAILED_FETCH_STATUSES:
        tier = "D"
        quality_flags.append(fetch_status)
    if item.get("promptInjectionMarkers"):
        quality_flags.append("prompt_injection_signal")
    item.update({
        "url": canonical_url or raw_url,
        "canonicalUrl": canonical_url or raw_url,
        "domain": domain,
        "sourceTier": tier,
        "sourceType": source_type,
        "sourcePolicyId": policy_id,
        "entityMatch": bool(entity_domain_match or entity_title_match),
        "qualityFlags": unique_strings(quality_flags),
    })
    item["sourceUid"] = stable_source_uid(canonical_url or raw_url)
    return item


def enrich_web_sources(
    result: dict[str, Any],
    query: str = "",
    *,
    source_policy_id: str = "",
    entity_terms: Sequence[str] = (),
) -> dict[str, Any]:
    """保留搜索结果外围字段，为每个来源补齐稳定身份和分级。"""
    enriched = dict(result)
    sources: list[dict[str, Any]] = []
    for index, raw_source in enumerate(
        result.get("sources") if isinstance(result.get("sources"), list) else [],
        1,
    ):
        if not isinstance(raw_source, dict):
            continue
        source = classify_web_source(
            raw_source,
            query,
            source_policy_id=source_policy_id,
            entity_terms=entity_terms,
        )
        source["id"] = str(source.get("id") or f"S{index}")
        sources.append(source)
    enriched["sources"] = sources
    enriched["sourcePolicyId"] = str(
        source_policy_id or research_source_policy_id(query)
    )
    return enriched


def source_has_readable_evidence(source: dict[str, Any]) -> bool:
    """只接受已读取且达到正文长度门槛的来源证据。"""
    body = str(source.get("body") or "").strip()
    # A provider snippet is discovery metadata, not proof that the page was
    # read. Only a substantive successful fetch can cross the evidence gate.
    return source_fetch_state(source) == "success" and len(body) >= 120


def source_satisfies_policy(source: dict[str, Any], policy_id: str) -> bool:
    """按来源策略判断分级和来源类型是否满足要求。"""
    if str(source.get("sourceTier") or "") == "D":
        return False
    source_type = str(source.get("sourceType") or "")
    if policy_id == "academic_primary":
        return source_type == "academic_primary"
    if policy_id == "official_policy":
        return source_type == "official_institution"
    if policy_id == "official_software":
        return source_type in {"official_documentation", "official_repository"}
    if policy_id == "official_entity":
        return bool(
            source_type == "official_entity_homepage"
            or (
                source_type in {
                    "official_documentation",
                    "official_repository",
                    "official_institution",
                }
                and source.get("entityMatch")
            )
        )
    if policy_id in {"current_event", "temporal_primary"}:
        return str(source.get("sourceTier") or "") in {"A", "B"}
    return True


__all__ = [
    "KNOWN_OFFICIAL_WEB_ROOTS",
    "classify_web_source",
    "enrich_web_sources",
    "known_official_web_domain",
    "official_entity_search_terms",
    "source_has_readable_evidence",
    "source_fetch_state",
    "source_satisfies_policy",
    "stable_source_uid",
]
