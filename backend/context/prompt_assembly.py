"""Explicit prompt ownership and content-free, per-request accounting.

Only application-owned modules with the same ID and version may replace one
another. User messages, tool receipts, attachments and unlabelled instructions
are never deduplicated by their wording.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from agent.context_pressure import estimate_tokens

MODULE_KEY = "_amadeusPromptModule"


def system_message(module_id: str, content: str, *, source: str,
                   version: str = "1", reason: str = "entry", category: str = "engineering") -> dict[str, Any]:
    return {"role": "system", "content": content, MODULE_KEY: {
        "id": module_id, "version": version, "source": source,
        "reason": reason, "category": category,
    }}


def assemble_modules(messages: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """The latest projection of a declared module owns its slot in this request."""
    latest: dict[tuple[str, str], int] = {}
    for index, message in enumerate(messages):
        module = message.get(MODULE_KEY)
        if message.get("role") == "system" and isinstance(module, Mapping) and module.get("id"):
            latest[(str(module["id"]), str(module.get("version", "1")))] = index
    active, removed = [], []
    for index, message in enumerate(messages):
        module = message.get(MODULE_KEY)
        if message.get("role") == "system" and isinstance(module, Mapping) and module.get("id"):
            key = (str(module["id"]), str(module.get("version", "1")))
            if latest[key] != index:
                removed.append({"id": key[0], "version": key[1], "source": str(module.get("source", ""))})
                continue
        active.append(dict(message))
    return active, removed


# Modules whose text is the same on every request under the same settings,
# most stable first. Providers bill a repeated prompt prefix at a fraction of
# the normal input price, and only an exact leading match counts, so these go
# ahead of everything that changes per turn. The order is fixed here rather
# than taken from each builder so the fast, tool and follow-up requests of one
# turn all share the same prefix. The wire-format rule and the cognitive
# protocol deliberately stay next to the user message and are not listed.
STABLE_MODULE_ORDER = (
    "reply.engineering", "reply.persona", "persona.core", "reply.persona.skills",
    "reply.expression", "persona.style", "persona.emotion", "persona.facts",
    "dialogue.speaker", "engineering.accuracy", "schedule.intent",
    "relationship.context", "cognitive.protocol",
    # Fixed for a given settings state, but worded slightly differently by the
    # fast and tool builders, so it comes after everything they share.
    "dignity.boundary",
    # Identical between chat turns; a tool follow-up adds its per-turn fields
    # at the end of the block, so the catalogue itself still matches.
    "capability.current",
)
_STABLE_RANK = {module_id: rank for rank, module_id in enumerate(STABLE_MODULE_ORDER)}

# The output-format rule and the identity rule are fixed text too, but they
# were placed next to the user message on purpose. They join the cached prefix
# only in the explicitly selected "wire" mode, which exists to measure whether
# moving them changes how reliably the model keeps the output format.
WIRE_MODULE_ORDER = ("wire.reply_rule", "wire.participant_identity")

# Modules that are mostly fixed but carry a value that changes every turn.
# The permission block states the turn's clock time when the time permission
# is on; anything placed after it can never be a cached prefix, so it closes
# the stable section (it used to sit in the middle and cut the cache short).
VOLATILE_TAIL_ORDER = ("permission.context",)


# Modules that are the same for every model call of one turn: the fast stream,
# the request that follows a tool and any further tool round. They change from
# turn to turn, so they cannot join the session prefix, but placed directly
# after it (and before anything a single call adds) the follow-up request
# repeats the first call's prefix up to the end of this section and only the
# call's own blocks are new input. The order is the fast builder's own order.
# The turn part of the cognitive packet comes late: it is the largest block
# and the most likely to differ, and a difference only costs what follows it.
# The session time base is last of all because it states the running clock to
# the minute: when a minute passes between two calls of a turn (seen in about
# one tool turn in eight) only this short block is new input.
TURN_STABLE_ORDER = (
    "reply.persona.continuity", "turn.style", "context.control", "context.reply",
    "memory.retrieved", "conversation.context",
    "dialogue.attribution", "conversation.history", "identity.grounding",
    "persona.nickname", "persona.motif", "conversation.immediate",
    "dialogue.pending_question", "context.reference", "cognitive.packet.turn",
    "session.time_base",
)

# The calls of one chat turn that are built to share a prefix. Repair,
# summary, vision and background requests keep the previous layout.
TURN_PREFIX_PURPOSES = frozenset({
    "chat_fast_utterance_stream", "chat_primary", "chat_tool_followup",
})


def turn_prefix_stage(mode: str, purpose: str) -> int:
    """How much of the turn-prefix layout applies: 0 none, 1 order, 2 block splits, 3 packet split.

    ``mode`` is AMADEUS_PROMPT_STABLE_PREFIX. Empty (the default) and "turn"
    select everything; "turn1" and "turn2" stop after the first or second
    step so each can be compared on its own; every older value ("wire",
    "split", "order", "off") keeps the layout those values had before.
    """

    if purpose not in TURN_PREFIX_PURPOSES and not purpose.startswith("chat_tool_query_loop_round_"):
        return 0
    return {"": 3, "turn": 3, "turn2": 2, "turn1": 1}.get(mode, 0)


HISTORY_BLOCK_HEADER = "[对话历史原文：数据]\n"


def label_history_block(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Name the backend's dialogue-history block so it can be ordered with the turn.

    The block is produced without a module id by the conversation store. Only
    an unlabelled system message that starts with the store's own header is
    named; its text is not changed.
    """

    result = []
    for message in messages:
        content = message.get("content")
        if (message.get("role") == "system" and MODULE_KEY not in message
                and isinstance(content, str) and content.startswith(HISTORY_BLOCK_HEADER)):
            result.append({**dict(message), MODULE_KEY: {
                "id": "conversation.history", "version": "1", "source": "backend/context/conversation.py",
                "reason": "turn_stable_history", "category": "mixed"}})
        else:
            result.append(dict(message))
    return result


def cache_stable_order(messages: Sequence[Mapping[str, Any]], *,
                       include_wire_rules: bool = False,
                       turn_stable: bool = False) -> list[dict[str, Any]]:
    """Move the stable modules to the front; keep everything else as it was.

    Only declared system modules move, and no message is added, dropped or
    edited. Messages that are not listed keep their relative order, so the
    blocks that were placed last on purpose are still last. With
    ``turn_stable`` the modules of TURN_STABLE_ORDER follow the stable section.
    """

    stable: list[tuple[int, int, dict[str, Any]]] = []
    rest: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        module = message.get(MODULE_KEY)
        rank = _STABLE_RANK.get(str(module.get("id"))) if (
            message.get("role") == "system" and isinstance(module, Mapping)) else None
        module_id = str(module.get("id")) if (
            message.get("role") == "system" and isinstance(module, Mapping)) else ""
        if rank is None and include_wire_rules and module_id in WIRE_MODULE_ORDER:
            rank = len(STABLE_MODULE_ORDER) + WIRE_MODULE_ORDER.index(module_id)
        if rank is None and module_id in VOLATILE_TAIL_ORDER:
            rank = (len(STABLE_MODULE_ORDER) + len(WIRE_MODULE_ORDER)
                    + VOLATILE_TAIL_ORDER.index(module_id))
        if rank is None and turn_stable and module_id in TURN_STABLE_ORDER:
            rank = (len(STABLE_MODULE_ORDER) + len(WIRE_MODULE_ORDER) + len(VOLATILE_TAIL_ORDER)
                    + TURN_STABLE_ORDER.index(module_id))
        if rank is None:
            rest.append(dict(message))
        else:
            stable.append((rank, index, dict(message)))
    stable.sort(key=lambda item: (item[0], item[1]))
    return [message for _, _, message in stable] + rest


def dedupe_repeated_text(
    messages: Sequence[Mapping[str, Any]], texts: Sequence[str], *, pointer: str,
) -> list[dict[str, Any]]:
    """Keep one copy of an instruction that several builders each embed verbatim.

    Only exact, complete copies of the given application-owned texts are
    affected; the copy closest to the user message stays and earlier ones are
    replaced by ``pointer``. Nothing is paraphrased and user or tool content is
    never matched against anything but these constants.
    """

    result = [dict(message) for message in messages]
    for text in texts:
        if not text:
            continue
        holders = [index for index, message in enumerate(result)
                   if message.get("role") == "system" and isinstance(message.get("content"), str)
                   and text in message["content"]]
        for index in holders[:-1]:
            result[index]["content"] = result[index]["content"].replace(text, pointer)
    return result


TOOL_EVIDENCE_HEADER = "[已执行工具的证据"
TOOL_EVIDENCE_LIST_MARKER = "evidenceRefs 使用下列稳定 id，不使用跨批次重复的 S1/S2 编号。\n"
COGNITIVE_PACKET_HEADER = "[Cognitive packet"
TOOL_EVIDENCE_POINTER = "回执与正文在后文 Cognitive packet 的 evidence 中相同 id 的条目里完整给出，此处不重复。"


def dedupe_tool_evidence(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Send a tool result once when the same request already carries it twice.

    After a tool runs, the follow-up request held the result both in its own
    evidence block and, entry for entry, inside the cognitive packet that the
    model cites from. An entry of the evidence block is replaced by a pointer
    only when the packet holds the same id with exactly the same content once
    empty fields are ignored; anything that differs, or that fails to parse,
    is left as it was. The packet copy, which claims refer to, is never
    touched.
    """

    result = [dict(message) for message in messages]
    packet_entries: dict[str, Any] = {}
    for message in result:
        content = message.get("content")
        if not (message.get("role") == "system" and isinstance(content, str)):
            continue
        start = content.find(COGNITIVE_PACKET_HEADER)
        if start < 0:
            continue
        line_start = content.find("\n", start) + 1
        line_end = content.find("\n", line_start)
        try:
            packet = json.loads(content[line_start:line_end if line_end > 0 else None])
        except (TypeError, ValueError):
            continue
        for entry in (packet.get("evidence") or []) if isinstance(packet, Mapping) else []:
            if isinstance(entry, Mapping) and entry.get("id"):
                packet_entries[str(entry["id"])] = entry
    if not packet_entries:
        return result
    from agent.cognitive_brain import without_empty_fields

    for message in result:
        content = message.get("content")
        if not (message.get("role") == "system" and isinstance(content, str)
                and content.startswith(TOOL_EVIDENCE_HEADER) and TOOL_EVIDENCE_LIST_MARKER in content):
            continue
        head, _, raw = content.rpartition(TOOL_EVIDENCE_LIST_MARKER)
        try:
            entries = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        replaced, changed = [], False
        for entry in entries:
            twin = packet_entries.get(str(entry.get("id"))) if isinstance(entry, Mapping) else None
            if twin is not None and without_empty_fields(entry) == without_empty_fields(twin):
                replaced.append({"id": entry["id"], "sameAs": TOOL_EVIDENCE_POINTER})
                changed = True
            else:
                replaced.append(entry)
        if changed:
            message["content"] = head + TOOL_EVIDENCE_LIST_MARKER + json.dumps(replaced, ensure_ascii=False)
    return result


COGNITIVE_PROTOCOL_HEADER = "[认知与任务修正协议]"


def split_static_protocol(
    messages: Sequence[Mapping[str, Any]], *, container_marker: str,
) -> list[dict[str, Any]]:
    """Send the fixed head of the cognitive block as its own stable module.

    The block is one string: fixed protocol text, then the per-turn packet.
    The fixed head was about a third of every request and could never be a
    cached prefix because it travelled with the packet. Here the text before
    ``container_marker`` becomes a separate module; every character is kept,
    the packet and the container-format reminder stay where they were. Runs on
    the outgoing copy only, after all replacement of the block by prefix has
    happened.

    The saved-task decision rules that follow the task snapshot are fixed
    text too, but they stay next to the snapshot on purpose: when they were
    sent ahead as a stable module the model stopped reviewing the tasks and
    answered "I removed it" without calling the tool (BUG-077).
    """

    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not (message.get("role") == "system" and isinstance(content, str)
                and content.startswith(COGNITIVE_PROTOCOL_HEADER)):
            result.append(dict(message))
            continue
        module = dict(message.get(MODULE_KEY) or {"id": "cognitive.current", "version": "1", "source": "cognitive"})
        head, rest = "", content
        cut = content.find("\n" + container_marker) if container_marker else -1
        if cut > 0:
            head, rest = content[:cut], content[cut + 1:]
        if head:
            result.append({**dict(message), "content": head,
                           MODULE_KEY: {**module, "id": "cognitive.protocol", "reason": "static_protocol"}})
        result.append({**dict(message), "content": rest})
    return result


CAPABILITY_HEADER = "[统一工具选择前置]\n"
CAPABILITY_CALL_KEYS = ("requiredTools", "candidateHints", "toolDefinitions", "currentStateEvidenceTool")
CAPABILITY_CALL_HEADER = "[统一工具选择前置：本次调用的字段]\n"
CAPABILITY_CALL_NOTE = "\n以上字段属于前文 [统一工具选择前置] 的同一份说明，含义与规则见该处。"


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def split_capability_block(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Send the tool catalogue and the fields of this call as two modules.

    The catalogue (permitted tools, index, examples, rules) depends on the
    settings only. The required tools, hints, loaded definitions and the
    current-state tool belong to one call; inside the same JSON object they
    made the block differ between the first call and its follow-up from the
    middle on. Every key and value is kept, the rule text stays with the
    catalogue. A block that is not the compact form, or does not serialize
    back to exactly what was sent, is left as it is.
    """

    result: list[dict[str, Any]] = []
    for message in messages:
        module = message.get(MODULE_KEY)
        content = message.get("content")
        if not (message.get("role") == "system" and isinstance(module, Mapping)
                and module.get("id") == "capability.current" and isinstance(content, str)
                and content.startswith(CAPABILITY_HEADER)):
            result.append(dict(message))
            continue
        raw, separator, rules = content[len(CAPABILITY_HEADER):].partition("\n")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        if not (isinstance(payload, dict) and "selectionOwner" in payload and separator
                and _compact_json(payload) == raw
                and any(key in payload for key in CAPABILITY_CALL_KEYS)):
            result.append(dict(message))
            continue
        catalogue = {key: value for key, value in payload.items() if key not in CAPABILITY_CALL_KEYS}
        call = {key: payload[key] for key in CAPABILITY_CALL_KEYS if key in payload}
        result.append({**dict(message),
                       "content": CAPABILITY_HEADER + _compact_json(catalogue) + "\n" + rules})
        result.append({**dict(message),
                       "content": CAPABILITY_CALL_HEADER + _compact_json(call) + CAPABILITY_CALL_NOTE,
                       MODULE_KEY: {**dict(module), "id": "capability.call", "reason": "per_call_fields"}})
    return result


# The last line of the dignity block says who reports the verdict. It is the
# only part that differs between the first call (the model reports it) and the
# request after a tool (the backend's own verdict is used).
DIGNITY_OUTPUT_LINE_STARTS = ("本轮属于 ordinary_conflict", "本轮边界判断由后端")


def split_dignity_output(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Send the reporting line of the dignity block as its own module.

    The rule, the counters and the verdict stay where they were, word for
    word; only the final line moves to the place of a per-call block. Blocks
    that do not end in one of the two known lines are left as they are.
    """

    result: list[dict[str, Any]] = []
    for message in messages:
        module = message.get(MODULE_KEY)
        content = message.get("content")
        head, separator, tail = content.rpartition("\n") if isinstance(content, str) else ("", "", "")
        if not (message.get("role") == "system" and isinstance(module, Mapping)
                and module.get("id") == "dignity.boundary" and separator and head
                and tail.startswith(DIGNITY_OUTPUT_LINE_STARTS)):
            result.append(dict(message))
            continue
        result.append({**dict(message), "content": head})
        result.append({**dict(message), "content": tail,
                       MODULE_KEY: {**dict(module), "id": "dignity.boundary.output", "reason": "per_call_fields"}})
    return result


COGNITIVE_PACKET_LINE = "[Cognitive packet: 后端状态和引用数据]\n"
# Fields of the packet that one call can change: the assistant's own task and
# plan, what this call's tools returned, and requirements derived from them.
COGNITIVE_CALL_KEYS = ("task", "successCriteriaCatalog", "pendingAction",
                       "currentStateEvidenceRequirement", "currentToolReceipts")
COGNITIVE_TURN_NOTE = ("\n这是 packet 中本轮各次调用相同的部分。task、currentToolReceipts 和本次调用新取得的 evidence 条目"
                       "在后文 [Cognitive packet 续] 中给出；两处合起来是同一个 packet，引用方式不变。")
COGNITIVE_CALL_LINE = ("[Cognitive packet 续: 本次调用的任务状态与工具回执；与前文 Cognitive packet 合为同一个 packet，"
                       "其中 evidence 是追加在前文 evidence 之后的条目]\n")


def _evidence_of_this_call(entry: Any) -> bool:
    return (isinstance(entry, Mapping) and not entry.get("historical")
            and entry.get("sourceType") != "user_message")


def split_cognitive_packet(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Send the part of the cognitive packet that a tool call cannot change ahead of the rest.

    The packet is a snapshot rebuilt for every call. After a tool ran, the
    task near its start and the new receipt inside its evidence list changed,
    so the whole snapshot was new input although most of it (ids, the user's
    request, earlier turns' evidence and history, the time anchor) was
    identical. Here those fields become a module of their own, in their
    original order; the task, this call's receipts and the evidence entries
    obtained in this call stay in the block next to the closing contract.
    No key, entry or value is dropped or rewritten. A packet that cannot be
    read back to exactly the text that was built is left whole.
    """

    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        module = message.get(MODULE_KEY)
        start = content.find(COGNITIVE_PACKET_LINE) if (
            message.get("role") == "system" and isinstance(module, Mapping)
            and module.get("id") in {"cognitive.current", "cognitive.fast"}
            and isinstance(content, str)) else -1
        if start < 0 or (start > 0 and content[start - 1] != "\n"):
            result.append(dict(message))
            continue
        line_start = start + len(COGNITIVE_PACKET_LINE)
        line_end = content.find("\n", line_start)
        raw = content[line_start:line_end if line_end >= 0 else None]
        try:
            packet = json.loads(raw)
        except (TypeError, ValueError):
            packet = None
        if not (isinstance(packet, dict) and _compact_json(packet) == raw):
            result.append(dict(message))
            continue
        turn_part: dict[str, Any] = {}
        call_part: dict[str, Any] = {}
        for key, value in packet.items():
            if key in COGNITIVE_CALL_KEYS:
                call_part[key] = value
            elif key == "evidence" and isinstance(value, list):
                kept = [entry for entry in value if not _evidence_of_this_call(entry)]
                added = [entry for entry in value if _evidence_of_this_call(entry)]
                if kept or not added:
                    turn_part[key] = kept
                if added:
                    call_part[key] = added
            else:
                turn_part[key] = value
        module = dict(message.get(MODULE_KEY) or {"id": "cognitive.current", "version": "1", "source": "cognitive"})
        result.append({**dict(message),
                       "content": COGNITIVE_PACKET_LINE + _compact_json(turn_part) + COGNITIVE_TURN_NOTE,
                       MODULE_KEY: {**module, "id": "cognitive.packet.turn", "reason": "turn_stable_packet"}})
        result.append({**dict(message),
                       "content": content[:start] + COGNITIVE_CALL_LINE + _compact_json(call_part)
                                  + (content[line_end:] if line_end >= 0 else "")})
    return result


def prompt_manifest(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Never log text, names, URLs, account IDs, credentials or tool arguments."""
    blocks = []
    for index, message in enumerate(messages):
        content = message.get("content", "")
        serialized = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        role = str(message.get("role") or "")
        module = message.get(MODULE_KEY)
        metadata = dict(module) if isinstance(module, Mapping) else {}
        blocks.append({
            "index": index, "role": role,
            "id": metadata.get("id", f"unlabelled:{role}:{index}"),
            "version": metadata.get("version"), "source": metadata.get("source", "unlabelled"),
            "reason": metadata.get("reason", "existing_message"),
            "category": metadata.get("category", "unclassified"),
            "characters": len(serialized), "estimatedTokens": estimate_tokens(content),
            "sha256": sha256(serialized.encode("utf-8")).hexdigest(),
        })
    return {"protocolVersion": "semantic-v3", "tokenMeasurement": "estimate", "blocks": blocks,
            "estimatedContentTokens": sum(block["estimatedTokens"] for block in blocks)}


def wire_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in message.items() if key != MODULE_KEY} for message in messages]
