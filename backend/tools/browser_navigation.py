"""浏览器导航执行与成功目的地学习。

先核对权限、公开 URL 和幂等回执，再执行导航；失败保持原错误和回执。
真正执行后才写回执和学习目的地，使用现有同库入口，不维护第二份操作记录。
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import re
import sqlite3
import urllib.parse
import uuid
import webbrowser
from typing import Any, Callable, Mapping
from agent.fingerprints import legacy_persistent_fingerprint
from backend.common.text import unique_strings
from backend.research.intent import extract_public_web_url
from tools.safe_web import SafeWebError, canonicalize_public_url, validate_public_url
from tools.web_destinations import normalize_destination_alias, resolve_web_destination_alias
from web_destination_store import learn_web_destination

@dataclass(frozen=True)
class ReceiptOperations:
    find_action_receipt_by_idempotency_key: Callable[..., Any]
    read_action_receipt_records: Callable[..., Any]
    record_action_receipt: Callable[..., Any]
    state_database: Callable[..., Any]

class BrowserNavigation:
    def __init__(self, receipts: ReceiptOperations, *, now: Callable[[], str], write_log: Callable[..., Any]) -> None:
        self.receipts = receipts
        self.now = now
        self.write_log = write_log

    def remember_opened_web_destination(
        self,
        canonical_url: str,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Remember a successfully used destination without claiming it is official."""

        if str(args.get("browserIntent") or "").strip().lower() == "search":
            return {}
        raw_aliases = [
            str(args.get("matchedAlias") or "").strip(),
            str(args.get("sourceTitle") or "").strip(),
            str(args.get("_usageAlias") or "").strip(),
            str(args.get("_usageFallbackAlias") or "").strip(),
        ]
        generic_keys = {
            normalize_destination_alias(item)
            for item in (
                "打开", "打开吧", "帮我打开", "帮我打开吧", "这个", "那个",
                "这个网站", "那个网站", "网页", "网站", "链接", "页面",
            )
        }
        aliases: list[str] = []
        for value in unique_strings(raw_aliases):
            key = normalize_destination_alias(value)
            if (
                not key
                or key in generic_keys
                or len(value) > 120
                or extract_public_web_url(value)
            ):
                continue
            curated = resolve_web_destination_alias(value)
            if curated and canonicalize_public_url(curated.get("url") or "") != canonical_url:
                # A one-off visit may not rewrite a curated well-known alias.
                continue
            aliases.append(value)
        hostname = (urllib.parse.urlsplit(canonical_url).hostname or "").removeprefix("www.")
        canonical_name = aliases[0] if aliases else hostname
        if not canonical_name:
            return {}
        try:
            with self.receipts.state_database() as connection:
                learned = learn_web_destination(
                    connection,
                    canonical_name=canonical_name,
                    canonical_url=canonical_url,
                    aliases=aliases or [hostname],
                    source_kind="observed_usage",
                    source_turn_id=str(args.get("_requestId") or "")[:160],
                )
            self.write_log(
                "web_destination_remembered",
                destinationId=str(learned.get("id") or "")[:100],
                url=canonical_url[:2000],
                aliases=list(learned.get("learnedAliases") or [])[:8],
                conflicts=list(learned.get("aliasConflicts") or [])[:8],
                sourceKind="observed_usage",
            )
            return learned
        except (sqlite3.Error, ValueError) as error:
            # Remembering usage is secondary; it must never turn a successful open
            # into a failed browser action.
            self.write_log(
                "web_destination_remember_failed",
                url=canonical_url[:2000],
                error=str(error)[:500],
            )
            return {}


    def tool_open_browser(self, settings: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        action_id = str(args.get("actionId") or f"act_{uuid.uuid4().hex}")[:100]
        action_group_id = str(args.get("_actionGroupId") or action_id)[:100]
        request_id = str(args.get("_requestId") or "").strip()[:160]
        requested_target = str(args.get("url") or args.get("query") or "").strip()[:4096]
        idempotency_key = ""

        def receipt(status: str, *, resolved: str = "", error: str = "") -> dict[str, Any]:
            return {
                "actionId": action_id,
                "tool": "open_browser",
                "actionGroupId": action_group_id,
                "retryOfActionId": str(args.get("_retryOfActionId") or "")[:100],
                "status": status,
                "requestedTarget": requested_target,
                "resolvedTarget": str(resolved)[:4096],
                "adapter": "python.webbrowser",
                "requestId": request_id,
                "idempotencyKey": idempotency_key,
                "error": str(error)[:500],
                "timestamp": self.now(),
            }

        def finish(
            payload: dict[str, Any],
            action_receipt: dict[str, Any],
            *,
            deduplicated: bool = False,
        ) -> dict[str, Any]:
            self.receipts.record_action_receipt(action_receipt)
            return {
                **payload,
                "actionReceipt": action_receipt,
                **({"actionDeduplicated": True} if deduplicated else {}),
            }

        if not settings.get("permissionOpenBrowser"):
            action_receipt = receipt("rejected", error="permission_denied")
            return finish({
                "name": "open_browser",
                "ok": False,
                "error": "permission_denied",
                "permission": "permissionOpenBrowser",
                "message": "打开默认浏览器权限未授予。",
            }, action_receipt)
        if args.get("needsConfirmation"):
            target = re.sub(r"\s+", " ", str(args.get("query") or "")).strip()[:240]
            error = "target_confirmation_required"
            action_receipt = receipt("rejected", error=error)
            return finish({
                "name": "open_browser",
                "ok": False,
                "error": error,
                "message": (
                    f"还不能确定“{target}”对应哪个网站，需要用户补充正式名称或网址。"
                    if target else
                    "还不能确定用户想打开哪个网站。"
                ),
                "clarificationQuestion": (
                    f"我不确定“{target}”具体是哪个网站。你可以告诉我它的正式名称，或者把网址发给我吗？"
                    if target else
                    "我还不知道你想打开哪个网站。可以告诉我网站名称或网址吗？"
                ),
            }, action_receipt)
        raw_url = str(args.get("url") or "").strip()
        query = re.sub(r"\s+", " ", str(args.get("query") or "")).strip()
        browser_intent = str(args.get("browserIntent") or "").strip().lower()
        if not raw_url and query and browser_intent == "search":
            raw_url = "https://duckduckgo.com/?" + urllib.parse.urlencode({"q": query[:500]})
        if not raw_url:
            error = "unresolved_browser_target" if query else "missing_url"
            action_receipt = receipt("rejected", error=error)
            return finish({
                "name": "open_browser",
                "ok": False,
                "error": error,
                "message": "还没有找到可以直接打开的网址。",
            }, action_receipt)
        try:
            parsed, _ = validate_public_url(raw_url)
            safe_url = canonicalize_public_url(urllib.parse.urlunsplit(parsed))
            if request_id:
                idempotency_key = legacy_persistent_fingerprint(json.dumps(
                    {
                        "requestId": request_id,
                        "tool": "open_browser",
                        "resolvedTarget": safe_url,
                        "browserIntent": browser_intent,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ), length=32)
                accepted = self.receipts.find_action_receipt_by_idempotency_key(idempotency_key)
                if accepted:
                    website_memory = (
                        self.remember_opened_web_destination(safe_url, args)
                        if args.get("_rememberUsage") else {}
                    )
                    action_receipt = receipt("accepted", resolved=safe_url)
                    action_receipt.update({
                        "deduplicated": True,
                        "deduplicatedFromActionId": str(accepted.get("actionId") or ""),
                    })
                    self.write_log(
                        "external_action_deduplicated",
                        requestId=request_id,
                        actionId=action_id,
                        priorActionId=str(accepted.get("actionId") or "")[:100],
                        tool="open_browser",
                        resolvedTarget=safe_url[:2000],
                    )
                    return finish({
                        "name": "open_browser",
                        "ok": True,
                        "url": safe_url,
                        "targetKind": "search" if browser_intent == "search" else "url",
                        "websiteMemory": website_memory,
                    }, action_receipt, deduplicated=True)
            previous = next(
                (
                    item for item in reversed(self.receipts.read_action_receipt_records())
                    if isinstance(item, dict)
                    and str(item.get("actionId") or "") == action_id
                    and str(item.get("resolvedTarget") or "") == safe_url
                    and str(item.get("status") or "") in {"accepted", "rejected", "failed"}
                ),
                None,
            )
            if previous:
                website_memory = (
                    self.remember_opened_web_destination(safe_url, args)
                    if previous.get("status") == "accepted" and args.get("_rememberUsage") else {}
                )
                return finish({
                    "name": "open_browser",
                    "ok": previous.get("status") == "accepted",
                    "url": safe_url,
                    "targetKind": "search" if browser_intent == "search" else "url",
                    "websiteMemory": website_memory,
                    **(
                        {}
                        if previous.get("status") == "accepted"
                        else {"error": str(previous.get("error") or "default_browser_rejected_request")}
                    ),
                }, dict(previous), deduplicated=True)
            opened = bool(webbrowser.open(safe_url, new=2))
            status = "accepted" if opened else "rejected"
            action_receipt = receipt(
                status,
                resolved=safe_url,
                error="" if opened else "default_browser_rejected_request",
            )
            self.write_log(
                "browser_open_attempt",
                actionId=action_id,
                targetKind="search" if browser_intent == "search" else "url",
                url=safe_url[:2000],
                opened=opened,
            )
            website_memory = (
                self.remember_opened_web_destination(safe_url, args)
                if opened and args.get("_rememberUsage") else {}
            )
            return finish({
                "name": "open_browser",
                "ok": opened,
                "url": safe_url,
                "targetKind": "search" if browser_intent == "search" else "url",
                "websiteMemory": website_memory,
                **({} if opened else {"error": "default_browser_rejected_request"}),
            }, action_receipt)
        except (SafeWebError, ValueError, OSError) as error:
            action_receipt = receipt("failed", error=str(error))
            return finish({
                "name": "open_browser",
                "ok": False,
                "error": str(error)[:300],
            }, action_receipt)
