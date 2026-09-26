"""把本轮导航目标、待续操作和已知来源绑定为浏览器参数。

解析层可以读取既有别名/书签和待续操作，但不打开浏览器。陌生目标继续走原确认流程。
"""
from __future__ import annotations
from dataclasses import dataclass
import re
import unicodedata
import urllib.parse
import uuid
from typing import Any, Callable, Mapping
from agent.action_grounding import PreparedTurn, interpret_user_input
from backend.common.text import unique_strings
from backend.context.recall import memory_keywords, retrieval_overlap_score
from backend.research.intent import extract_public_web_url, is_external_factual_question
from backend.research.planning import outbound_web_query
from backend.research.query import web_query_is_context_only, web_query_overlap_tokens, web_search_query_from_message
from backend.tools.browser_rules import browser_url_site_identity, semantic_task_mentions_site_identity, browser_explicit_search_query, browser_references_previous_source
from bookmark_store import resolve_bookmark
from web_destination_store import resolve_learned_web_destination
from tools.web_destinations import resolve_web_destination_alias
from tools.safe_web import canonicalize_public_url

@dataclass(frozen=True)
class StateOperations:
    current_pending_action: Callable[..., Any]
    write_pending_action_record: Callable[..., Any]
    clear_pending_action: Callable[..., Any]
    state_database: Callable[..., Any]


@dataclass(frozen=True)
class InterpretationOperations:
    action_grounding_config: Callable[..., Any]
    prepare_action_turn: Callable[..., Any]


@dataclass(frozen=True)
class SourceOperations:
    recent_assistant_web_subject: Callable[..., Any]
    recent_message_web_sources: Callable[..., Any]
    public_web_sources: Callable[..., Any]


class BrowserResolver:
    def __init__(self, state: StateOperations, interpretation: InterpretationOperations,
                 sources: SourceOperations, *, now: Callable[[], str], write_log: Callable[..., Any]) -> None:
        self.state = state
        self.interpretation = interpretation
        self.sources = sources
        self.now = now
        self.write_log = write_log

    def save_pending_browser_action(
        self,
        *,
        request_id: str,
        target_hint: str = "",
        batch_id: str = "",
        status: str = "needs_target",
        candidates: list[dict[str, Any]] | None = None,
        reuse_existing: bool = True,
    ) -> dict[str, Any]:
        existing = self.state.current_pending_action()
        existing = (
            existing
            if reuse_existing
            and isinstance(existing, dict)
            and existing.get("type") == "open_browser"
            else {}
        )
        action_id = str(existing.get("actionId") or f"act_{uuid.uuid4().hex}")[:100]
        pending = {
            "actionId": action_id,
            "type": "open_browser",
            "actionGroupId": str(existing.get("actionGroupId") or action_id),
            "status": str(status)[:40],
            "targetHint": re.sub(r"\s+", " ", str(target_hint or existing.get("targetHint") or "")).strip()[:300],
            "batchId": str(batch_id or existing.get("batchId") or "")[:100],
            "candidateSources": [
                item for item in (candidates or existing.get("candidateSources") or [])[:8]
                if isinstance(item, dict)
            ],
            "originRequestId": str(existing.get("originRequestId") or request_id)[:120],
            "updatedRequestId": str(request_id)[:120],
            "createdAt": str(existing.get("createdAt") or self.now())[:80],
            "updatedAt": self.now(),
        }
        self.state.write_pending_action_record(pending)
        self.write_log(
            "pending_action_updated" if existing else "pending_action_created",
            requestId=str(request_id)[:120],
            actionId=action_id,
            actionType="open_browser",
            status=pending["status"],
            batchId=pending["batchId"],
            targetHint=pending["targetHint"],
        )
        return pending


    def browser_args_with_usage_tracking(
        self,
        args: Mapping[str, Any],
        *,
        user_message: str,
        request_id: str,
        fallback_alias: str = "",
    ) -> dict[str, Any]:
        """Add server-owned usage metadata to a real chat navigation call."""

        tracked = dict(args)
        tracked["_requestId"] = str(request_id or "")[:160]
        tracked["_rememberUsage"] = True
        if not tracked.get("_usageAlias"):
            prepared = self.interpretation.prepare_action_turn(user_message)
            usage_alias = str(prepared.action_candidate.target_text or "").strip()
            if not usage_alias:
                usage_alias = str(fallback_alias or "").strip()
            if usage_alias:
                tracked["_usageAlias"] = usage_alias[:240]
        if fallback_alias:
            tracked["_usageFallbackAlias"] = str(fallback_alias).strip()[:240]
        return tracked


    def strip_leading_conversational_vocative(self, text: str) -> str:
        """Compatibility view backed by the bounded structural vocative parser."""

        return interpret_user_input(
            str(text or ""),
            self.interpretation.action_grounding_config(),
        ).action_parse_text


    def browser_named_target_phrase(self, text: str) -> str:
        value = self.strip_leading_conversational_vocative(text)
        value = re.sub(
            r"^(?:(?:请|麻烦|拜托|帮我|你|给我|我叫你)[，,\s]*)*"
            r"(?:(?:用|在)?(?:默认)?浏览器(?:里|中)?\s*)?"
            r"(?:打开|访问|进入|跳转到?)\s*",
            "",
            value,
            flags=re.I,
        )
        value = re.sub(
            r"(?:一下)?(?:吧|好吗|可以吗|行吗|谢谢)?[。！？!?]*$",
            "",
            value,
            flags=re.I,
        ).strip(" ，。！？!?")
        return value[:240]


    def browser_named_target_identity(self, text: str) -> str:
        """Return one conservative ASCII identity from an explicit named target."""

        phrase = self.browser_named_target_phrase(text)
        phrase = re.sub(
            r"(?:官方网站|官网|网站|网页|主页|网页版)$",
            "",
            phrase,
            flags=re.I,
        ).strip()
        normalized = unicodedata.normalize("NFKC", phrase).casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9\s_.+\-]{0,119}", normalized):
            return ""
        return re.sub(r"[^a-z0-9]+", "", normalized)


    def grounded_model_browser_url(
        self,
        value: Any,
        user_message: str,
        resolved_task: str = "",
    ) -> str:
        """Accept a model URL only when its site identity matches this turn's target.

        This is positive current-turn grounding, not a general model-URL trust path.
        Full public-network and SSRF validation still belongs to ``tool_open_browser``.
        """

        target_identities = {
            identity
            for identity in (
                self.browser_named_target_identity(user_message),
                self.browser_named_target_identity(resolved_task),
            )
            if identity
        }
        site_identity = browser_url_site_identity(value)
        if (
            site_identity not in target_identities
            and not semantic_task_mentions_site_identity(resolved_task, site_identity)
            and not semantic_task_mentions_site_identity(self.browser_named_target_phrase(user_message), site_identity)
        ):
            return ""
        return canonicalize_public_url(str(value or "").strip())


    def known_browser_destination(self, text: str) -> dict[str, str] | None:
        phrase = self.browser_named_target_phrase(text)
        candidates = [
            phrase,
            re.sub(r"(?:官方网站|官网|网站|网页|主页|网页版)$", "", phrase, flags=re.I).strip(),
        ]
        for candidate in unique_strings(candidates):
            destination = resolve_web_destination_alias(candidate)
            if destination:
                return destination
            with self.state.state_database() as connection:
                learned = resolve_learned_web_destination(connection, candidate)
            if learned:
                return learned
        return None


    def explicit_domain_destination(self, text: str) -> str:
        phrase = self.browser_named_target_phrase(text)
        phrase = re.sub(r"(?:官方网站|官网|网站|网页|主页)$", "", phrase, flags=re.I).strip()
        if not re.fullmatch(
            r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})\.)+"
            r"[A-Za-z]{2,24}(?::\d{1,5})?(?:/[^\s]*)?",
            phrase,
        ):
            return ""
        return f"https://{phrase}"[:4096]


    def browser_target_requires_confirmation(self, text: str) -> bool:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not re.search(r"打开|访问|进入|跳转", value, re.I):
            return False
        if extract_public_web_url(value) or self.explicit_domain_destination(value) or self.known_browser_destination(value):
            return False
        if re.search(r"文件|文件夹|目录|工作区|本机|电脑里|[A-Za-z]:\\", value, re.I):
            return False
        phrase = self.browser_named_target_phrase(value)
        if not phrase:
            return False
        # A paper, source, or prior-page reference has enough type information to
        # enter the source resolver. A bare unfamiliar brand/token does not.
        if re.search(
            r"论文|文献|来源|链接|页面|网页|网站|官网|主页|搜索结果|"
            r"arxiv|openreview|doi|github",
            phrase,
            re.I,
        ):
            return False
        return bool(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9._+\- ]{1,80}", phrase)
            or re.search(r"(?:站|网)$", phrase)
        )


    def browser_target_confirmation_response(self, text: str) -> str:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if re.search(r"不是(?:网站|网页)|不是，|不是,|不用了|算了|取消|别打开", value, re.I):
            return "rejected"
        normalized = value.strip(" \t\r\n，。！？!?;；").casefold()
        confirmation_markers = {
            marker.casefold()
            for marker in self.interpretation.action_grounding_config().get("confirmationMarkers", [])
            if isinstance(marker, str) and marker.strip()
        }
        if normalized in confirmation_markers or re.search(
            r"^(?:对|是|没错|嗯|对的|是的|确认|确定)[，,\s]*(?:它)?(?:就)?是"
            r"(?:一个)?(?:网站|网页|站点)|(?:它)?是(?:一个)?(?:网站|网页|站点)",
            value,
            re.I,
        ):
            return "confirmed_website"
        return ""


    def is_open_browser_query(self, text: str) -> bool:
        prepared = self.interpretation.prepare_action_turn(text, pending_action={})
        if prepared.action_candidate.negated or prepared.action_candidate.diagnostic:
            return False
        if prepared.action_candidate.executable:
            return True
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if self.known_browser_destination(value) or self.explicit_domain_destination(value):
            return bool(re.search(r"打开|访问|进入|跳转", value, re.I))
        if self.browser_target_requires_confirmation(value):
            return True
        if re.search(
            r"(?:打开|启动|唤起).{0,12}(?:浏览器|网页|网站|链接)|"
            r"(?:用|在).{0,8}浏览器.{0,12}(?:打开|搜索|查找)|"
            r"(?:浏览器).{0,10}(?:打开|搜索|查找)|"
            r"(?:打开|访问|进入|跳转到?).{0,24}"
            r"(?:网页|网站|链接|页面|论文|文献|来源|论文页|文献页|来源页|搜索结果)",
            value,
            re.I,
        ):
            return True
        # Natural follow-ups often separate the web object from the final action:
        # "查几篇论文给我参考，帮我打开一下吧".  Requiring the noun to sit
        # immediately after "打开" routes that request through the tool-less chat
        # path, where the model can only pretend that it opened something.
        has_web_object = bool(re.search(
            r"浏览器|网页|网站|链接|页面|论文|文献|arxiv|openreview|doi|搜索结果|来源",
            value,
            re.I,
        ))
        has_open_action = bool(re.search(
            r"(?:帮我|请|麻烦|你来|给我)?.{0,16}(?:打开|访问|进入|跳转)(?:一下)?(?:吧|好吗|可以吗)?(?:[。！？!?]|$)",
            value,
            re.I,
        ))
        has_local_file_object = bool(re.search(
            r"文件|文件夹|目录|工作区|[A-Za-z]:\\|[\w.-]+\.(?:txt|md|json|csv|py|js|html|pdf)",
            value,
            re.I,
        ))
        return bool(has_web_object and has_open_action and not has_local_file_object)


    def browser_target_resolution_query(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> str:
        """Build a narrow internal lookup target, never the whole user sentence."""

        value = re.sub(r"\s+", " ", str(user_message or "")).strip()
        explicit_arxiv = re.search(r"\b(?:arxiv\s*[:：]?\s*)?(\d{4}\.\d{4,5})(?:v\d+)?\b", value, re.I)
        if explicit_arxiv:
            return f"arXiv {explicit_arxiv.group(1)}"
        explicit_acronym = re.search(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b", value, re.I)
        if explicit_acronym:
            return explicit_acronym.group(0)[:240]
        quoted = re.search(r"[“\"']([^“”\"']{2,160})[”\"']", value)
        if quoted:
            return quoted.group(1).strip()[:240]
        value = re.sub(r"^(?:助手|阿玛迪乌斯)[，,]?\s*", "", value, flags=re.I)
        value = re.sub(
            r"^(?:(?:请|麻烦|帮我|你|给我|我叫你)[，,\s]*)*"
            r"(?:(?:用|在)?(?:默认)?浏览器(?:里|中)?\s*)?"
            r"(?:打开|访问|进入|跳转到?)\s*",
            "",
            value,
            flags=re.I,
        )
        value = re.sub(
            r"(?:这个|那个|对应的?|刚才的?|上面的?|前面的?)?"
            r"(?:网站|网页|链接|页面|论文页|文献页|来源页)"
            r"(?:一下)?(?:吧|好吗|可以吗)?[。！？!?]*$",
            "",
            value,
            flags=re.I,
        )
        value = value.strip(" ，。！？!?")
        if browser_references_previous_source(user_message):
            deictic_only = re.sub(
                r"刚才|刚刚|上面|前面|提到|说到|找到|查到|推荐|对应|"
                r"这个|那个|这篇|那篇|这条|那条|其中|的|论文|文献|"
                r"网站|网页|链接|页面|一下|吧",
                "",
                value,
                flags=re.I,
            ).strip(" ，。！？!?")
            if not deictic_only:
                prior_subject = self.sources.recent_assistant_web_subject(user_message, messages)
                if prior_subject and not web_query_is_context_only(prior_subject):
                    return prior_subject[:240]
        if not value or re.fullmatch(
            r"(?:这个|那个|这篇|那篇|那条|对应的?|刚才的?|刚刚的?|上面的?|前面的?|"
            r"链接|页面|网页|网站|论文|文献|arxiv论文)",
            value,
            re.I,
        ):
            prior_subject = self.sources.recent_assistant_web_subject(user_message, messages)
            if prior_subject and not web_query_is_context_only(prior_subject):
                return prior_subject[:240]
            pending = self.state.current_pending_action()
            hint = str(pending.get("targetHint") or "").strip()
            return (
                hint[:240]
                if hint and not web_query_is_context_only(hint)
                else ""
            )
        return value[:240]


    def preferred_browser_source(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None,
    ) -> dict[str, str] | None:
        sources, prior_answer = self.sources.recent_message_web_sources(messages)
        if not sources:
            return None
        requested_id_match = re.search(r"\bS(\d+)\b", str(user_message or ""), re.I)
        if requested_id_match:
            requested_id = f"S{requested_id_match.group(1)}".upper()
            selected = next(
                (item for item in sources if item.get("id", "").upper() == requested_id),
                None,
            )
            if selected:
                return selected
        ordinal_words = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8,
        }
        ordinal_match = re.search(r"第\s*([一二两三四五六七八]|\d+)\s*(?:个|篇|条|项|份)?", user_message)
        if ordinal_match:
            token = ordinal_match.group(1)
            ordinal = int(token) if token.isdigit() else ordinal_words.get(token, 0)
            if 1 <= ordinal <= len(sources):
                return sources[ordinal - 1]

        user_terms = memory_keywords(user_message)
        prior_compact = re.sub(r"\s+", "", prior_answer).lower()
        wants_paper = bool(re.search(r"论文|文献|paper|arxiv|openreview|doi", user_message, re.I))
        wants_code = bool(re.search(r"代码|仓库|repo|repository|github", user_message, re.I))
        wants_official = bool(re.search(r"官网|官方(?:页面|网站|文档)?", user_message, re.I))
        paper_domains = {
            "arxiv.org", "openreview.net", "doi.org", "aclanthology.org",
            "proceedings.mlr.press", "dl.acm.org", "ieeexplore.ieee.org",
        }
        scored: list[tuple[float, int, dict[str, str]]] = []
        for index, source in enumerate(sources):
            title = source.get("title", "")
            domain = source.get("domain", "").lower()
            source_terms = memory_keywords(f"{title} {domain}")
            overlap = retrieval_overlap_score(user_terms, source_terms)
            title_compact = re.sub(r"\s+", "", title).lower()
            mentioned_bonus = 0.0
            if title_compact and len(title_compact) >= 4 and title_compact in prior_compact:
                mentioned_bonus = 5.0
            else:
                distinctive = [
                    term for term in source_terms
                    if len(term) >= 4 and re.search(r"[a-z0-9-]", term, re.I)
                ]
                if any(term.lower() in prior_answer.lower() for term in distinctive):
                    mentioned_bonus = 3.0
            paper_bonus = 3.5 if wants_paper and domain in paper_domains else 0.0
            code_bonus = 3.5 if wants_code and domain in {"github.com", "gitlab.com"} else 0.0
            official_bonus = (
                2.5
                if wants_official
                and (
                    source.get("sourceTier") == "A"
                    or re.search(r"official|官网|官方", f"{title} {source.get('sourceType') or ''}", re.I)
                )
                else 0.0
            )
            scored.append((
                overlap + mentioned_bonus + paper_bonus + code_bonus + official_bonus,
                -index,
                source,
            ))
        ranked = sorted(scored, key=lambda row: (row[0], row[1]), reverse=True)
        if not ranked:
            return None
        if len(sources) == 1:
            return ranked[0][2]
        best_score = ranked[0][0]
        second_score = ranked[1][0]
        if best_score <= 0 or abs(best_score - second_score) < 0.25:
            tier_a = [source for source in sources if source.get("sourceTier") == "A"]
            return tier_a[0] if len(tier_a) == 1 else None
        return ranked[0][2]


    def pending_browser_action_continuation(self, user_message: str) -> bool:
        pending = self.state.current_pending_action()
        if pending.get("type") != "open_browser":
            return False
        value = re.sub(r"\s+", " ", str(user_message or "")).strip()
        if not value:
            return False
        if str(pending.get("status") or "") == "needs_confirmation":
            confirmation = self.browser_target_confirmation_response(value)
            if confirmation == "rejected":
                self.state.clear_pending_action(str(pending.get("actionId") or ""))
                return False
            if confirmation == "confirmed_website":
                return True
        if extract_public_web_url(value):
            return False
        if re.search(r"算了|取消|不用打开|别打开|先不打开|停止", value):
            self.state.clear_pending_action(str(pending.get("actionId") or ""))
            return False
        if (
            re.search(r"读取|阅读|总结|概括|分析|解释", value, re.I)
            and not re.search(r"打开|访问|进入|跳转", value, re.I)
        ):
            return False
        has_action = bool(re.search(r"打开|访问|进入|跳转", value, re.I))
        has_source_selector = bool(re.search(
            r"\bS\d+\b|第[一二两三四五六七八九十\d]+(?:个|篇|条)?",
            value,
            re.I,
        ))
        has_web_object = bool(re.search(
            r"链接|网页|网站|页面|论文|文献|arxiv|openreview|doi|github",
            value,
            re.I,
        ))
        has_deictic = bool(re.search(
            r"那篇|这篇|那条|这条|那个|这个|对应|刚才|刚刚|上面|前面",
            value,
            re.I,
        ))
        has_continuation_shape = bool(
            has_action
            or has_source_selector
            or (has_deictic and has_web_object)
        )
        if not has_continuation_shape:
            return False
        if has_action and not has_source_selector and not has_deictic:
            named_target = re.sub(
                r"^(?:请|麻烦|帮我|你|给我|我叫你|用浏览器|在浏览器里)*\s*"
                r"(?:打开|访问|进入|跳转到?)\s*",
                "",
                value,
                flags=re.I,
            )
            named_target = re.sub(
                r"(?:网站|网页|链接|页面|论文|文献)(?:一下)?(?:吧|好吗|可以吗)?[。！？!?]*$",
                "",
                named_target,
                flags=re.I,
            ).strip(" ，。！？!?")
            if named_target:
                hint = str(pending.get("targetHint") or "")
                if not (
                    web_query_overlap_tokens(named_target)
                    & web_query_overlap_tokens(hint)
                ):
                    return False
        # A complete unrelated factual question starts a new task even while an
        # older browser action is waiting for clarification.
        if (
            len(value) > 100
            and is_external_factual_question(value)
            and not re.search(r"打开|链接|网页|页面|论文|文献", value, re.I)
        ):
            return False
        return True


    def browser_action_args(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        prepared_turn: PreparedTurn | None = None,
    ) -> dict[str, Any]:
        prepared = prepared_turn or self.interpretation.prepare_action_turn(user_message)
        candidate = prepared.action_candidate
        if candidate.negated or candidate.diagnostic:
            return {}
        target_text = candidate.target_text or prepared.interpreted.action_parse_text
        if candidate.action_type in {"open_target", "open_browser"} and target_text:
            with self.state.state_database() as connection:
                saved_destination = resolve_bookmark(connection, target_text)
            if saved_destination:
                return {
                    "url": str(saved_destination.get("url") or ""),
                    "bookmarkId": str(saved_destination.get("bookmarkId") or ""),
                    "sourceTitle": str(saved_destination.get("name") or ""),
                    "matchedAlias": str(saved_destination.get("name") or ""),
                }
            if candidate.action_type == "open_target":
                learned_or_curated = self.known_browser_destination(f"打开 {target_text}")
                if learned_or_curated:
                    return {
                        "url": learned_or_curated["url"],
                        "destinationId": learned_or_curated["id"],
                        "sourceTitle": learned_or_curated["title"],
                        "matchedAlias": learned_or_curated["matchedAlias"],
                    }
        if (
            candidate.action_type not in {"open_browser", "browser_search"}
            and prepared.pending_relation.relation != "continue"
        ):
            return {}
        user_url = extract_public_web_url(user_message)
        if user_url:
            return {"url": user_url}
        known_destination = self.known_browser_destination(f"打开 {target_text}")
        if known_destination:
            return {
                "url": known_destination["url"],
                "destinationId": known_destination["id"],
                "sourceTitle": known_destination["title"],
                "matchedAlias": known_destination["matchedAlias"],
            }
        domain_destination = self.explicit_domain_destination(f"打开 {target_text}")
        if domain_destination:
            return {
                "url": domain_destination,
                "sourceTitle": urllib.parse.urlsplit(domain_destination).hostname or domain_destination,
            }
        explicit_search = (
            candidate.target_text
            if candidate.action_type == "browser_search"
            else browser_explicit_search_query(prepared.interpreted.action_parse_text)
        )
        if explicit_search:
            return {
                "query": explicit_search,
                "browserIntent": "search",
            }
        pending_continuation = prepared.pending_relation.relation == "continue"
        pending = self.state.current_pending_action() if pending_continuation else {}
        if (
            pending.get("type") == "open_browser"
            and pending.get("status") == "needs_confirmation"
            and self.browser_target_confirmation_response(user_message) == "confirmed_website"
        ):
            hint = str(pending.get("targetHint") or "").strip()
            confirmed_known = self.known_browser_destination(f"打开 {hint}")
            if confirmed_known:
                return {
                    "url": confirmed_known["url"],
                    "destinationId": confirmed_known["id"],
                    "sourceTitle": confirmed_known["title"],
                    "matchedAlias": confirmed_known["matchedAlias"],
                }
            return {
                "resolveQuery": hint,
                "confirmedAsWebsite": True,
            } if hint else {}
        combined_research_open = bool(
            candidate.action_type == "open_browser"
            and not candidate.target_text
            and re.search(r"查|搜索|检索|研究", prepared.interpreted.action_parse_text, re.I)
        )
        if combined_research_open:
            query = (
                self.browser_target_resolution_query(prepared.interpreted.action_parse_text, messages)
                or outbound_web_query(web_search_query_from_message(prepared.interpreted.action_parse_text))
            )
            return {"resolveQuery": query} if query else {"resolveQuery": prepared.interpreted.action_parse_text}
        # A pending action says that a browser workflow exists; it does not say
        # that the user is referring to an older source.  Only an explicit source
        # selector/deictic reference may bind a URL from conversation history.
        prior_source = (
            self.preferred_browser_source(user_message, messages)
            if browser_references_previous_source(user_message)
            else None
        )
        if prior_source:
            return {
                "url": prior_source["url"],
                "sourceId": prior_source.get("id", ""),
                "sourceUid": prior_source.get("sourceUid", ""),
                "batchId": prior_source.get("batchId", ""),
                "sourceTitle": prior_source.get("title", ""),
            }
        if self.browser_target_requires_confirmation(prepared.interpreted.action_parse_text):
            target = candidate.target_text or self.browser_named_target_phrase(prepared.interpreted.action_parse_text)
            return {
                "query": target,
                "needsConfirmation": True,
                "browserIntent": "clarify",
            }
        resolution_query = (
            self.browser_target_resolution_query(prepared.interpreted.action_parse_text, messages)
            or (
                candidate.target_text
                if candidate.action_type == "open_browser" and candidate.target_text
                else ""
            )
        )
        return {"resolveQuery": resolution_query} if resolution_query else {}


    def browser_action_args_from_tool_results(
        self,
        user_message: str,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sources = self.sources.public_web_sources(results)
        if not sources:
            return {}
        synthetic_messages = [{
            "role": "assistant",
            "content": "\n".join(str(item.get("title") or "") for item in sources),
            "meta": {"sources": sources},
        }]
        selected = self.preferred_browser_source(user_message, synthetic_messages)
        if not selected:
            return {}
        return {
            "url": selected["url"],
            "sourceId": selected.get("id", ""),
            "sourceUid": selected.get("sourceUid", ""),
            "batchId": selected.get("batchId", ""),
            "sourceTitle": selected.get("title", ""),
        }
