"""向模型投影本轮实际授权及时间快照，不授予权限或执行任何工具。"""
from __future__ import annotations
from typing import Any, Callable

def screen_context_line() -> str:
    return (
        "- 屏幕读取权限：已授予。用户询问当前屏幕内容时调用 capture_screen；"
        "用户明确说上一张、之前那张或前几张截图时，应使用后端提供的截图历史，不要用一次新截图替换旧证据。"
        "不要在普通对话中假装持续监视屏幕。"
    )


class PermissionContext:
    def __init__(self, *, clock: Callable[..., dict[str, Any]], defaults: Callable[[], dict[str, Any]]) -> None:
        self.clock = clock
        self.defaults = defaults

    def permission_context_prompt(self, settings: dict[str, Any], *, body: dict[str, Any] | None = None) -> str:
        lines = ["[权限上下文]"]
        clock = self.clock(body if body is not None else {}, settings)
        if settings.get("permissionReadSystemTime"):
            lines.append(f"- 系统时间权限：已授予。本轮本地时间快照 ISO：{clock['iso']}")
            lines.append(f"- 本轮本地时间可读格式：{clock['display']}")
            lines.append("- 回答当前本地日期/时间可直接使用这份本轮后端快照，无需重复调用工具；它不是外部网络事实，也不是已执行工具的回执。需要回答生成时更晚的读数时可调用 get_system_time，并以实际返回值为准。不得使用历史消息、会话近似时间或模型知识猜测秒数和时区。")
        else:
            lines.append("- 系统时间权限：未授予。不要声称知道当前时间。")
        if settings.get("permissionReadScreen"):
            lines.append(screen_context_line())
        else:
            lines.append("- 屏幕读取权限：未授予。不要声称看到用户屏幕。")
        if settings.get("permissionReadCamera"):
            lines.append(
                "- 摄像头权限：已授予，capture_camera 始终是可选工具。遇到任何必须观察用户此刻状态才能可靠回答的意图时，"
                "你应自行判断并调用它；用户不必说出‘摄像头’。典型情况包括脸色/表情/疲劳、穿着、姿势/手势、手中展示的物品、"
                "用户身后的环境、光线以及是否在镜头前。它只按需拍摄一帧，不要持续拍摄，也不要假装一直看着用户。"
            )
        else:
            lines.append("- 摄像头权限：未授予。不要声称通过摄像头看到用户。")
        if settings.get("permissionWebSearch"):
            providers = ["duckduckgo", "yahoo", "wikipedia"]
            if str(settings.get("webSearchApiKey") or "").strip():
                providers.append("brave")
            lines.append(
                "- 在线搜索权限：已授予。用户要求查证资料或需要最新事实时调用 web_search；"
                "read_web_page 可读取用户给出的、搜索找到的或网页中链接的公网 URL。最终回答用实际页面标题和 URL 引用后端来源；若沿用 [S1] 等标签，不得重排编号。"
            )
            lines.append("- 本轮可选搜索 provider：" + ", ".join(providers)
                         + "；省略 provider 使用配置的搜索入口。duckduckgo 入口会自动尝试可用的备用搜索线路。")
        else:
            lines.append("- 在线搜索权限：未授予。不要声称已经联网搜索或读取网页。")
        workspace = str(settings.get("agentFileWorkspace") or self.defaults()["agentFileWorkspace"])
        if settings.get("permissionFileRead"):
            lines.append(f"- Agent 文件读取权限：已授予；唯一允许访问的工作区是 {workspace}。")
        else:
            lines.append("- Agent 文件读取权限：未授予。不要声称已经查看本机文件或目录。")
        if settings.get("permissionFileWrite"):
            lines.append(
                f"- Agent 文件修改权限：已授予；仅可在 {workspace} 内新建目录、创建、覆盖或追加文本文件。"
                "只有用户本轮明确要求修改文件时才可使用，不能删除、移动或执行文件。"
            )
        else:
            lines.append("- Agent 文件修改权限：未授予。不要声称已经创建或修改本机文件。")
        if settings.get("permissionOpenBrowser"):
            lines.append("- 打开默认浏览器权限：已授予。仅在用户明确要求时调用 open_browser。")
        else:
            lines.append("- 打开默认浏览器权限：未授予。不要声称已经在用户电脑上打开网页。")
        return "\n".join(lines)
