"""Server-owned context correction and recall controls.

The narrow lexical gate below only decides whether a user is explicitly
controlling context.  It never treats a matched phrase as proof of an entity,
message, artifact, or tool target.  Targets are resolved from stable IDs and
server-owned message text; ambiguity remains ambiguity.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from agent.turn_context import turn_messages


DIRECTIVE_KINDS = {"CORRECT", "RECALL", "BRIDGE", "EXCLUDE"}
DEFAULT_CONSTRAINT_TURNS = 8
MAX_RECALL_CANDIDATES = 20
MAX_RECALLED_MESSAGES = 4


def _compact(text: str) -> str:
    return "".join(str(text or "").split()).strip("，。！？?!；;：:、…")


def _contains_any(text: str, values: Sequence[str]) -> bool:
    return any(value in text for value in values)


def detect_context_directive(
    text: str,
    *,
    has_previous_resolution: bool,
    has_reply_anchor: bool,
    semantic_scores: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Detect only explicit meta-level context control, never its target."""

    compact = _compact(text)
    if not compact:
        return {}
    previous_available = has_previous_resolution or has_reply_anchor
    correction_cue = _contains_any(compact, (
        "你理解错了", "你搞错了", "你看错了", "你说错了", "你弄错了",
        "不是这个", "不是那个", "我不是说这个", "我不是说那个",
        "我说的是", "我指的是", "我指的不是", "别往那边理解",
    ))
    recall_cue = (
        _contains_any(compact, ("你忘了", "我明明说过", "回看前面", "看看前面", "看看前文", "再看前面"))
        and _contains_any(compact, ("前面", "前文", "之前", "说过", "忘了"))
    )
    bridge_cue = (
        _contains_any(compact, ("结合", "联系起来", "一起考虑", "放在一起"))
        and _contains_any(compact, ("前面", "前文", "之前", "刚才", "上次"))
    )
    exclude_cue = (
        _contains_any(compact, ("别往", "不要往", "不要联系", "别联系", "排除"))
        and _contains_any(compact, ("前面", "刚才", "那个", "那张", "那边", "联想", "理解"))
    )
    method = "lexical_fast_path"
    if correction_cue and previous_available:
        kind = "CORRECT"
        confidence = 0.99 if _contains_any(compact, ("你理解错了", "我说的是", "我指的是")) else 0.94
    elif exclude_cue:
        kind, confidence = "EXCLUDE", 0.96
    elif bridge_cue:
        kind, confidence = "BRIDGE", 0.96
    elif recall_cue:
        kind, confidence = "RECALL", 0.95
    else:
        method = "semantic_intent"
        scores = {
            str(kind): float(score)
            for kind, score in (semantic_scores or {}).items()
            if str(kind) in DIRECTIVE_KINDS
        }
        if not scores:
            return {}
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        kind, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        ordinary_score = float((semantic_scores or {}).get("NONE", 0.0))
        if (
            top_score < 0.62
            # RECALL and BRIDGE are intentionally close semantic neighbors;
            # both expand retrieval and neither authorizes an object/action.
            # A strong gap from ordinary speech matters more than forcing an
            # artificial wide margin between those two safe control classes.
            or top_score - second_score < 0.01
            or top_score - ordinary_score < 0.05
            or (kind == "CORRECT" and not previous_available)
        ):
            return {}
        confidence = min(0.97, max(0.90, top_score))
    return {
        "kind": kind,
        "directiveConfidence": confidence,
        "sourceSpan": str(text).strip(),
        "detectionMethod": method,
    }


def _query_text(text: str, kind: str) -> str:
    compact = str(text or "").strip()
    phrases = (
        "你理解错了", "你搞错了", "你看错了", "你说错了", "你弄错了",
        "你忘了", "我明明说过", "回看前面", "再看看前面", "看看前面",
        "看看前文", "结合前面", "结合前文", "结合之前", "结合刚才",
        "我不是说这个", "我不是说那个", "不是这个", "不是那个",
        "我说的是", "我指的是", "别往那边理解", "不要联系刚才",
    )
    for phrase in phrases:
        compact = compact.replace(phrase, " ")
    compact = compact.strip(" ，。！？?!；;：:、…")
    if kind == "EXCLUDE":
        return ""
    return " ".join(compact.split())[:500]


def _ngrams(text: str) -> set[str]:
    value = _compact(text).lower()
    if not value:
        return set()
    if len(value) <= 2:
        return {value}
    return {value[index:index + 2] for index in range(len(value) - 1)}


def _lexical_score(query: str, text: str) -> float:
    normalized_query = _compact(query).lower()
    normalized_text = _compact(text).lower()
    if not normalized_query or not normalized_text:
        return 0.0
    exact = 1.0 if normalized_query in normalized_text else 0.0
    query_terms = _ngrams(normalized_query)
    text_terms = _ngrams(normalized_text)
    overlap = len(query_terms & text_terms) / max(1, len(query_terms))
    return exact * 2.0 + overlap


def _ordered_messages(turns: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for sequence, turn in enumerate(turns, 1):
        if isinstance(turn, Mapping):
            result.extend(turn_messages(turn, sequence))
    return result


def _rank_messages(
    query: str,
    turns: Sequence[Mapping[str, Any]],
    *,
    rejected_ids: set[str],
    semantic_scores: Mapping[str, float] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    if len(_compact(query)) < 2:
        return [], True
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for index, message in enumerate(_ordered_messages(turns)):
        if message["messageId"] in rejected_ids or message["role"] != "user":
            continue
        lexical = _lexical_score(query, message["text"])
        semantic = float((semantic_scores or {}).get(message["messageId"], -1.0))
        score = lexical + max(0.0, semantic - 0.45) * 1.6
        if score > 0:
            scored.append((score, index, message))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    candidates = [dict(item[2]) for item in scored[:MAX_RECALL_CANDIDATES]]
    if not scored:
        return [], True
    top = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    top_message_id = str(scored[0][2].get("messageId") or "")
    top_semantic = float((semantic_scores or {}).get(top_message_id, -1.0))
    unique = (
        (top >= 1.0 or top_semantic >= 0.58)
        and (len(scored) == 1 or top - second >= 0.12)
    )
    # A unique target means exactly one authoritative message.  Lower-ranked
    # candidates remain diagnostic alternatives and must not silently become
    # additional preferred anchors.
    return candidates[:1] if unique else candidates[:2], not unique


def resolve_context_control(
    *,
    user_text: str,
    conversation_id: str,
    turn_id: str,
    turns: Sequence[Mapping[str, Any]],
    previous_resolution_records: Sequence[Mapping[str, Any]],
    active_constraints: Sequence[Mapping[str, Any]],
    reply_anchor: Mapping[str, Any] | None = None,
    semantic_directive_scores: Mapping[str, float] | None = None,
    semantic_message_scores: Mapping[str, float] | None = None,
    semantic_query_text: str = "",
) -> dict[str, Any]:
    active_records = [
        dict(item) for item in previous_resolution_records
        if isinstance(item, Mapping) and item.get("status") == "active"
    ]
    previous = active_records[-1] if active_records else {}
    anchor = dict(reply_anchor) if isinstance(reply_anchor, Mapping) else {}
    parsed = detect_context_directive(
        user_text,
        has_previous_resolution=bool(previous),
        has_reply_anchor=bool(anchor),
        semantic_scores=semantic_directive_scores,
    )
    live_constraints = [
        dict(item) for item in active_constraints
        if isinstance(item, Mapping)
        and item.get("status") == "active"
        and int(item.get("remainingTurns") or item.get("validThroughTurnCount") or 0) > 0
    ]
    rejected_ids = {
        str(value)
        for item in live_constraints
        for value in item.get("rejectMessageIds", [])
        if str(value)
    }
    anchor_ids = {str(value) for value in anchor.get("targetMessageIds", []) if str(value)}
    expired_constraint_ids: list[str] = []
    if anchor_ids & rejected_ids:
        expired_constraint_ids = [
            str(item.get("constraintId") or "") for item in live_constraints
            if anchor_ids & {str(value) for value in item.get("rejectMessageIds", [])}
        ]
        live_constraints = [
            item for item in live_constraints
            if str(item.get("constraintId") or "") not in expired_constraint_ids
        ]
        rejected_ids = {
            str(value)
            for item in live_constraints
            for value in item.get("rejectMessageIds", [])
            if str(value)
        }

    result: dict[str, Any] = {
        "directives": [],
        "activeConstraints": live_constraints,
        "expiredConstraintIds": expired_constraint_ids,
        "rejectedMessageIds": sorted(rejected_ids),
        "preferredMessageIds": sorted(anchor_ids),
        "recalledMessages": [],
        "ambiguous": False,
    }
    if not parsed:
        return result

    kind = parsed["kind"]
    query = (
        str(semantic_query_text or "").strip()[:500]
        if str(parsed.get("detectionMethod") or "") == "semantic_intent"
        and str(semantic_query_text or "").strip()
        else _query_text(user_text, kind)
    )
    resolution_ids: list[str] = []
    previous_selected = {
        str(value) for value in previous.get("selectedMessageIds", previous.get("selectedIds", []))
        if str(value)
    }
    if kind in {"CORRECT", "EXCLUDE"}:
        rejected_ids |= previous_selected
        if previous.get("resolutionId"):
            resolution_ids.append(str(previous["resolutionId"]))

    recalled: list[dict[str, Any]] = []
    ambiguous = False
    if kind in {"RECALL", "BRIDGE", "CORRECT"} and query:
        recalled, ambiguous = _rank_messages(
            query,
            turns,
            rejected_ids=rejected_ids,
            semantic_scores=semantic_message_scores,
        )
        if not ambiguous:
            anchor_ids |= {str(item["messageId"]) for item in recalled}

    directive_id = f"dir_{turn_id}_{kind.lower()}"
    directive = {
        "directiveId": directive_id,
        "conversationId": conversation_id,
        "sourceTurnId": turn_id,
        "kind": kind,
        "sourceSpan": parsed["sourceSpan"],
        "targets": {"resolutionIds": resolution_ids},
        "reject": {"messageIds": sorted(rejected_ids)},
        "prefer": {
            "messageIds": sorted(anchor_ids),
            "descriptors": [query] if query else [],
        },
        "recall": ({"queryText": query, "searchScope": "current_conversation"}
                   if kind in {"RECALL", "BRIDGE"} else None),
        "directiveConfidence": parsed["directiveConfidence"],
        "authority": "explicit_user",
        "status": "staged",
    }
    staged_constraints: list[dict[str, Any]] = []
    if kind == "CORRECT" and rejected_ids:
        staged_constraints.append({
            "constraintId": f"corr_{turn_id}",
            "conversationId": conversation_id,
            "sourceDirectiveId": directive_id,
            "rejectMessageIds": sorted(rejected_ids),
            "preferMessageIds": sorted(anchor_ids),
            "preferredDescriptors": [query] if query else [],
            "authority": "explicit_user_correction",
            "validFromTurnId": turn_id,
            "validThroughTurnCount": DEFAULT_CONSTRAINT_TURNS,
            "remainingTurns": DEFAULT_CONSTRAINT_TURNS,
            "status": "staged",
        })
    result.update({
        "directives": [directive],
        "stagedConstraints": staged_constraints,
        "rejectedResolutionIds": resolution_ids,
        "rejectedMessageIds": sorted(rejected_ids),
        "preferredMessageIds": sorted(anchor_ids),
        "recalledMessages": recalled,
        "ambiguous": ambiguous,
        "recallQuery": query if kind in {"RECALL", "BRIDGE"} else "",
        "detectionMethod": str(parsed.get("detectionMethod") or "lexical_fast_path"),
        "retrievalTrace": {
            "traceId": f"trace_{turn_id}_{kind.lower()}",
            "conversationId": conversation_id,
            "sourceTurnId": turn_id,
            "directiveKind": kind,
            "detectionMethod": str(parsed.get("detectionMethod") or "lexical_fast_path"),
            "queryText": query,
            "consideredMessageIds": [
                str(item.get("messageId") or "")
                for item in recalled
                if str(item.get("messageId") or "")
            ],
            "selectedMessageIds": (
                [
                    str(item.get("messageId") or "")
                    for item in recalled
                    if str(item.get("messageId") or "")
                ]
                if not ambiguous
                else []
            ),
            "rejectedMessageIds": sorted(rejected_ids),
            "ambiguous": ambiguous,
            "status": "staged",
        },
    })
    return result


def apply_control_to_context_state(
    context: Mapping[str, Any],
    control: Mapping[str, Any],
) -> dict[str, Any]:
    value = deepcopy(dict(context))
    rejected = {str(item) for item in control.get("rejectedMessageIds", []) if str(item)}
    if not rejected:
        return value
    filtered_turns: list[dict[str, Any]] = []
    for turn in value.get("recentTurns", []):
        if not isinstance(turn, Mapping):
            continue
        ids = {
            str(turn.get("userMessageId") or ""),
            str(turn.get("assistantMessageId") or ""),
        }
        if ids & rejected:
            continue
        filtered_turns.append(dict(turn))
    value["recentTurns"] = filtered_turns
    return value


def context_control_prompt(control: Mapping[str, Any] | None) -> str:
    if not isinstance(control, Mapping):
        return ""
    directives = [item for item in control.get("directives", []) if isinstance(item, Mapping)]
    constraints = [item for item in control.get("activeConstraints", []) if isinstance(item, Mapping)]
    recalled = [item for item in control.get("recalledMessages", []) if isinstance(item, Mapping)]
    if not directives and not constraints:
        return ""
    lines = ["[Context Control：用户当前纠正/前文提醒，优先于旧助手推断]"]
    for directive in directives[:1]:
        kind = str(directive.get("kind") or "")
        descriptor = "；".join(str(item) for item in (directive.get("prefer") or {}).get("descriptors", []) if str(item))
        if kind == "CORRECT":
            lines.append("用户明确纠正了上一轮的理解；被纠正的旧消息不得继续作为当前对象。")
            lines.append(f"用户给出的正确描述：{descriptor}" if descriptor else "用户没有给出可唯一定位的新对象；请简短询问具体指哪个，不要自动猜另一个。")
        elif kind == "RECALL":
            lines.append(f"用户要求回看更早内容：{descriptor or '目标描述不足'}。")
        elif kind == "BRIDGE":
            lines.append(f"用户要求把当前问题与更早内容结合：{descriptor or '目标描述不足'}。")
        elif kind == "EXCLUDE":
            lines.append("用户明确要求排除上一轮相关对象；不要因时间接近或旧助手回复把它带回来。")
    if recalled and not control.get("ambiguous"):
        lines.append("服务端找回的用户原文：")
        lines.extend(f"- 用户 [{item.get('messageId')}]: {str(item.get('text') or '')[:700]}" for item in recalled[:MAX_RECALLED_MESSAGES])
    elif directives and control.get("ambiguous"):
        lines.append("候选不唯一；不得声称已经回想起具体哪一件事。必要时请用户说明或使用引用回复。")
    if constraints and not directives:
        lines.append("此前用户纠正仍在短期有效：不要重新绑定到已被用户否定的旧消息。")
    lines.append("旧助手说法只表示当时的回复，不是高于用户本轮纠正的事实。")
    return "\n".join(lines)


def commit_context_control(
    context: Mapping[str, Any],
    control: Mapping[str, Any] | None,
    *,
    resolution_record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply staged control only after the surrounding turn has succeeded."""

    value = deepcopy(dict(context))
    control = control if isinstance(control, Mapping) else {}
    expired = {str(item) for item in control.get("expiredConstraintIds", []) if str(item)}
    constraints: list[dict[str, Any]] = []
    for item in value.get("correctionConstraints", []):
        if not isinstance(item, Mapping):
            continue
        current = dict(item)
        constraint_id = str(current.get("constraintId") or "")
        remaining = max(0, int(current.get("remainingTurns") or 0) - 1)
        current["remainingTurns"] = remaining
        if constraint_id in expired or remaining <= 0:
            current["status"] = "expired"
        constraints.append(current)
    for item in control.get("stagedConstraints", []):
        if isinstance(item, Mapping):
            current = dict(item)
            current["status"] = "active"
            constraints.append(current)
    value["correctionConstraints"] = constraints[-24:]

    records = [dict(item) for item in value.get("resolutionRecords", []) if isinstance(item, Mapping)]
    rejected_records = {str(item) for item in control.get("rejectedResolutionIds", []) if str(item)}
    for item in records:
        if str(item.get("resolutionId") or "") in rejected_records:
            item["status"] = "rejected_by_user"
            item["supersededBy"] = str((resolution_record or {}).get("resolutionId") or "") or None
    if isinstance(resolution_record, Mapping) and resolution_record:
        records.append(dict(resolution_record))
    value["resolutionRecords"] = records[-80:]

    directives = [dict(item) for item in value.get("contextDirectives", []) if isinstance(item, Mapping)]
    for item in control.get("directives", []):
        if isinstance(item, Mapping):
            current = dict(item)
            current["status"] = "active"
            directives.append(current)
    value["contextDirectives"] = directives[-40:]
    if control.get("recallQuery"):
        value["recallHints"] = [
            *[dict(item) for item in value.get("recallHints", []) if isinstance(item, Mapping)][-11:],
            {
                "hintId": f"hint_{str((resolution_record or {}).get('sourceTurnId') or '')}",
                "queryText": str(control.get("recallQuery") or "")[:500],
                "selectedMessageIds": [str(item.get("messageId") or "") for item in control.get("recalledMessages", []) if isinstance(item, Mapping)],
                "ambiguous": bool(control.get("ambiguous")),
                "expiresAfterTurn": True,
            },
        ]
    trace = control.get("retrievalTrace")
    if isinstance(trace, Mapping) and trace:
        current_trace = dict(trace)
        current_trace["status"] = "committed"
        value["retrievalTraces"] = [
            *[
                dict(item)
                for item in value.get("retrievalTraces", [])
                if isinstance(item, Mapping)
            ][-39:],
            current_trace,
        ]
    return value
