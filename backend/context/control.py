"""冻结轮次上的上下文控制识别、召回与暂存编排。

词法解析和控制状态变换直接复用 ``agent.context_control``。本模块只协调语义
相似度、一次短模型路由、动作关系和归档召回；它把结果写回同一请求快照，但不
提交会话状态，也不授予工具或动作权限。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol, Sequence

from agent.context_control import (
    apply_control_to_context_state,
    detect_context_directive,
    resolve_context_control,
)
from agent.turn_context import legacy_message_id, turn_messages
from backend.chat.parsing import parse_json_object
from backend.common.values import safe_nonnegative_int
from backend.context.conversation import turn_conversation_id


CONTEXT_DIRECTIVE_PROTOTYPES: dict[str, tuple[str, ...]] = {
    "RECALL": (
        "重新联想一下我们更早聊过的相关内容",
        "你漏掉了我先前告诉你的关键信息",
        "回到以前谈过的那件事再回答",
        "根据之前的聊天重新想一想",
        "这和我们早些时候聊的内容有关",
    ),
    "BRIDGE": (
        "把我之前说的事情和现在这个问题放在一起考虑",
        "联系更早的背景综合回答当前问题",
        "同时参考前面的情况和眼下的情况",
    ),
    "CORRECT": (
        "你刚才理解的对象错了，我指的是另一个",
        "不是你说的那个，请按我真正指的对象重新理解",
        "你把两件事弄混了，我正在纠正你的理解",
    ),
    "EXCLUDE": (
        "不要再联系刚才那个话题",
        "把先前那张图排除在当前问题之外",
        "这次不要沿用之前的对象或证据",
    ),
    "NONE": (
        "我确认，按你刚才问的继续",
        "对，就这样做",
        "继续刚才没有完成的事情",
        "我明白了",
        "这个问题怎么解决",
        "请回答我现在的问题",
        "不用了，先停下",
    ),
}


class ContextSemanticEmbedding(Protocol):
    def similarities(
        self,
        query: str,
        documents: Sequence[str],
    ) -> Sequence[float] | None: ...


@dataclass(frozen=True)
class ContextSemanticServices:
    embedding: ContextSemanticEmbedding


class ContextDirectiveModel(Protocol):
    def configured(self, settings: dict[str, Any]) -> bool: ...
    def send_chat(self, *args: Any, **kwargs: Any) -> str: ...


class ContextDirectiveRuntime(Protocol):
    def write_log(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class ContextDirectiveServices:
    model: ContextDirectiveModel
    runtime: ContextDirectiveRuntime


class ContextControlState(Protocol):
    """读取请求认知状态、动作解释和同会话归档。"""

    def cognitive_turn_state(self, body: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def prepare_action_turn(self, *args: Any, **kwargs: Any) -> Any: ...
    def read_archive(self) -> list[dict[str, Any]]: ...


class ContextControlRouting(Protocol):
    """保留旧动态替换点的三项上下文路由能力。"""

    def directive_scores(self, user_message: str) -> dict[str, float]: ...
    def model_decision(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...
    def history_scores(
        self,
        query: str,
        turns: Sequence[Mapping[str, Any]],
    ) -> dict[str, float]: ...


@dataclass(frozen=True)
class ContextControlServices:
    state: ContextControlState
    routing: ContextControlRouting
    runtime: ContextDirectiveRuntime


def semantic_context_directive_scores(
    services: ContextSemanticServices,
    user_message: str,
) -> dict[str, float]:
    """比较控制意图原型，只返回召回提示，不直接创建控制指令。"""

    labels: list[str] = []
    examples: list[str] = []
    for label, values in CONTEXT_DIRECTIVE_PROTOTYPES.items():
        for value in values:
            labels.append(label)
            examples.append(value)
    scores = services.embedding.similarities(user_message, examples)
    if scores is None:
        return {}
    grouped: dict[str, list[float]] = {}
    for label, score in zip(labels, scores):
        grouped.setdefault(label, []).append(float(score))
    return {label: max(values) for label, values in grouped.items() if values}


def model_context_directive_decision(
    services: ContextDirectiveServices,
    settings: Mapping[str, Any],
    user_message: str,
    previous_turn: Mapping[str, Any] | None,
    *,
    request_id: str,
) -> dict[str, Any]:
    """用无人格、无记忆、无工具权限的短请求判断上下文控制意图。"""

    if not services.model.configured(dict(settings)) or not settings.get(
        "contextSemanticRouterEnabled", True
    ):
        return {}
    payload: dict[str, Any] = {
        "model": settings["apiModel"],
        "temperature": 0.0,
        "max_tokens": 140,
        "messages": [
            {
                "role": "system",
                "content": (
                    "只判断当前用户是否在控制对话上下文。输出 JSON："
                    '{"kind":"CORRECT|RECALL|BRIDGE|EXCLUDE|NONE",'
                    '"confidence":0到1,"queryText":"用于检索的对象或话题，不含抱怨和控制套话"}。'
                    "CORRECT=纠正你刚才理解错的对象；RECALL=要求重新联想更早内容；"
                    "BRIDGE=要求把前文与当前内容结合；EXCLUDE=要求排除旧对象；"
                    "普通否定句、普通评价和普通新问题必须是 NONE。"
                    "不得决定具体消息、人物、文件或工具目标，不得输出回答。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "previousUser": str((previous_turn or {}).get("user") or "")[
                            :400
                        ],
                        "previousAssistant": str(
                            (previous_turn or {}).get("assistant") or ""
                        )[:500],
                        "currentMessage": user_message,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    if settings.get("useResponseFormat"):
        payload["response_format"] = {"type": "json_object"}
    if "deepseek.com" in str(settings.get("apiEndpoint") or ""):
        payload["thinking"] = {"type": "disabled"}
    try:
        content = services.model.send_chat(
            dict(settings),
            payload,
            timeout=8,
            request_id=request_id,
            purpose="context_control_gate",
        )
        parsed = parse_json_object(content) or {}
    except Exception as error:
        services.runtime.write_log(
            "context_control_router_failed",
            requestId=request_id,
            errorType=type(error).__name__,
            error=str(error)[:400],
        )
        return {}
    kind = str(parsed.get("kind") or "NONE").strip().upper()
    confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
    if kind not in {"CORRECT", "RECALL", "BRIDGE", "EXCLUDE"} or confidence < 0.90:
        return {"kind": "NONE", "confidence": confidence, "queryText": ""}
    return {
        "kind": kind,
        "confidence": confidence,
        "queryText": str(parsed.get("queryText") or "").strip()[:500],
    }


def semantic_history_message_scores(
    services: ContextSemanticServices,
    query: str,
    turns: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    messages = [
        message
        for sequence, turn in enumerate(turns, 1)
        if isinstance(turn, Mapping)
        for message in turn_messages(turn, sequence)
        if message.get("role") == "user"
    ]
    scores = services.embedding.similarities(
        query,
        [str(item.get("text") or "") for item in messages],
    )
    if scores is None:
        return {}
    return {
        str(item.get("messageId") or ""): float(score)
        for item, score in zip(messages, scores)
        if str(item.get("messageId") or "")
    }


def ensure_context_control(
    services: ContextControlServices,
    body: dict[str, Any],
    *,
    user_message: str,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """依据冻结的服务端状态解析显式纠正与历史召回。"""

    snapshot = body.get("_turnContextSnapshot")
    if not isinstance(snapshot, dict):
        return {}
    if services.state.cognitive_turn_state(body):
        # 认知模式由普通主模型决定纠正和召回。候选仍可用，但任何词法或嵌入分数
        # 都不能提前拒绝指代对象，也不能自行创建权威补丁。
        snapshot.setdefault("_contextControl", {})
        return snapshot["_contextControl"]
    existing = snapshot.get("_contextControl")
    if isinstance(existing, dict):
        return existing
    context = snapshot.get("_contextState")
    context = context if isinstance(context, dict) else {}
    epoch = safe_nonnegative_int(snapshot.get("epoch"))
    conversation_id = str(snapshot.get("conversationId") or "")
    recent_turns = [
        item
        for item in context.get("recentTurns", [])
        if isinstance(item, Mapping) and turn_conversation_id(item) == conversation_id
    ]
    previous_resolution_records = [
        item
        for item in context.get("resolutionRecords", [])
        if isinstance(item, Mapping)
    ]
    if not previous_resolution_records and recent_turns:
        previous_turn = recent_turns[-1]
        previous_turn_id = str(previous_turn.get("turnId") or "legacy_previous_turn")
        previous_resolution_records = [
            {
                "resolutionId": f"res_{previous_turn_id}_legacy",
                "sourceTurnId": previous_turn_id,
                "kind": "legacy_turn_resolution",
                "selectedMessageIds": [
                    str(
                        previous_turn.get("assistantMessageId")
                        or legacy_message_id(previous_turn_id, "assistant")
                    )
                ],
                "status": "active",
            }
        ]
    pending_action = (
        dict(context.get("pendingAction"))
        if isinstance(context.get("pendingAction"), Mapping)
        else {}
    )
    coordinated_prepared = services.state.prepare_action_turn(
        user_message,
        request_id=str(snapshot.get("requestId") or ""),
        pending_action=pending_action,
    )
    body["_preparedActionTurn"] = coordinated_prepared
    action_relation = coordinated_prepared.pending_relation.relation
    action_transition = action_relation in {"continue", "replace", "cancel", "pause"}
    # 本轮只解析一次话语作用域。当前文字若是活跃动作的类型化转移，就不能再被
    # 独立解释为对话纠正；动作与上下文两个子系统共同消费这一决定。
    context_control_text = "" if action_transition else user_message
    lexical_directive = detect_context_directive(
        context_control_text,
        has_previous_resolution=bool(previous_resolution_records),
        has_reply_anchor=bool(snapshot.get("resolvedAnchor")),
    )
    embedding_scores = (
        {}
        if lexical_directive or not context_control_text
        else services.routing.directive_scores(context_control_text)
    )
    embedding_candidate = (
        detect_context_directive(
            context_control_text,
            has_previous_resolution=bool(previous_resolution_records),
            has_reply_anchor=bool(snapshot.get("resolvedAnchor")),
            semantic_scores=embedding_scores,
        )
        if embedding_scores and not lexical_directive
        else {}
    )
    # 嵌入分数只是低成本召回提示，没有修改上下文的权威。语义接近无法可靠区分
    # “我确认”一类短确认与元层纠正；只有显式词法控制或上下文短路由可以建指令。
    directive_scores: dict[str, float] = {}
    semantic_query_text = ""
    router_used = False
    if (
        context_control_text
        and not lexical_directive
        and (bool(embedding_candidate) or not embedding_scores)
    ):
        router = services.routing.model_decision(
            settings or {},
            context_control_text,
            recent_turns[-1] if recent_turns else None,
            request_id=str(snapshot.get("requestId") or ""),
        )
        router_kind = str(router.get("kind") or "NONE")
        if router_kind in {"CORRECT", "RECALL", "BRIDGE", "EXCLUDE"}:
            confidence = float(router.get("confidence") or 0.0)
            directive_scores = {
                "CORRECT": 0.0,
                "RECALL": 0.0,
                "BRIDGE": 0.0,
                "EXCLUDE": 0.0,
                "NONE": 0.0,
                router_kind: confidence,
            }
            semantic_query_text = str(router.get("queryText") or "")
            router_used = True
    inputs = {
        "user_text": context_control_text,
        "conversation_id": str(snapshot.get("conversationId") or ""),
        "turn_id": str(snapshot.get("currentTurnId") or ""),
        "previous_resolution_records": previous_resolution_records,
        "active_constraints": context.get("correctionConstraints", []),
        "reply_anchor": snapshot.get("resolvedAnchor"),
        "semantic_directive_scores": directive_scores,
        "semantic_query_text": semantic_query_text,
    }
    control = resolve_context_control(turns=recent_turns, **inputs)
    directive_kind = str(
        ((control.get("directives") or [{}])[0]).get("kind") or ""
    )
    if control.get("ambiguous") and directive_kind in {"CORRECT", "RECALL", "BRIDGE"}:
        message_scores = services.routing.history_scores(
            semantic_query_text or user_message,
            recent_turns,
        )
        control = resolve_context_control(
            turns=recent_turns,
            semantic_message_scores=message_scores,
            **inputs,
        )
    if control.get("ambiguous") and directive_kind in {"CORRECT", "RECALL", "BRIDGE"}:
        durable_turns = [
            item
            for item in services.state.read_archive()
            if turn_conversation_id(item) == conversation_id
        ]
        control = resolve_context_control(
            turns=durable_turns,
            semantic_message_scores=services.routing.history_scores(
                semantic_query_text or user_message,
                durable_turns,
            ),
            **inputs,
        )
    if router_used and control.get("directives"):
        control["detectionMethod"] = "semantic_router"
        retrieval_trace = control.get("retrievalTrace")
        if isinstance(retrieval_trace, dict):
            retrieval_trace["detectionMethod"] = "semantic_router"
    coordination = {
        "utteranceScope": "action_transition" if action_transition else "dialogue",
        "pendingActionId": str(pending_action.get("actionId") or "")[:100],
        "pendingActionRelation": action_relation,
        "contextDirectiveKind": directive_kind,
        "pendingActionMutationOwner": "action_state_machine",
        "contextMayMutatePendingAction": False,
    }
    body["_turnCoordination"] = coordination
    snapshot["_turnCoordination"] = coordination
    snapshot["_contextControl"] = control
    snapshot["_resolvedContextState"] = apply_control_to_context_state(context, control)
    services.runtime.write_log(
        "context_control_resolved",
        requestId=str(snapshot.get("requestId") or ""),
        rootSnapshotId=str(snapshot.get("rootSnapshotId") or ""),
        directiveKind=directive_kind,
        rejectedMessageIds=list(control.get("rejectedMessageIds") or []),
        recalledMessageIds=[
            str(item.get("messageId") or "")
            for item in control.get("recalledMessages", [])
            if isinstance(item, Mapping)
        ],
        ambiguous=bool(control.get("ambiguous")),
        detectionMethod=str(control.get("detectionMethod") or "none"),
        embeddingScores={
            str(key): round(float(value), 6)
            for key, value in embedding_scores.items()
        },
        embeddingCandidate=str(embedding_candidate.get("kind") or ""),
        semanticRouterUsed=router_used,
        utteranceScope=coordination["utteranceScope"],
        pendingActionRelation=coordination["pendingActionRelation"],
    )
    return control
