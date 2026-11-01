"""模型工具请求的语义投影边界。

本模块只把一轮已经解析出的用户意图、prepared action 和模型结构化
请求投影为可执行请求；它不执行工具、不读写状态，也不推断公开研究。
浏览器、视觉和工作区解析仍通过窄端口接入，避免工具规则反向依赖
``server``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.action_grounding import PreparedTurn
from agent.tool_architecture import DEFAULT_TOOL_REGISTRY
from backend.tools.validation import normalize_tool_authorization


@dataclass(frozen=True)
class ToolRequestPorts:
    """请求投影所需的解析端口；端口只提供已存在的语义状态。"""

    contextual_reference_resolution: Callable[
        [str, list[dict[str, Any]] | None], dict[str, Any]
    ]
    prepare_action_turn: Callable[[str], PreparedTurn]
    is_time_query: Callable[[str], bool]
    is_attachment_followup_query: Callable[[str], bool]
    visual_followup_tool_name: Callable[[str, dict[str, Any] | None], str]
    screen_history_request_mode: Callable[[str], str]
    is_screen_query: Callable[[str], bool]
    is_camera_query: Callable[[str], bool]
    browser_action_args: Callable[
        [str, list[dict[str, Any]] | None, PreparedTurn], dict[str, Any]
    ]
    infer_workspace_current_state_request: Callable[
        [str, Mapping[str, Any] | None], dict[str, Any] | None
    ]


class ToolRequestService:
    """Owns explicit local request projection and structured request cleanup."""

    def __init__(self, ports: ToolRequestPorts) -> None:
        self.ports = ports

    def infer_agent_tool_requests(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        resolution: dict[str, Any] | None = None,
        prepared_turn: PreparedTurn | None = None,
        todo_reconciliation: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return explicit execution obligations, never speculative tool guesses."""
        resolution = resolution or self.ports.contextual_reference_resolution(user_message, messages)
        prepared = prepared_turn or self.ports.prepare_action_turn(user_message)
        requests: list[dict[str, Any]] = []
        # The model owns tool selection in the cognitive path.  Local keyword
        # classifiers may provide capability hints to prompts, but they must
        # not turn time/visual wording into an execution obligation here.
        attachment_followup = self.ports.is_attachment_followup_query(user_message)
        browser_continuation = prepared.pending_relation.relation == "continue"
        saved_bookmark_args = (
            self.ports.browser_action_args(user_message, messages, prepared)
            if prepared.action_candidate.action_type == "open_target"
            else {}
        )
        has_browser_action = (
            prepared.action_candidate.executable
            or browser_continuation
            or bool(saved_bookmark_args.get("bookmarkId"))
        )
        # An explicit reference to a newly uploaded attachment starts an artifact
        # task. A stale pending browser action must not reinterpret the attachment
        # name as its replacement URL. A later structured model request can still
        # navigate if the user actually asks to open a URL found in that artifact.
        if has_browser_action and not attachment_followup:
            open_args = saved_bookmark_args or self.ports.browser_action_args(
                user_message, messages, prepared,
            )
            resolution_query = str(open_args.get("resolveQuery") or "").strip()
            requests.append({
                "name": "open_browser",
                "args": (
                    {"deferToResearch": True}
                    if resolution_query else open_args
                ),
                "reason": "user explicitly asked to open the default browser",
            })
            # An unresolved browser target is reported to QueryLoop.  The main
            # model may then choose web_search; this action parser must not create
            # a network request from lexical matching either.
        # File reads/listing are intentionally not inferred from natural
        # language.  The cognitive model must emit a concrete file call; its
        # arguments, permission and receipt are then checked by the executor.
        # Reading/searching public information is likewise not inferred here.
        # Web schemas are exposed to the main model, which must emit the structured
        # tool_use itself after interpreting the complete turn.
        has_research = any(item.get("name") in {"web_search", "read_web_page"} for item in requests)
        if has_research:
            for request in requests:
                if request.get("name") != "open_browser":
                    continue
                args = request.get("args") if isinstance(request.get("args"), dict) else {}
                if not args.get("url"):
                    request["args"] = {"deferToResearch": True}
                    request["reason"] = "open the best verified source after research completes"
        return requests


def normalize_tool_requests(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    # semantic-v3 names the compact tool array ``calls``. Keep accepting the
    # two legacy spellings so older providers and saved fixtures remain usable.
    raw = (
        value.get("calls")
        or value.get("toolRequests")
        or value.get("tool_requests")
        or []
    )
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    root_target = str(value.get("targetDomain") or "").strip()
    if root_target not in {
        "none", "screen", "physical_user", "uploaded_image", "document", "unknown_visual",
    }:
        root_target = ""
    root_authorization = normalize_tool_authorization(value.get("toolAuthorization"))
    sensor_names = {
        str(item.get("name") or item.get("tool") or "") if isinstance(item, dict) else str(item)
        for item in raw[:4]
    } & {"capture_screen", "capture_camera"}
    requests: list[dict[str, Any]] = []
    for item in raw[:4]:
        if isinstance(item, str):
            name = item
            args = {}
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("tool") or "").strip()
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
        else:
            continue
        # Registry owns capability names; authorization remains in validation.
        # A separate list silently discarded newly registered QQ tool calls.
        if DEFAULT_TOOL_REGISTRY.get(name) is not None:
            selection = (
                dict(item.get("selection"))
                if isinstance(item, dict) and isinstance(item.get("selection"), Mapping)
                else {}
            )
            # The container supplies a default for older providers. Explicit
            # per-call choices (including "none") belong to that operation.
            if "toolAuthorization" in selection:
                authorization = normalize_tool_authorization(selection["toolAuthorization"])
            elif isinstance(item, dict) and "toolAuthorization" in item:
                authorization = normalize_tool_authorization(item["toolAuthorization"])
            else:
                authorization = root_authorization
            if (name in {"capture_screen", "capture_camera"} and not selection.get("targetDomain")
                    and (not root_target or len(sensor_names) > 1)):
                selection["targetDomain"] = "screen" if name == "capture_screen" else "physical_user"
            elif root_target and not selection.get("targetDomain"):
                selection["targetDomain"] = root_target
            if (authorization != "none" or "toolAuthorization" in selection
                    or isinstance(item, dict) and "toolAuthorization" in item):
                selection["toolAuthorization"] = authorization
            request = {"name": name, "args": args}
            if isinstance(item, dict):
                for field in ("stepId", "toolCallId", "callId"):
                    if item.get(field):
                        request[field] = str(item[field])
            if selection:
                request["selection"] = selection
            requests.append(request)
    return requests
