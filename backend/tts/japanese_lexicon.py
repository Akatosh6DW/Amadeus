"""按调用读取日语词音资源，并统一专名替换和固定读法检查。

资源路径动态传入；只有确实包含日语发声内容时才读取文件，不缓存旧配置。
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Callable
from backend.chat import presentation as chat_presentation
from backend.tts.japanese_rules import _spoken_ja_alias_present

class JapaneseLexicon:
    def __init__(self, *, pronunciation_path: Callable[[], Path]) -> None:
        self.pronunciation_path = pronunciation_path

    def spoken_ja_pronunciation_entries(self, ) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.pronunciation_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        raw_entries = payload.get("entries") if isinstance(payload, dict) else []
        if not isinstance(raw_entries, list):
            return []
        entries: list[dict[str, Any]] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            reading = str(raw.get("reading") or "").strip()
            aliases = [
                str(alias).strip()
                for alias in (raw.get("aliases") or [])
                if str(alias or "").strip()
            ]
            if reading and aliases:
                entries.append({"reading": reading, "aliases": aliases})
        return entries


    def spoken_ja_pronunciation_glossary(self, *values: Any) -> list[dict[str, str]]:
        combined = "\n".join(str(value or "") for value in values)
        if not combined:
            return []
        glossary: list[dict[str, str]] = []
        seen_readings: set[str] = set()
        for entry in self.spoken_ja_pronunciation_entries():
            reading = str(entry["reading"])
            matched_alias = next(
                (
                    alias
                    for alias in sorted(entry["aliases"], key=len, reverse=True)
                    if _spoken_ja_alias_present(combined, alias)
                ),
                "",
            )
            if matched_alias and reading not in seen_readings:
                glossary.append({"term": matched_alias, "reading": reading})
                seen_readings.add(reading)
        return glossary


    def normalize_spoken_japanese_terms(self, value: Any) -> str:
        text = chat_presentation.prepare_spoken_japanese_terms(value)
        if not text:
            return ""
        # 词音文件只在确有日语文本时读取，资源路径在每次调用时解析。
        return chat_presentation.apply_spoken_japanese_pronunciations(
            text, self.spoken_ja_pronunciation_entries(),
        )


    def spoken_japanese_pronunciation_needs_repair(self, reply: Any, spoken: Any) -> bool:
        glossary = self.spoken_ja_pronunciation_glossary(reply)
        if not glossary:
            return False
        normalized_spoken = self.normalize_spoken_japanese_terms(spoken)
        return any(item["reading"] not in normalized_spoken for item in glossary)
