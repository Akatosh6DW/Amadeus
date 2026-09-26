"""已授权浏览器动作的确定性失败恢复。

恢复只消费当前 prepared action、请求范围内的持久回执和既有解析结果，
最多重放一次已经授权且可确定解析的浏览器动作。工具执行、回复归一化、
取消状态和最终会话封存分别通过窄端口接入；本模块不反向导入 ``server``，
也不把模型生成当作外部动作成功证据。
"""
from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any, Callable, Mapping

from agent.action_grounding import PreparedTurn


@dataclass(frozen=True)
class ActionRecoveryPorts:
    """确定性 recovery 需要的状态、执行和交付端口。"""

    read_action_receipt_records: Callable[[], list[dict[str, Any]]]
    current_pending_action: Callable[[], dict[str, Any]]
    cognitive_turn_state: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    prepare_action_turn: Callable[..., PreparedTurn]
    chat_request_cancelled: Callable[[str], bool]
    browser_action_args: Callable[..., dict[str, Any]]
    tool_open_browser: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    clear_pending_action: Callable[[str], Any]
    normalize_model_reply: Callable[..., dict[str, Any]]
    seal_recovery_turn_result: Callable[..., dict[str, Any]]


class ActionRecoveryService:
    """Recover one authorized browser action without a model or new intent."""

    def __init__(self, ports: ActionRecoveryPorts) -> None:
        self.ports = ports

    def deterministic_external_action_recovery(
        self,
        body: dict[str, Any],
        settings: dict[str, Any],
        user_message: str,
        messages: list[dict[str, Any]],
        *,
        request_id: str,
        failure: Exception,
    ) -> dict[str, Any] | None:
        """Finish an authorized browser task without asking a model to improvise.

        This recovery is deliberately narrow. It consumes the prepared action
        state and durable action receipts, may replay one already-authorized and
        deterministically resolved browser action, and reports only the real
        outcome. It never infers a new side effect from generated prose.
        """

        prepared = body.get("_preparedActionTurn")
        if not isinstance(prepared, PreparedTurn):
            return None
        receipts = [
            dict(item)
            for item in self.ports.read_action_receipt_records()
            if isinstance(item, Mapping)
            and str(item.get("tool") or "") == "open_browser"
            and str(item.get("requestId") or "") == request_id
        ]
        pending = self.ports.current_pending_action()
        cognitive = bool(self.ports.cognitive_turn_state(body))
        # The legacy parser can promote a target correction into open_browser
        # solely because an old pending action exists. For exception recovery,
        # distinguish that inherited action from an instruction in this request.
        current_candidate = (
            self.ports.prepare_action_turn(
                user_message,
                pending_action={},
                request_id=request_id,
            ).action_candidate
            if cognitive else prepared.action_candidate
        )
        browser_task = bool(
            (current_candidate.action_type == "open_browser" and current_candidate.executable)
            or receipts
            or (
                # In cognitive chat, only the current request can authorize tools.
                # A legacy pending relation must not turn a reply failure into an
                # instruction to execute an earlier browser task.
                not cognitive
                and str(pending.get("type") or "") == "open_browser"
                and prepared.pending_relation.relation in {"continue", "replace"}
            )
        )
        if not browser_task or self.ports.chat_request_cancelled(request_id):
            return None

        latest = receipts[0] if receipts else {}
        accepted = next(
            (item for item in receipts if item.get("status") == "accepted"),
            None,
        )
        tool_result: dict[str, Any] | None = None

        # A deterministic URL/bookmark/known alias can safely be replayed. The
        # adapter's request-scoped idempotency key prevents a duplicate browser
        # open if the first attempt succeeded before the reply pipeline broke.
        if accepted is None:
            resolved_args = self.ports.browser_action_args(user_message, messages, prepared)
            if resolved_args.get("url"):
                resolved_args = {
                    **resolved_args,
                    "actionId": str(
                        latest.get("actionId")
                        or pending.get("actionId")
                        or f"act_{uuid.uuid4().hex}"
                    )[:100],
                    "_requestId": request_id,
                    "_rememberUsage": True,
                }
                tool_result = self.ports.tool_open_browser(settings, resolved_args)
                recovered_receipt = tool_result.get("actionReceipt")
                if isinstance(recovered_receipt, Mapping):
                    latest = dict(recovered_receipt)
                    receipts.insert(0, latest)
                    if latest.get("status") == "accepted":
                        accepted = latest
                        self.ports.clear_pending_action(str(latest.get("actionId") or ""))

        receipt = dict(accepted or latest)
        target = str(
            receipt.get("resolvedTarget")
            or receipt.get("requestedTarget")
            or prepared.action_candidate.target_text
            or self.ports.current_pending_action().get("targetHint")
            or ""
        ).strip()[:300]
        status = str(receipt.get("status") or "")
        error = str(receipt.get("error") or "")
        if status == "accepted":
            reply = (
                f"已经帮你用默认浏览器打开“{target}”了。"
                if target else "已经帮你用默认浏览器打开了。"
            )
            spoken = "既定のブラウザーで開いたわ。"
            requires_user_response = False
            expected_response_type = ""
            tool_status = "ok"
        elif error == "permission_denied":
            reply = "我现在没有打开浏览器的权限，所以这次没能替你打开。"
            spoken = "ブラウザの権限が無効なので、今回は開けなかったわ。"
            requires_user_response = False
            expected_response_type = ""
            tool_status = "failed"
        elif error in {
            "target_confirmation_required",
            "unresolved_browser_target",
            "missing_url",
            "no_verified_source_to_open",
            "ambiguous_browser_target",
        } or not target:
            reply = (
                f"我不确定你说的“{target}”具体是哪个网站。"
                "你可以告诉我它的正式名称，或者直接把网址发给我。"
                if target else
                "我还不知道你想打开哪个网站。告诉我网站名称，或者直接把网址发给我吧。"
            )
            spoken = "どのサイトを開けばいいか、まだ特定できていないの。正式な名前かURLを教えて。"
            requires_user_response = True
            expected_response_type = "browser_target"
            tool_status = "failed"
        else:
            target_name = f"「{target}」" if target else "这个网站"
            reply = f"我刚才没能打开{target_name}。你要我再试一次吗？"
            spoken = "さっきは開けなかったわ。もう一度試そうか？"
            requires_user_response = True
            expected_response_type = "confirmation"
            tool_status = "failed"

        result = self.ports.normalize_model_reply({
            "reply": reply,
            "spokenTextJa": spoken,
            "emotion": "concerned" if tool_status == "failed" else "neutral",
            "gesture": "nod" if tool_status == "ok" else "tilt",
            "voiceStyle": "calm",
            "memoryUpdates": [],
            "requiresUserResponse": requires_user_response,
            "expectedResponseType": expected_response_type,
            "completedActs": ["execute_or_report_action"],
            "standalone": True,
            "dependsOnUndeliveredPrefix": False,
            "turnComplete": True,
            "source": "local-action-state-recovery",
        }, settings=settings)
        result.update({
            "toolUsed": "open_browser",
            "toolStatus": tool_status,
            "actionStateRecovered": True,
            "pipelineFailureType": type(failure).__name__,
        })
        if receipt:
            result["actionReceipt"] = receipt
        if receipts:
            result["actionReceipts"] = list(reversed(receipts[-12:]))
        if tool_result is not None:
            result["toolResults"] = [tool_result]
        return self.ports.seal_recovery_turn_result(
            body,
            user_message,
            result,
            settings=settings,
            request_id=request_id,
            delivery_path="local_action_recovery",
        )
