"""Lossless reduction of derived views; durable conversation is never edited."""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

SHORT_CONTEXT_MARKER = "[当前会话短期上下文：带说话人标记的历史数据]\n"
HISTORY_RECORDS_MARKER = "[history_records:v1]\n"
HISTORY_COVERAGE_MARKER = "[history_coverage] "


@dataclass
class HistoryView:
    index: int
    prefix: str
    suffix: str
    packet: dict
    key: str
    priority: int

    def rows(self) -> list[dict]:
        sources = self.packet.get("messageSources") or []
        result = []
        for index, row in enumerate(self.packet[self.key]):
            if not isinstance(row, dict):
                continue
            metadata = sources[index] if index < len(sources) and isinstance(sources[index], dict) else {}
            scope = {"conversationId": self.packet["conversationId"]} if self.packet.get("conversationId") else {}
            result.append({**scope, **metadata, **row})
        return result

    def write(self, messages: list[dict], rows: list[dict]) -> None:
        self.packet[self.key] = [{k: v for k, v in row.items() if not (
            k == "conversationId" and v == self.packet.get("conversationId"))} for row in rows]
        self.packet.pop("messageSources", None)
        messages[self.index]["content"] = self.prefix + json.dumps(
            self.packet, ensure_ascii=False, separators=(",", ":")
        ) + self.suffix


def history_views(messages: Sequence[Mapping[str, Any]]) -> list[HistoryView]:
    """Read only backend-owned history protocols, never JSON in user/tool text."""
    result = []
    for index, message in enumerate(messages):
        text = message.get("content")
        if message.get("role") != "system" or not isinstance(text, str):
            continue
        if text.startswith("[对话历史原文：数据]\n"):
            start, key, priority = text.find('\n{"messages":'), "messages", 0
            start += 1 if start >= 0 else 0
        elif text.startswith(SHORT_CONTEXT_MARKER):
            start, key, priority = len(SHORT_CONTEXT_MARKER), "recentMessages", 2
        elif text.startswith(("[持久会话上下文]", "[QQ 子对话的")) and HISTORY_RECORDS_MARKER in text:
            start = text.index(HISTORY_RECORDS_MARKER) + len(HISTORY_RECORDS_MARKER)
            key, priority = "messages", 1
        else:
            continue
        if start < 0:
            continue
        try:
            packet, end = json.JSONDecoder().raw_decode(text[start:])
        except (ValueError, TypeError):
            continue
        if not isinstance(packet, dict) or not isinstance(packet.get(key), list):
            continue
        # Invalid records are left untouched rather than silently discarded.
        if any(not isinstance(row, dict) or "role" not in row or "content" not in row for row in packet[key]):
            continue
        result.append(HistoryView(index, text[:start], text[start + end:], packet, key, priority))
    return result


def history_identity(row: Mapping[str, Any]) -> dict:
    return {**{key: row[key] for key in ("conversationId", "messageId", "turnId", "role") if row.get(key)},
            "contentHash": sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()}


def _covered(row: Mapping, coverage: Mapping) -> bool:
    if str(row.get("conversationId") or "") != str(coverage.get("conversationId") or ""):
        return False
    if coverage.get("role") and row.get("role") != coverage["role"]:
        return False
    if coverage.get("contentHash"):
        if not coverage.get("messageId") and not coverage.get("turnId"):
            # Repeated words without a stable source ID may be different turns.
            return False
        return history_identity(row)["contentHash"] == coverage["contentHash"] and (
            not coverage.get("messageId") or row.get("messageId") == coverage["messageId"]) and (
            not coverage.get("turnId") or row.get("turnId") == coverage["turnId"])
    return bool((coverage.get("turnId") and row.get("turnId") == coverage["turnId"])
                or (coverage.get("messageId") and row.get("messageId") == coverage["messageId"]))


def _same_history(row: Mapping, prior: Mapping, *, excerpt: bool = False) -> bool:
    if row.get("role") != prior.get("role"):
        return False
    left_scope, right_scope = row.get("conversationId"), prior.get("conversationId")
    if left_scope and right_scope and left_scope != right_scope:
        return False
    for key in ("messageId", "turnId"):
        if row.get(key) and prior.get(key) and row[key] != prior[key]:
            return False
    # regex-purpose: projection
    left = re.sub(r"\s+", " ", str(row.get("content") or "")).strip()
    # regex-purpose: projection
    right = re.sub(r"\s+", " ", str(prior.get("content") or "")).strip()
    return bool(left and (left == right or (excerpt and right.startswith(left))))


def deduplicate_history_views(messages: list[dict]) -> list[dict]:
    views = history_views(messages)
    coverage = [row for view in views for row in view.packet.get("summaryCoverage", []) if isinstance(row, dict)]
    for message in messages:
        text = str(message.get("content") or "")
        if message.get("role") == "system" and text.startswith("[compaction:prompt_history v1]"):
            for line in text.splitlines():
                if line.startswith(HISTORY_COVERAGE_MARKER):
                    try:
                        coverage.extend(json.loads(line[len(HISTORY_COVERAGE_MARKER):]))
                    except (ValueError, TypeError):
                        pass
    claimed: list[dict] = []
    outputs = []
    for view in sorted(views, key=lambda item: (item.priority, item.index)):
        kept = []
        used = set()
        for row in view.rows():
            pinned = bool(row.get("replyAnchor") or row.get("pinned") or row.get("protected"))
            if not pinned and not view.packet.get("explicitRecall") and any(_covered(row, item) for item in coverage):
                continue
            match = next((i for i, prior in enumerate(claimed) if i not in used and
                          _same_history(row, prior, excerpt=view.priority == 2 or bool(row.get("excerpt")))), None)
            if match is not None and not pinned:
                used.add(match)
                # Keep provenance from the stored copy when the client omitted it.
                for key in ("conversationId", "messageId", "turnId"):
                    if row.get(key) and not claimed[match].get(key):
                        claimed[match][key] = row[key]
                continue
            kept.append(row)
        # A repeated utterance within ONE history is still a real separate turn.
        claimed.extend(kept)
        outputs.append((view, kept))
    for view, kept in outputs:
        if kept != view.rows():
            view.write(messages, kept)
    return messages


def deduplicate_prompt_views(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = [deepcopy(dict(message)) for message in messages]
    # Only actual conversational messages cover a derived quoted history.
    # System instructions and tool output cannot impersonate a user turn.
    history: dict[str, list[dict]] = {"user": [], "assistant": []}
    for message in result:
        content = message.get("content")
        role = message.get("role")
        if not isinstance(content, str):
            continue
        if role in history:
            history[role].append(message)
            continue
        if role != "system" or not content.startswith(SHORT_CONTEXT_MARKER):
            continue
        encoded = content[len(SHORT_CONTEXT_MARKER):]
        try:
            packet, end = json.JSONDecoder().raw_decode(encoded)
        except (ValueError, TypeError):
            continue
        if not isinstance(packet, dict) or not isinstance(packet.get("recentMessages"), list):
            continue
        kept = []
        kept_sources = []
        sources = packet.get("messageSources") or []
        for index, entry in enumerate(packet["recentMessages"]):
            source = sources[index] if index < len(sources) else {}
            if not isinstance(entry, dict):
                kept.append(entry)
                kept_sources.append(source)
                continue
            row = {"conversationId": packet.get("conversationId", ""), **source, **entry}
            if not row.get("protected") and not row.get("replyAnchor") and any(
                    _same_history(row, prior, excerpt=True) for prior in history.get(entry.get("role"), [])):
                continue
            kept.append(entry)
            kept_sources.append(source)
        if len(kept) != len(packet["recentMessages"]):
            packet["recentMessages"] = kept
            if "messageSources" in packet:
                packet["messageSources"] = kept_sources
            message["content"] = SHORT_CONTEXT_MARKER + json.dumps(
                packet, ensure_ascii=False, separators=(",", ":")
            ) + encoded[end:]
    return deduplicate_history_views(result)
