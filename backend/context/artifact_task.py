"""附件与已保存画面的任务续接、语义选择和提示投影。

本模块只选择本轮任务绑定的工件并生成上下文。上传存储、画面采集、认知状态和
模型传输继续由各自 owner 管理；模型语义选择不能绕过后续工具权限校验。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import mimetypes
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from agent.artifact_task_context import (
    TASK_CONTEXT_RULE,
    normalize_task_selection,
    recent_artifact_task,
)
from agent.dialogue_architecture import UtteranceInterpretation
from agent.visual_artifact_cache import compact_visual_artifact, visual_artifact_descriptor
from backend.attachments.storage import safe_attachment_name
from backend.chat.parsing import parse_json_object
from backend.common.values import safe_nonnegative_int


class ArtifactTaskState(Protocol):
    def frozen_context(self, body: Mapping[str, Any]) -> Mapping[str, Any] | None: ...
    def read_context(self) -> Mapping[str, Any]: ...
    def current_epoch(self) -> int: ...
    def cognitive_state(self, body: Mapping[str, Any]) -> Mapping[str, Any] | None: ...


class ArtifactTaskAssets(Protocol):
    def restore_screen_capture(self, value: Any) -> Mapping[str, Any] | None: ...
    def read_attachment(self, attachment_id: Any) -> Mapping[str, Any] | None: ...
    def edit_capabilities(self, body: Mapping[str, Any]) -> list[dict[str, Any]]: ...


class ArtifactTaskModel(Protocol):
    def configured(self, settings: Mapping[str, Any]) -> bool: ...

    def send(
        self,
        settings: Mapping[str, Any],
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> str: ...


class ArtifactTaskRuntime(Protocol):
    def write_log(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class ArtifactTaskServices:
    state: ArtifactTaskState
    assets: ArtifactTaskAssets
    model: ArtifactTaskModel
    runtime: ArtifactTaskRuntime


@dataclass(frozen=True)
class ArtifactTaskPolicy:
    max_chat_attachments: int
    image_attachment_extensions: frozenset[str]
    attachment_reply_rule: str


def current_turn_context_artifacts(
    policy: ArtifactTaskPolicy,
    body: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """返回本轮新绑定或明确续接的任务工件。"""

    processed = body.get("_processedAttachments")
    raw = (
        processed
        if isinstance(processed, list) and processed
        else body.get("attachments")
    )
    raw_items = list(raw) if isinstance(raw, list) else []
    continued = body.get("_continuedAttachments")
    if isinstance(continued, list):
        raw_items.extend(continued)
    continued_visuals = body.get("_continuedVisualArtifacts")
    if isinstance(continued_visuals, list):
        raw_items.extend(
            {**visual_artifact_descriptor(item), "_taskContinuation": True}
            for item in continued_visuals
        )
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_items[: policy.max_chat_attachments]):
        if not isinstance(item, Mapping):
            continue
        identity = str(
            item.get("id")
            or item.get("attachmentId")
            or f"{item.get('name') or item.get('fileName') or 'attachment'}:{index}"
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result


def current_task_semantic_request(body: Mapping[str, Any], user_message: str) -> str:
    """返回解析后的当前任务，不改写用户原文。

    该文本只供回复合同和审计使用。工具授权必须继续读取用户实际消息，不能使用
    这里合成的问题。
    """

    repaired = body.get("_cognitiveRepairRoute") or {}
    if repaired.get("phase") == "resume":
        goal = str(
            (repaired.get("cognitiveDecision") or {}).get("understoodGoal") or ""
        ).strip()
        if goal:
            return goal[:2000]
    recovered = body.get("_recoveredTaskContext")
    if isinstance(recovered, Mapping):
        goal = str(
            recovered.get("task_goal") or recovered.get("taskGoal") or ""
        ).strip()
        if goal:
            return goal[:2000]
    task = body.get("_artifactTaskContext")
    if isinstance(task, Mapping) and task.get("relation") in {
        "continue",
        "unrelated",
        "new_capture",
    }:
        question = str(task.get("question") or "").strip()
        if question:
            return question[:1200]
    return user_message


def task_local_dialogue_messages(body: Mapping[str, Any]) -> list[dict[str, str]]:
    """在视觉处理中保留说话人和邻近任务对话。"""

    task = body.get("_artifactTaskContext")
    recovered = body.get("_recoveredTaskContext")
    if isinstance(recovered, Mapping):
        dialogue = recovered.get("selectedDialogue")
    elif isinstance(task, Mapping) and task.get("relation") == "continue":
        dialogue = task.get("dialogue")
    else:
        return []
    result: list[dict[str, str]] = []
    for turn in dialogue if isinstance(dialogue, list) else []:
        if not isinstance(turn, Mapping):
            continue
        role = str(turn.get("role") or "").strip().lower()
        content = str(turn.get("content") or turn.get("text") or "").strip()[:600]
        if role in {"user", "assistant"} and content:
            result.append({"role": role, "content": content})
            continue
        for field, role in (("user", "user"), ("assistant", "assistant")):
            content = str(turn.get(field) or "").strip()[:600]
            if content:
                result.append({"role": role, "content": content})
    return result


def task_review_dialogue_messages(
    body: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """把真实前因带入最终复核，包括非视觉轮次。"""

    recovered = body.get("_recoveredTaskContext") or {}
    task = body.get("_artifactTaskContext") or {}
    dialogue = recovered.get("selectedDialogue") or task.get("dialogue") or []
    history: list[dict[str, str]] = []
    for turn in dialogue if isinstance(dialogue, list) else []:
        if not isinstance(turn, Mapping):
            continue
        direct_role = str(turn.get("role") or "").strip().lower()
        direct_content = str(turn.get("content") or turn.get("text") or "").strip()
        if direct_role in {"user", "assistant"} and direct_content:
            history.append({"role": direct_role, "content": direct_content[:600]})
            continue
        for field, role in (("user", "user"), ("assistant", "assistant")):
            content = str(turn.get(field) or "").strip()
            if content:
                history.append({"role": role, "content": content[:600]})
    if not history:
        history = [
            {
                "role": str(item["role"]),
                "content": str(item.get("content") or "")[:600],
            }
            for item in messages
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
    return history[-18:]


def bind_resolved_task_interpretation(
    interpretation: UtteranceInterpretation,
    body: Mapping[str, Any],
) -> UtteranceInterpretation:
    """投影唯一语义决策，不能再次运行词法分类器。"""

    task = body.get("_artifactTaskContext")
    if (
        not isinstance(task, Mapping)
        or task.get("relation") not in {"continue", "unrelated", "new_capture"}
        or not task.get("question")
    ):
        return interpretation
    question = current_task_semantic_request(body, interpretation.original_text)
    return replace(
        interpretation,
        semantic_text=question,
        requires_answer=True,
        answer_target=question,
        primary_speech_act=(
            "request"
            if interpretation.primary_speech_act
            in {"other", "acknowledgement", "clarification_answer"}
            else interpretation.primary_speech_act
        ),
    )


def resolve_recent_artifact_task_context(
    services: ArtifactTaskServices,
    body: dict[str, Any],
    settings: dict[str, Any],
    user_message: str,
    *,
    request_id: str,
) -> dict[str, Any]:
    """在人格回复前用小模型选择视觉来源。

    此步骤不含人格或长期记忆，只在对话、已保存工件、新屏幕和新摄像头之间作
    语义选择。正则 preflight 仅是失败回退，不能决定本轮是否存在摄像头工具。
    """

    existing = body.get("_artifactTaskContext")
    if isinstance(existing, dict):
        return existing
    if body.get("attachments"):
        resolved = {
            "relation": "current",
            "artifactIds": [],
            "question": user_message,
            "visualAction": "reanalyze",
        }
        body["_artifactTaskContext"] = resolved
        return resolved
    context = services.state.frozen_context(body) or services.state.read_context()
    task = recent_artifact_task(
        context.get("recentTurns")
        if isinstance(context.get("recentTurns"), list)
        else [],
        (
            safe_nonnegative_int(context.get("conversationEpoch"))
            if context.get("conversationEpoch") is not None
            else services.state.current_epoch()
        ),
        context.get("activeVisualArtifact"),
        str(context.get("conversationId") or ""),
    )
    candidates = (
        task.get("candidates") if isinstance(task.get("candidates"), list) else []
    )
    active_visual = context.get("activeVisualArtifact") or {}
    saved_sensors = (
        {str(active_visual.get("id")): active_visual}
        if active_visual.get("id")
        else {}
    )
    for old_capture in context.get("screenCaptures") or []:
        if (
            isinstance(old_capture, Mapping)
            and str(old_capture.get("id")) not in saved_sensors
        ):
            receipt = services.assets.restore_screen_capture(old_capture)
            restored = compact_visual_artifact(receipt) if receipt else None
            if restored:
                saved_sensors[restored["id"]] = restored
    metadata_by_id = {
        str(item.get("id")): (
            saved_sensors.get(str(item.get("id")))
            if item.get("sourceType") in {"screen_capture", "camera_capture"}
            else services.assets.read_attachment(item.get("id"))
        )
        for item in candidates
    }
    candidates = [
        {
            **item,
            "savedUnderstanding": {
                "caption": str(
                    (metadata_by_id.get(str(item.get("id"))) or {}).get(
                        "visionCaption"
                    )
                    or (
                        (
                            metadata_by_id.get(str(item.get("id"))) or {}
                        ).get("vision")
                        or {}
                    ).get("caption")
                    or ""
                )[:1600],
                "text": str(
                    (metadata_by_id.get(str(item.get("id"))) or {}).get(
                        "extractedText"
                    )
                    or (
                        (
                            metadata_by_id.get(str(item.get("id"))) or {}
                        ).get("vision")
                        or {}
                    ).get("ocrText")
                    or ""
                )[:600],
            },
        }
        for item in candidates
    ]
    if services.state.cognitive_state(body):
        # 主认知模型会收到这些候选，并决定复用工件、查看屏幕/摄像头或继续
        # 对话。在主模型看到请求前不能再运行另一个语义模型。
        body["_cognitiveArtifactCandidates"] = deepcopy(candidates)
        resolved = {
            "relation": "model_pending",
            "artifactIds": [],
            "question": "",
            "visualAction": "model_decides",
            "dialogue": task.get("dialogue") or [],
        }
        body["_artifactTaskContext"] = resolved
        return resolved
    if not services.model.configured(settings):
        resolved = {
            "relation": "unrelated",
            "artifactIds": [],
            "question": "",
            "visualAction": "reuse",
        }
        body["_artifactTaskContext"] = resolved
        return resolved
    dialogue_context = (
        task.get("dialogue") if isinstance(task.get("dialogue"), list) else []
    )
    if not dialogue_context:
        dialogue_context = [
            {
                "role": str(item.get("role") or "")[:20],
                "content": str(item.get("content") or item.get("text") or "")[:600],
            }
            for item in (
                body.get("messages") if isinstance(body.get("messages"), list) else []
            )[-6:]
            if isinstance(item, Mapping)
            and str(item.get("role") or "") in {"user", "assistant"}
            and str(item.get("content") or item.get("text") or "").strip()
        ]
    try:
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.0,
            "max_tokens": 360,
            "messages": [
                {"role": "system", "content": TASK_CONTEXT_RULE},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "currentMessage": user_message,
                            "candidates": candidates,
                            "dialogueSinceArtifact": dialogue_context,
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
        raw = services.model.send(
            settings,
            payload,
            timeout=35,
            request_id=request_id,
            purpose="semantic_visual_source_resolution",
        )
        resolved = normalize_task_selection(parse_json_object(raw), candidates)
    except Exception as error:
        resolved = {
            "relation": "unresolved",
            "artifactIds": [],
            "question": "",
            "visualAction": "reuse",
        }
        services.runtime.write_log(
            "artifact_task_resolution_failed",
            requestId=request_id,
            errorType=type(error).__name__,
            error=str(error)[:300],
        )
    continued: list[dict[str, Any]] = []
    continued_visuals: list[dict[str, Any]] = []
    if resolved.get("relation") == "continue":
        for artifact_id in resolved.get("artifactIds", []):
            metadata = metadata_by_id.get(artifact_id)
            if metadata:
                if metadata.get("sourceType") in {
                    "screen_capture",
                    "camera_capture",
                }:
                    continued_visuals.append(metadata)
                else:
                    continued.append({**metadata, "_taskContinuation": True})
        if not (continued or continued_visuals):
            resolved = {
                "relation": "unresolved",
                "artifactIds": [],
                "question": "",
                "visualAction": "reuse",
            }
    resolved["dialogue"] = task.get("dialogue") or []
    body["_artifactTaskContext"] = resolved
    body["_continuedAttachments"] = continued
    body["_continuedVisualArtifacts"] = continued_visuals
    services.runtime.write_log(
        "artifact_task_context_resolved",
        requestId=request_id,
        relation=resolved.get("relation"),
        artifactIds=resolved.get("artifactIds", []),
        visualAction=resolved.get("visualAction"),
        boundCount=len(continued) + len(continued_visuals),
        contextSource=str(resolved.get("contextSource") or ""),
        inspectionQuestion=str(resolved.get("inspectionQuestion") or "")[:600],
        resolvedQuestion=str(resolved.get("question") or "")[:1200],
        antecedentTurnIds=[
            str(item.get("turnId") or "") for item in resolved.get("dialogue", [])
        ],
    )
    return resolved


def current_turn_attachment_binding_prompt(
    services: ArtifactTaskServices,
    policy: ArtifactTaskPolicy,
    body: Mapping[str, Any],
) -> str:
    """把新附件声明为当前指代的权威目标。

    fast 模型运行在附件提取之前，仍需获得紧邻语义对象以理解“看看这个”等表达，
    否则上一轮屏幕可能成为最近的自然语言指代。这里只投影安全公开元数据，绝不
    包含 base64 文件正文。
    """

    raw_items = current_turn_context_artifacts(policy, body)
    if not raw_items:
        return ""
    attachments = []
    for item in raw_items[: policy.max_chat_attachments]:
        name = safe_attachment_name(item.get("name"), "attachment")
        mime_type = str(
            item.get("mimeType")
            or item.get("type")
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )[:160]
        is_image = (
            mime_type.lower().startswith("image/")
            or Path(name).suffix.lower() in policy.image_attachment_extensions
        )
        attachments.append(
            {
                "id": str(item.get("id") or item.get("attachmentId") or "")[:80],
                "name": name,
                "mimeType": mime_type,
                "targetDomain": item.get("targetDomain")
                or ("uploaded_image" if is_image else "document"),
                "sourceType": item.get("sourceType") or "uploaded_attachment",
            }
        )
    continued = bool(
        raw_items and all(item.get("_taskContinuation") for item in raw_items)
    )
    task = body.get("_artifactTaskContext")
    resolved_question = str(
        task.get("question") if continued and isinstance(task, Mapping) else ""
    ).strip()[:1200]
    if body.get("_continuedVisualArtifacts"):
        return (
            "[已绑定的历史画面]\n"
            + json.dumps(
                {"artifacts": attachments},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + f"\n当前任务：{resolved_question}。这些是已保存的画面，不是本轮新上传或新采集。"
            "工具结果保留原截图/摄像头来源；使用提供的画面理解回答，不要再次采集。"
            "用户原话、最近追问和纠正优先于任务摘要；摘要不得缩小用户指定范围。"
        )
    return (
        (
            "[最近附件任务续接：高优先级]\n"
            if continued
            else "[本轮新附件指代绑定：高优先级]\n"
        )
        + json.dumps(
            {"attachments": attachments},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + (
            "\n语义任务解析已确认当前消息继续处理这些最近附件。"
            if continued
            else "\n这些是用户随当前消息新上传的对象。"
        )
        + (f"补全后的当前任务：{resolved_question}。" if resolved_question else "")
        + "当前句中的‘这个、这份、这张、它、看看这个’"
        + ("默认指向这些附件，" if continued else "默认指向这些新附件，")
        + (
            "不得续接为屏幕、摄像头或其他旧附件。"
            if continued
            else "不得续接为上一轮的屏幕、摄像头或旧附件。"
        )
        + "先使用附件对应的 read_attachment/read_image 能力；只有当前用户消息明确同时要求实时屏幕或摄像头时，"
        "才可额外申请传感器工具。"
        + policy.attachment_reply_rule
        + "\n附件编辑能力："
        + json.dumps(services.assets.edit_capabilities(body), ensure_ascii=False)
    )
