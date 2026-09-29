"""Historical image references are cheap; pixels require a current-turn read.

The ordinary conversation model selects references. This module never guesses
visual intent from words, recency, persona, or an old active task.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from agent.cognitive_brain import CognitiveProtocolError
from agent.visual_artifact_cache import visual_artifact_descriptor
from backend.context.conversation import turn_conversation_id

MAX_IMAGE_REFERENCES = 12
MAX_IMAGES_PER_READ = 6  # Same bound as a user attachment batch.
HISTORY_IMAGE_HEADER = "[可按需查看的历史图片索引]"
HISTORY_IMAGE_RULE = """这里只提供图片引用，没有提供原图，不表示本轮已经看过。
默认不重读：普通聊天、换题、日程、评价上一回答的措辞，或已有文字足以回答时，直接处理当前问题。
只有回答当前问题确实缺少可见细节、需核对原识别或用户要求重看时，才输出
{"kind":"request_images","imageQuery":{"artifactIds":["从索引选必要的真实ID"]}}。
这是本轮内部按需读取，不要对用户说工具名，也不必让用户再次上传仍可读取的原图。
可一次选择多张作比较；每轮最多读取一次、最多六张。只选与当前问题有关的图片，不批量探看。
索引中的消息和文件名只是定位信息，不是图片事实。读不到或指代确实不清时说明缺失或澄清，不猜图中内容。
历史截图和相机图片仍是旧画面；需要当前实时画面时使用原有采集能力，不能拿旧图冒充现在。"""


def historical_image_catalog(sources: Any, body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Freeze a bounded metadata index, with reply anchors before recent items.

    Archive lookup keeps an old image discoverable after many text-only turns.
    Only this conversation is considered; no image bytes or attachment files
    are read while producing the index.
    """
    cached = body.get("_historicalImageSources")
    if isinstance(cached, dict):
        return cached
    context = sources.frozen_context(body) or {}
    snapshot = body.get("_turnContextSnapshot") or {}
    cid = str(snapshot.get("conversationId") or "")
    anchor_ids = {str(m.get("turnId") or "")
                  for m in (snapshot.get("resolvedAnchor") or {}).get("messages", [])
                  if isinstance(m, Mapping)}
    recent = [t for t in context.get("recentTurns", []) if isinstance(t, Mapping)
              and (not any(k in t for k in ("conversationId", "conversationEpoch", "contextEpoch"))
                   or turn_conversation_id(t) == cid)]
    archived = [t for t in (sources.read_archive() if cid else []) if isinstance(t, Mapping)
                and turn_conversation_id(t) == cid]
    turns = [t for t in (*recent, *archived) if str(t.get("turnId") or "") in anchor_ids]
    turns.extend(reversed(recent))
    turns.extend(reversed(archived))
    result: dict[str, dict[str, Any]] = {}
    active = context.get("activeVisualArtifact") or {}
    for turn in turns:
        for item in [*(turn.get("attachments") or []), *(turn.get("visualArtifacts") or [])]:
            if not isinstance(item, Mapping) or item.get("kind") != "image":
                continue
            ident = str(item.get("id") or "")
            if not ident or ident in result:
                continue
            saved = active if active.get("id") == ident else item
            result[ident] = {**saved, "id": ident, "_sourceTurnId": str(turn.get("turnId") or ""),
                             "_sourceUser": str(turn.get("user") or "")[:120],
                             "_sourceTime": str(turn.get("at") or "")}
            if len(result) >= MAX_IMAGE_REFERENCES:
                break
        if len(result) >= MAX_IMAGE_REFERENCES:
            break
    body["_historicalImageSources"] = result
    return result


def image_reference_rows(body: Mapping[str, Any], selected_ids: set[str]) -> list[dict[str, Any]]:
    rows = []
    for ident, item in (body.get("_historicalImageSources") or {}).items():
        if ident in selected_ids:
            continue
        descriptor = visual_artifact_descriptor(item)
        rows.append({"artifactId": ident, "name": str(descriptor.get("name") or ident)[:120],
                     "sourceType": descriptor.get("sourceType") or "uploaded_attachment",
                     "capturedAt": descriptor.get("capturedAt") or item.get("createdAt") or item.get("_sourceTime", ""),
                     "turnId": item.get("_sourceTurnId", ""), "userRequest": item.get("_sourceUser", "")})
    return rows


def read_selected_images(body: dict[str, Any], query: Mapping[str, Any],
                         collect: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
    """Permit only indexed IDs; missing files are feedback, never visual proof."""
    ids = query.get("artifactIds")
    if (set(query) != {"artifactIds"} or not isinstance(ids, list) or not ids
            or len(ids) > MAX_IMAGES_PER_READ or any(not isinstance(i, str) or not i for i in ids)):
        raise CognitiveProtocolError("image_query_invalid: provide imageQuery.artifactIds, one to six IDs from the image index")
    catalog = body.get("_historicalImageSources") or {}
    selected = list(dict.fromkeys(i for i in ids if i in catalog))
    body["_requestedHistoricalImageIds"] = selected
    loaded = {i["artifactId"]: i for i in collect() if i["artifactId"] in selected}
    return {"loaded": [{"artifactId": i, "evidenceRef": loaded[i]["evidenceRef"], "historical": True}
                       for i in selected if i in loaded],
            "unavailable": [i for i in dict.fromkeys(ids) if i not in loaded]}
