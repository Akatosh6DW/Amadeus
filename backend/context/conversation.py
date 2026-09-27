"""会话历史、近轮原文与说话人归属的提示装配。

请求快照仍由入口拥有；本模块只筛选传入快照或经明确读取接口取得的历史。
记忆召回、主动联系状态及画面历史各自有所有者，不在这里重建第二套状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any, Callable, Mapping, Protocol

from agent.cognitive_context import (
    candidate_is_preferred, candidate_is_suppressed, effective_context_controls,
    effective_retrieval_query, prioritize_context_candidates, scope_allows_archive,
)
from agent.context_pressure import bounded_text_preserving_authority
from agent.dialogue_architecture import TurnPolicy
from agent.epistemic_architecture import compile_claim_context, compiled_context_prompt
from agent.memory_architecture import MemoryNeed
from agent.turn_context import conversation_id_for_epoch, turn_messages
from backend.common.values import safe_nonnegative_int


@dataclass(frozen=True)
class ConversationLimits:
    """每次调用读取的原有历史窗口限值，不缓存入口配置。"""

    recent: int
    immediate: int
    medium: int


class ConversationStore(Protocol):
    """历史读取仍由现有仓储负责；快照路径不会无条件读回文件。"""

    def read_context(self) -> dict[str, Any]: ...
    def read_archive(self) -> list[dict[str, Any]]: ...
    def recover_recent(
        self, limit: int, *, prefer_working_set: bool = False,
    ) -> tuple[list[dict[str, Any]], str]: ...


class ConversationSession(Protocol):
    """运行会话时间与旧承接规则；权限与时间快照不在提示装配里更新。"""

    def started_at(self) -> datetime: ...
    def parse_datetime(self, value: Any) -> datetime | None: ...
    def has_user_turn(self, turns: list[dict[str, Any]]) -> bool: ...
    def needs_recent_context(self, text: str) -> bool: ...


class ConversationOverlays(Protocol):
    """给已选历史补充既有主动联系状态和画面记录。"""

    def proactive_prompt(self, context: Mapping[str, Any]) -> str: ...
    def screen_prompt(self, user_message: str, context: dict[str, Any]) -> str: ...


def turn_conversation_epoch(turn: dict[str, Any]) -> int:
    return max(0, int(turn.get("conversationEpoch") or turn.get("contextEpoch") or 0))


def turn_conversation_id(turn: Mapping[str, Any]) -> str:
    """读出归档轮次的会话身份，旧记录沿用原代际回退规则。"""

    return str(
        turn.get("conversationId")
        or conversation_id_for_epoch(
            safe_nonnegative_int(
                turn.get("conversationEpoch") or turn.get("contextEpoch")
            )
        )
    ).strip()


def conversation_history_messages(
    body: Mapping[str, Any],
    messages: list[dict[str, Any]],
    *,
    cognitive_state_reader: Callable[[Mapping[str, Any]], Any],
) -> list[dict[str, Any]]:
    """保留历史原话与引用身份；认知模式下把旧对话编码为数据。"""
    snapshot = body.get("_turnContextSnapshot") or {}
    context = snapshot.get("_contextState") or {}
    conversation_id = snapshot.get("conversationId") or context.get("conversationId")
    stored_users = {}
    for turn in context.get("recentTurns", []):
        if not isinstance(turn, Mapping) or turn_conversation_id(turn) != conversation_id:
            continue
        for message in turn_messages(turn):
            if message["role"] == "user":
                stored_users[message["messageId"]] = (message, turn.get("replyAnchor"))
    chain_edges = {edge["sourceMessageId"]: edge
                   for edge in (snapshot.get("replyContextChain") or {}).get("links", [])}
    history = []
    for item in messages:
        if not isinstance(item, Mapping):
            continue
        meta = item.get("meta") if isinstance(item.get("meta"), Mapping) else {}
        entry = {"role": "assistant" if item.get("role") == "assistant" else "user",
                 "content": str(item.get("content") or ""),
                 **{key: item.get(key) or meta[key] for key in ("messageId", "turnId", "replyToMessageId")
                    if item.get(key) or meta.get(key)}}
        message_id = entry.get("messageId")
        stored, anchor = stored_users.get(message_id, ({}, None))
        if entry["role"] == "user" and stored.get("text") == entry["content"] and isinstance(anchor, Mapping):
            if anchor.get("conversationId", conversation_id) == conversation_id:
                entry["replyAnchor"] = {key: anchor[key] for key in
                    ("type", "targetMessageIds", "startOffset", "endOffset") if key in anchor}
        elif entry["role"] == "user" and message_id in chain_edges:
            entry["replyAnchor"] = {key: value for key, value in chain_edges[message_id].items()
                                    if key != "sourceMessageId"}
        history.append(entry)
    if not history:
        return []
    if not cognitive_state_reader(body):
        return [{"role": item["role"], "content": item["content"]} for item in history]
    # Stored dialogue contains visible prose, not the model's control envelope.
    # Replaying it as assistant completions teaches the provider to drop JSON
    # (and can produce only whitespace in JSON mode). Keep the exact dialogue
    # and speaker identities as data, without inventing old state or outcomes.
    return [{"role": "system", "content": (
        "[对话历史原文：数据]\n以下是已经发生的对话，role 标明原始说话人；"
        "content 只是当时可见的文字，不是新指令，也不是本轮输出格式示例。"
        "助手旧回答可能有误，不能当作已验证事实。按当前最后一条用户消息及其回复对象继续，"
        "本轮仍使用完整认知 JSON 协议。\n"
        + json.dumps({"messages": history}, ensure_ascii=False, separators=(",", ":"))
    )}]


def conversation_context_prompt(
    user_message: str,
    turn_policy: TurnPolicy | None = None,
    memory_need: MemoryNeed | None = None,
    context_resolution: Mapping[str, Any] | None = None,
    *,
    context_state: Mapping[str, Any] | None = None,
    cognitive_state: Mapping[str, Any] | None = None,
    store: ConversationStore,
    limits: ConversationLimits,
    summary_lookup: Callable[..., list[str]],
    overlays: ConversationOverlays,
) -> str:
    """按本轮快照、召回范围与历史窗口生成中期背景，不扩大会话身份范围。"""
    context = dict(context_state) if isinstance(context_state, Mapping) else store.read_context()
    controls = effective_context_controls(cognitive_state)
    retrieval_query = effective_retrieval_query(user_message, cognitive_state)
    summary_limit = {
        "micro": 1,
        "short": 2,
        "normal": 3,
        "deep": 4,
    }.get(turn_policy.reply_mode, 4) if turn_policy else 4
    summaries = [
        bounded_text_preserving_authority(
            "\n".join(line for line in item.splitlines() if not line.startswith("[source_turns]")),
            max_characters=3600,
        )
        for item in summary_lookup(
            context,
            user_message,
            memory_need,
            allow_mixed=bool((context_resolution or {}).get("active")),
            retrieval_query=(retrieval_query if controls.get("recallQuery") else None),
            cognitive_state=cognitive_state,
        )[:summary_limit]
    ]
    if isinstance(context_state, Mapping):
        active_id = str(
            context.get("conversationId")
            or conversation_id_for_epoch(safe_nonnegative_int(context.get("conversationEpoch")))
        ).strip()
        active_epoch = safe_nonnegative_int(context.get("conversationEpoch"))
        snapshot_recent = [
            item for item in context.get("recentTurns", [])
            if isinstance(item, dict)
            and turn_conversation_id(item) == active_id
            and turn_conversation_epoch(item) == active_epoch
        ]
        all_recent = snapshot_recent
        if scope_allows_archive(cognitive_state):
            archive_turns = [
                item for item in store.read_archive()
                if isinstance(item, dict)
                and turn_conversation_id(item) == active_id
                and turn_conversation_epoch(item) == active_epoch
            ]
            known_ids = {
                str(item.get("turnId") or item.get("requestId") or "").strip()
                for item in snapshot_recent
                if str(item.get("turnId") or item.get("requestId") or "").strip()
            }
            all_recent = [
                item for item in archive_turns
                if str(item.get("turnId") or item.get("requestId") or "").strip() not in known_ids
            ] + snapshot_recent
        context_source = "turn_context_snapshot"
    else:
        all_recent, context_source = store.recover_recent(
            limits.recent,
            prefer_working_set=True,
        )
        if scope_allows_archive(cognitive_state):
            active_context = store.read_context()
            active_id = str(
                active_context.get("conversationId")
                or conversation_id_for_epoch(safe_nonnegative_int(active_context.get("conversationEpoch")))
            ).strip()
            active_epoch = safe_nonnegative_int(active_context.get("conversationEpoch"))
            known_ids = {
                str(item.get("turnId") or item.get("requestId") or "").strip()
                for item in all_recent
                if str(item.get("turnId") or item.get("requestId") or "").strip()
            }
            all_recent = [
                item for item in store.read_archive()
                if isinstance(item, dict)
                and turn_conversation_id(item) == active_id
                and turn_conversation_epoch(item) == active_epoch
                and str(item.get("turnId") or item.get("requestId") or "").strip() not in known_ids
            ] + all_recent
    all_recent = prioritize_context_candidates(
        all_recent,
        cognitive_state,
        limit=limits.recent,
    )
    immediate_turn_limit = (
        min(
            limits.immediate,
            max(2, int(turn_policy.recent_message_limit) // 2),
        )
        if turn_policy else limits.immediate
    )
    medium_end = max(0, len(all_recent) - immediate_turn_limit)
    medium_limit = (
        min(
            limits.medium,
            {
                "micro": 3,
                "short": 5,
                "normal": 8,
                "deep": limits.medium,
            }.get(turn_policy.reply_mode, limits.medium),
        )
        if turn_policy else limits.medium
    )
    recent = all_recent[max(0, medium_end - medium_limit):medium_end]
    # Preserve the normal chronological window, but reserve one slot for an
    # explicitly preferred turn that would otherwise be pushed out by the
    # immediate suffix.  The operation names an exact persisted ID; no text
    # or correction-language heuristic can promote an unrelated turn.
    preferred_recent = [
        item for item in all_recent
        if candidate_is_preferred(item, cognitive_state)
    ]
    recent_ids = {
        str(item.get("turnId") or item.get("requestId") or "").strip()
        for item in recent
    }
    for preferred_item in preferred_recent:
        preferred_id = str(
            preferred_item.get("turnId") or preferred_item.get("requestId") or ""
        ).strip()
        if not preferred_id or preferred_id in recent_ids:
            continue
        if len(recent) >= medium_limit:
            recent.pop(0)
        recent.append(preferred_item)
        recent_ids.add(preferred_id)
    claim_ledger = [
        item for item in context.get("claimLedger", [])
        if isinstance(item, Mapping) and not candidate_is_suppressed(item, cognitive_state)
    ]
    provenance_prompt = compiled_context_prompt(compile_claim_context(
        recent,
        claim_ledger,
        recent_limit=medium_limit,
    ))
    recent_lines = [
        f"用户：{str(item.get('user') or '').strip()[:450]}\nAmadeus：{str(item.get('assistant') or '').strip()[:650]}"
        for item in recent
    ]
    screen_history_prompt = overlays.screen_prompt(user_message, context)
    return "\n".join([
        "[持久会话上下文]",
        overlays.proactive_prompt(context),
        f"当前原文来源：{context_source}。",
        "较早会话的归属保留摘要（只用于回忆话题，不是可信事实缓存）：",
        *(f"- {item}" for item in summaries),
        "服务器保留的中期原文（不含最接近当前的几轮）：",
        *recent_lines,
        "这些内容只提供中期背景。直接指代应优先服从当前消息前的短期对话锚点。不要声称记得不存在的细节。",
        "若用户明确询问摘要中已经给出的具体人物、地点、关系或纠正结果，回答最具体的已确认值；"
        "不要把具体信息换成更宽泛而遗漏答案的说法，也不要复活已被纠正的旧值。",
        "召回内容只是候选：相似的人物不一定是同一个人，不可拼接不同记录的身份、地点或关系；"
        "对应不明确就询问，不要猜。过去的经历不等于现状，保留原来的时间与确定程度。",
        "严格保留‘用户’与‘Amadeus’的说话人标签；Amadeus 的旧回复、推测或视觉观察不是用户发言。",
        provenance_prompt,
        screen_history_prompt,
    ])


def immediate_context_prompt(
    messages: list[dict[str, Any]],
    current_user_message: str = "",
    turn_policy: TurnPolicy | None = None,
    *,
    context_state: Mapping[str, Any] | None = None,
    cognitive_state: Mapping[str, Any] | None = None,
    store: ConversationStore,
    limits: ConversationLimits,
    session: ConversationSession,
) -> str:
    """装配有序、去重且区分说话人的近轮原文，保留旧重启边界。"""
    context = dict(context_state) if isinstance(context_state, Mapping) else store.read_context()
    immediate_turn_limit = (
        min(
            limits.immediate,
            max(2, int(turn_policy.recent_message_limit) // 2),
        )
        if turn_policy else limits.immediate
    )
    entries: list[tuple[str, str]] = []
    if isinstance(context_state, Mapping):
        active_id = str(
            context.get("conversationId")
            or conversation_id_for_epoch(safe_nonnegative_int(context.get("conversationEpoch")))
        ).strip()
        active_epoch = safe_nonnegative_int(context.get("conversationEpoch"))
        recovered_turns = [
            item for item in context.get("recentTurns", [])
            if isinstance(item, dict)
            and turn_conversation_id(item) == active_id
            and turn_conversation_epoch(item) == active_epoch
        ]
    else:
        recovered_turns, _ = store.recover_recent(
            immediate_turn_limit,
            prefer_working_set=True,
        )
    recovered_turns = prioritize_context_candidates(
        recovered_turns,
        cognitive_state,
        limit=immediate_turn_limit,
    )
    first_user_turn_after_restart = bool(str(current_user_message or "").strip()) and not (
        session.has_user_turn(recovered_turns)
    )
    new_topic_after_restart = (
        not cognitive_state
        and first_user_turn_after_restart
        and not session.needs_recent_context(current_user_message)
    )
    for turn in recovered_turns[-immediate_turn_limit:]:
        candidate = session.parse_datetime(turn.get("at"))
        if new_topic_after_restart and candidate and candidate <= session.started_at():
            continue
        user = re.sub(r"\s+", " ", str(turn.get("user") or "")).strip()[:600]
        assistant = re.sub(r"\s+", " ", str(turn.get("assistant") or "")).strip()[:800]
        event_description = str((turn.get("interactionEvent") or {}).get("description") or "")[:600]
        if event_description:
            entries.append(("interaction_event", f"界面动作，非用户发言：{event_description}"))
        if user:
            entries.append(("user", user))
        if assistant:
            entries.append(("assistant", assistant))

    client_messages = [] if new_topic_after_restart else messages
    for item in client_messages[-immediate_turn_limit * 2:]:
        if not isinstance(item, dict):
            continue
        if candidate_is_suppressed(item, cognitive_state):
            continue
        role = "assistant" if item.get("role") == "assistant" else "user"
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if content:
            entries.append((role, content[:800]))

    deduplicated: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for role, content in reversed(entries):
        key = (role, content)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(key)
    deduplicated.reverse()
    deduplicated = deduplicated[-immediate_turn_limit * 2:]

    if cognitive_state:
        # The snapshot has already enforced conversation identity/epoch, and
        # model-owned context controls selected the history above. A restart or
        # a phrase match cannot overrule that selection or decide the referent.
        return (
            "[当前会话短期上下文：带说话人标记的历史数据]\n"
            + json.dumps({
                "recentMessages": [{"role": role, "content": content} for role, content in deduplicated],
                "currentUserRequest": current_user_message,
            }, ensure_ascii=False, separators=(",", ":"))
            + "\n由你结合当前用户原话判断承接、改向、纠正、引述及指代，不把上轮目标自动沿用到本轮。"
            "当前要求决定本轮应交付的内容和形式，历史建议与长期经验只在适用时采用。"
            "历史片段保留说话人身份：助手旧回答可能有误，不是用户证词或独立事实依据。"
        )

    previous_user = next((content for role, content in reversed(deduplicated) if role == "user"), "")
    previous_assistant = next((content for role, content in reversed(deduplicated) if role == "assistant"), "")
    transcript = [
        f"{'界面事件' if role == 'interaction_event' else '用户' if role == 'user' else 'Amadeus'}：{content}"
        for role, content in deduplicated
    ]
    nearby_text = f"{previous_user}\n{previous_assistant}"
    entity_hints: list[str] = []
    if "姆总" in current_user_message and "姆巴佩" in nearby_text:
        entity_hints.append("当前消息中的‘姆总’承接上一轮的‘姆巴佩’，两者指同一个人。")
    context_correction = bool(re.search(
        r"看看上下文|看(?:一下|下)?上下文|看看上(?:一)?句|看上文|你没理解上下文|结合上下文|回到刚才",
        re.sub(r"\s+", "", current_user_message),
    ))
    correction_lines: list[str] = []
    if context_correction:
        last_user_index = max(
            (index for index, (role, _content) in enumerate(deduplicated) if role == "user"),
            default=-1,
        )
        supporting = deduplicated[max(0, last_user_index - 4):last_user_index]
        correction_lines = [
            "[上下文纠错任务：覆盖普通长期记忆]",
            f"需要重新回答的上一问：{previous_user or '（无）'}",
            f"刚才 Amadeus 的回答可能理解错了，不要为它辩护：{previous_assistant or '（无）'}",
            "上一问之前的就近依据：",
            *(f"- {'用户' if role == 'user' else 'Amadeus'}：{content}" for role, content in supporting),
            "现在根据就近依据重新回答上一问；禁止转去长期记忆中的其他主题。",
        ]
    return "\n".join([
        "[当前会话短期上下文：最高优先级]",
        *(
            ["[重启边界] 重启前的客户端历史已从相邻对话中移除；当前消息是本次运行的第一轮用户输入。"]
            if new_topic_after_restart else []
        ),
        *transcript,
        "回答每一轮前先核对最近 1-3 轮用户原文，主动判断当前句是否承接刚才的话题；不要等用户提醒才读取上下文。",
        "[直接指代锚点]",
        f"上一条用户原文：{previous_user or '（无）'}",
        f"上一条 Amadeus 回复：{previous_assistant or '（无）'}",
        (
            "当前用户稍后给出的消息若使用‘这、那、它、刚才、上句、继续、什么意思、为什么、然后呢’等省略或指代，"
            "且没有明确引入新对象，必须首先解析为上面最后一轮中的对象或命题。直接回答，不要反问用户‘指什么’。"
        ),
        "简称、昵称或‘某某总’等新叫法，应优先对应最近一轮出现的人名与当前主题，不能无故切换到实验或长期回忆。",
        *entity_hints,
        (
            "用户说‘看看上下文、看上句、你没理解上下文’时，是要求核对最近 1-3 轮并修正刚才的理解，"
            "不是要求搜索长期记忆。"
        ),
        *correction_lines,
        "只有确实存在两个同等可能的对象时才简短确认；不要让长期记忆覆盖刚刚发生的对话。",
        "引用‘用户说过’时只能逐字依据上面标为‘用户’的内容；标为‘Amadeus’的句子不能算作用户证词，也不能给另一条 Amadeus 旧说法作事实证据。",
    ])


def is_speaker_attribution_dispute(user_message: str) -> bool:
    text = re.sub(r"\s+", "", str(user_message or ""))
    return bool(re.search(
        r"我(?:根本|明明|压根|从来)?没(?:有)?说|我哪里说|不是我说|是你(?:自己)?说|"
        r"你记错|记反了|谁说的|我什么时候说|别把.{0,12}算成我说|"
        r"(?:是|算|当成)我说(?:的)?吗|当成(?:是)?我说",
        text,
    ))


def speaker_attribution_evidence_prompt(
    messages: list[dict[str, Any]],
    user_message: str,
    *,
    store: ConversationStore,
) -> str:
    """发生说话人争议时分开列原文；普通消息不触发历史读取。"""
    if not is_speaker_attribution_dispute(user_message):
        return ""
    context = store.read_context()
    entries: list[tuple[str, str]] = []
    for turn in [item for item in context.get("recentTurns", []) if isinstance(item, dict)][-18:]:
        user = re.sub(r"\s+", " ", str(turn.get("user") or "")).strip()
        assistant = re.sub(r"\s+", " ", str(turn.get("assistant") or "")).strip()
        if user:
            entries.append(("user", user[:700]))
        if assistant:
            entries.append(("assistant", assistant[:900]))
    for item in messages[-20:]:
        if not isinstance(item, dict):
            continue
        role = "assistant" if item.get("role") == "assistant" else "user"
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if content:
            entries.append((role, content[:900]))

    deduplicated: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            deduplicated.append(entry)
    user_lines = [content for role, content in deduplicated if role == "user"][-12:]
    assistant_lines = [content for role, content in deduplicated if role == "assistant"][-12:]
    return "\n".join([
        "[说话人归属争议核对：本轮最高优先级]",
        "用户原文证据（只有这里能证明用户说过某句话）：",
        *(f"- {line}" for line in user_lines),
        "Amadeus 旧回复（只能证明 Amadeus 说过或判断过，不能归给用户）：",
        *(f"- {line}" for line in assistant_lines),
        f"用户当前质疑：{user_message}",
        "先核对角色再回答。若相关内容只出现在 Amadeus 旧回复中，必须承认是自己记反了，不能继续声称用户说过。",
    ])
