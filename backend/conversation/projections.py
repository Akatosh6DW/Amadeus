"""认知提交 outbox 的会话文件投影边界。

真正的 outbox/schema/lease 规则仍由 ``agent.cognitive_commit`` 持有；本模块
只组织会话 context/archive 投影和恢复顺序，并通过端口接入 memory、relationship
等已有领域 projector，不创建第二套提交或重试状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class ConversationProjectionPorts:
    """outbox 执行器、会话锁和领域 projector 的窄端口。"""

    lock: Any
    open_state_database: Callable[[], Any]
    project_pending: Callable[..., Any]
    read_context: Callable[[], dict[str, Any]]
    write_context: Callable[[dict[str, Any]], dict[str, Any]]
    append_archive: Callable[..., Any]
    memory_projector: Callable[..., Any]
    relationship_projector: Callable[[Mapping[str, Any]], Any]
    write_log: Callable[..., Any]


class ConversationProjectionService:
    """Owns projection orchestration while each domain owns its actual state."""

    def __init__(self, ports: ConversationProjectionPorts) -> None:
        self.ports = ports

    def run(
        self,
        commit_id: str,
        projectors: Mapping[str, Any],
        *,
        kinds: Sequence[str],
        raise_on_error: bool = True,
    ) -> None:
        """Run a bounded set of commit projections and preserve retryable failures."""

        report = self.ports.project_pending(
            self.ports.open_state_database,
            {
                str(kind): projector
                for kind, projector in projectors.items()
                if callable(projector)
            },
            commit_id=str(commit_id or ""),
            kinds=kinds,
            raise_on_error=raise_on_error,
        )
        for failure in report.failed:
            self.ports.write_log(
                "cognitive_projection_deferred",
                commitId=commit_id,
                kind=failure.kind,
                error=failure.error,
            )

    def project_context(self, payload: Mapping[str, Any]) -> None:
        """Retry a committed projection without resurrecting a cleared/newer session."""

        candidate = dict(payload.get("context") or {})
        if not candidate:
            return
        with self.ports.lock:
            current = self.ports.read_context()
            if (
                candidate.get("conversationId") != current.get("conversationId")
                or candidate.get("conversationEpoch") != current.get("conversationEpoch")
                or int(candidate.get("archiveCount") or 0)
                < int(current.get("archiveCount") or 0)
            ):
                return
            self.ports.write_context(candidate)

    def recover(self, settings: dict[str, Any]) -> None:
        """Drain committed work before freezing another model context (also after restart)."""

        with self.ports.lock:
            # Memory/relationship writes retain their existing commit semantics.
            # Conversation files are derived copies; readers can use the durable
            # pending payload if these copies remain unavailable.
            report = self.ports.project_pending(
                self.ports.open_state_database,
                {
                    "memory_records": lambda payload: self.ports.memory_projector(
                        payload,
                        settings=settings,
                    ),
                    "relationship_state": self.ports.relationship_projector,
                },
                kinds=("memory_records", "relationship_state"),
                limit=32,
                raise_on_error=True,
            )
            copies = self.ports.project_pending(
                self.ports.open_state_database,
                {
                    "conversation_archive": lambda payload: self.ports.append_archive(
                        dict(payload.get("turn") or {}),
                        write_turn_record=False,
                    ),
                    "conversation_context": self.project_context,
                },
                kinds=("conversation_archive", "conversation_context"),
                limit=32,
                raise_on_error=False,
            )
            for failure in copies.failed:
                self.ports.write_log(
                    "cognitive_projection_deferred",
                    kind=failure.kind,
                    error=failure.error,
                )
        if report.applied:
            self.ports.write_log(
                "cognitive_projections_recovered",
                count=len(report.applied),
            )
