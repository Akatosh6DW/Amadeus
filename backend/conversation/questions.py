"""会话末尾的真实提问判定；用于等待状态和主动发言的共同边界。

问号后继续自答不算等待用户，避免普通对话和主动交流各维护一套判定。
"""
from __future__ import annotations
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Protocol

from agent.attachment_editing import attachment_request_stopped
from agent.dialogue_waiting import refresh_waiting_context
from backend.research.query import web_query_overlap_tokens


class PendingQuestionState(Protocol):
    """读取待回答问题所需的实时会话状态，不缓存第二份副本。"""

    def read_context(self) -> dict[str, Any]: ...

    def current_pending_action(self) -> dict[str, Any]: ...

    def session_id(self) -> str: ...

def assistant_question_from_text(value: Any) -> str:
    """Return the final explicit question while preserving its local wording."""

    text = re.sub(r"[ \t]+", " ", str(value or "")).strip()
    if not text:
        return ""
    explicit = re.findall(r"(?:^|[。！!\n])\s*([^。！!\n]{1,260}[？?])", text)
    if explicit:
        return explicit[-1].strip()[:300]
    final_sentence = re.split(r"[。！!\n]+", text)[-1].strip()
    if (
        2 <= len(final_sentence) <= 220
        and re.search(
            r"(?:吗|么|呢|哪(?:个|些|种|里|边)?|谁|什么|怎么|如何|为何|为什么|是否|"
            r"能不能|会不会|有没有|还是|愿不愿意|想不想)$",
            final_sentence,
        )
    ):
        return final_sentence[:300]
    return ""


def terminal_answerable_question(value: Any) -> str:
    """Return an explicit question only when it is the final spoken move.

    A rhetorical question followed by the assistant's own explanation is not
    an invitation for the user to answer.  Keeping this distinction at the
    conversation boundary prevents both stale pending-question state and
    proactive monologues that merely contain a question mark.
    """

    text = re.sub(r"[ \t]+", " ", str(value or "")).strip()
    if not text:
        return ""
    question_marks = list(re.finditer(r"[？?]", text))
    if not question_marks:
        return ""
    final_mark = question_marks[-1]
    trailing = text[final_mark.end():]
    trailing = re.sub(r"[\s。！!…~～\"'“”‘’）)\]】》〉]+", "", trailing)
    if trailing:
        return ""
    return assistant_question_from_text(text[:final_mark.end()])


def should_persist_assistant_question(
    user_message: str,
    assistant_text: str,
    question: str,
    result: dict[str, Any] | None = None,
) -> bool:
    """排除动作回声式反问，只保留真正等待用户回答的问题。"""

    question_text = re.sub(r"\s+", " ", str(question or "")).strip()
    if isinstance(result, Mapping) and attachment_request_stopped(result):
        return False
    if isinstance(result, dict) and result.get("requiresUserResponseProvided"):
        if not bool(result.get("requiresUserResponse")):
            return False
        return bool(str(assistant_text or "").strip())
    if not question_text:
        return False
    if not terminal_answerable_question(assistant_text):
        return False
    user_text = re.sub(r"\s+", " ", str(user_message or "")).strip()
    assistant_value = re.sub(r"\s+", " ", str(assistant_text or "")).strip()
    if (
        re.search(r"打开|访问|进入|跳转|读取|保存|创建|修改|搜索", user_text, re.I)
        and re.search(r"(?:是吧|对吧|没错吧|对吗)[？?]?$", question_text)
        and re.search(r"稍等|等一下|我来|帮你|给你|开始|处理|打开|执行", assistant_value, re.I)
    ):
        subject = re.sub(
            r"^[…。\s“”\"']+|(?:是吧|对吧|没错吧|对吗)[？?]?$",
            "",
            question_text,
            flags=re.I,
        ).strip(" ，,。！？!?“”\"'")
        if (
            not subject
            or subject.casefold() in user_text.casefold()
            or bool(
                web_query_overlap_tokens(subject)
                & web_query_overlap_tokens(user_text)
            )
        ):
            return False
    receipt = (
        result.get("actionReceipt")
        if isinstance(result, dict) and isinstance(result.get("actionReceipt"), dict)
        else {}
    )
    if (
        receipt.get("status") == "accepted"
        and re.search(r"(?:是吧|对吧|没错吧|对吗)[？?]?$", question_text)
    ):
        return False
    return True


def pending_assistant_question_prompt(
    state: PendingQuestionState,
    messages: list[dict[str, Any]] | None,
    current_user_message: str,
) -> str:
    """把实时 typed pending 状态投影为相邻话轮提示。"""

    context = refresh_waiting_context(
        state.read_context(),
        now=datetime.now(timezone.utc),
        session_id=state.session_id(),
    )
    pending = context.get("pendingAssistantQuestion") or {}
    question = str(pending.get("text") or "").strip()
    origin = str(pending.get("origin") or "chat")
    asked_at = str(pending.get("askedAt") or "")
    latest_turn = next((
        turn
        for turn in reversed(context.get("recentTurns", []))
        if isinstance(turn, Mapping) and turn.get("assistant")
    ), {})
    if attachment_request_stopped(latest_turn):
        return ""
    # typed waiting 状态是唯一依据；旧客户端文本不能复活已回答、暂停或旧会话问题。
    if not question:
        return ""
    pending_todo = state.current_pending_action()
    continues_authorized_todo = pending_todo.get("type") == "todo_mutation"
    return "\n".join([
        "[待回答问题：相邻话轮连续性]",
        f"AMADEUS 上一轮亲自提出的问题：{question}",
        f"问题ID：{pending.get('questionId', '')}；这只是可接续的上下文，不是强制作答。",
        f"问题来源：{'随机主动互动' if origin == 'proactive' else '正常对话追问'}"
        + (f"；提出时间：{asked_at}" if asked_at else ""),
        f"用户当前回复：{str(current_user_message or '').strip()}",
        (
            "先判断当前回复是否在回答这个问题。简短肯定、否定、二选一、名词短语或补充说明，"
            "都可能是对上一问的自然回答，不能因为省略了问题全文就擅自切换到更早的话题。"
        ),
        "当前消息也可能自然换题或取消，不要求用户先回答或先说取消；由模型理解整体语义，不能强行把所有新消息都解释成答案。",
        (
            "当前还存在结构化的待续接 Todo 操作。若用户是在补充那项已授权操作的缺失信息，"
            "这不是把回答包装成一个新任务：应继续原操作并发出 todo_manage；只有语义无关、取消或仍缺关键信息时才不写入。"
            if continues_authorized_todo else
            "如果当前回复是在回答你刚才的问题，就把这一轮当作同一次自然交谈的后半句：先理解并回应用户给出的答案，"
            "用符合人格的一点反应自然收住。不要把用户的回答重新包装成一个待办理的新任务，不要复述一遍后主动推销"
            "你能提供的服务，也不要仅为了延长交流而立刻再抛出另一个问题。只有用户明确请你继续做事，或缺少关键条件"
            "导致当前请求无法完成时，才提出必要的下一步或澄清。"
        ),
    ])
