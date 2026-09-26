"""会话摘要后台任务。

摘要工作保留旧的代际校验、模型校验和确定性 artifact fallback。任务注册表
在本模块单例化，append reserve 与 refine finally discard 共用同一集合。
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.context_architecture import (
    CompactionArtifact, canonical_turn_id, microcompact_turns, validate_compaction,
)
from agent.epistemic_architecture import user_claim_provenance
from backend.common.values import safe_nonnegative_int


def compact_turns_artifact(
    turns: list[dict[str, Any]],
    *,
    write_log: Callable[..., None],
) -> CompactionArtifact:
    """Validate the deterministic raw prefix before any summary model call."""

    artifact = microcompact_turns(
        turns,
        user_provenance=user_claim_provenance,
        max_characters=3600,
    )
    if not artifact.valid:
        write_log(
            "conversation_compaction_validation_failed",
            level=artifact.level.value,
            sourceTurnCount=artifact.source_count,
            issues=list(artifact.validation_issues),
        )
        raise ValueError(
            "conversation compaction would lose required context: "
            + ",".join(artifact.validation_issues)
        )
    return artifact


class SummaryJobRegistry:
    """唯一的摘要 in-flight 注册表；不复制 server 的旧 set/lock。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: set[tuple[str, str]] = set()

    def reserve(self, key: tuple[str, str]) -> bool:
        with self.lock:
            if key in self.jobs:
                return False
            self.jobs.add(key)
            return True

    def discard(self, key: tuple[str, str]) -> None:
        with self.lock:
            self.jobs.discard(key)


SUMMARY_JOB_REGISTRY = SummaryJobRegistry()


@dataclass(frozen=True)
class SummaryPorts:
    conversation_lock: Any
    scope_context: contextvars.ContextVar[str]
    normalize_scope: Callable[[Any], str]
    current_epoch: Callable[[], int]
    read_context: Callable[[], dict[str, Any]]
    write_context: Callable[[dict[str, Any]], Any]
    now_iso: Callable[[], str]
    configured: Callable[[dict[str, Any]], bool]
    circuit: Any
    compact_artifact: Callable[[list[dict[str, Any]]], CompactionArtifact]
    send_upstream: Callable[..., str]
    parse_json: Callable[[str], Any]
    write_log: Callable[..., None]
    summary_jobs: SummaryJobRegistry


class ConversationSummaryService:
    def __init__(self, ports: SummaryPorts) -> None:
        self.ports = ports

    def refine(
        self,
        summary_id: str,
        turns: list[dict[str, Any]],
        settings: dict[str, Any],
        storage_scope: str = "normal",
        activation_epoch: int | None = None,
    ) -> None:
        normalized_scope = self.ports.normalize_scope(storage_scope)
        job_key = (normalized_scope, str(summary_id))
        scope_token = self.ports.scope_context.set(self.ports.normalize_scope(storage_scope))
        circuit_key = "conversation_summary_polish:" + self.ports.normalize_scope(storage_scope)
        artifact: CompactionArtifact | None = None
        source_epoch = safe_nonnegative_int(
            activation_epoch
            if activation_epoch is not None
            else turns[0].get("conversationEpoch") if turns else self.ports.current_epoch()
        )

        def commit_summary(
            candidate: CompactionArtifact,
            *,
            source: str,
            model_validated: bool,
        ) -> bool:
            """Atomically publish the handoff and remove its exact raw prefix."""

            with self.ports.conversation_lock:
                context = self.ports.read_context()
                if safe_nonnegative_int(context.get("conversationEpoch")) != source_epoch:
                    return False
                summaries = context.get("summaries", [])
                if not isinstance(summaries, list):
                    return False
                target = next((
                    item for item in summaries
                    if isinstance(item, dict) and item.get("id") == summary_id
                ), None)
                if target is None:
                    return False
                pending_compaction = bool(target.get("pendingCompaction"))
                if pending_compaction and model_validated:
                    recent = [
                        item for item in context.get("recentTurns", [])
                        if isinstance(item, dict)
                    ]
                    prefix_ids = tuple(
                        canonical_turn_id(item, index)
                        for index, item in enumerate(recent[:candidate.source_count])
                    )
                    if prefix_ids != candidate.source_turn_ids:
                        return False
                    context["recentTurns"] = recent[candidate.source_count:]
                target.update({
                    "text": candidate.text,
                    "source": source,
                    "status": "pending" if pending_compaction and not model_validated else "complete",
                    "pendingCompaction": pending_compaction and not model_validated,
                    "polishValidated": bool(model_validated),
                    "summaryPurpose": "dialogue_continuity_only",
                    "longTermMemoryWritten": False,
                    "artifact": candidate.to_dict(),
                })
                pressure = (
                    context.get("contextPressure")
                    if isinstance(context.get("contextPressure"), dict) else {}
                )
                pressure["summaryPending"] = bool(target["pendingCompaction"])
                pressure["retainedTurnCount"] = len(context.get("recentTurns", []))
                pressure["updatedAt"] = self.ports.now_iso()
                context["contextPressure"] = pressure
                self.ports.write_context(context)
                return True

        try:
            artifact = self.ports.compact_artifact(turns)
            if not self.ports.configured(settings):
                commit_summary(
                    artifact,
                    source="local_fallback",
                    model_validated=False,
                )
                return
            if not self.ports.circuit.allow(circuit_key):
                commit_summary(
                    artifact,
                    source="local_fallback",
                    model_validated=False,
                )
                self.ports.write_log(
                    "conversation_summary_polish_circuit_open",
                    summaryId=summary_id,
                    circuit=self.ports.circuit.get(circuit_key).to_dict(),
                )
                return
            correction_texts = [
                re.sub(r"\s+", " ", str(turn.get("user") or "")).strip()[:300]
                for turn in turns
                if isinstance(turn.get("contextProtection"), Mapping)
                and "correction" in (
                    turn.get("contextProtection", {}).get("reasons") or ()
                )
            ]
            source_turns = []
            for index, turn in enumerate(turns):
                turn_id = canonical_turn_id(turn, index)
                source_turns.append({
                    "turnId": turn_id,
                    "user": str(turn.get("user") or ""),
                    "assistant": str(turn.get("assistant") or ""),
                    "contextProtection": turn.get("contextProtection", {}),
                })
            payload: dict[str, Any] = {
                "model": settings["apiModel"],
                "temperature": 0.15,
                "max_tokens": 1500,
                "messages": [
                    {"role": "system", "content": (
                        "你只负责把一段连续的 AMADEUS 旧对话压缩成下一轮聊天可直接接续的中文中期摘要。"
                        "这不是长期记忆提取任务，禁止输出 memoryCandidates，也不得建议写入长期记忆。"
                        "完整阅读按时间排序的 sourceTurns；保留当前话题、人物身份与关系、用户明确事实、"
                        "双方已经达成的共识、情绪变化、承诺、明确纠正、未完成事项和自然接话所需线索；"
                        "删除寒暄、重复措辞、一次性工具过程和临时噪声。不要用关键词或固定句式猜语义，不要编造。"
                        "必须保留说话人归属：assistant/AMADEUS 的猜测、截图描述和旧回复不能压缩成‘用户说过’或用户事实；"
                        "若用户纠正了 AMADEUS 的误判，以纠正后的事实为准并标明此前是 AMADEUS 判断错误。"
                        "没有工具或来源的助手陈述只能作为 assistant_unverified_claim，不得升级成用户事实。"
                        "保留时间与确定程度：过去去过某地不代表现在住在那里，偶尔联系不代表恋爱；"
                        "不得把不同人物或不同阶段合并。continuationHint 仅保留已有待续事项，没有就留空，不新增提问。"
                        "只输出 JSON：{\"summary\":\"...\",\"currentTopic\":\"...\","
                        "\"relationshipContext\":\"...\",\"emotionalContext\":\"...\","
                        "\"continuationHint\":\"...\",\"sourceTurnIds\":[...],"
                        "\"requiredActs\":[...],\"unresolvedItems\":[...],\"corrections\":[...]}。"
                        "sourceTurnIds、requiredActs、unresolvedItems、corrections 四个数组必须逐项照抄输入，"
                        "不得删除或改写；summary 控制在 800 汉字以内，其余字段简短，没有内容就输出空字符串。"
                    )},
                    {"role": "user", "content": json.dumps({
                        "deterministicArtifact": artifact.to_dict(),
                        "corrections": correction_texts,
                        "sourceTurns": source_turns,
                    }, ensure_ascii=False)},
                ],
            }
            if settings.get("useResponseFormat"):
                payload["response_format"] = {"type": "json_object"}
            if "deepseek.com" in settings["apiEndpoint"]:
                payload["thinking"] = {"type": "disabled"}
            content = self.ports.send_upstream(settings, payload, timeout=60)
            parsed = self.ports.parse_json(content) or {}
            summary = str(parsed.get("summary") or "").strip()
            if not summary:
                raise ValueError("summary polish returned empty summary")
            if tuple(str(item) for item in parsed.get("sourceTurnIds", [])) != artifact.source_turn_ids:
                raise ValueError("summary polish changed source turn ids")
            if tuple(str(item) for item in parsed.get("requiredActs", [])) != artifact.required_acts:
                raise ValueError("summary polish changed required acts")
            if tuple(str(item) for item in parsed.get("unresolvedItems", [])) != artifact.unresolved_items:
                raise ValueError("summary polish changed unresolved items")
            returned_corrections = [str(item) for item in parsed.get("corrections", [])]
            if returned_corrections != correction_texts:
                raise ValueError("summary polish changed user corrections")
            header = "\n".join(
                line for line in artifact.text.splitlines()
                if line.startswith("[compaction:")
                or line.startswith("[source_turns]")
                or line.startswith("[required_acts]")
                or line.startswith("[unresolved]")
                or line.startswith("[protection_reasons]")
                or line.startswith("[protected_claim]")
            )
            sections = [
                "[chat_summary][speaker_attribution_preserved] " + summary[:2600],
            ]
            for key, label, limit in (
                ("currentTopic", "current_topic", 320),
                ("relationshipContext", "relationship_context", 520),
                ("emotionalContext", "emotional_context", 320),
                ("continuationHint", "continuation_hint", 520),
            ):
                value = str(parsed.get(key) or "").strip()
                if value:
                    sections.append(f"[{label}] {value[:limit]}")
            candidate_text = header + "\n" + "\n".join(sections)
            candidate = CompactionArtifact(
                version=artifact.version,
                level=artifact.level,
                text=candidate_text,
                source_turn_ids=artifact.source_turn_ids,
                required_acts=artifact.required_acts,
                unresolved_items=artifact.unresolved_items,
                source_count=artifact.source_count,
                original_characters=artifact.original_characters,
                compacted_characters=len(candidate_text),
                protection_reasons=artifact.protection_reasons,
                protected_claims=artifact.protected_claims,
            )
            issues = validate_compaction(candidate, turns)
            if issues:
                raise ValueError("invalid polished summary: " + ",".join(issues))
            committed = commit_summary(candidate, source="api", model_validated=True)
            if committed:
                self.ports.circuit.success(circuit_key)
                self.ports.write_log(
                    "conversation_summary_refined",
                    summaryId=summary_id,
                    sourceTurnCount=len(artifact.source_turn_ids),
                    longTermMemoryWritten=False,
                )
        except Exception as error:
            circuit = self.ports.circuit.failure(circuit_key, error)
            fallback_committed = artifact is not None and commit_summary(
                artifact,
                source="local_fallback",
                model_validated=False,
            )
            self.ports.write_log(
                "conversation_summary_polish_failed",
                summaryId=summary_id,
                errorType=type(error).__name__,
                error=str(error)[:1000],
                circuit=circuit.to_dict(),
                fallback=(
                    "deterministic_artifact_committed"
                    if fallback_committed else "stale_job_discarded"
                ),
            )
        finally:
            self.ports.scope_context.reset(scope_token)
            self.ports.summary_jobs.discard(job_key)
