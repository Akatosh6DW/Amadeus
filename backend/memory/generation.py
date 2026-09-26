"""长期记忆的模型修复与兼容生成流程。

这里拥有两种模型边界：当主回复声明保存却缺少结构化记录时，做一次
受限的结构修复；旧的独立记忆生成入口仍按原提示、超时和清空代际写回。
证据 gate 由 ``grounding`` 服务提供，文件锁和实际写入继续由 storage
端口提供，避免模型服务复制持久化状态。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping

from backend.common.text import unique_strings
from backend.providers.errors import UpstreamProtocolError


@dataclass(frozen=True)
class MemoryGenerationPorts:
    """模型生成所需的窄端口；每个端口均可晚绑定旧 server 入口。"""

    load_settings: Callable[[], dict[str, Any]]
    configured: Callable[[dict[str, Any]], bool]
    read_memory: Callable[[dict[str, Any] | None], list[str]]
    clear_generation: Callable[[dict[str, Any] | None], int]
    persistence_lock: Any
    write_memory: Callable[..., list[str]]
    extract_memories: Callable[[str], list[str]]
    grounded_candidates: Callable[..., list[dict[str, Any]]]
    grounded_updates: Callable[..., list[str]]
    send_upstream: Callable[..., str]
    parse_json: Callable[[str], dict[str, Any] | None]
    normalize_model_reply: Callable[..., Mapping[str, Any]]
    write_log: Callable[..., Any]


class MemoryGenerationService:
    """拥有记忆相关模型调用的参数、预算、重试和清空代际边界。"""

    def __init__(self, ports: MemoryGenerationPorts) -> None:
        self.ports = ports

    def recover_declared_memory_action_with_model(
        self,
        settings: dict[str, Any],
        user_message: str,
        result: Mapping[str, Any],
        *,
        request_id: str,
    ) -> list[dict[str, Any]]:
        """Repair a model-declared save action without generating new intent."""

        if not self.ports.configured(settings) or str(result.get("memoryAction") or "") != "save":
            return []
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.0,
            "max_tokens": 480,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是长期记忆结构修复器，不参与聊天，也不靠关键词规则做新的保存决定。"
                        "主对话模型已经通过 memoryAction=save 决定本轮应保存；你只把当前用户原话"
                        "整理成一到四条结构化记录。只输出 JSON：{\"memoryUpdates\":[{\"type\":"
                        "\"user_memory|task_state|correction|episode|shared_reference\",\"content\":\"中文记忆\","
                        "\"subject\":\"...\",\"predicate\":\"...\",\"object\":\"...\","
                        "\"polarity\":\"positive|negative|neutral\",\"modality\":"
                        "\"asserted|requested|corrected\",\"cardinality\":\"single|multi\","
                        "\"importance\":0.0,\"shouldSave\":true,\"sourceSpan\":\"逐字用户原话\"}]}。"
                        "sourceSpan 必须是 userMessage 中连续、逐字存在的片段；object 也必须能从该片段"
                        "逐字复制出来，不能换同义词。拿不准 sourceSpan 时可以直接逐字复制整个"
                        "userMessage。不得引用 assistantReply，不得扩写隐私或虚构事实。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "userMessage": str(user_message or "")[:2400],
                        "assistantReply": str(result.get("reply") or "")[:1200],
                        "memoryAction": "save",
                    }, ensure_ascii=False),
                },
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        last_diagnostics: list[str] = []
        for semantic_attempt in (1, 2):
            content = self.ports.send_upstream(
                settings,
                payload,
                timeout=45,
                request_id=request_id,
                purpose=(
                    "memory_action_structured_repair"
                    if semantic_attempt == 1 else
                    "memory_action_structured_repair_retry"
                ),
            )
            parsed = self.ports.parse_json(content)
            updates = parsed.get("memoryUpdates") if isinstance(parsed, Mapping) else None
            if not isinstance(updates, list):
                last_diagnostics = ["missing_memory_updates_array"]
            else:
                last_diagnostics = []
                grounded = self.ports.grounded_candidates(
                    updates,
                    user_message,
                    diagnostics=last_diagnostics,
                )
                if grounded:
                    return grounded
            if semantic_attempt == 1:
                self.ports.write_log(
                    "memory_action_structured_repair_retrying",
                    requestId=request_id,
                    rejectionReasons=list(last_diagnostics),
                )
                payload["messages"] = [
                    *payload["messages"],
                    {
                        "role": "system",
                        "content": (
                            "上一份结构没有通过逐字证据校验。重新独立输出完整 JSON。"
                            "rejectionReasons 只说明字段错误：sourceSpan 必须从 userMessage 原样复制，"
                            "object 必须是 sourceSpan 中可逐字找到的连续内容；不要沿用助手措辞。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps({
                            "userMessage": str(user_message or "")[:2400],
                            "rejectionReasons": list(last_diagnostics),
                        }, ensure_ascii=False),
                    },
                ]
        self.ports.write_log(
            "memory_action_structured_repair_rejected",
            requestId=request_id,
            rejectionReasons=list(last_diagnostics),
        )
        raise UpstreamProtocolError(
            "memory action repair produced no grounded record: "
            + ",".join(last_diagnostics[:8])
        )

    def generate_memory_from_model(self, body: dict[str, Any]) -> list[str]:
        settings = self.ports.load_settings()
        expected_clear_generation = self.ports.clear_generation(settings)
        current = self.ports.read_memory(settings)
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        notes = str(body.get("notes") or "")

        if not self.ports.configured(settings):
            extracted: list[str] = []
            for item in messages[-40:]:
                if isinstance(item, dict):
                    extracted.extend(self.ports.extract_memories(str(item.get("content") or "")))
            if notes:
                extracted.extend(self.ports.extract_memories(notes))
            with self.ports.persistence_lock:
                if self.ports.clear_generation(settings) != expected_clear_generation:
                    return self.ports.read_memory(settings)
                return self.ports.write_memory(
                    unique_strings(extracted + current),
                    settings=settings,
                )

        instruction = (
            "你要为一个长期陪伴型 AMADEUS 智能体整理长期记忆文件。"
            "只保留长期有用、稳定、可帮助之后对话的信息。"
            "不要保存一次性闲聊。不要保存敏感隐私，除非用户明确要求长期记住。"
            "严格区分说话人：只能从 role=user 和 userNotes 提取用户事实；role=assistant 的猜测、"
            "截图/摄像头描述和工具观测不能改写成用户事实。当前正在看的页面或临时动作不属于长期记忆。"
            "输出 JSON：{\"memories\":[{\"type\":\"user_memory|task_state|correction|episode|shared_reference\","
            "\"content\":\"中文记忆\",\"subject\":\"...\",\"predicate\":\"...\",\"object\":\"...\","
            "\"polarity\":\"positive|negative\",\"modality\":\"asserted|requested|corrected\","
            "\"cardinality\":\"single|multi\",\"importance\":0.0,\"sourceSpan\":\"逐字用户原话\"}]}。"
            "最多 32 条；sourceSpan 必须逐字来自 recentMessages 中 role=user 或 userNotes，不能引用 assistant。"
        )
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "system", "content": "必须只输出 JSON，且 memories 必须是数组。"},
                {"role": "user", "content": json.dumps({
                    "existingMemories": current,
                    "recentMessages": messages[-40:],
                    "userNotes": notes,
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        content = self.ports.send_upstream(
            settings,
            payload,
            timeout=90,
            purpose="memory_generation",
        )
        parsed = self.ports.normalize_model_reply(content, settings=settings)
        memory_payload = self.ports.parse_json(content) or {}
        memories = memory_payload.get("memories") if isinstance(memory_payload, dict) else []
        if not isinstance(memories, list):
            memories = parsed.get("memoryUpdates", [])
        user_evidence = "\n".join(
            str(item.get("content") or "")
            for item in messages[-40:]
            if isinstance(item, dict) and item.get("role") == "user"
        )
        if notes:
            user_evidence += "\n" + notes
        grounded = self.ports.grounded_updates(memories, user_evidence, limit=32)
        with self.ports.persistence_lock:
            if self.ports.clear_generation(settings) != expected_clear_generation:
                self.ports.write_log(
                    "memory_generation_discarded_after_clear",
                    expectedClearGeneration=expected_clear_generation,
                    currentClearGeneration=self.ports.clear_generation(settings),
                )
                return self.ports.read_memory(settings)
            return self.ports.write_memory(
                unique_strings(grounded + current),
                settings=settings,
            )
