"""绑定模型工具请求到当前轮授权、真实来源与持久回执。

这里保序筛选和规范参数，不执行工具。执行器仍负责权限、文件范围和网络地址检查。
认知计划与非认知请求沿用原分支；已有赛事字段仍随通用查证参数传递。
"""
from __future__ import annotations
from dataclasses import dataclass
import urllib.parse
from typing import Any, Callable, Mapping
from agent.tool_architecture import DEFAULT_TOOL_REGISTRY
from agent.todo_architecture import (
    TodoCommand, TodoTargetRef, classify_todo_request, classify_todo_request_with_context,
    recent_todo_action_context, recent_todo_task_ids, todo_request_targets_recent_receipt,
    todo_mutation_prohibited,
)
from agent.cognitive_runtime import task_tool_violations
from agent.cognitive_tools import canonical_tool_arguments, tool_step_binding
from agent.tool_arguments import file_tool_argument_error
from agent.action_identity import browser_retry_identity
from backend.research.query import web_search_freshness
from backend.research.intent import extract_public_web_url, web_research_explicitly_disabled
from backend.research.planning import fallback_web_research_query, safe_planned_web_query
from backend.tools.validation import structured_tool_authorization, structured_browser_authorized
from bookmark_store import resolve_bookmark
from tools.safe_web import SafeWebError, canonicalize_public_url, validate_public_url

def cognitive_readonly_file_plan_authorized(
    state: Mapping[str, Any] | None,
    request: Mapping[str, Any],
) -> bool:
    """Authorize a read-only file continuation from the current task plan.

    A cognitive continuation may carry no lexical file wording and may omit a
    repeated tool selection.  The plan is still a bounded authorization: it
    must belong to the current active task, match exactly one executable step
    after schema normalization, and satisfy all task restrictions.  This
    helper only covers read-only file tools; settings permissions and the
    executor's validation remain authoritative.
    """

    if not isinstance(state, Mapping) or not state:
        return False
    task = state.get("task")
    if not isinstance(task, Mapping):
        return False
    if str(task.get("status") or "").strip().lower() != "active":
        return False
    if not str(task.get("taskId") or "").strip():
        return False
    name = str(request.get("name") or request.get("tool") or "").strip()
    if name not in {"list_directory", "read_text_file"}:
        return False
    if not isinstance(request.get("args"), Mapping):
        return False
    # This enforces restrictions, invalidation, executable status, and the
    # unique canonical argument match before the continuation can bypass the
    # user-message classifier.
    if task_tool_violations(state, [request], DEFAULT_TOOL_REGISTRY):
        return False
    binding = tool_step_binding(task, request)
    return bool(
        str(binding.get("taskId") or "").strip() == str(task.get("taskId") or "").strip()
        and str(binding.get("stepId") or "").strip()
    )


@dataclass(frozen=True)
class BrowserOperations:
    browser_action_args: Callable[..., Any]
    browser_named_target_phrase: Callable[..., Any]
    browser_url_site_identity: Callable[..., Any]
    grounded_model_browser_url: Callable[..., Any]
    is_agent_file_action_query: Callable[..., Any]
    is_open_browser_query: Callable[..., Any]
    known_browser_destination: Callable[..., Any]
    pending_browser_action_continuation: Callable[..., Any]


@dataclass(frozen=True)
class StateOperations:
    current_pending_action: Callable[..., Any]
    state_database: Callable[..., Any]


class ModelToolRequestConstraints:
    def __init__(self, browser: BrowserOperations, state: StateOperations, *,
                 screen_history_mode: Callable[[str], str], file_max_bytes: Callable[[], int],
                 write_log: Callable[..., Any]) -> None:
        self.browser = browser
        self.state = state
        self.screen_history_mode = screen_history_mode
        self.file_max_bytes = file_max_bytes
        self.write_log = write_log

    def constrain_model_tool_requests(
        self,
        requests: list[dict[str, Any]],
        user_message: str,
        resolution: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        schedule_decision: Mapping[str, Any] | None = None,
        *,
        cognitive_state: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Apply the current task's restrictions to model-selected operations.

        Query validation stays here; permission and public-URL validation also
        run in the actual network executor before any page is fetched.
        """

        constrained: list[dict[str, Any]] = []
        resolution = resolution or {"active": False}
        user_url = extract_public_web_url(user_message)
        used_page_tool = False
        web_query_count = 0
        semantic_schedule = (
            schedule_decision
            if isinstance(schedule_decision, Mapping)
            and not bool(schedule_decision.get("omitted"))
            else {}
        )
        semantic_intent = (
            semantic_schedule.get("intent")
            if isinstance(semantic_schedule.get("intent"), Mapping)
            else {}
        )
        semantic_policy = (
            semantic_schedule.get("policy")
            if isinstance(semantic_schedule.get("policy"), Mapping)
            else {}
        )
        schedule_authoritative = bool(semantic_intent and semantic_policy)
        todo_kind = (
            str(semantic_intent.get("kind") or "none")
            if schedule_authoritative
            else classify_todo_request_with_context(user_message, messages)
        )
        direct_todo_kind = classify_todo_request(user_message)
        recent_todo = recent_todo_action_context(messages)
        bind_recent_todo = (
            False
            if schedule_authoritative
            else todo_request_targets_recent_receipt(user_message, messages)
        )
        recent_task_ids = list(recent_todo_task_ids(messages))
        pending_todo = self.state.current_pending_action()
        pending_todo = pending_todo if pending_todo.get("type") == "todo_mutation" else {}
        pending_candidates = [
            item for item in pending_todo.get("candidateTasks", [])
            if isinstance(item, Mapping) and str(item.get("taskId") or "").strip()
        ]
        if pending_candidates:
            recent_task_ids = [
                *[str(item.get("taskId") or "") for item in pending_candidates],
                *recent_task_ids,
            ]
            recent_task_ids = list(dict.fromkeys(item for item in recent_task_ids if item))[:12]
        for request in requests:
            name = str(request.get("name") or "")
            args = request.get("args") if isinstance(request.get("args"), dict) else {}
            if name == "todo_manage" and schedule_authoritative and (
                str(semantic_policy.get("action") or "") != "execute"
                or not bool(semantic_policy.get("allowed"))
            ):
                continue
            if cognitive_state and name in {"todo_manage", "todo_list"}:
                if task_tool_violations(cognitive_state, [request], DEFAULT_TOOL_REGISTRY):
                    continue
                # The brain owns the requested operation and target. A sentence
                # may deny deletion or completion while authorizing an update;
                # a whole-sentence lexical veto cannot represent that distinction.
                if name == "todo_manage":
                    if structured_tool_authorization(request) != "write" or not bool(args.get("commitIntent") or args.get("commit_intent")):
                        continue
                    try:
                        command = TodoCommand.from_mapping(args)
                    except ValueError:
                        continue
                    if command.action != "list":
                        # The executor deserializes TodoCommand too. Expanding
                        # defaults here would make arguments differ from the
                        # model's already-bound task step before execution.
                        constrained.append({**request, "args": dict(args)})
                else:
                    constrained.append({**request, "args": dict(args)})
                continue
            if cognitive_state and name == "open_browser":
                if task_tool_violations(cognitive_state, [request], DEFAULT_TOOL_REGISTRY):
                    continue
                # The cognitive plan owns target interpretation, including aliases
                # and conversational references. Do not replace its URL with a
                # lexical resolver's search/confirmation operation after planning.
                # Explicit user URLs retain precedence; execution still checks the
                # plan, browser permission and public URL validity.
                browser_args = {
                    key: value for key, value in args.items()
                    if not key.startswith("_") and key not in {"actionId", "actionGroupId"}
                }
                if user_url:
                    browser_args = {"url": user_url}
                if isinstance(browser_args.get("url"), str):
                    try:
                        browser_args["url"] = canonicalize_public_url(browser_args["url"])
                    except ValueError:
                        pass  # Let the browser executor report invalid URL arguments.
                constrained.append({**request,
                    "args": {**browser_args, **browser_retry_identity(request, evidence or {})},
                    "selection": {**dict(request.get("selection") or {}), "toolAuthorization": "navigate", "targetDomain": "browser"},
                })
                continue
            if cognitive_state and name in {"web_search", "read_web_page"}:
                if task_tool_violations(cognitive_state, [request], DEFAULT_TOOL_REGISTRY):
                    continue
                if name == "web_search":
                    # The brain owns reformulation/translation; the output still
                    # passes the existing outbound credential and size checks.
                    query = safe_planned_web_query(args.get("query"), user_message, str(args.get("query") or ""))
                    if query and web_query_count < 3:
                        constrained.append({**request, "args": {**args, "query": query}})
                        web_query_count += 1
                elif str(args.get("url") or "").strip():
                    # An authorized research plan may try a known document URL
                    # directly, including when search has returned no results.
                    # Fetching is what establishes evidence; prior discovery is
                    # not an additional permission. The executor still checks
                    # web permission, public addresses, DNS and every redirect.
                    constrained.append(dict(request))
                continue
            if name in {"web_search", "read_web_page"} and web_research_explicitly_disabled(user_message):
                continue
            if name == "web_search":
                if web_query_count >= 3:
                    continue
                model_query = safe_planned_web_query(
                    args.get("query"),
                    user_message,
                    str(resolution.get("resolvedQuestion") or ""),
                    require_overlap=web_query_count == 0,
                )
                if not model_query:
                    model_query = fallback_web_research_query(user_message, resolution, messages)
                if not model_query:
                    continue
                constrained.append({
                    **request,
                    "args": {
                        "query": model_query,
                        "freshness": web_search_freshness(
                            f"{user_message} {resolution.get('resolvedQuestion') or ''}"
                        ),
                        "plannedResearch": True,
                        "sportsIntent": str(resolution.get("intent") or "") if resolution.get("kind") == "sports" else "",
                        "sportsSubject": str(resolution.get("subject") or "") if resolution.get("kind") == "sports" else "",
                        "sportsEntities": list(resolution.get("entities") or []) if resolution.get("kind") == "sports" else [],
                        "analysisFocus": list(resolution.get("analysisFocus") or []) if resolution.get("kind") == "sports" else [],
                    },
                })
                web_query_count += 1
            elif name == "read_web_page":
                if used_page_tool or not user_url:
                    continue
                constrained.append({**request, "args": {"url": user_url}})
                used_page_tool = True
            elif name == "capture_screen" and not cognitive_state and self.screen_history_mode(user_message) == "history_only":
                continue
            elif name == "open_browser":
                structured_navigation = structured_browser_authorized(request)
                resolved_browser_args = self.browser.browser_action_args(user_message, messages)
                saved_bookmark_navigation = bool(resolved_browser_args.get("bookmarkId"))
                if not (
                    self.browser.is_open_browser_query(user_message)
                    or self.browser.pending_browser_action_continuation(user_message)
                    or structured_navigation
                    or saved_bookmark_navigation
                ):
                    continue
                proposed_model_url = str(args.get("url") or "").strip()
                grounded_proposed_url = (
                    self.browser.grounded_model_browser_url(
                        proposed_model_url,
                        user_message,
                        str(resolution.get("resolvedQuestion") or "")
                        if resolution.get("active") else "",
                    )
                    if structured_navigation and proposed_model_url
                    else ""
                )
                browser_target_still_unresolved = bool(
                    not resolved_browser_args
                    or resolved_browser_args.get("needsConfirmation")
                    or (
                        resolved_browser_args.get("resolveQuery")
                        and not resolved_browser_args.get("url")
                    )
                )
                explicit_user_url = extract_public_web_url(user_message)
                same_resolved_site = bool(
                    grounded_proposed_url
                    and self.browser.browser_url_site_identity(resolved_browser_args.get("url"))
                    == self.browser.browser_url_site_identity(grounded_proposed_url)
                )
                if grounded_proposed_url and not explicit_user_url and (browser_target_still_unresolved or same_resolved_site):
                    # The current user named a site and the model supplied a URL
                    # whose site identity is the same target. Preserve its chosen
                    # documentation/subpage rather than replacing it with a root
                    # alias; an explicit user URL still takes precedence.
                    grounded_alias = self.browser.browser_named_target_phrase(user_message)
                    resolved_browser_args = {
                        "url": grounded_proposed_url,
                        **(
                            {"sourceTitle": grounded_alias, "matchedAlias": grounded_alias}
                            if grounded_alias else {}
                        ),
                    }
                    self.write_log(
                        "browser_model_target_grounded",
                        requestedTarget=self.browser.browser_named_target_phrase(user_message),
                        resolvedTarget=self.browser.browser_named_target_phrase(
                            str(resolution.get("resolvedQuestion") or "")
                        ),
                        proposedHost=urllib.parse.urlsplit(grounded_proposed_url).hostname or "",
                    )
                elif (
                    structured_navigation
                    and proposed_model_url
                    and browser_target_still_unresolved
                ):
                    self.write_log(
                        "browser_model_target_rejected",
                        requestedTarget=self.browser.browser_named_target_phrase(user_message),
                        proposedHost=(
                            urllib.parse.urlsplit(proposed_model_url).hostname or ""
                        )[:240],
                        reason="proposed_url_not_grounded_in_current_named_target",
                    )
                if structured_navigation and not resolved_browser_args:
                    current_url = extract_public_web_url(user_message)
                    known_destination = self.browser.known_browser_destination(user_message)
                    if current_url:
                        resolved_browser_args = {"url": current_url}
                    elif known_destination:
                        resolved_browser_args = {
                            "url": known_destination["url"],
                            "destinationId": known_destination["id"],
                            "sourceTitle": known_destination["title"],
                            "matchedAlias": known_destination["matchedAlias"],
                        }
                    else:
                        grounded_query = safe_planned_web_query(
                            str(args.get("query") or args.get("resolveQuery") or ""),
                            user_message,
                            str(resolution.get("resolvedQuestion") or ""),
                            require_overlap=True,
                        )
                        if grounded_query:
                            resolved_browser_args = {"resolveQuery": grounded_query}
                if not resolved_browser_args:
                    self.write_log(
                        "browser_model_request_omitted",
                        requestedTarget=self.browser.browser_named_target_phrase(user_message),
                        reason="no_grounded_or_resolvable_browser_target",
                    )
                    continue
                constrained_request: dict[str, Any] = {
                    **request,
                    "name": name,
                    "args": {**resolved_browser_args, **browser_retry_identity(request, evidence or {})},
                }
                if isinstance(request.get("selection"), Mapping):
                    constrained_request["selection"] = dict(request["selection"])
                constrained.append(constrained_request)
            elif name in {"list_directory", "read_text_file", "create_directory", "write_text_file"}:
                authorization = structured_tool_authorization(request)
                structured_file_authorized = (
                    authorization in {"read", "write"}
                    if name in {"list_directory", "read_text_file"}
                    else authorization == "write"
                )
                cognitive_read_plan_authorized = (
                    name in {"list_directory", "read_text_file"}
                    and cognitive_readonly_file_plan_authorized(cognitive_state, request)
                )
                if not (
                    self.browser.is_agent_file_action_query(user_message)
                    or structured_file_authorized
                    or cognitive_read_plan_authorized
                ):
                    continue
                if file_tool_argument_error(name, args):
                    constrained.append({**request, "args": dict(args)})
                    continue  # Execution will report the argument error without touching the filesystem.
                safe_args = canonical_tool_arguments(name, args)
                safe_args["path"] = safe_args["path"][:1000]
                if name == "write_text_file":
                    safe_args["content"] = safe_args["content"][:self.file_max_bytes()]
                constrained_request = {**request, "args": safe_args}
                if isinstance(request.get("selection"), Mapping):
                    constrained_request["selection"] = dict(request["selection"])
                constrained.append(constrained_request)
            elif name == "todo_search":
                try:
                    target = TodoTargetRef.from_mapping(
                        args.get("targetRef") or args.get("target_ref") or args
                    ).to_dict()
                except ValueError:
                    continue
                # Recent references are server-owned conversation state. Never let
                # a model smuggle arbitrary IDs into a relative selector.
                target["recentTaskIds"] = recent_task_ids
                constrained.append({
                    **request,
                    "args": {**canonical_tool_arguments(name, args), "targetRef": target},
                })
            elif name == "todo_list":
                if todo_kind == "none" or (
                    schedule_authoritative
                    and str(semantic_policy.get("action") or "") != "read"
                ):
                    continue
                scope = str(args.get("scope") or "all").strip().lower()
                constrained.append({
                    **request,
                    "args": {"scope": scope if scope in {"active", "paused", "completed", "all"} else "all"},
                })
            elif name == "todo_manage":
                safe_args = dict(args)
                selected_pending_target = False
                if pending_todo:
                    intended = (
                        dict(pending_todo.get("intendedCommand"))
                        if isinstance(pending_todo.get("intendedCommand"), Mapping)
                        else {}
                    )
                    target_input = safe_args.get("targetRef") or safe_args.get("target_ref") or {}
                    target = TodoTargetRef.from_mapping(target_input).to_dict()
                    selected: Mapping[str, Any] | None = None
                    if target.get("ordinal"):
                        ordinal = int(target["ordinal"])
                        if 1 <= ordinal <= len(pending_candidates):
                            selected = pending_candidates[ordinal - 1]
                    requested_id = str(safe_args.get("taskId") or target.get("taskId") or "")
                    if requested_id and selected is None:
                        selected = next(
                            (item for item in pending_candidates if item.get("taskId") == requested_id),
                            None,
                        )
                    safe_args = {**intended, **safe_args}
                    if selected is not None:
                        safe_args["taskId"] = str(selected.get("taskId") or "")
                        safe_args["expectedVersion"] = int(selected.get("version") or 0)
                        selected_pending_target = True
                if schedule_authoritative:
                    if (
                        str(semantic_policy.get("action") or "") != "execute"
                        or not bool(semantic_policy.get("allowed"))
                    ):
                        continue
                elif todo_mutation_prohibited(user_message):
                    continue
                model_commit_intent = bool(
                    safe_args.get("commitIntent") or safe_args.get("commit_intent")
                )
                if todo_kind != "mutation" and not model_commit_intent:
                    continue
                action = str(safe_args.get("action") or "list").strip().lower()
                if direct_todo_kind == "none" and not model_commit_intent and action not in {
                    "update", "complete", "reopen", "delete", "snooze", "pause", "resume",
                }:
                    continue
                if bind_recent_todo and not selected_pending_target:
                    if action not in {
                        "update", "complete", "reopen", "delete", "snooze", "pause", "resume",
                    }:
                        continue
                    if not recent_todo.get("taskId"):
                        continue
                    # The user used an anaphoric follow-up. Bind it to the
                    # authoritative receipt target rather than trusting a model-
                    # supplied or hallucinated identifier.
                    safe_args["taskId"] = recent_todo["taskId"]
                    if int(recent_todo.get("taskVersion") or 0) > 0:
                        safe_args["expectedVersion"] = int(recent_todo["taskVersion"])
                try:
                    command = TodoCommand.from_mapping(safe_args)
                except ValueError:
                    continue
                if command.action == "list":
                    continue
                constrained.append({**request, "args": command.to_dict()})
            elif name == "bookmark_list":
                constrained_request = {
                    **request,
                    "args": canonical_tool_arguments(name, args),
                }
                if isinstance(request.get("selection"), Mapping):
                    constrained_request["selection"] = dict(request["selection"])
                constrained.append(constrained_request)
            elif name == "bookmark_manage":
                if structured_tool_authorization(request) != "write":
                    continue
                if not bool(args.get("commitIntent") or args.get("commit_intent")):
                    continue
                action = str(args.get("action") or "create").strip().lower()
                if action == "save":
                    action = "create"
                if action not in {"create", "update", "delete"}:
                    continue
                safe_args = canonical_tool_arguments(name, args)
                remembered_url = ""
                if safe_args["name"]:
                    with self.state.state_database() as connection:
                        remembered = resolve_bookmark(connection, safe_args["name"])
                    if not remembered:
                        remembered = self.browser.known_browser_destination(
                            f"打开 {safe_args['name']}"
                        ) or {}
                    remembered_url = str(remembered.get("url") or "").strip()
                proposed_url = str(args.get("url") or "").strip()
                if proposed_url:
                    # A model may select the tool, but it cannot invent the value
                    # being persisted. Bind writes to either the explicit URL in
                    # the current message or an exact local destination learned
                    # from a previous successful open/bookmark.
                    try:
                        proposed_canonical = canonicalize_public_url(
                            urllib.parse.urlunsplit(validate_public_url(proposed_url)[0])
                        )
                        trusted_url = user_url or remembered_url
                        trusted_canonical = canonicalize_public_url(
                            urllib.parse.urlunsplit(validate_public_url(trusted_url)[0])
                        ) if trusted_url else ""
                    except SafeWebError:
                        continue
                    if not trusted_canonical or proposed_canonical != trusted_canonical:
                        continue
                    safe_args["url"] = trusted_canonical
                elif action == "create" and remembered_url:
                    try:
                        safe_args["url"] = canonicalize_public_url(
                            urllib.parse.urlunsplit(validate_public_url(remembered_url)[0])
                        )
                    except SafeWebError:
                        continue
                if action == "create" and (not safe_args["name"] or "url" not in safe_args):
                    continue
                if action in {"update", "delete"} and not safe_args["bookmarkId"]:
                    continue
                constrained_request = {**request, "args": safe_args}
                if isinstance(request.get("selection"), Mapping):
                    constrained_request["selection"] = dict(request["selection"])
                constrained.append(constrained_request)
            else:
                constrained.append({**request, "args": args})
        return constrained
