"""配置 JSON 的文件、环境回退和密钥更新规则。

规范化和路径业务规则仍由调用入口持有；这里仅处理配置记录的输入输出。
所有外部依赖都通过参数传入，便于测试路径、环境和保存后的记忆文件行为。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
from pathlib import Path
from typing import Any


SettingsMapping = Mapping[str, Any]
Settings = dict[str, Any]
Normalize = Callable[[Settings], Settings]

_SECRET_KEYS = {"apiKey", "webSearchApiKey", "visionApiKey"}


def load_settings_record(
    path: Path,
    *,
    environment: Mapping[str, str],
    normalize: Normalize,
) -> Settings:
    """读取配置文件，按文件值、环境变量、规范化默认值的顺序合并。"""

    settings: Settings = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                settings.update(raw)
        except (OSError, json.JSONDecodeError):
            # 配置损坏时沿用环境和规范化默认值，不覆盖原文件。
            pass

    env_endpoint = str(environment.get("AMADEUS_API_ENDPOINT", "") or "").strip()
    env_model = str(environment.get("AMADEUS_MODEL", "") or "").strip()
    env_key = str(environment.get("AMADEUS_API_KEY", "") or "").strip()
    if env_endpoint and not settings.get("apiEndpoint"):
        settings["apiEndpoint"] = env_endpoint
    if env_model and not settings.get("apiModel"):
        settings["apiModel"] = env_model
    if env_key and not settings.get("apiKey"):
        settings["apiKey"] = env_key
    if not settings.get("visionApiKey"):
        settings["visionApiKey"] = str(
            environment.get("AMADEUS_VISION_API_KEY", "") or ""
        ).strip()
    return normalize(settings)


def save_settings_record(
    update: SettingsMapping,
    *,
    path: Path,
    ensure_data_dir: Callable[[], None],
    load_current: Callable[[], Settings],
    normalize: Normalize,
    ensure_memory_file: Callable[[Settings], Any],
    default_keys: Iterable[str],
    release_mode: bool,
) -> Settings:
    """合并并保存配置，保持密钥 KEEP/CLEAR 和 release 过滤语义。"""

    # 原入口先确保目录，再读取当前配置；写入后才初始化记忆文件。
    ensure_data_dir()
    effective_update: SettingsMapping = update
    if release_mode:
        effective_update = {
            key: value
            for key, value in update.items()
            if key not in {"memoryFile", "debugDirectSpeech"}
        }
    current = load_current()
    merged = dict(current)
    for key in default_keys:
        if key not in effective_update:
            continue
        if key in _SECRET_KEYS:
            key_update = str(effective_update.get(key) or "")
            if key_update in {"", "__KEEP__"}:
                continue
            if key_update == "__CLEAR__":
                merged[key] = ""
                continue
        merged[key] = effective_update[key]
    merged = normalize(merged)
    path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ensure_memory_file(merged)
    return merged


__all__ = ["load_settings_record", "save_settings_record"]
