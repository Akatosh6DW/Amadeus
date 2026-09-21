"""Model-declared response needs and bounded conversational waiting state."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Mapping

WAIT_TTL_SECONDS = 30 * 60
WAIT_HISTORY_LIMIT = 8
RELATIONS = frozenset({"answer", "new_topic", "cancel", "resume", "unclear"})
WAITING_PROTOCOL = """[对话等待]
缺少完成当前请求所必需的信息时，outcome.status=needs_input，直接自然说明需要用户补充什么；不必有问号。后端据此同步等待状态。修辞反问、自问自答不表示等待；已回答当前问题时不要仅为延长交流制造追问。
packet.dialogueWaiting 中 active 是近期未解决问题，history 是已搁置/取消/回答的问题，均不是要求用户必须作答的指令。根据当前消息的整体语义，判断是在回答、换题、取消、恢复旧事还是暂不明确；不用固定关键词，换题也不要求用户先说“先不管了”。active 非空时必须在 cognitive.questionRelation 给出 {questionId:真实问题ID,relation:answer|new_topic|cancel|resume|unclear}；明确接续历史问题时也给出对应历史 ID。
用户回答则承接；换题优先处理新问题并搁置原请求；取消则结束原请求。明确恢复时再接回旧上下文，缺少的信息仍需补充。历史问题不能默认占据当前话题，沉默不算完成或取消，不因没有回答而催问。不确定是否接续时自然澄清，不能执行缺少目标或授权的操作。
问题绑定当前 TaskFrame 时，new_topic/cancel/resume 会同步暂停/取消/恢复该任务；其他新任务仍按正常状态协议管理。
"""


def question_relation(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    ident, relation = value.get("questionId"), value.get("relation")
    if isinstance(ident, str) and ident and isinstance(relation, str) and relation in RELATIONS:
        return {"questionId": ident[:160], "relation": relation}
    return {}


def synchronize_response_waiting(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project typed model intent; never infer questions from punctuation."""
    result = dict(value)
    cognitive = result.get("cognitive")
    outcome = result.get("_cognitiveOutcome") or result.get("outcome")
    if not isinstance(outcome, Mapping) and isinstance(cognitive, Mapping):
        outcome = cognitive.get("outcome")
    status = outcome.get("status") if isinstance(outcome, Mapping) else None
    kind = str(result.get("semanticKind") or result.get("kind") or "").lower()
    if status == "needs_input" or (kind in {"clarification", "clarify"} and status != "failed"):
        result.update(requiresUserResponse=True, requiresUserResponseProvided=True,
                      semanticKind="clarification")
        if result.get("expectedResponseType") not in {"free_text", "confirmation", "browser_target", "choice"}:
            result["expectedResponseType"] = "free_text"
    elif status == "failed":
        result.update(requiresUserResponse=False, requiresUserResponseProvided=True,
                      expectedResponseType="")
        if kind in {"clarification", "clarify"}:
            result["semanticKind"] = "turn"
    elif isinstance(result.get("requiresUserResponse"), bool):
        result["requiresUserResponseProvided"] = True
    relation = question_relation(result.get("questionRelation"))
    if not relation and isinstance(cognitive, Mapping):
        relation = question_relation(cognitive.get("questionRelation"))
    if relation:
        result["questionRelation"] = relation
    return result


def _date(value: Any, now: datetime) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else result.replace(tzinfo=now.tzinfo)
    except (TypeError, ValueError):
        return None


def remember_wait(context: dict, pending: Mapping, status: str, reason: str, now: datetime) -> None:
    history = [dict(item) for item in context.get("assistantQuestionHistory", []) if isinstance(item, Mapping)]
    item = {**deepcopy(dict(pending)), "status": status, "closedReason": reason, "closedAt": now.isoformat()}
    ident = item.get("questionId") or item.get("sourceTurnId") or item.get("askedAt")
    history = [row for row in history if (row.get("questionId") or row.get("sourceTurnId") or row.get("askedAt")) != ident]
    context["assistantQuestionHistory"] = (history + [item])[-WAIT_HISTORY_LIMIT:]


def refresh_waiting_context(context: dict, *, now: datetime, session_id: str) -> dict:
    """In-memory projection; normal atomic conversation writes persist it later."""
    pending = context.get("pendingAssistantQuestion")
    if not isinstance(pending, Mapping) or not pending:
        return context
    # Question context may age out, but silence/restart is not permission to
    # contact the user again. Keep the outreach latch until a user message.
    if pending.get("status", "waiting") == "waiting":
        context["waitingForUser"] = True
    asked = _date(pending.get("askedAt"), now)
    expires = _date(pending.get("expiresAt"), now)
    reason = ""
    if pending.get("sessionId") != session_id:
        reason = "session_changed"
    elif not asked or now >= (expires or asked + timedelta(seconds=WAIT_TTL_SECONDS)):
        reason = "inactive_timeout"
    elif pending.get("status", "waiting") != "waiting":
        reason = "not_active"
    if reason:
        remember_wait(context, pending, "suspended", reason, now)
        context["pendingAssistantQuestion"] = {}
        context.setdefault("conversationState", {})["pendingAssistantQuestionId"] = ""
    return context


def waiting_packet(context: Mapping) -> dict:
    return {
        "active": deepcopy(context.get("pendingAssistantQuestion") or {}),
        "history": [deepcopy(item) for item in context.get("assistantQuestionHistory", [])
                    if isinstance(item, Mapping)][-3:],
    }


def related_task_patch(directive: Mapping, context: Mapping, task: Mapping) -> dict | None:
    relation = question_relation(directive.get("questionRelation"))
    if not relation:
        return None
    packet = waiting_packet(context)
    pending = next((item for item in [packet["active"], *reversed(packet["history"])]
                    if item.get("questionId") == relation["questionId"]), {})
    if not pending.get("taskId") or pending["taskId"] != task.get("taskId"):
        return None
    operation = {"new_topic": "PAUSE_TASK", "cancel": "CANCEL_TASK", "resume": "RESUME_TASK"}.get(relation["relation"])
    if not operation or task.get("status") not in {"active", "paused"}:
        return None
    # Explicit task patches own the transition if the model already supplied one.
    if any(isinstance(p, Mapping) and p.get("operation") in {
        "START_TASK", "CANCEL_TASK", "PAUSE_TASK", "RESUME_TASK", "FAIL_TASK",
    } for p in directive.get("patches", [])):
        return None
    if operation == "RESUME_TASK" and task.get("status") != "paused":
        return None
    if operation == "PAUSE_TASK" and task.get("status") == "paused":
        return None
    return {"operation": operation, "targetId": task["taskId"]}


def update_waiting_after_turn(context: dict, *, turn: Mapping, result: Mapping,
                              question: str, now: datetime, session_id: str, task_id: str = "") -> None:
    refresh_waiting_context(context, now=now, session_id=session_id)
    if turn.get("user"):
        context["waitingForUser"] = False
    pending = context.get("pendingAssistantQuestion") or {}
    relation = question_relation(result.get("questionRelation"))
    if pending and turn.get("user"):
        matched = relation.get("questionId") == pending.get("questionId")
        kind = relation.get("relation") if matched else "unspecified"
        status = {"answer": "answered", "cancel": "cancelled", "resume": "resumed"}.get(kind, "suspended")
        remember_wait(context, pending, status, kind or "superseded", now)
        context["pendingAssistantQuestion"] = {}
    elif relation and turn.get("user"):
        old = next((item for item in context.get("assistantQuestionHistory", [])
                    if item.get("questionId") == relation["questionId"]), {})
        if old:
            status = {"answer": "answered", "cancel": "cancelled", "resume": "resumed"}.get(relation["relation"], "suspended")
            remember_wait(context, old, status, relation["relation"], now)
    if question:
        context["waitingForUser"] = True
        context["pendingAssistantQuestion"] = {
            "questionId": str(turn["turnId"]), "sourceTurnId": str(turn["turnId"]),
            "text": question, "askedAt": str(turn["at"]), "sessionId": session_id,
            "expiresAt": (now + timedelta(seconds=WAIT_TTL_SECONDS)).isoformat(),
            "status": "waiting", "origin": str(turn.get("origin") or "chat"),
            "originUserMessage": str(turn.get("user") or "")[:1200],
            "expectedResponseType": str(result.get("expectedResponseType") or "free_text"),
            "taskId": task_id,
            **({"clarificationActionId": result["clarificationActionId"]} if result.get("clarificationActionId") else {}),
            **({"proactiveEventId": turn["proactiveEventId"]} if turn.get("proactiveEventId") else {}),
        }
    context.setdefault("conversationState", {})["pendingAssistantQuestionId"] = str(
        (context.get("pendingAssistantQuestion") or {}).get("questionId") or "")
