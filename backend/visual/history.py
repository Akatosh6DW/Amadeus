"""既有屏幕观察的压缩与回忆投影，不读取当前屏幕。

会话仍只有 context_store 一份状态。本服务按调用读取当前 context，并明确区分
历史观察与本轮采集；调用方仍负责把新观察提交到会话中。
"""
from __future__ import annotations
from typing import Any, Mapping, Sequence
import re

from pathlib import Path
from typing import Callable
import json
import uuid
from .sensor_intent import screen_history_request_mode

class ScreenObservationHistory:
    def __init__(self, *, read_context: Callable[[], dict[str, Any]], now: Callable[[], str],
                 memory_limit: Callable[[], int]) -> None:
        self.read_context = read_context
        self.now = now
        self.memory_limit = memory_limit

    def compact_screen_capture_memory(self, result: dict[str, Any]) -> dict[str, Any] | None:
        """Store an observation and its provider identity for historical recall."""

        if result.get("name") != "capture_screen" or not result.get("ok"):
            return None
        path = str(result.get("path") or "").strip()
        vision = result.get("vision") if isinstance(result.get("vision"), dict) else {}
        regions = vision.get("regions") if isinstance(vision.get("regions"), list) else []
        ocr_items = vision.get("ocrItems") if isinstance(vision.get("ocrItems"), list) else []
        visual_tiles = vision.get("visualTiles") if isinstance(vision.get("visualTiles"), list) else []
        return {
            "id": str(result.get("captureId") or (Path(path).stem if path else uuid.uuid4().hex))[:120],
            "capturedAt": str(result.get("capturedAt") or self.now())[:80],
            "path": path[:1200],
            "width": result.get("width"),
            "height": result.get("height"),
            "foregroundWindowTitle": str(result.get("foregroundWindowTitle") or "")[:500],
            "caption": str(vision.get("caption") or vision.get("globalDescription") or "")[:5000],
            "visionModel": str(vision.get("model") or ""),
            "visionProvider": str(vision.get("provider") or ""),
            "ocrText": str(vision.get("ocrText") or "")[:6000],
            "regions": [item for item in regions[:24] if isinstance(item, dict)],
            "visualTiles": [item for item in visual_tiles[:6] if isinstance(item, dict)],
            "ocrItems": [item for item in ocr_items[:80] if isinstance(item, dict)],
        }


    def screen_capture_memories_from_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        raw_results = result.get("_conversationToolResults")
        if not isinstance(raw_results, list):
            raw_results = result.get("toolResults") if isinstance(result.get("toolResults"), list) else []
        memories: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            memory = self.compact_screen_capture_memory(item)
            if memory:
                memories.append(memory)
        return memories


    def screen_capture_tool_result_from_memory(self, value: Any) -> dict[str, Any] | None:
        """Rehydrate only the compact observation fields sent back by the local UI."""

        item = value if isinstance(value, dict) else {}
        capture_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(item.get("id") or ""))[:120]
        if not capture_id:
            return None
        return {
            "name": "capture_screen",
            "ok": True,
            "captureId": capture_id,
            "capturedAt": str(item.get("capturedAt") or "")[:80],
            "path": str(item.get("path") or "")[:1200],
            "width": item.get("width"),
            "height": item.get("height"),
            "foregroundWindowTitle": str(item.get("foregroundWindowTitle") or "")[:500],
            "vision": {
                "available": True,
                "model": str(item.get("visionModel") or "legacy_cached_observation"),
                "provider": str(item.get("visionProvider") or "legacy_cache"),
                "caption": str(item.get("caption") or "")[:5000],
                "globalDescription": str(item.get("caption") or "")[:5000],
                "ocrText": str(item.get("ocrText") or "")[:6000],
                "regions": [
                    nested for nested in (
                        item.get("regions") if isinstance(item.get("regions"), list) else []
                    )[:24]
                    if isinstance(nested, dict)
                ],
                "visualTiles": [
                    nested for nested in (
                        item.get("visualTiles") if isinstance(item.get("visualTiles"), list) else []
                    )[:6]
                    if isinstance(nested, dict)
                ],
                "ocrItems": [
                    nested for nested in (
                        item.get("ocrItems") if isinstance(item.get("ocrItems"), list) else []
                    )[:80]
                    if isinstance(nested, dict)
                ],
            },
        }


    def screen_capture_history_prompt(self, user_message: str, context: dict[str, Any] | None = None) -> str:
        mode = screen_history_request_mode(user_message)
        if not mode:
            return ""
        context = context if isinstance(context, dict) else self.read_context()
        captures = [item for item in context.get("screenCaptures", []) if isinstance(item, dict)]
        captures = list(reversed(captures[-self.memory_limit():]))
        if not captures:
            return (
                "[截图历史]\n用户正在引用旧截图，但当前会话中没有已保存的截图证据。"
                "必须如实说明没有可比较的旧图，不能把当前画面或旧回复冒充成旧截图。"
            )
        rows: list[str] = []
        for index, item in enumerate(captures, start=1):
            rows.append(json.dumps({
                "reference": f"S{index}",
                "meaning": "最近一次保存的截图" if index == 1 else f"往前第 {index} 次保存的截图",
                "capturedAt": str(item.get("capturedAt") or ""),
                "path": str(item.get("path") or ""),
                "size": [item.get("width"), item.get("height")],
                "foregroundWindowTitle": str(item.get("foregroundWindowTitle") or ""),
                "caption": str(item.get("caption") or "")[:1800],
                "visualTiles": item.get("visualTiles") if isinstance(item.get("visualTiles"), list) else [],
                "ocrText": str(item.get("ocrText") or "")[:1800],
                "regions": item.get("regions") if isinstance(item.get("regions"), list) else [],
            }, ensure_ascii=False))
        action = (
            "本轮还会截取当前画面；比较时必须明确区分新截图与 S1、S2 等旧截图。"
            if mode == "compare_current" else
            "本轮只引用历史截图，不得假装又截取了当前屏幕，也不得用 screen_latest 替换 S1、S2。"
        )
        return "\n".join([
            "[多张截图历史：后端保存的视觉证据]",
            action,
            "编号按新到旧排列：S1 最新，S2 是再前一张。caption 描述画面，ocrText 只补充文字。",
            *rows,
        ])


    def recalled_screen_capture_results(
        self,
        user_message: str,
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Turn explicit saved-image follow-ups into groundable visual evidence."""

        text = str(user_message or "")
        compact = re.sub(r"\s+", "", text.lower())
        mode = screen_history_request_mode(compact)
        reaction_followup = bool(re.search(
            r"(?:看到|看见|看了).{0,10}(?:图片|图像|画面).{0,12}(?:反应|感觉|评价|怎么看)|"
            r"(?:对|看完).{0,10}(?:裸女|裸体|裸露|r18|成人).{0,10}(?:图片|图像|画面)?.{0,10}(?:反应|感觉|评价|怎么看)?|"
            r"(?:裸女|裸体|裸露|r18|成人).{0,8}(?:图片|图像|画面).{0,12}(?:反应|感觉|评价|怎么看)",
            compact,
            flags=re.I,
        ))
        if not mode and not reaction_followup:
            return []
        context = context if isinstance(context, dict) else self.read_context()
        captures = [item for item in context.get("screenCaptures", []) if isinstance(item, dict)]
        captures = list(reversed(captures[-self.memory_limit():]))
        if not captures:
            return []
        asks_adult = bool(re.search(r"裸女|裸体|裸露|r18|成人|色情|情色", compact, flags=re.I))
        if asks_adult:
            adult_matches = []
            for item in captures:
                evidence = " ".join([
                    str(item.get("caption") or ""),
                    *(str(tile.get("caption") or "") for tile in item.get("visualTiles", []) if isinstance(tile, dict)),
                ])
                if re.search(
                    r"sexual|sex scene|intercourse|nude|naked|topless|breasts?|adult content|裸体|裸露|成人内容|性行为",
                    evidence,
                    flags=re.I,
                ):
                    adult_matches.append(item)
            if adult_matches:
                captures = adult_matches
        multiple = bool(re.search(r"几张|这些|前两张|前几张|最近两张|最近几张", compact))
        selected = captures[:3] if multiple else captures[:1]
        results: list[dict[str, Any]] = []
        for item in selected:
            results.append({
                "name": "capture_screen",
                "ok": True,
                "recalled": True,
                "captureId": str(item.get("id") or ""),
                "capturedAt": str(item.get("capturedAt") or ""),
                "foregroundWindowTitle": str(item.get("foregroundWindowTitle") or ""),
                "vision": {
                    "caption": str(item.get("caption") or ""),
                    "visualTiles": item.get("visualTiles") if isinstance(item.get("visualTiles"), list) else [],
                    "ocrText": str(item.get("ocrText") or ""),
                    "regions": item.get("regions") if isinstance(item.get("regions"), list) else [],
                },
            })
        return results
