"""工具结果 grounding 校验与网页答案形状诊断。

本模块只消费本轮结果和候选回复，不采集视觉、不执行工具，也不读取 server 全局。
视觉证据的成人 profile 通过窄端口接入，模型与对话编排继续由上层持有。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from backend.chat.completion import reply_integrity_issues
from backend.chat.parsing import parse_json_object


_WEB_PROCESS_ONLY_CLAUSE = re.compile(
    r"^(?:好|好的|可以|没问题|稍等|等一下|等等|先等一下)$|"
    r"^(?:我)?(?:现在|马上|这就|先|去)?(?:联网|上网|在线)?"
    r"(?:查(?:一下|下|查)|搜(?:一下|下|搜)|搜索一下|检索一下|查询一下|查找一下).*$|"
    r"^(?:我)?(?:已经|已)?(?:查到|搜到)(?:了|的)?"
    r"(?:相关|一些|若干|可用|新的|最新的)?(?:资料|来源|结果|信息|内容|网页|页面|链接|公告)(?:了)?$|"
    r"^(?:联网|上网|在线)?(?:搜索|检索|查询)(?:已经|已)?"
    r"(?:完成|结束|成功|失败|取得(?:了)?(?:相关|一些|若干)?(?:资料|来源|结果|信息|内容)?)$|"
    r"^(?:证据综合|检索结果)(?:没有|未能|尚未|已经|已)?(?:完成|失败|结束)?$|"
    r"^(?:最相关|最接近|首个)(?:的)?来源(?:是|为)?$",
    re.I,
)


def web_answer_has_substantive_statement(reply: str, source_titles: list[str]) -> bool:
    """Return whether a Web answer contains something beyond retrieval metadata."""

    compact = re.sub(r"\s+", "", str(reply or ""))
    compact = re.sub(r"https?://[^\s，,。；;！？!?]+", "", compact, flags=re.I)
    compact = re.sub(r"\[(?:S\d+)\]", "", compact, flags=re.I)
    for title in source_titles:
        normalized_title = re.sub(r"\s+", "", str(title or ""))
        if normalized_title:
            compact = compact.replace(normalized_title, "")
    clauses = [
        re.sub(r"^(?:但|不过|然而|另外|以及|并且)", "", item).strip()
        for item in re.split(r"[，,。；;！？!?：:\n]+", compact)
        if item.strip()
    ]
    return any(
        re.search(r"[A-Za-z0-9\u4e00-\u9fff]", clause)
        and not _WEB_PROCESS_ONLY_CLAUSE.fullmatch(clause)
        for clause in clauses
    )


@dataclass(frozen=True)
class ToolGroundingValidationPorts:
    """工具 grounding 只借用视觉证据摘要，不拥有视觉状态。"""

    visual_adult_evidence_profile: Callable[[list[dict[str, Any]]], dict[str, bool]]


class ToolGroundingValidationService:
    """Validate claims against actual tool outcomes and evidence shape."""

    def __init__(self, ports: ToolGroundingValidationPorts) -> None:
        self.ports = ports

    def web_answer_diagnostics(self, value: Any, results: list[dict[str, Any]]) -> list[str]:
        """Detect answer-shape failures without knowing the research domain.

        This deliberately does not look for football, products, politics, or any
        other subject vocabulary.  It only checks whether a web-backed response is
        an actual answer with a verified citation instead of a retrieval status or
        a source-title inventory.
        """

        web_sources = [
            source
            for item in results
            if item.get("ok") and item.get("name") in {"web_search", "read_web_page"}
            for source in (item.get("sources") if isinstance(item.get("sources"), list) else [])
            if isinstance(source, dict)
        ]
        if not web_sources:
            return []
        parsed = parse_json_object(value) if isinstance(value, str) else value if isinstance(value, dict) else {}
        reply = str((parsed or {}).get("reply") or value or "").strip()
        if not reply:
            return ["empty_answer"]
        diagnostics: list[str] = reply_integrity_issues(value)
        valid_ids = {str(source.get("id") or "").upper() for source in web_sources if source.get("id")}
        cited_ids = {item.upper() for item in re.findall(r"\[(S\d+)\]", reply, re.I)}
        web_statuses = [
            item.get("evidenceStatus")
            for item in results
            if item.get("ok")
            and item.get("name") in {"web_search", "read_web_page"}
            and isinstance(item.get("evidenceStatus"), dict)
        ]
        citations_required = bool(
            valid_ids
            and (
                not web_statuses
                or any(status.get("sufficient") for status in web_statuses)
            )
        )
        if citations_required and not (valid_ids & cited_ids):
            diagnostics.append("missing_verified_citation")
        research_plan = next(
            (
                item.get("researchPlan") for item in results
                if item.get("ok") and isinstance(item.get("researchPlan"), dict)
            ),
            {},
        )
        comparison_task = bool(re.search(
            r"相比|比较|对比|区别|差异|强弱|优劣|(?:哪个|哪一个|谁|孰).{0,6}更|"
            r"\b(?:vs\.?|versus|compare|comparison)\b",
            str(research_plan.get("resolvedQuestion") or ""),
            flags=re.I,
        ))
        source_identity_by_id = {
            str(source.get("id") or "").upper(): str(
                source.get("sourceUid")
                or source.get("canonicalUrl")
                or source.get("url")
                or source.get("id")
                or ""
            ).strip().casefold()
            for source in web_sources
            if source.get("id")
        }
        distinct_source_identities = {
            identity for identity in source_identity_by_id.values() if identity
        }
        cited_source_identities = {
            source_identity_by_id[source_id]
            for source_id in valid_ids & cited_ids
            if source_identity_by_id.get(source_id)
        }
        if (
            citations_required
            and comparison_task
            and len(distinct_source_identities) >= 2
            and len(cited_source_identities) < 2
        ):
            diagnostics.append("insufficient_cross_source_citations")

        compact = re.sub(r"\s+", "", reply)
        progress_only = bool(re.search(
            r"^(?:好|好的|可以|没问题|稍等|等一下|等等|先等一下)?[，,。.!！]*"
            r"(?:我)?(?:现在|马上|这就|先|去)?"
            r"(?:联网|上网|在线)?(?:查|搜|搜索|检索|查询|查找)(?:一下|下)?",
            compact,
            re.I,
        ))
        retrieval_status = bool(re.search(
            r"^(?:联网|搜索|检索|查询)(?:已经|已|后|结果|到了|发现|显示|返回)|"
            r"^(?:我)?(?:查到|搜到)(?:了|的)|"
            r"(?:证据综合|检索结果).{0,18}(?:没有|未能|失败|完成)|"
            r"(?:最相关|最接近|首个)(?:的)?来源(?:是|为)",
            compact,
            re.I,
        ))
        titles = [re.sub(r"\s+", "", str(source.get("title") or "")) for source in web_sources]
        copied_title_chars = sum(len(title) for title in titles if len(title) >= 6 and title in compact)
        has_substantive_statement = web_answer_has_substantive_statement(reply, titles)
        if retrieval_status and not has_substantive_statement:
            diagnostics.append("retrieval_status_instead_of_answer")
        if progress_only and not has_substantive_statement:
            diagnostics.append("search_progress_instead_of_answer")
        if copied_title_chars >= 12 and not has_substantive_statement:
            diagnostics.append("source_inventory_instead_of_synthesis")
        return list(dict.fromkeys(diagnostics))
