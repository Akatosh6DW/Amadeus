"""主对话模型的图片投影、请求发送与认知结果解析。

图片字节只存在于本轮供应商请求，认知状态继续由请求 body 唯一持有。本模块
不读取配置文件、不提交会话，也不决定工具权限；存储、模型传输和请求取消分别
通过窄端口接入现有 owner。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol
import urllib.parse

from agent.deepseek_vision import image_input_content
from agent.multimodal_chat import DIRECT_IMAGE_INSTRUCTION, native_visual_chat_enabled
from agent.turn_context import pin_reply_context
from backend.attachments.storage import safe_attachment_name
from backend.context.conversation import turn_conversation_id
from backend.tools.validation import SENSOR_TOOL_NAMES


class ConversationImageSources(Protocol):
    """提供本轮图片定位所需的会话与附件存储视图。"""

    def frozen_context(self, body: Mapping[str, Any]) -> Mapping[str, Any] | None: ...
    def read_archive(self) -> list[dict[str, Any]]: ...
    def read_attachment(self, attachment_id: str) -> Mapping[str, Any] | None: ...
    def attachments_dir(self) -> Path: ...
    def data_dir(self) -> Path: ...


class ConversationImageRuntime(Protocol):
    def write_log(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class ConversationImageServices:
    sources: ConversationImageSources
    runtime: ConversationImageRuntime


class ConversationBrainVision(Protocol):
    def collect(
        self, body: dict[str, Any], settings: Mapping[str, Any]
    ) -> list[dict[str, Any]]: ...

    def attach(
        self,
        body: dict[str, Any],
        payload: Mapping[str, Any],
        images: list[dict[str, Any]],
        *,
        purpose: str,
    ) -> dict[str, Any]: ...


class ConversationBrainCognitive(Protocol):
    def turn_state(self, body: Mapping[str, Any]) -> Mapping[str, Any] | None: ...
    def prompt(self, body: Mapping[str, Any]) -> str: ...

    def repaired_messages(
        self, body: Mapping[str, Any], messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...

    def resolve(
        self,
        body: dict[str, Any],
        settings: dict[str, Any],
        payload: dict[str, Any],
        content: str,
        *,
        request_id: str,
    ) -> str: ...


class ConversationBrainModel(Protocol):
    def send(
        self,
        settings: Mapping[str, Any],
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> str: ...

    def stream(
        self,
        settings: Mapping[str, Any],
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> Iterable[str]: ...


class ConversationBrainRuntime(Protocol):
    def record_persona_input(
        self, body: Mapping[str, Any], payload: Mapping[str, Any], purpose: str
    ) -> None: ...

    def raise_if_cancelled(self, request_id: str) -> None: ...


@dataclass(frozen=True)
class ConversationBrainServices:
    vision: ConversationBrainVision
    cognitive: ConversationBrainCognitive
    model: ConversationBrainModel
    runtime: ConversationBrainRuntime


@dataclass(frozen=True)
class ConversationBrainPolicy:
    participant_realization_rule: str


def conversation_image_inputs(
    services: ConversationImageServices,
    body: dict[str, Any],
    settings: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """定位本轮拥有的图片，像素只进入供应商请求。"""

    if not native_visual_chat_enabled(settings):
        return []
    current = body.get("_processedAttachments") or []
    receipts = [
        entry["receipt"]
        for entry in (body.get("_cognitiveEvidence") or {}).values()
        if isinstance(entry, Mapping)
        and not entry.get("historical")
        and isinstance(entry.get("receipt"), Mapping)
        and entry["receipt"].get("name") in {"read_image", *SENSOR_TOOL_NAMES}
        and entry["receipt"].get("ok")
    ]
    sources = [
        (item, False)
        for item in current
        if isinstance(item, Mapping) and item.get("kind") == "image"
    ]
    sources.extend((item, False) for item in receipts)
    if not current and not receipts:
        context = services.sources.frozen_context(body) or {}
        snapshot = body.get("_turnContextSnapshot") or {}
        anchor = snapshot.get("resolvedAnchor") or {}
        anchor_turns = {
            str(item.get("turnId") or "")
            for item in anchor.get("messages", [])
            if isinstance(item, Mapping)
        }
        turns = [
            item for item in context.get("recentTurns", []) if isinstance(item, Mapping)
        ]
        if anchor_turns:
            turns = [
                item
                for item in turns
                if str(item.get("turnId") or "") in anchor_turns
            ]
            if not turns:
                turns = [
                    item
                    for item in services.sources.read_archive()
                    if str(item.get("turnId") or "") in anchor_turns
                    and turn_conversation_id(item)
                    == str(snapshot.get("conversationId") or "")
                ]
        else:
            # 只有邻近可见对话中的图片才可作为隐式引用。压缩状态里无关的
            # activeVisualArtifact 不能自行变成常驻任务。
            turns = turns[-6:]
        for turn in reversed(turns):
            items = [
                *(turn.get("attachments") or []),
                *(turn.get("visualArtifacts") or []),
            ]
            images = [
                item
                for item in items
                if isinstance(item, Mapping) and item.get("kind") == "image"
            ]
            if images:
                for item in images:
                    ident = str(item.get("id") or "")
                    saved = services.sources.read_attachment(ident)
                    if not saved:
                        active = context.get("activeVisualArtifact") or {}
                        saved = active if active.get("id") == ident else item
                    sources.append((saved, True))
                break
    result, seen = [], set()
    cache = body.setdefault("_nativeImageCache", {})
    for source, historical in sources:
        artifact = source.get("imageArtifact") or {}
        ident = str(
            source.get("id")
            or source.get("attachmentId")
            or artifact.get("artifactId")
            or source.get("captureId")
            or ""
        )
        if not ident or ident in seen:
            continue
        seen.add(ident)
        metadata = services.sources.read_attachment(ident)
        if metadata:
            path = (
                services.sources.attachments_dir()
                / ident
                / safe_attachment_name(
                    metadata.get("storedName") or metadata.get("name"), "attachment"
                )
            ).resolve()
            allowed = services.sources.attachments_dir().resolve() in path.parents
        else:
            path = Path(str(source.get("path") or artifact.get("path") or "")).resolve()
            allowed = path.parent in {
                (services.sources.data_dir() / "runtime" / folder).resolve()
                for folder in ("screen-captures", "camera-captures")
            }
        if not allowed or not path.is_file():
            continue
        if ident not in cache:
            parts, dimensions = image_input_content(path)
            cache[ident] = {"parts": parts[1:], **dimensions}
        ref = "image:" + ident
        if ref not in (body.get("_cognitiveEvidence") or {}):
            body.setdefault("_cognitiveEvidence", {})[ref] = {
                "ok": True,
                "sourceType": "visual",
                "historical": historical,
                "name": "image_input",
                "body": json.dumps(
                    {
                        "artifactId": ident,
                        "imageInputAvailable": True,
                        "sourceType": source.get("sourceType")
                        or artifact.get("sourceType")
                        or "uploaded_attachment",
                    }
                ),
                "understandingAvailable": True,
            }
        result.append(
            {
                "artifactId": ident,
                "evidenceRef": ref,
                "historical": historical,
                **cache[ident],
            }
        )
    return result


def with_conversation_images(
    services: ConversationImageServices,
    body: dict[str, Any],
    payload: Mapping[str, Any],
    images: list[dict[str, Any]],
    *,
    purpose: str,
) -> dict[str, Any]:
    """把已定位图片附到模型请求，并固定回复锚点。"""

    if not images:
        if not (body.get("_turnContextSnapshot") or {}).get("resolvedAnchor"):
            return dict(payload)
        return {
            **payload,
            "messages": pin_reply_context(
                payload.get("messages", []), body.get("_turnContextSnapshot")
            ),
        }
    content = [
        {
            "type": "text",
            "text": "本轮可直接查看的图片。依据当前问题判断其相关性；历史参考不是新的任务。",
        }
    ]
    for item in images:
        content.append(
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "artifactId": item["artifactId"],
                        "evidenceRef": item["evidenceRef"],
                        "scope": "historical_reference"
                        if item["historical"]
                        else "current_turn_image",
                    },
                    ensure_ascii=False,
                ),
            }
        )
        content.extend(item["parts"])
    services.runtime.write_log(
        "conversation_image_input_prepared",
        requestId=body.get("requestId"),
        purpose=purpose,
        model=payload.get("model"),
        artifactIds=[item["artifactId"] for item in images],
        imageCount=sum(item["imageCount"] for item in images),
    )
    return {
        **payload,
        "messages": pin_reply_context(
            [
                *payload.get("messages", []),
                {"role": "system", "content": DIRECT_IMAGE_INSTRUCTION},
                {"role": "user", "content": content},
            ],
            body.get("_turnContextSnapshot"),
        ),
    }


def with_cognitive_transport_state(
    body: dict[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    """共享同一轮的 JSON 模式状态；供应商请求发送前会移除该私有字段。"""

    return {
        **payload,
        "_cognitiveJsonModeState": body.setdefault("_cognitiveJsonModeState", {}),
    }


def send_conversation_brain_payload(
    services: ConversationBrainServices,
    policy: ConversationBrainPolicy,
    body: dict[str, Any],
    settings: dict[str, Any],
    payload: dict[str, Any],
    *,
    timeout: int = 90,
    request_id: str = "",
    purpose: str = "chat_primary",
) -> str:
    """发送原子对话请求，并在返回前完成认知结果解析。"""

    images = services.vision.collect(body, settings)
    prompt = services.cognitive.prompt(body)
    active = dict(payload)
    active["messages"] = services.cognitive.repaired_messages(
        body, list(payload.get("messages", []))
    )
    if prompt:
        body["_cognitiveOutcomeRequired"] = True
        active["messages"].append({"role": "system", "content": prompt})
        # 旧的 1500 token 语音额度还要承载任务补丁、主张和双语台词，
        # 因此为整个结构化容器预留空间。
        active["max_tokens"] = max(4096, int(active.get("max_tokens") or 0))
    if (
        prompt
        and purpose.startswith("chat_tool_")
        and urllib.parse.urlsplit(str(settings.get("apiEndpoint") or "")).hostname
        == "api.deepseek.com"
        and str(active.get("model") or settings.get("apiModel") or "").strip()
    ):
        active["thinking"] = {"type": "enabled"}
        active["reasoning_effort"] = "low"
        active["max_tokens"] = max(6000, int(active.get("max_tokens") or 0))
        active.pop("temperature", None)
    if prompt:
        active["messages"].append(
            {"role": "system", "content": policy.participant_realization_rule}
        )
    outgoing = with_cognitive_transport_state(
        body,
        services.vision.attach(body, active, images, purpose=purpose),
    )
    services.runtime.record_persona_input(body, outgoing, purpose)
    content = services.model.send(
        settings,
        outgoing,
        timeout=timeout,
        request_id=request_id,
        purpose=purpose,
    )
    body["_nativeVisualInputIds"] = [item["artifactId"] for item in images]
    return services.cognitive.resolve(
        body, settings, active, content, request_id=request_id
    )


def iter_conversation_brain_stream(
    services: ConversationBrainServices,
    policy: ConversationBrainPolicy,
    body: dict[str, Any],
    settings: dict[str, Any],
    payload: dict[str, Any],
    *,
    timeout: int,
    request_id: str,
    purpose: str,
):
    """收集原子认知容器流，在任何内容可见前解析并校验。"""

    # semantic-v3 本身是原子 JSON 容器，可见台词会一直保留到校验完成。
    # 先解析认知控制，再让既有解析器分派工具或公开容器里的台词。
    images = services.vision.collect(body, settings)
    if services.cognitive.turn_state(body):
        payload = {
            **payload,
            "max_tokens": max(4096, int(payload.get("max_tokens") or 0)),
        }
    if (
        images
        or body.get("_cognitiveRefreshedContext")
        or body.get("_cognitiveRepairRoute")
    ):
        payload = {
            **payload,
            "messages": services.cognitive.repaired_messages(
                body, list(payload.get("messages", []))
            ),
        }
        payload["messages"].append(
            {"role": "system", "content": services.cognitive.prompt(body)}
        )
    parts = []
    if services.cognitive.turn_state(body):
        payload = {
            **payload,
            "messages": [
                *payload.get("messages", []),
                {
                    "role": "system",
                    "content": policy.participant_realization_rule,
                },
            ],
        }
    outgoing = with_cognitive_transport_state(
        body,
        services.vision.attach(body, payload, images, purpose=purpose),
    )
    services.runtime.record_persona_input(body, outgoing, purpose)
    for delta in services.model.stream(
        settings,
        outgoing,
        timeout=timeout,
        request_id=request_id,
        purpose=purpose,
    ):
        services.runtime.raise_if_cancelled(request_id)
        parts.append(str(delta or ""))
    body["_nativeVisualInputIds"] = [item["artifactId"] for item in images]
    yield services.cognitive.resolve(
        body, settings, payload, "".join(parts), request_id=request_id
    )
