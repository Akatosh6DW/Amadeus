"""聊天请求的应用层入口。

本模块只拥有一次请求从校验、附件准备、生成路由到最终化的临时状态。
会话代际与取消标记仍由请求运行时拥有，请求 ``body`` 仍是本轮认知、工具
和冻结上下文的唯一载体。各端口按职责连接现有服务，避免 HTTP 层重新实现
生成或恢复规则。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.multimodal_chat import native_visual_chat_enabled
from backend.chat.recovery import finalize_with_recovery
from backend.common.values import safe_nonnegative_int
from backend.runtime.requests import RequestValidationError


@dataclass(frozen=True)
class RequestOperations:
    current_conversation_epoch: Callable[[], int]
    chat_request_cancelled: Callable[[str], bool]
    update_progress: Callable[[str, str, str], None]
    wall_time: Callable[[], float]


@dataclass(frozen=True)
class PreparationOperations:
    streaming_user_message: Callable[[dict[str, Any]], tuple[str, list[dict[str, Any]]]]
    ensure_turn_context_snapshot: Callable[..., Any]
    ensure_context_control: Callable[..., Any]
    resolve_recent_artifact_task_context: Callable[..., Any]
    process_chat_attachments: Callable[..., list[dict[str, Any]]]
    refresh_continued_artifacts: Callable[..., list[dict[str, Any]]]
    refresh_continued_visual_artifacts: Callable[..., Any]
    attachment_read_tool_results: Callable[[dict[str, Any]], list[dict[str, Any]]]
    body_prepared_action_turn: Callable[..., Any]
    dignity_preflight: Callable[..., dict[str, Any]]
    configured: Callable[[Mapping[str, Any]], bool]
    build_persona_snapshot: Callable[..., Any]


@dataclass(frozen=True)
class GenerationOperations:
    infer_agent_tool_requests: Callable[..., list[dict[str, Any]]]
    is_time_query: Callable[[str], bool]
    local_time_reply: Callable[..., dict[str, Any]]
    local_screen_reply: Callable[..., dict[str, Any]]
    local_camera_reply: Callable[..., dict[str, Any]]
    call_upstream: Callable[..., dict[str, Any]]
    mock_reply: Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class FinalizationOperations:
    apply_dignity_assessment: Callable[..., tuple[dict[str, Any], Mapping[str, Any]]]
    dignity_reply_contract_issues: Callable[[Mapping[str, Any]], list[str]]
    repair_dignity_reply_with_model: Callable[..., dict[str, Any]]
    public_attachment_metadata: Callable[[Mapping[str, Any]], dict[str, Any]]
    displayed_tool_name: Callable[[Mapping[str, Any]], str]
    public_attachment_tool_result: Callable[[Mapping[str, Any]], dict[str, Any]]
    read_relationship_state: Callable[[], Mapping[str, Any]]
    bind_turn_context_to_result: Callable[[dict[str, Any], dict[str, Any]], Any]
    finalize_chat_response: Callable[..., dict[str, Any]]
    recover_final_projection_candidate: Callable[..., dict[str, Any]]
    write_log: Callable[..., None]


@dataclass
class PreparedRequest:
    settings: dict[str, Any]
    user_message: str
    messages: list[dict[str, Any]]
    processed_attachments: list[dict[str, Any]]
    attachment_tool_results: list[dict[str, Any]]
    prepared_action_turn: Any
    preflight: dict[str, Any]
    persona_snapshot: Any


class ChatRequestPipeline:
    """按固定顺序执行一次非 HTTP 绑定的聊天请求。"""

    def __init__(
        self,
        requests: RequestOperations,
        preparation: PreparationOperations,
        generation: GenerationOperations,
        finalization: FinalizationOperations,
    ) -> None:
        self.requests = requests
        self.preparation = preparation
        self.generation = generation
        self.finalization = finalization

    def run(
        self,
        body: dict[str, Any],
        settings: dict[str, Any],
        *,
        request_id: str,
        started_at: float,
        preflight: dict[str, Any] | None = None,
        stream_preamble: dict[str, Any] | None = None,
        progress_callback: Any | None = None,
        persona_snapshot: Any | None = None,
    ) -> dict[str, Any]:
        """校验请求、准备冻结输入、选择生成路径并提交最终化。"""

        active_settings = {
            **settings,
            "_directVisualChat": native_visual_chat_enabled(settings),
        }
        self._validate_request(body, request_id)
        prepared = self._prepare(
            body,
            active_settings,
            request_id=request_id,
            preflight=preflight,
            persona_snapshot=persona_snapshot,
        )
        result = self._generate(
            body,
            prepared,
            request_id=request_id,
            progress_callback=progress_callback,
        )
        self._apply_dignity(result, prepared, request_id=request_id)
        self._attach_metadata(result, prepared)
        self._raise_if_cancelled(request_id)
        if result.get("silentLocked"):
            result.update({
                "ok": True,
                "requestId": request_id,
                "latencyMs": round((self.requests.wall_time() - started_at) * 1000),
                "relationshipTone": str(
                    self.finalization.read_relationship_state().get("stage")
                    or "close_companion"
                ),
            })
            return result
        return self._finalize(
            body,
            result,
            prepared,
            request_id=request_id,
            started_at=started_at,
            stream_preamble=stream_preamble,
            progress_callback=progress_callback,
        )

    def _validate_request(self, body: dict[str, Any], request_id: str) -> None:
        active_epoch = self.requests.current_conversation_epoch()
        supplied_epoch = body.get("contextEpoch")
        request_epoch = safe_nonnegative_int(
            supplied_epoch if supplied_epoch is not None else active_epoch
        )
        if supplied_epoch is not None and request_epoch != active_epoch:
            raise RequestValidationError(
                "stale_conversation_epoch",
                "这轮请求属于已经清除的会话代际，未进入附件、工具或模型处理。",
            )
        self._raise_if_cancelled(request_id)
        body["_conversationEpoch"] = request_epoch

    def _prepare(
        self,
        body: dict[str, Any],
        settings: dict[str, Any],
        *,
        request_id: str,
        preflight: dict[str, Any] | None,
        persona_snapshot: Any | None,
    ) -> PreparedRequest:
        if body.get("_backendSingleTurnRecoveryAttempted"):
            self.requests.update_progress(
                request_id,
                "model-generating",
                "沿用本轮上下文与工具计划，正在完成受控生成",
            )
        else:
            self.requests.update_progress(
                request_id, "attachments", "正在读取消息与本轮附件"
            )
        user_message, _ = self.preparation.streaming_user_message(body)
        self.preparation.ensure_turn_context_snapshot(
            body, request_id=request_id, user_message=user_message
        )
        self.preparation.ensure_context_control(
            body, user_message=user_message, settings=settings
        )
        self.preparation.resolve_recent_artifact_task_context(
            body, settings, user_message, request_id=request_id
        )
        processed_attachments = self.preparation.process_chat_attachments(body, settings)
        processed_attachments.extend(
            self.preparation.refresh_continued_artifacts(body, settings, user_message)
        )
        body["_processedAttachments"] = processed_attachments
        self.preparation.refresh_continued_visual_artifacts(
            body, settings, user_message
        )
        attachment_tool_results = self.preparation.attachment_read_tool_results(body)
        user_message, messages = self.preparation.streaming_user_message(body)
        prepared_action_turn = self.preparation.body_prepared_action_turn(
            body, user_message, request_id=request_id
        )
        active_preflight = preflight or self.preparation.dignity_preflight(
            settings, user_message, messages, request_id=request_id
        )
        if isinstance(active_preflight.get("assessment"), dict):
            body["_dignityAssessment"] = dict(active_preflight["assessment"])
        active_persona_snapshot = persona_snapshot
        if self.preparation.configured(settings) and active_persona_snapshot is None:
            active_persona_snapshot = self.preparation.build_persona_snapshot(
                body, user_message, messages, request_id=request_id
            )
        return PreparedRequest(
            settings=settings,
            user_message=user_message,
            messages=messages,
            processed_attachments=processed_attachments,
            attachment_tool_results=attachment_tool_results,
            prepared_action_turn=prepared_action_turn,
            preflight=active_preflight,
            persona_snapshot=active_persona_snapshot,
        )

    def _generate(
        self,
        body: dict[str, Any],
        prepared: PreparedRequest,
        *,
        request_id: str,
        progress_callback: Any | None,
    ) -> dict[str, Any]:
        configured = self.preparation.configured(prepared.settings)
        local_tool_requests = (
            self.generation.infer_agent_tool_requests(
                prepared.user_message,
                prepared.messages,
                prepared_turn=prepared.prepared_action_turn,
            )
            if not configured else []
        )
        local_tool_names = {
            str(item.get("name") or "") for item in local_tool_requests
        }
        action = prepared.preflight.get("action")
        if action in {"locked", "cold"}:
            return dict(prepared.preflight["response"])
        if not configured and self.generation.is_time_query(prepared.user_message):
            return self.generation.local_time_reply(prepared.settings)
        if "capture_screen" in local_tool_names and not configured:
            return self.generation.local_screen_reply(
                prepared.settings, prepared.user_message
            )
        if "capture_camera" in local_tool_names and not configured:
            return self.generation.local_camera_reply(
                prepared.settings, prepared.user_message
            )
        if configured:
            return self.generation.call_upstream(
                body,
                progress_callback=progress_callback,
                persona_snapshot=prepared.persona_snapshot,
            )
        result = self.generation.mock_reply(
            prepared.user_message, settings=prepared.settings
        )
        if prepared.processed_attachments:
            result.update({
                "reply": "附件已经安全接收了。填入 API Key 后，我就能结合文件或图片内容继续分析和修改。",
                "spokenTextJa": "添付ファイルは安全に受け取ったわ。APIキーを設定すれば、内容を確認して続けられる。",
                "emotion": "focused",
            })
        return result

    def _apply_dignity(
        self,
        result: dict[str, Any],
        prepared: PreparedRequest,
        *,
        request_id: str,
    ) -> None:
        if not (
            prepared.preflight.get("action") == "continue"
            and self.preparation.configured(prepared.settings)
        ):
            return
        assessed, dignity_state = self.finalization.apply_dignity_assessment(
            result, prepared.preflight
        )
        if assessed is not result:
            result.clear()
            result.update(assessed)
        result["dignityViolationCount"] = int(
            dignity_state.get("dignityViolationCount") or 0
        )
        result["dignityLocked"] = bool(dignity_state.get("dignityLocked"))
        if not self.finalization.dignity_reply_contract_issues(result):
            return
        try:
            repaired = self.finalization.repair_dignity_reply_with_model(
                prepared.settings,
                prepared.user_message,
                result,
                int(dignity_state.get("dignityViolationCount") or 1),
                request_id=request_id,
                persona_snapshot=prepared.persona_snapshot,
            )
            result.clear()
            result.update(repaired)
        except Exception as error:
            result["dignityReplyUnverified"] = True
            result["dignityReplyIssues"] = (
                self.finalization.dignity_reply_contract_issues(result)
            )
            self.finalization.write_log(
                "dignity_reply_repair_failed",
                requestId=request_id,
                errorType=type(error).__name__,
                error=str(error)[:500],
            )

    def _attach_metadata(
        self, result: dict[str, Any], prepared: PreparedRequest
    ) -> None:
        if not prepared.processed_attachments:
            return
        result["attachments"] = [
            self.finalization.public_attachment_metadata(item)
            for item in prepared.processed_attachments
        ]
        if result.get("toolUsed"):
            return
        tool_names = list(dict.fromkeys(
            self.finalization.displayed_tool_name(item)
            for item in prepared.attachment_tool_results
        ))
        result["toolUsed"] = ",".join(tool_names)
        result["toolStatus"] = (
            "failed"
            if any(not item.get("ok") for item in prepared.attachment_tool_results)
            else "ok"
        )
        result["toolResults"] = [
            self.finalization.public_attachment_tool_result(item)
            for item in prepared.attachment_tool_results
        ]

    def _finalize(
        self,
        body: dict[str, Any],
        result: dict[str, Any],
        prepared: PreparedRequest,
        *,
        request_id: str,
        started_at: float,
        stream_preamble: dict[str, Any] | None,
        progress_callback: Any | None,
    ) -> dict[str, Any]:
        self.requests.update_progress(
            request_id,
            "postprocessing",
            "正在校准情绪、日语发声稿与记忆更新",
        )
        self.finalization.bind_turn_context_to_result(body, result)
        prepared_action_turn = self.preparation.body_prepared_action_turn(
            body, prepared.user_message, request_id=request_id
        )
        return finalize_with_recovery(
            prepared.settings,
            prepared.user_message,
            result,
            body=body,
            request_id=request_id,
            started_at=started_at,
            prepared_turn=prepared_action_turn,
            stream_preamble=stream_preamble,
            progress_callback=progress_callback,
            persona_snapshot=prepared.persona_snapshot,
            finalize=self.finalization.finalize_chat_response,
            repair_projection=self.finalization.recover_final_projection_candidate,
            prepare_turn=self.preparation.body_prepared_action_turn,
            generate=self.generation.call_upstream,
            log_event=self.finalization.write_log,
        )

    def _raise_if_cancelled(self, request_id: str) -> None:
        if self.requests.chat_request_cancelled(request_id):
            raise RequestValidationError(
                "request_superseded",
                "这轮请求已被用户的后续输入替代。",
            )
