"""普通聊天的请求去重、生成、失败恢复和最终交付。

只拥有本次调用的临时变量；请求缓存/取消状态由同一请求运行时持有。
先登记完成结果再发送响应，保留断连后的结果可重取语义。等待重复请求
不会再启动模型；只对既有允许恢复的异常执行一次隔离恢复。
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Protocol

from agent.cognitive_commit import CognitiveCommitError
from backend.chat.input import streaming_user_message
from backend.chat.delivery import DeliveryError, require_delivery
from backend.runtime.requests import RequestRuntime


class RequestPort(Protocol):
    def prepare_external_chat_body(self, body: dict[str, Any]) -> str: ...
    def claim_chat_request(self, request_id: str, supersedes_request_id: str = "") -> tuple[str, Any]: ...
    def completed_chat_request(self, request_id: str) -> dict[str, Any] | None: ...
    def finish_chat_request(self, request_id: str, result: dict[str, Any] | None = None, *, error: str = "") -> None: ...
    def chat_request_cancelled(self, request_id: str) -> bool: ...


class JsonResponse(Protocol):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> bool: ...


@dataclass(frozen=True)
class GenerationPort:
    begin_persona_skill_record: Callable[[dict[str, Any]], Any]
    process_chat_request_body: Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class RecoveryPort:
    pipeline_failure_allows_model_rescue: Callable[[Exception], bool]
    run_isolated_context_recovery: Callable[..., dict[str, Any] | None]


_DELIVERY_STATES = frozenset({"draft", "sending", "sent", "unknown", "failed", "rejected"})
_RECEIPT_STATUSES = frozenset({"accepted", "unknown", "failed", "rejected"})
_SAFE_RECEIPT_FIELDS = (
    "tool", "action", "status", "deliveryState", "messageId", "draftId",
    "targetType", "targetId", "account", "sent", "sentPartCount",
    "totalParts", "remainingPartCount",
)


def _safe_id(value: Any, *, limit: int = 180) -> str:
    text = str(value or "").strip()
    return text if text and len(text) <= limit and text.isascii() else ""


def _safe_token(value: Any, *, limit: int = 80) -> str:
    text = str(value or "").strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:"
    return text if text and len(text) <= limit and all(char in allowed for char in text) else ""


def _safe_delivery_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Project a QQ/action receipt without copying model or transport data."""

    safe: dict[str, Any] = {}
    for key in _SAFE_RECEIPT_FIELDS:
        value = receipt.get(key)
        if key in {"tool", "action", "targetType"}:
            value = _safe_token(value)
        elif key in {"status", "deliveryState"}:
            value = str(value or "").strip().lower()
            if value not in _DELIVERY_STATES and value not in _RECEIPT_STATUSES:
                value = ""
        elif key in {"messageId", "draftId", "targetId", "account"}:
            value = _safe_id(value)
        elif key == "sent":
            value = value is True
        elif key in {"sentPartCount", "totalParts", "remainingPartCount"}:
            value = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        if value not in (None, ""):
            safe[key] = value
    return safe


def _iter_tool_results(body: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """Read only execution receipts left on the mutable turn body."""

    results: list[Mapping[str, Any]] = []
    evidence = body.get("_cognitiveEvidence")
    if isinstance(evidence, Mapping):
        for item in evidence.values():
            if not isinstance(item, Mapping):
                continue
            receipt = item.get("receipt")
            if isinstance(receipt, Mapping):
                results.append(receipt)
            # A test/recovery adapter may store the raw tool result directly.
            elif item.get("sourceType") == "tool" and (
                item.get("name") or item.get("deliveryState") or item.get("actionReceipt")
            ):
                results.append(item)
    for key in ("_rescueToolResults", "_toolResults", "toolResults"):
        value = body.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            results.extend(item for item in value if isinstance(item, Mapping))
    return results


def _delivery_receipts(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return unique, bounded QQ/action delivery receipts from a turn body."""

    receipts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for result in _iter_tool_results(body):
        action_receipt = result.get("actionReceipt")
        if isinstance(action_receipt, Mapping):
            candidate = _safe_delivery_receipt(action_receipt)
            if candidate:
                candidate.setdefault("tool", _safe_token(result.get("name")) or "external_action")
                identity = (
                    str(candidate.get("tool") or ""),
                    str(candidate.get("messageId") or candidate.get("draftId") or ""),
                    str(candidate.get("targetId") or ""),
                    str(candidate.get("status") or ""),
                )
                if identity not in seen:
                    seen.add(identity)
                    receipts.append(candidate)

        # qq_manage predates the generic actionReceipt contract.  Its ledger
        # receipt is the sent/deliveryState/messageId tuple; retain that exact
        # platform receipt instead of manufacturing a generic action ID.
        if str(result.get("name") or "") == "qq_manage":
            delivery_state = str(result.get("deliveryState") or "").strip().lower()
            message_id = _safe_id(result.get("messageId"))
            sent = result.get("sent") is True or (
                delivery_state == "sent" and bool(message_id)
            )
            if delivery_state in _DELIVERY_STATES or message_id or sent:
                candidate = _safe_delivery_receipt({
                    "tool": "qq_manage",
                    "action": result.get("action") or "send",
                    "status": "accepted" if sent else (
                        "unknown" if delivery_state in {"sending", "unknown"} else delivery_state
                    ),
                    "deliveryState": delivery_state,
                    "messageId": message_id,
                    "draftId": result.get("draftId"),
                    "targetType": result.get("targetType"),
                    "targetId": result.get("targetId"),
                    "account": result.get("account"),
                    "sent": sent,
                    "sentPartCount": result.get("sentPartCount"),
                    "totalParts": result.get("totalParts"),
                    "remainingPartCount": result.get("remainingPartCount"),
                })
                identity = (
                    "qq_manage",
                    str(candidate.get("messageId") or candidate.get("draftId") or ""),
                    str(candidate.get("targetId") or ""),
                    str(candidate.get("status") or ""),
                )
                if candidate and identity not in seen:
                    seen.add(identity)
                    receipts.append(candidate)
    return receipts


def _has_applied_delivery(receipt: Mapping[str, Any]) -> bool:
    return (
        receipt.get("status") == "accepted"
        or (
            receipt.get("tool") == "qq_manage"
            and receipt.get("deliveryState") == "sent"
            and bool(receipt.get("messageId"))
        )
    )


def _has_uncertain_delivery(receipt: Mapping[str, Any]) -> bool:
    if receipt.get('status') == 'accepted' and receipt.get('deliveryState') == 'sent' and receipt.get('messageId'):
        return False
    if receipt.get("status") == "unknown" or receipt.get("deliveryState") in {"sending", "unknown"}:
        return True
    if receipt.get("deliveryState") == "sent" and not receipt.get("messageId"):
        return True
    try:
        return int(receipt.get("sentPartCount") or 0) > 0
    except (TypeError, ValueError):
        return False


def _commit_failure_stage(error: BaseException) -> str:
    """Classify known cognitive commit failures without logging their text."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__.lower()
        if "revision" in name or "conflict" in name:
            return "cognitive_revision_conflict"
        if "projection" in name:
            return "cognitive_projection"
        current = current.__cause__ or current.__context__
    text = str(error).lower()
    if "revision changed" in text or "revision conflict" in text or "conflict" in text:
        return "cognitive_revision_conflict"
    if "projection" in text:
        return "cognitive_projection"
    return "cognitive_commit"


def _cognitive_commit_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, CognitiveCommitError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _commit_failure_result(request_id: str, body: Mapping[str, Any], error: BaseException) -> dict[str, Any] | None:
    """Build a safe response when a QQ side effect preceded commit failure.

    This function deliberately never retries the generation pipeline.  A
    platform send already has its own idempotency/ledger boundary, while the
    local cognitive transaction can be repaired independently on a later turn.
    """

    if not _cognitive_commit_error(error):
        return None
    receipts = _delivery_receipts(body)
    applied = [item for item in receipts if _has_applied_delivery(item)]
    uncertain = [item for item in receipts if _has_uncertain_delivery(item)]
    if not applied and not uncertain:
        return None
    stage = _commit_failure_stage(error)
    selected = uncertain[-1] if uncertain else applied[-1]
    message_id = _safe_id(selected.get("messageId"))
    if applied and not uncertain:
        effect = 'QQ 消息已发送' if all(item.get('tool') == 'qq_manage' for item in applied) else '已确认的操作已经执行'
        reply = effect + '，但本地对话保存失败，请勿重复发送'
        outcome = "side_effect_applied"
        delivery_state = "sent"
    else:
        reply = ('部分操作已完成，另有操作结果未知，先查状态，不要重复发送' if applied else
                 '操作结果还没确认，先查状态，不要重复发送')
        outcome = "side_effect_unknown"
        delivery_state = "unknown"
    result = {
        # ``ok`` means the owner-control request received a definitive safe
        # response.  ``outcome`` carries the external action truth.
        "ok": True,
        "reply": reply,
        "requestId": request_id,
        "route": "owner_task",
        "outcome": outcome,
        "deliveryState": delivery_state,
        "attempted": True,
        "deliveryReceipt": selected,
        "errorCode": "cognitive_commit_failed",
        "errorStage": stage,
        "persistenceWarning": True,
    }
    if message_id:
        result["messageId"] = message_id
    return result


class AtomicChatDelivery:
    def __init__(
        self, requests: RequestPort, generation: GenerationPort, recovery: RecoveryPort,
        *, load_settings: Callable[[], dict[str, Any]], write_log: Callable[..., None],
        clock: Callable[[], float],
    ) -> None:
        self.requests = requests
        self.generation = generation
        self.recovery = recovery
        self.load_settings = load_settings
        self.write_log = write_log
        self.clock = clock

    def deliver(self, response: JsonResponse, body: dict[str, Any]) -> None:
        request_id = self.requests.prepare_external_chat_body(body)
        supersedes_request_id = RequestRuntime.normalize_chat_request_id(body.get("supersedesRequestId"))
        claim, claim_value = self.requests.claim_chat_request(request_id, supersedes_request_id)
        if claim == "owner":
            self.generation.begin_persona_skill_record(body)
        if claim == "cached":
            cached = dict(claim_value)
            cached["requestDeduplicated"] = True
            response.send_json(cached)
            return
        if claim == "wait":
            event = claim_value
            if isinstance(event, threading.Event) and event.wait(190):
                cached = self.requests.completed_chat_request(request_id)
                if cached:
                    cached["requestDeduplicated"] = True
                    response.send_json(cached)
                    return
            response.send_json({
                "ok": False,
                "code": "duplicate_request_wait_timeout",
                "error": "相同请求仍在处理中，请稍后重试。",
                "retryable": True,
                "requestId": request_id,
            }, 503)
            return

        settings = self.load_settings()
        started = self.clock()
        try:
            result = self.generation.process_chat_request_body(
                body,
                settings,
                request_id=request_id,
                started_at=started,
            )
            result = require_delivery(result, settings=settings, logger=self.write_log, request_id=request_id)
            self.requests.finish_chat_request(request_id, result)
            delivered = response.send_json(result)
            self.write_log(
                "chat_response_delivery",
                requestId=request_id,
                delivered=delivered,
                source=str(result.get("source") or ""),
                characters=len(str(result.get("reply") or "")),
                latencyMs=int(result.get("latencyMs") or 0),
                postprocessDegraded=bool(result.get("pipelineWarnings")),
            )
        except Exception as error:
            # A QQ/tool side effect may have reached the external service
            # before cognitive finalisation tried to commit its revision.  In
            # that case recovery must never call the model/tools again: return
            # the durable delivery receipt with a local-persistence warning.
            preserved = _commit_failure_result(request_id, body, error)
            if preserved is not None:
                self.requests.finish_chat_request(request_id, preserved)
                delivered = response.send_json(preserved)
                self.write_log(
                    "chat_side_effect_preserved_after_commit_failure",
                    requestId=request_id,
                    stage=preserved["errorStage"],
                    outcome=preserved["outcome"],
                    delivered=delivered,
                    receiptCount=len(_delivery_receipts(body)),
                    deliveryState=preserved["deliveryState"],
                )
                return
            user_message, messages = streaming_user_message(body)
            recovered = (
                self.recovery.run_isolated_context_recovery(
                    body,
                    settings,
                    user_message,
                    messages,
                    request_id=request_id,
                    route_name="atomic_chat",
                    failure=error,
                    started_at=started,
                )
                if not (isinstance(error, DeliveryError) and error.code == "leak_blocked")
                and self.recovery.pipeline_failure_allows_model_rescue(error)
                and not self.requests.chat_request_cancelled(request_id)
                else None
            )
            if recovered is not None:
                recovered = require_delivery(recovered, settings=settings, logger=self.write_log, request_id=request_id)
                self.requests.finish_chat_request(request_id, recovered)
                response.send_json(recovered)
                self.write_log(
                    "chat_atomic_isolated_context_recovered",
                    requestId=request_id,
                    initialErrorType=type(error).__name__,
                    decision=str(recovered.get("contextRecoveryDecision") or ""),
                )
                return
            self.requests.finish_chat_request(request_id, error=str(error))
            raise
