"""Bounded model-assisted recovery for a failed AMADEUS turn.

The normal dialogue/persona pipeline is intentionally absent here.  This
module describes a control-plane decision: inspect a compact task snapshot,
choose one safe next step, and return a machine-readable plan.  Runtime code
still owns permissions, tool execution, receipts, and retry limits.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping, Sequence


RescueDecision = Literal[
    "execute_tools",
    "deliver",
    "ask_user",
    "retry_answer",
    "abort",
]

ContextRecoveryDecision = Literal["retry_normal", "ask_user", "abort"]


ISOLATED_CONTEXT_RECOVERY_SYSTEM_PROMPT = """你是 AMADEUS 的上下文整理器，不是聊天角色，也不负责回答用户。
正常处理链路刚刚因为上下文冲突或协议错误而失败。请只根据本次提供的近期原始对话、当前用户输入、回复引用、待执行动作和真实动作回执，重建“用户这一轮真正要完成的任务”。
这里没有人格、长期记忆和角色设定；不要补入这些信息，不要调用工具，不要写最终回答。

规则：
1. currentUserMessage 是当前输入；recentDialogue 按时间顺序排列，user 消息高于 assistant 的旧推断。
2. “我确认 / 继续 / 就这个 / 对”等短句通常承接上一条明确提问或待执行动作，不等于纠正前文。
3. 只有用户明确表示先前对象、指代或理解错误时，才把旧消息列入 excludedMessageIds。
4. relevantMessageIds 只列重建当前任务真正需要的消息；currentUserMessageId 必须视为相关。
5. pendingAction 和 actionReceipts 是后端事实。不得凭文字创造权限，不得把未成功动作说成成功。
6. 若上下文足够，decision=retry_normal，并给出独立、明确、可直接交给正常回答链路的 taskGoal。
7. 只有确实缺少不可替代的信息时才 decision=ask_user，并在 clarificationZh 中自然地说“我没太理解具体指什么，可以再解释一下吗”或询问唯一缺失项。
8. 用户已取消时 decision=abort。不要因为内部报错而要求用户重说已经清楚的内容。

只输出 JSON：
{"decision":"retry_normal|ask_user|abort","taskGoal":"明确的当前任务","missingContext":["缺失项"],"relevantMessageIds":["消息ID"],"excludedMessageIds":["会造成串话且与本任务无关的消息ID"],"pendingActionRelation":"continue|replace|cancel|pause|none","confidence":0到1,"diagnosis":"内部简短原因","clarificationZh":"仅 ask_user 时填写"}。"""


PIPELINE_RESCUE_SYSTEM_PROMPT = """你是 AMADEUS 的故障恢复控制器，不是角色聊天模型。
正常回答架构已经报错或互相矛盾。你只根据给出的当前用户输入、已解析任务、待处理动作、真实工具回执、可用工具和异常摘要，决定下一步。
不要使用人格、长期记忆、角色口癖，也不要延续失败回答的猜测。
orderedUserInputs 按时间排序；后一句可能是在补充、纠正或取消前一句。用户原文高于旧助手回复，真实 toolResults/actionReceipts 高于任何文字声明。
你可以：
1. execute_tools：当前授权和目标已足够时调用一到两个必要工具；
2. retry_answer：已有工具结果足够，只需重新形成回答；
3. deliver：无需工具即可给出准确、独立的最终说明；
4. ask_user：确实缺少不可替代的目标、权限或选择；
5. abort：用户已取消，或继续会造成未授权副作用。
不得声称未成功的工具已经完成；不得重复已有副作用；不得为了“保险”调用无关工具；不得把工具错误伪装成听不懂用户。
只输出一个 JSON 对象：
{"decision":"execute_tools|deliver|ask_user|retry_answer|abort","diagnosis":"一句内部原因","
"toolAuthorization":"none|read|write|observe|navigate","targetDomain":"none|screen|physical_user|uploaded_image|document|local_schedule|browser","calls":[{"name":"工具名","args":{}}],"
"replyZh":"deliver/ask_user/retry_answer 时的中文最终回答","spokenJa":"对应日语；无法准确对应时留空","requiresUserResponse":false,"expectedResponseType":"free_text|confirmation|browser_target|choice|"}。
execute_tools 时 replyZh/spokenJa 可留空。abort 必须留空 calls。"""


PIPELINE_RESCUE_FINALIZE_PROMPT = """你是 AMADEUS 的故障恢复收尾器，不是角色聊天模型。
根据原始任务和刚执行的真实工具结果写一个简短、独立、如实的最终回答。成功只能来自 ok=true 或 accepted 回执；失败必须明确说没有完成，并保留可重试目标。不要再请求工具，不要解释内部架构。
只输出 JSON：{"decision":"deliver|ask_user|abort","diagnosis":"一句内部原因","calls":[],"replyZh":"中文","spokenJa":"严格对应的日语或空字符串","requiresUserResponse":false,"expectedResponseType":"free_text|confirmation|browser_target|choice|"}。"""


@dataclass(frozen=True)
class PipelineRescuePlan:
    decision: RescueDecision
    diagnosis: str
    calls: tuple[dict[str, Any], ...]
    tool_authorization: str
    target_domain: str
    reply_zh: str
    spoken_ja: str
    requires_user_response: bool
    expected_response_type: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["calls"] = [dict(item) for item in self.calls]
        return value


@dataclass(frozen=True)
class IsolatedContextRecovery:
    decision: ContextRecoveryDecision
    task_goal: str
    missing_context: tuple[str, ...]
    relevant_message_ids: tuple[str, ...]
    excluded_message_ids: tuple[str, ...]
    pending_action_relation: str
    confidence: float
    diagnosis: str
    clarification_zh: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_isolated_context_recovery(
    value: Any,
    *,
    allowed_message_ids: Sequence[str] = (),
) -> IsolatedContextRecovery | None:
    if not isinstance(value, Mapping):
        return None
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"retry_normal", "ask_user", "abort"}:
        return None
    task_goal = str(value.get("taskGoal") or "").strip()[:2000]
    clarification = str(value.get("clarificationZh") or "").strip()[:1000]
    if decision == "retry_normal" and not task_goal:
        return None
    if decision == "ask_user" and not clarification:
        clarification = "我好像没太理解你具体指什么，可以再解释一下吗？"
    allowed = {str(item) for item in allowed_message_ids if str(item)}

    def message_ids(field: str) -> tuple[str, ...]:
        raw = value.get(field) if isinstance(value.get(field), list) else []
        result: list[str] = []
        for item in raw:
            message_id = str(item or "").strip()[:200]
            if message_id and (not allowed or message_id in allowed) and message_id not in result:
                result.append(message_id)
        return tuple(result[:40])

    missing = tuple(
        str(item).strip()[:300]
        for item in (
            value.get("missingContext")
            if isinstance(value.get("missingContext"), list)
            else []
        )[:8]
        if str(item).strip()
    )
    relation = str(value.get("pendingActionRelation") or "none").strip().lower()
    if relation not in {"continue", "replace", "cancel", "pause", "none"}:
        relation = "none"
    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return IsolatedContextRecovery(
        decision=decision,  # type: ignore[arg-type]
        task_goal=task_goal,
        missing_context=missing,
        relevant_message_ids=message_ids("relevantMessageIds"),
        excluded_message_ids=message_ids("excludedMessageIds"),
        pending_action_relation=relation,
        confidence=confidence,
        diagnosis=str(value.get("diagnosis") or "").strip()[:500],
        clarification_zh=clarification,
    )


def normalize_pipeline_rescue_plan(value: Any) -> PipelineRescuePlan | None:
    if not isinstance(value, Mapping):
        return None
    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {
        "execute_tools", "deliver", "ask_user", "retry_answer", "abort",
    }:
        return None
    authorization = str(value.get("toolAuthorization") or "none").strip().lower()
    if authorization not in {"none", "read", "write", "observe", "navigate"}:
        authorization = "none"
    target_domain = str(value.get("targetDomain") or "none").strip().lower()[:40]
    raw_calls: Sequence[Any] = (
        value.get("calls") if isinstance(value.get("calls"), list) else []
    )
    calls: list[dict[str, Any]] = []
    for item in raw_calls[:2]:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()[:80]
        args = item.get("args") if isinstance(item.get("args"), Mapping) else {}
        if name:
            calls.append({"name": name, "args": dict(args)})
    if decision != "execute_tools":
        calls = []
    if decision == "execute_tools" and not calls:
        return None
    expected = str(value.get("expectedResponseType") or "").strip()
    if expected not in {"", "free_text", "confirmation", "browser_target", "choice"}:
        expected = ""
    return PipelineRescuePlan(
        decision=decision,  # type: ignore[arg-type]
        diagnosis=str(value.get("diagnosis") or "").strip()[:400],
        calls=tuple(calls),
        tool_authorization=authorization,
        target_domain=target_domain or "none",
        reply_zh=str(value.get("replyZh") or "").strip()[:4000],
        spoken_ja=str(value.get("spokenJa") or "").strip()[:4000],
        requires_user_response=bool(value.get("requiresUserResponse")),
        expected_response_type=expected,
    )
