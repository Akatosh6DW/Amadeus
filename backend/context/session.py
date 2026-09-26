"""会话时间边界、低频时间快照和重启后承接提示。

本模块只拥有会话内的时间快照、调用次数和同步锁。启动时间、历史读取、
语义承接判断及实际时钟都由入口在每次调用时显式传入，避免复制 server 的
全局状态，也让未授予时间权限的路径可以完全不读取时钟或历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import threading
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class SessionTemporalClock:
    """会话提示所需的两个时间读口；入口负责按需晚绑定真实时钟。"""

    now_local: Callable[[], datetime]
    time: Callable[[], float]


def parse_local_datetime(value: Any, *, session_started_local: datetime) -> datetime | None:
    """解析历史时间；无时区值沿用本次进程启动时区。"""

    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=session_started_local.tzinfo)
    return parsed.astimezone()


def human_time_gap(seconds: float) -> str:
    """将重启前后的间隔格式化为原有中文提示。"""

    total_minutes = max(0, int(seconds // 60))
    if total_minutes < 2:
        return "不到两分钟"
    if total_minutes < 60:
        return f"约 {total_minutes} 分钟"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 48:
        return f"约 {hours} 小时" + (f" {minutes} 分钟" if minutes >= 10 else "")
    days, remainder = divmod(hours, 24)
    return f"约 {days} 天" + (f" {remainder} 小时" if remainder else "")


class SessionContextService:
    """持有会话时间快照与计数，读取外部资源的职责仍由入口提供。"""

    def __init__(self, initial_snapshot: datetime) -> None:
        self._temporal_context_lock = threading.Lock()
        self._temporal_snapshot = initial_snapshot
        self._temporal_snapshot_turns = 0

    def current_runtime_has_user_turn(
        self,
        recovered_turns: list[dict[str, Any]] | None = None,
        *,
        session_started_local: datetime,
        conversation_recent_limit: int,
        recoverable_recent_conversation_turns: Callable[[int], tuple[list[dict[str, Any]], str]],
        parse_datetime: Callable[[Any], datetime | None] | None = None,
    ) -> bool:
        """判断当前进程启动后是否已有带用户正文的可恢复轮次。"""

        turns = recovered_turns
        if turns is None:
            turns, _ = recoverable_recent_conversation_turns(conversation_recent_limit)
        parse = parse_datetime or (
            lambda value: parse_local_datetime(
                value,
                session_started_local=session_started_local,
            )
        )
        return any(
            (candidate := parse(turn.get("at"))) is not None
            and candidate > session_started_local
            and bool(str(turn.get("user") or "").strip())
            for turn in turns
            if isinstance(turn, dict)
        )

    def session_temporal_context_prompt(
        self,
        current_user_message: str = "",
        *,
        settings: Mapping[str, Any] | None = None,
        session_started_local: datetime,
        server_started_at: float,
        refresh_seconds: float,
        refresh_turns: int,
        conversation_recent_limit: int,
        recoverable_recent_conversation_turns: Callable[[int], tuple[list[dict[str, Any]], str]],
        message_needs_recent_context: Callable[[str], bool],
        clock: SessionTemporalClock,
        parse_datetime: Callable[[Any], datetime | None] | None = None,
    ) -> str:
        """提供低频本机时间快照，并明确本次后端重启边界。"""

        # 权限拒绝必须在任何时钟、历史和快照状态操作之前返回。
        if settings is not None and not settings.get("permissionReadSystemTime"):
            return "[会话边界] 重启前的记录不等于刚刚发生；系统时间权限未授予，不提供当前时间或相对时间推算。"
        actual_now = clock.now_local()
        with self._temporal_context_lock:
            self._temporal_snapshot_turns += 1
            age_seconds = max(0.0, (actual_now - self._temporal_snapshot).total_seconds())
            if (
                age_seconds >= refresh_seconds
                or self._temporal_snapshot_turns >= refresh_turns
            ):
                self._temporal_snapshot = actual_now
                self._temporal_snapshot_turns = 0
            snapshot = self._temporal_snapshot

        previous_activity: datetime | None = None
        recovered_turns, _ = recoverable_recent_conversation_turns(
            conversation_recent_limit
        )
        current_session_has_user_turn = self.current_runtime_has_user_turn(
            recovered_turns,
            session_started_local=session_started_local,
            conversation_recent_limit=conversation_recent_limit,
            recoverable_recent_conversation_turns=recoverable_recent_conversation_turns,
            parse_datetime=parse_datetime,
        )
        parse = parse_datetime or (
            lambda value: parse_local_datetime(
                value,
                session_started_local=session_started_local,
            )
        )
        for turn in reversed(recovered_turns):
            if not isinstance(turn, dict):
                continue
            candidate = parse(turn.get("at"))
            if candidate and candidate <= session_started_local:
                previous_activity = candidate
                break

        uptime = max(0.0, clock.time() - server_started_at)
        approximate_now = snapshot + timedelta(seconds=max(0.0, uptime - (
            snapshot - session_started_local
        ).total_seconds()))
        lines = [
            "[本次运行会话的时间基准]",
            f"本次 AMADEUS 后端启动：{session_started_local.isoformat(timespec='minutes')}",
            f"最近一次本机时间快照：{snapshot.isoformat(timespec='minutes')}",
            f"按本次运行时长推算的当前时间：{approximate_now.isoformat(timespec='minutes')}",
            "这是一次新的应用运行会话。重启前的最后一句不是刚刚发生；涉及昨晚、今天、刚才、睡觉或作息时，必须先按时间间隔判断。",
        ]
        if previous_activity:
            gap = (session_started_local - previous_activity).total_seconds()
            lines.extend([
                f"重启前最后记录：{previous_activity.isoformat(timespec='minutes')}",
                f"该记录到本次启动相隔：{human_time_gap(gap)}",
            ])
        if not current_session_has_user_turn and message_needs_recent_context(current_user_message):
            lines.extend([
                "当前是重启后用户发来的第一轮消息，但当前措辞明确要求继续或指向先前内容；可以使用重启前记录完成承接。",
                "即使承接旧内容，也不要把重启本身或话题间隔写成固定吐槽，除非用户正在讨论这个间隔。",
            ])
        elif not current_session_has_user_turn:
            lines.extend([
                "当前是重启后用户发来的第一轮消息。重启前的对话只能作为可回忆的背景，不是紧挨着当前消息发生的上一轮。",
                "用户在这里另开话题是正常的新会话开场：直接回应当前内容，不要评价它与重启前话题跨度大，也不要用‘怎么突然从 A 跳到 B’一类转场吐槽。",
            ])
        lines.append(
            "不要每句都复述时间；只有当前话题与日期、时段、间隔或作息有关时才自然使用这个基准。"
        )
        return "\n".join(lines)


__all__ = [
    "SessionContextService",
    "SessionTemporalClock",
    "human_time_gap",
    "parse_local_datetime",
]
