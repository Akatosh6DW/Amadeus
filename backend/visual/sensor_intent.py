"""当前用户语句中的屏幕、相机和附件指向规则；不执行采集或提升权限。"""
from __future__ import annotations
from typing import Any, Mapping, Sequence
import re

from backend.tools.validation import SENSOR_TOOL_NAMES, structured_sensor_authorized

def screen_history_request_mode(text: str) -> str:
    """Classify explicit references to saved screenshots.

    ``history_only`` must not silently take a new screenshot. ``compare_current``
    keeps the old evidence and additionally captures the current screen.
    """

    compact = re.sub(r"\s+", "", str(text or "").lower())
    if not compact:
        return ""
    historical = bool(re.search(
        r"上一张|上张截图|上上张|前一张|前两张|前几张|之前(?:那|的)?(?:张)?(?:截图|截屏|画面)|"
        r"(?:刚才|刚刚)(?:那|的)?(?:张)?(?:截图|截屏|画面|图片)|"
        r"(?:刚才|刚刚).{0,10}(?:截了|截的|保存的).{0,6}(?:截图|截屏|画面|图片)|"
        r"最近(?:的)?(?:两|几|[2-9])张(?:截图|截屏|画面|图片)|"
        r"这些(?:截图|截屏|画面|图片)|截图历史|历史截图|倒数第[一二三四五六123456]张",
        compact,
    ))
    if not historical:
        return ""
    wants_current = bool(re.search(
        r"现在|当前|此刻|这次|这一张|最新画面|新截|再截|重新截|"
        r"(?:和|与|跟).{0,10}(?:上一张|前一张|之前|刚才)|"
        r"(?:上一张|前一张|之前|刚才).{0,10}(?:和|与|跟)",
        compact,
    ))
    return "compare_current" if wants_current else "history_only"


def is_screen_query(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if not normalized:
        return False
    explicit_visual_terms = (
        r"屏幕|截图|截屏|桌面|当前画面|画面上|显示器|前台窗口|"
        r"(?:当前|电脑上的?)(?:页面|网页|界面|窗口)(?:上|里|中|显示|内容|有什么|是什么)|"
        r"(?:页面|网页|界面|窗口)(?:上|里|中|显示|写着|有)|"
        r"screen|screenshot|desktop|currentwindow|what(?:'s|is)onmyscreen"
    )
    contextual_visual_requests = (
        r"看.*屏幕|看到.*屏幕|看一下(?:这里|这儿|屏幕|页面|画面)|"
        r"(?:看看|看下|看一下|看一看)我(?:现在|正在)?(?:在)?(?:做什么|干什么|干嘛|做啥)|"
        r"你(?:能|可以)?(?:看见|看到).{0,10}(?:我(?:现在)?(?:打开|显示|播放|在做)|这里|这儿|页面|画面|屏幕)|"
        r"(?:帮我|你来)?(?:读|读取|识别|检查|分析|看).{0,6}(?:这里|这儿|上面|页面上|画面上).{0,8}(?:文字|内容|写着|显示|是什么|有什么)|"
        r"我(?:现在|正在)(?:打开|看|显示|播放)的?(?:是什么|什么|东西|内容|页面)|"
        r"我(?:现在|正在)看什么|这里(?:写着|显示|有)什么|上面(?:写着|显示)什么"
    )
    return bool(re.search(explicit_visual_terms, normalized) or re.search(contextual_visual_requests, normalized))


def is_camera_query(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if not normalized:
        return False
    if re.search(r"昨天|以前|上次|曾经|假如|如果|想象|一般来说|通常", normalized):
        return False
    explicit_camera = re.search(
        r"摄像头|相机画面|镜头里|打开相机|camera|webcam|"
        r"^(?:你)?(?:看看|看下|看一下|看一看|瞧瞧)我(?:吧|呢|呀|啊)?[？?!！。]*$|"
        r"看看我(?:现在)?(?:长什么样|的样子|的状态|的表情|的脸)|"
        r"你(?:能|可以)?(?:从摄像头)?(?:看见|看到)我吗|"
        r"我(?:现在)?看起来怎么样|看看镜头里的我",
        normalized,
    )
    implicit_current_visual = re.search(
        r"(?:看|看看|瞧|观察|判断|猜猜|认得出|看得出).{0,8}(?:我的?(?:脸|脸色|表情|精神|气色|衣服|穿着|姿势|动作|手势|手里|手中|身后|背后)|脸|脸色|表情|手里|手中|身后|背后)|"
        r"(?:你觉得|你看|帮我判断)我(?:现在|今天|此刻)?.{0,8}(?:精神|气色|脸色|表情|穿着|衣服|姿势|动作).{0,8}(?:怎么样|如何|好不好|对不对|正常吗)|"
        r"我(?:现在|今天|此刻)?(?:是不是|有没有|看起来|显得).{0,10}(?:累|困|精神|开心|难过|生气|脸色|气色|表情|好看|合适|在镜头前|在看你)|"
        r"我(?:现在|此刻)?(?:是|做的)什么(?:脸色|表情|姿势|动作)|"
        r"我(?:现在|今天)?(?:穿|戴|拿|举|做)的?(?:这个|这件|什么).{0,8}(?:怎么样|是什么|好看|合适|看得清|认得出)|"
        r"(?:我)?(?:手里|手中|身后|背后)(?:这个|拿着|有|是).{0,8}(?:什么|怎么样)|"
        r"(?:这个|这件)(?:衣服|东西|动作|手势).{0,8}(?:怎么样|是什么|好看|合适)",
        normalized,
    )
    return bool(explicit_camera or implicit_current_visual)


def is_attachment_followup_query(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    return bool(re.search(
        r"(?:我)?(?:刚刚|刚才|新|最新)(?:发|传|上传|添加)(?:的)?(?:这张|那张|这个|那个)?(?:图|图片|图像|附件|文件)|"
        r"我发的(?:这张|那张|这个|那个)?(?:图|图片|图像|附件|文件)|"
        r"(?:这张|那张|这个|那个)(?:刚发的|刚传的|上传的)(?:图|图片|图像|附件|文件)|"
        r"我说的是.{0,8}(?:图|图片|图像|附件|文件)",
        compact,
    ))


def explicitly_requested_sensor_tools(text: str) -> frozenset[str]:
    """Return live sensors grounded in the current user utterance itself.

    A capability promoted by the model or execution planner is not equivalent
    to user authorization.  This helper deliberately ignores conversation
    history so a previous visual turn cannot authorize another capture when a
    new attachment is the current object.
    """

    compact = re.sub(r"\s+", "", str(text or "").lower())
    requested: set[str] = set()
    explicit_screen_action = bool(re.search(
        r"我的屏幕|当前(?:的)?屏幕|电脑屏幕|"
        r"(?:看看|看下|看一下|观察|检查|读取).{0,8}(?:当前)?(?:桌面|前台窗口|屏幕(?:上|里|内容|画面))|"
        r"(?:截屏|截图|抓屏)(?:一下|看看|并|后|再|当前)?|"
        r"(?:看看|看下|看一下|看一看)我(?:现在|正在)?(?:在)?(?:做什么|干什么|干嘛|做啥)",
        compact,
        re.I,
    ))
    if explicit_screen_action and screen_history_request_mode(text) != "history_only":
        requested.add("capture_screen")
    explicit_camera_action = bool(re.search(
        r"(?:打开|使用|通过|从|看看|看下|看一下).{0,6}(?:摄像头|相机画面|镜头)|"
        r"(?:摄像头|相机画面|镜头里).{0,8}(?:看|观察|拍|我)|"
        r"^(?:你)?(?:看看|看下|看一下|看一看|瞧瞧)我(?:吧|呢|呀|啊)?[？?!！。]*$|"
        r"看看我(?:现在)?(?:长什么样|的样子|的状态|的表情|的脸)|"
        r"我(?:现在)?看起来怎么样",
        compact,
        re.I,
    ))
    if explicit_camera_action:
        requested.add("capture_camera")
    return frozenset(requested)


def constrain_attachment_sensor_requests(
    requests: Sequence[dict[str, Any]],
    user_message: str,
    *,
    has_bound_attachment: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep a newly bound attachment authoritative over unrelated sensors."""

    normalized = [dict(item) for item in requests if isinstance(item, dict)]
    if not has_bound_attachment:
        return normalized, []
    explicitly_requested = explicitly_requested_sensor_tools(user_message)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in normalized:
        name = str(item.get("name") or "").strip()
        if (
            name in SENSOR_TOOL_NAMES
            and name not in explicitly_requested
            and not structured_sensor_authorized(item)
        ):
            rejected.append(item)
        else:
            accepted.append(item)
    return accepted, rejected


def visual_followup_tool_name(user_message: str, resolution: dict[str, Any] | None) -> str:
    """Resolve short deictic follow-ups to the most recent verified visual tool.

    A phrase such as ``这个是什么`` has to observe the current scene again. Reusing
    the assistant's old screenshot description can otherwise turn a ten-hour-old
    answer into apparent fresh visual evidence.
    """

    if is_attachment_followup_query(user_message):
        return ""
    resolution = resolution if isinstance(resolution, dict) else {}
    if not resolution.get("active") or not resolution.get("isContinuation"):
        return ""
    compact = re.sub(r"[\s，。！？?!,.]+", "", str(user_message or "").lower())
    if not re.fullmatch(
        r"(?:这|这个|这个是啥|这个是什么|这是什么|这里是什么|现在呢|现在这个呢|"
        r"现在是什么|现在怎么样|再看看|再看一下|你再看看|你再看一下|你现在再看)",
        compact,
    ):
        return ""
    evidence = resolution.get("trustedToolEvidence")
    evidence = evidence if isinstance(evidence, list) else []
    for item in reversed(evidence):
        value = str(item or "")
        if value.startswith("capture_screen:"):
            return "capture_screen"
        if value.startswith("capture_camera:"):
            return "capture_camera"
    subject = " ".join(
        str(resolution.get(key) or "")
        for key in ("subject", "resolvedQuestion")
    )
    if re.search(r"屏幕|截图|桌面|页面|窗口|画面", subject):
        return "capture_screen"
    if re.search(r"摄像头|相机|镜头|看看我", subject):
        return "capture_camera"
    return ""
