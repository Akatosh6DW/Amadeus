"""一次主动交流准备结果，生成重试复用同一话题与屏幕证据快照。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class PreparedProactiveTurn:
    body: dict[str, Any]
    context: dict[str, Any]
    settings: dict[str, Any]
    messages: list[dict[str, Any]]
    previous_prompt: str
    unanswered_count: int
    kind: str
    silence_check_in: bool
    recent_replies: list[str]
    resolution: dict[str, Any]
    loaded_modules: list[str]
    proactive_persona_policy: dict[str, Any]
    recent_user_texts: list[str]
    pending_todo_topic_id: str
    screen_result: dict[str, Any] | None
    screen_evidence: dict[str, Any] | None
    allowed_topic_source_ids: set[str]
    silence_topic_id: str
    pending_question_text: str
    exchange_status: str
    generation_rule: str
    prompt_input: dict[str, Any]
