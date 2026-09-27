"""主动人格资源读取，限制源文件根目录且只供主动话题使用。

路径在每次调用时由入口提供，保留测试/配置作用域；普通聊天人格不经此裁剪。
"""
from __future__ import annotations
import json
from pathlib import Path
import re
from typing import Any, Callable
from backend.common.text import unique_strings

class PersonaResources:
    def __init__(self, *, root: Callable[[], Path], layers_path: Callable[[], Path],
                 prompt_path: Callable[[], Path], read_json: Callable[..., Any]) -> None:
        self.root = root
        self.layers_path = layers_path
        self.prompt_path = prompt_path
        self.read_json = read_json

    def read_proactive_persona_prompt(self) -> str:
        """Return the deliberately limited persona used only for idle outreach."""

        try:
            value = self.prompt_path().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            value = ""
        return value or (
            "你是 Amadeus。主动聊天应简短、自然、重视证据。"
            "公开兴趣可以成为话题；不愿主动公开的人格事实只能影响反应方式，"
            "不能由你主动拿来开场、延伸或自我揭露。"
        )


    def proactive_persona_topic_policy(self) -> dict[str, Any]:
        """Load disclosure intent from persona sources, not a proactive keyword island."""

        persona_layers = self.read_json(self.layers_path(), {})
        value = (
            persona_layers.get("proactiveDisclosurePolicy")
            if isinstance(persona_layers, dict)
            else {}
        )
        policy = dict(value) if isinstance(value, dict) else {
            "publicSelfTopics": [],
            "privateSelfTopics": [],
        }
        markers = [
            str(item).strip()
            for item in policy.get("privateSemanticMarkers", [])
            if str(item).strip()
        ]
        evidence: list[str] = []

        def text_fragments(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [
                    fragment
                    for item in value
                    for fragment in text_fragments(item)
                ]
            if isinstance(value, dict):
                return [
                    fragment
                    for item in value.values()
                    for fragment in text_fragments(item)
                ]
            return []

        for relative_path in policy.get("sourceFiles", []):
            source_path = (self.root() / str(relative_path)).resolve()
            try:
                source_path.relative_to(self.root())
            except ValueError:
                continue
            try:
                if source_path.suffix.lower() == ".json":
                    source_value: Any = json.loads(source_path.read_text(encoding="utf-8"))
                    source_fragments = text_fragments(source_value)
                else:
                    source_fragments = [source_path.read_text(encoding="utf-8")]
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            for fragment in source_fragments:
                clauses = re.split(r"(?<=[。！？!?；;])|\r?\n", fragment)
                for clause in clauses:
                    normalized = str(clause or "").strip(" \t\r\n-•")
                    if (
                        len(normalized) >= 4
                        and any(marker in normalized for marker in markers)
                    ):
                        evidence.append(normalized[:900])
        policy["privateSemanticEvidence"] = unique_strings(evidence)
        return policy
