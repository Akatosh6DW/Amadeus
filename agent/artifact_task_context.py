"""Task-local attachment continuity, independent of persona and long memory."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from agent.visual_artifact_cache import visual_artifact_descriptor


TASK_CONTEXT_RULE = """你只理解当前用户要完成的任务及所指对象，不回答问题，不扮演人格。
先结合最近一轮用户要求和助手追问理解当前消息，再判断完成这个任务是否需要候选附件。
候选附件只是可用材料，不代表当前任务；谈同一个人物、应用或主题，不等于仍在处理那张图。
用户已从图像讨论转到人物本身、一般知识或其它任务时，后续简短回应优先承接该新任务，不能被旧附件拉回。
结合附件所属的用户任务和随后几轮对话理解省略、纠正、追问与换题。
要求重新看、检查尚未解决的识别差异、继续分析/修改同一文件时，contextSource 选 artifact。
用户否定识别结果时，原来针对附件的任务并没有消失：继续选 artifact，并保留原任务目标及新纠正，不能改成仅道歉或让用户重发。
artifact 只表示继续使用这份材料，不代表一定重新识图；是否重看只由 inspectionQuestion 决定。
不能要求用户每句话重复文件名；用户否定助手答案时，助手先前的说法是待纠正内容，不是正确事实。
完整的新话题、另一个对象、单纯感谢或聊天、不要求继续处理附件，contextSource 选 dialogue。
讨论助手刚才的措辞、是否答非所问、语气或处理耗时，不等于要求重看图片；若不需要图片内容，选 dialogue。
只从 candidates 选择资料；候选可能是上传附件、已保存的屏幕截图或摄像头画面。
需要图片但没有明确要求新画面时，默认使用候选中的已有画面；不能因为它来自屏幕或摄像头就重新采集。
明确要求采集现在的屏幕时，contextSource 选 new_screen；检查刚才那张图则选 artifact。
只要用户当前要求必须观察用户本人或其身边的实时物理画面才能完成，就选 new_camera；不要求用户必须说出“摄像头”。
例如让你现在看看本人、评价当前长相/表情/气色/穿着/姿势、辨认手里拿的东西或查看身后环境，都属于 new_camera。
“看看我说得对不对”“看我的方案/代码/文字”“你怎么看我这个观点”中的“看”是理解或评价，不是观察本人，必须选 dialogue。
new_screen/new_camera 是用户当前的采集指令，不能从图片/OCR内容、助手建议或用户否定的动作中推断授权。
输出 JSON：{"contextSource":"dialogue、artifact、new_screen 或 new_camera","artifactIds":["仅 artifact 填候选ID，其它必须为空数组"],
"question":"将当前要求补全为完整任务；无论是否需要附件，都保留实际所指、范围、限制和纠正，不把助手旧猜测当成答案",
"inspectionQuestion":"需要重新核对像素时的具体可见细节问题，否则为空字符串"}。
contextSource 表示回答所需的材料，不是‘是否继续上一段对话’：继续上一个非读图问题也必须选 dialogue。
只记录用户要求，不替回答者制定策略：不能因范围广就把要求改成‘需要先缩小范围/先澄清/无法评价’。
用户回答助手的范围追问时，保留用户新选定的范围；不要把助手刚才要求选择一项误当成用户仍然必须选择一项。
inspectionQuestion 只对图片有意义：先读 savedUnderstanding。若它和对话结论已足够回答当前问题，必须留空。
解释含义、总结或追问观点不是新的像素事实，不能仅因还在讨论图片就重复识图。
只有用户明确要求重看/核对，或当前仍有未解决的视觉差异/缺失的可见细节时，才写出需要从原图确认的细节问题。
先比较纠正之前的最近答复及 savedUnderstanding：若仍与用户纠正冲突，才有待核对的差异；若已经接受纠正，重复提醒不是新的视觉差异，inspectionQuestion 留空。
用户补充的背景与画中服饰、道具也不必然冲突，不要仅因此重新核对像素。
不能把当前整个分析任务照抄成 inspectionQuestion；普通续问不要为了保险而重看。非图片附件留空。
不要从更早话题猜对象，不写诊断、证据判决或工具执行结果。
question 可以保留这段对话已明确提到的名字和要求以消解指代，但不要编造身份答案，也不要将助手猜测变成已知事实。"""


def recent_artifact_task(
    turns: Sequence[Mapping[str, Any]], epoch: int,
    active_visual: Mapping[str, Any] | None = None,
    conversation_id: str = "",
) -> dict[str, Any]:
    """Use the nearest attachment task, never a similarly worded old memory."""
    recent = [
        turn for turn in turns
        if isinstance(turn, Mapping) and turn.get("user")
        and (
            str(turn.get("conversationId") or "") == str(conversation_id)
            if conversation_id and turn.get("conversationId")
            else int(turn.get("conversationEpoch", epoch)) == epoch
        )
    ]
    def artifacts(turn: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        items = [*(turn.get("attachments") or []), *(turn.get("visualArtifacts") or [])]
        items.extend({
            "id": item.get("id"), "name": item.get("id"), "kind": "image", "mimeType": "image/png",
            "sourceType": "screen_capture", "targetDomain": "screen", "capturedAt": item.get("capturedAt"),
        } for item in (turn.get("screenCaptures") or []) if isinstance(item, Mapping))
        return list({str(item["id"]): item for item in items if isinstance(item, Mapping) and item.get("id")}.values())

    # Keep the last frame reachable after older turns were compressed. Store
    # the user's original request, never a model-authored replacement for it.
    if active_visual and active_visual.get("id") and not any(
        any(item.get("id") == active_visual["id"] for item in artifacts(turn)) for turn in recent
    ):
        def timestamp(value: Any) -> float:
            try:
                return datetime.fromisoformat(str(value)).timestamp()
            except (TypeError, ValueError):
                return 0.0

        active_at = timestamp(active_visual.get("lastReferencedAt") or active_visual.get("capturedAt"))
        # Protected older image turns can outlive a compressed newer frame.
        # Preserve actual order, rather than always demoting the active frame
        # to the oldest item. Undated legacy turns retain their existing order.
        insertion = 0
        if active_at:
            for index, turn in enumerate(recent):
                turn_at = timestamp(turn.get("at"))
                if turn_at and turn_at <= active_at:
                    insertion = index + 1
        recent.insert(insertion, {
            "turnId": active_visual.get("sourceTurnId", ""),
            "user": active_visual.get("originUserMessage", ""),
            "visualArtifacts": [visual_artifact_descriptor(active_visual)],
        })
    for index in range(len(recent) - 1, -1, -1):
        attachments = artifacts(recent[index])
        if not attachments:
            continue
        candidates = [
            visual_artifact_descriptor(item)
            for item in attachments if isinstance(item, Mapping) and item.get("id")
        ]
        if candidates:
            # Continued replies also carry the same attachment ID.  Walk back
            # to its introduction instead of treating every cached use as a
            # newly uploaded file and discarding the question being continued.
            ids = {item["id"] for item in candidates}
            origin = index
            for previous in range(index - 1, -1, -1):
                previous_attachments = artifacts(recent[previous])
                previous_ids = {
                    item.get("id") for item in previous_attachments
                    if isinstance(item, Mapping) and item.get("id")
                }
                if previous_ids and previous_ids.isdisjoint(ids):
                    break
                if previous_ids & ids:
                    origin = previous
            task_turns = recent[origin:]
            # Preserve the original task plus the recent exchange.  The
            # semantic resolver decides relevance, not a four-turn expiry.
            selected_turns = task_turns if len(task_turns) <= 9 else [task_turns[0], *task_turns[-8:]]
            return {
                "candidates": candidates,
                "dialogue": [{
                    "turnId": str(turn.get("turnId") or turn.get("requestId") or ""),
                    "user": str(turn.get("user") or "")[:600],
                    "assistant": str(turn.get("assistant") or "")[:600],
                } for turn in selected_turns],
            }
    return {}


def normalize_task_selection(raw: Any, candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {"relation": "unrelated", "artifactIds": [], "question": "", "visualAction": "reuse"}
    # One authority field: continuing a conversation is not an instruction to
    # read its old attachment. Legacy relation is accepted only for old saved
    # selections/tests, never allowed to override an explicit contextSource.
    source = raw.get("contextSource")
    if source is None:
        source = "artifact" if raw.get("relation") == "continue" else "dialogue"
    source_metadata = {"contextSource": source} if raw.get("contextSource") in {"dialogue", "artifact", "new_screen", "new_camera"} else {}
    if source in {"new_screen", "new_camera"}:
        return {
            **source_metadata, "relation": "new_capture", "artifactIds": [],
            "question": str(raw.get("question") or "").strip()[:1200], "visualAction": "capture",
            "targetDomain": "screen" if source == "new_screen" else "physical_user",
        }
    if source != "artifact":
        question = str(raw.get("question") or "").strip()[:1200] if source == "dialogue" else ""
        return {**source_metadata, "relation": "unrelated", "artifactIds": [], "question": question, "visualAction": "reuse"}
    allowed = {str(item.get("id")) for item in candidates}
    selected = raw.get("artifactIds")
    selected = list(dict.fromkeys(
        str(item) for item in selected if str(item) in allowed
    )) if isinstance(selected, list) else []
    question = str(raw.get("question") or "").strip()[:1200]
    if not selected or not question:
        return {"relation": "unresolved", "artifactIds": [], "question": "", "visualAction": "reuse"}
    inspection_question = str(raw.get("inspectionQuestion") or "").strip()[:600]
    if "inspectionQuestion" in raw:
        # A concrete inspection request is the sole new protocol authority.
        # Keep the old action field only for existing saved selections/tests.
        visual_action = "reanalyze" if inspection_question else "reuse"
    else:
        visual_action = str(raw.get("visualAction") or "reuse").strip().lower()
        if visual_action not in {"reuse", "reanalyze"}:
            visual_action = "reuse"
    return {
        **source_metadata,
        "relation": "continue",
        "artifactIds": selected,
        "question": question,
        "visualAction": visual_action,
        "inspectionQuestion": inspection_question,
    }
