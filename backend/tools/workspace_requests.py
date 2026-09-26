"""当前用户工作区动作的解析；引用中的动作不能升级为用户授权。

这里只返回请求建议，实际文件范围、权限与执行仍由工具执行器复核。
"""
from __future__ import annotations
from typing import Any, Callable, Mapping
import re

def extract_agent_file_path(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    quoted = re.search(r"[“\"']([^“”\"']{1,500})[”\"']", value)
    if quoted:
        return quoted.group(1).strip()
    windows_path = re.search(
        r"(?<![\w])([A-Za-z]:\\[^<>:\"|?*\r\n]+?)(?=[，。！？；,\s]|$)",
        value,
    )
    if windows_path:
        return windows_path.group(1).strip()
    for relative_path in re.finditer(
        r"[^<>:\"|?*\s，。！？；,'“”]+",
        value,
    ):
        candidate = relative_path.group(0).strip()
        if (
            ("/" in candidate or "\\" in candidate)
            and not candidate.startswith(("/", "\\"))
            and "://" not in candidate
        ):
            return candidate
    filename = re.search(
        r"([^/\s，。！？；,:\\\"']+\.[A-Za-z0-9]{1,12})(?=[，。！？；,\s]|$)",
        value,
    )
    if filename:
        return filename.group(1).strip()
    directory = re.search(
        r"([A-Za-z0-9_.@+-]{1,200})\s*(?:文件夹|目录)",
        value,
        re.I,
    )
    if directory:
        return directory.group(1).strip()
    return ""


def workspace_action_text(
    user_message: str,
    reference_ledger: Mapping[str, Any] | None = None,
) -> str:
    """移除当前消息中明确归属于说话者的引语动作。"""

    text = str(user_message or "")
    if not isinstance(reference_ledger, Mapping):
        return text
    current_message_id = str(
        reference_ledger.get("currentMessageId")
        or reference_ledger.get("current_message_id")
        or ""
    )
    characters = list(text)
    for quote in reference_ledger.get("quotes") or []:
        if not isinstance(quote, Mapping):
            continue
        message_id = str(quote.get("message_id") or quote.get("messageId") or "")
        quoted_speaker = str(
            quote.get("quoted_speaker") or quote.get("quotedSpeaker") or "unknown"
        )
        if message_id != current_message_id or quoted_speaker == "unknown":
            continue
        span = quote.get("span") if isinstance(quote.get("span"), Mapping) else {}
        try:
            start = max(0, int(span.get("start") or 0))
            end = min(len(characters), max(start, int(span.get("end") or start)))
        except (TypeError, ValueError, OverflowError):
            continue
        for index in range(start, end):
            characters[index] = " "
    return "".join(characters)


class WorkspaceRequests:
    def __init__(self, *, is_open_browser: Callable[[str], bool]) -> None:
        self.is_open_browser = is_open_browser

    def is_agent_file_action_query(self, text: str) -> bool:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if (
            self.is_open_browser(value)
            and not re.search(r"文件|文本|目录|文件夹|工作区|[A-Za-z]:\\", value, re.I)
        ):
            return False
        return bool(re.search(
            r"(?:读取|查看|列出|浏览|打开|新建|创建|写入|保存|修改|编辑|追加|覆盖)"
            r".{0,32}(?:文件|文本|目录|文件夹|工作区|[A-Za-z]:\\|[\w.-]+\.[A-Za-z0-9]{1,10})|"
            r"(?:文件|文本|目录|文件夹|工作区|[A-Za-z]:\\|[\w.-]+\.[A-Za-z0-9]{1,10})"
            r".{0,24}(?:读取|查看|列出|打开|新建|创建|写入|保存|修改|编辑|追加|覆盖)",
            value,
            re.I,
        ))


    def is_agent_action_query(self, text: str) -> bool:
        return self.is_open_browser(text) or self.is_agent_file_action_query(text)


    def infer_workspace_current_state_request(
        self,
        user_message: str,
        reference_ledger: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """复用既有文件动作语法，返回精确的只读当前状态请求。"""

        actionable = workspace_action_text(user_message, reference_ledger)
        if not self.is_agent_file_action_query(actionable):
            return None
        requested_path = extract_agent_file_path(actionable)
        if re.search(r"列出|目录内容|有哪些文件|文件列表|浏览目录|查看目录", actionable, re.I):
            return {
                "name": "list_directory",
                "args": {
                    "path": requested_path or ".",
                    "recursive": bool(re.search(r"递归|所有子目录", actionable)),
                },
                "reason": "user explicitly asked for current Agent workspace contents",
            }
        if requested_path and re.search(r"读取|查看|打开|读一下|内容", actionable, re.I):
            return {
                "name": "read_text_file",
                "args": {"path": requested_path},
                "reason": "user explicitly asked for current Agent workspace file contents",
            }
        return None
