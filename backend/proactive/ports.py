"""主动交流依赖按会话、记忆、模型与视觉分组，不保存第二份用户状态。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping

@dataclass(frozen=True)
class ContextOperations:
    read_conversation_context: Callable[..., Any]
    normalize_proactive_control: Callable[..., Any]
    proactive_control_for_model: Callable[..., Any]
    contextual_reference_resolution: Callable[..., Any]
    current_pending_action: Callable[..., Any]
    read_relationship_state: Callable[..., Any]
    relationship_context_prompt: Callable[..., Any]


@dataclass(frozen=True)
class MemoryOperations:
    read_memory_records: Callable[..., Any]
    relevant_memories: Callable[..., Any]


@dataclass(frozen=True)
class ModelOperations:
    load_settings: Callable[..., Any]
    configured: Callable[..., Any]
    send_upstream_chat_payload: Callable[..., Any]
    normalize_model_reply: Callable[..., Any]
    align_spoken_text_ja: Callable[..., Any]
    dialogue_validation_for_result: Callable[..., Any]


@dataclass(frozen=True)
class VisualOperations:
    tool_capture_screen: Callable[..., Any]
    tool_grounding_violation: Callable[..., Any]
    audit_visual_grounding_with_model: Callable[..., Any]
    compact_screen_capture_memory: Callable[..., Any]
