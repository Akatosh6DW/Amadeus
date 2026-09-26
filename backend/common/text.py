"""不依赖业务状态的公共文本集合操作。"""
from typing import Any


def unique_strings(items: list[Any]) -> list[str]:
    """转为非空字符串并按首次出现顺序去重，保持原有空白处理规则。"""
    seen = set()
    result: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
