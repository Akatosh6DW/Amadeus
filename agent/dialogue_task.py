"""Trusted, process-local conversation frame for delegated dialogue workers.

Installed by a worker before importing the application. Incoming messages and
model output cannot install a frame; an ordinary desktop process has none.
"""
from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Mapping
from agent.reply_prompt_layers import ReplyPromptLayers


@dataclass(frozen=True)
class DialogueTask:
    system_prompt: str
    facts_prompt: str
    perspective_rule: str
    speech_rule: str
    style_prompt: str
    referents: Mapping[str, Mapping[str, str]]
    reply_layers: ReplyPromptLayers | None = None


_task: DialogueTask | None = None


def install_dialogue_task(task: DialogueTask) -> None:
    global _task
    if _task is not None or "server" in sys.modules:
        raise RuntimeError("A delegated dialogue frame must be installed once, before application startup")
    _task = task


def current_dialogue_task() -> DialogueTask | None:
    return _task
