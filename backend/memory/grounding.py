"""长期记忆候选的确定性提取与逐字证据校验。

本模块只负责候选结构的规则处理：模型提出的记录先做字段归一化，
再要求 sourceSpan、object 和用户原话互相可验证。它不读写记忆文件，
也不决定是否调用模型；持久化与模型修复分别归其它 memory 服务。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any, Callable, Collection, Mapping, Sequence


@dataclass(frozen=True)
class MemoryGroundingPorts:
    """候选规范化和时间端口；规则本身不反向依赖 server。"""

    normalize_candidate: Callable[[Any], dict[str, Any] | None]
    candidate_types: Callable[[], Collection[str]]
    normalize_slot: Callable[[Any], str]
    now: Callable[[], datetime]


class MemoryGroundingService:
    """拥有记忆候选的纯提取、话语断言判断和逐字证据 gate。"""

    def __init__(self, ports: MemoryGroundingPorts) -> None:
        self.ports = ports

    @staticmethod
    def clause_is_asserted(clause: str, *, explicit_request: bool = False) -> bool:
        """Classify discourse force before extracting any fact from a clause."""

        compact = re.sub(r"\s+", "", str(clause or ""))
        if not compact:
            return False
        if explicit_request:
            return True
        if "?" in compact or "？" in compact:
            return False
        if compact.startswith(("如果", "假如", "假设", "要是", "万一")):
            return False
        if compact.startswith(("别人说", "有人说", "他说", "她说", "它说", "听说")):
            return False
        if re.search(r"(?:会不会|是否|是不是|什么|哪(?:个|种|里)|谁|为何|为什么)$", compact):
            return False
        return True

    @staticmethod
    def _candidate(
        content: str,
        *,
        source_span: str,
        predicate: str,
        object_value: str,
        memory_type: str = "user_memory",
        polarity: str = "positive",
        modality: str = "asserted",
        temporal: str = "current",
        importance: float = 0.9,
        valid_until: str = "",
    ) -> dict[str, Any]:
        return {
            "content": content,
            "type": memory_type,
            "importance": importance,
            "shouldSave": True,
            "subject": "user",
            "predicate": predicate,
            "object": object_value,
            "polarity": polarity,
            "modality": modality,
            "temporal": temporal,
            "sourceSpan": source_span,
            "validUntil": valid_until,
            "userPinned": False,
        }

    def extract_candidates(self, text: str) -> list[dict[str, Any]]:
        """Extract a small typed fact set after discourse-level validation.

        Patterns identify candidate slots only. Questions, hypotheticals,
        reported speech and transient observations are rejected by the
        surrounding clause classifier before a candidate can be produced.
        """

        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return []
        candidates: list[dict[str, Any]] = []
        clauses = [
            item.strip()
            for item in re.split(r"(?<=[。！？?!\n])|[；;]", value)
            if item.strip()
        ] or [value]
        for clause in clauses:
            explicit = re.match(
                r"^(?:请)?(?:帮我)?(?:记住|记下来)(?:[:：]\s*|\s*)(.{2,160})",
                clause,
            )
            if explicit:
                fact = explicit.group(1).strip(" ，,:：。！？?!")
                if fact:
                    transient_request = bool(re.search(
                        r"正在|当前|此刻|刚才|今天|这次|屏幕|截图|摄像头|在看|开着",
                        fact,
                    ))
                    candidates.append(self._candidate(
                        "用户要求记住：" + fact,
                        source_span=explicit.group(0).strip(),
                        predicate="explicit_memory_request",
                        object_value=fact,
                        modality="requested",
                        temporal="current",
                        importance=1.0,
                        memory_type="episode" if transient_request else "user_memory",
                        valid_until=(
                            (self.ports.now() + timedelta(hours=24)).isoformat(
                                timespec="seconds"
                            )
                            if transient_request else ""
                        ),
                    ))
                # Do not also infer a second implicit fact from an explicit
                # request; the requested record preserves user authorization.
                continue
            if not self.clause_is_asserted(clause):
                continue
            transient = bool(re.search(
                r"正在|当前|此刻|刚才|今天|这次|屏幕|截图|摄像头|在看|开着",
                clause,
            ))
            if transient:
                continue
            corrected_preference = re.search(
                r"我以前(?:很)?喜欢\s*([^，。！？?!]{1,60})[，,但不过]*现在(?:已经)?不喜欢(?:它|这个|那个)?了?",
                clause,
            )
            if corrected_preference:
                object_value = corrected_preference.group(1).strip(" ，,:：。！？?!")
                candidates.append(self._candidate(
                    "用户现在不喜欢" + object_value,
                    source_span=corrected_preference.group(0).strip(),
                    predicate="preference",
                    object_value=object_value,
                    polarity="negative",
                    modality="corrected",
                ))
                continue
            preference = re.search(
                r"(?:^|[，,。！？?!]\s*)我(?:现在|一直|很|非常)?\s*(喜欢|讨厌|不喜欢)\s*([^，。！？?!]{1,60})",
                clause,
            )
            if preference:
                verb = preference.group(1)
                object_value = preference.group(2).strip(" ，,:：。！？?!")
                if object_value:
                    candidates.append(self._candidate(
                        "用户" + verb + object_value,
                        source_span=preference.group(0).strip(" ，,。！？?!"),
                        predicate="preference",
                        object_value=object_value,
                        polarity="negative" if verb in {"讨厌", "不喜欢"} else "positive",
                        temporal="habitual",
                    ))
                continue
            profile = re.search(
                r"(?:^|[，,。！？?!]\s*)我的\s*([^，。！？?!]{1,24})\s*是\s*([^，。！？?!]{1,100})",
                clause,
            )
            if profile:
                predicate = profile.group(1).strip()
                object_value = profile.group(2).strip()
                if predicate and object_value:
                    candidates.append(self._candidate(
                        f"用户的{predicate}是{object_value}",
                        source_span=profile.group(0).strip(" ，,。！？?!"),
                        predicate=predicate,
                        object_value=object_value,
                    ))
                continue
            future = re.search(r"(?:^|[，,。！？?!]\s*)以后(?:请|要)?\s*(.{2,120})", clause)
            if future:
                object_value = future.group(1).strip(" ，,:：。！？?!")
                object_value = re.sub(r"^我", "", object_value).strip()
                durable_routine = bool(re.search(
                    r"每天|每周|每月|每次|总是|都要|不要|记得|称呼|回复|回答",
                    object_value,
                ))
                if durable_routine:
                    candidates.append(self._candidate(
                        "用户希望以后" + object_value,
                        source_span=future.group(0).strip(" ，,。！？?!"),
                        predicate="future_routine",
                        object_value=object_value,
                        temporal="future",
                    ))
        deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for candidate in candidates:
            key = (
                str(candidate.get("predicate") or ""),
                str(candidate.get("object") or ""),
                str(candidate.get("polarity") or ""),
            )
            deduplicated[key] = candidate
        return list(deduplicated.values())[:4]

    def extract_memories(self, text: str) -> list[str]:
        """Legacy string projection of the structured deterministic extractor."""

        return [item["content"] for item in self.extract_candidates(text)][:4]

    def grounded_candidates(
        self,
        updates: Sequence[Any],
        user_message: str,
        *,
        limit: int = 4,
        diagnostics: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Accept only candidates whose slot and value have user evidence."""

        evidence = str(user_message or "")
        grounded: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(updates):
            candidate = self.ports.normalize_candidate(raw)
            prefix = f"candidate_{index}:"
            if candidate is None:
                if diagnostics is not None:
                    diagnostics.append(prefix + "invalid_structure")
                continue
            if not candidate["shouldSave"]:
                if diagnostics is not None:
                    diagnostics.append(prefix + "should_save_false")
                continue
            if candidate.get("type") not in self.ports.candidate_types():
                if diagnostics is not None:
                    diagnostics.append(prefix + "invalid_type")
                continue
            object_value = self.ports.normalize_slot(candidate.get("object"))
            source_span = str(candidate.get("sourceSpan") or "").strip()
            if not source_span:
                source_span = next((
                    str(item.get("quote") or "").strip()
                    for item in candidate.get("evidence", [])
                    if isinstance(item, Mapping)
                    and str(item.get("quote") or "").strip() in evidence
                ), "")
            rejection_reasons: list[str] = []
            if not source_span:
                rejection_reasons.append("missing_source_span")
            elif source_span not in evidence:
                rejection_reasons.append("source_span_not_in_user_message")
            elif not self.clause_is_asserted(
                source_span,
                explicit_request=candidate.get("modality") == "requested",
            ):
                rejection_reasons.append("source_span_not_asserted")
            if not object_value:
                rejection_reasons.append("missing_object")
            elif object_value not in self.ports.normalize_slot(source_span):
                rejection_reasons.append("object_not_in_source_span")
            if rejection_reasons:
                if diagnostics is not None:
                    diagnostics.extend(prefix + reason for reason in rejection_reasons)
                continue
            merged = dict(candidate)
            merged["sourceSpan"] = source_span
            merged_key = self.ports.normalize_slot(merged.get("content"))
            if merged_key and merged_key not in seen:
                seen.add(merged_key)
                grounded.append(merged)
        return grounded[:max(0, min(32, int(limit)))]

    def validated_summary_candidates(
        self,
        value: Any,
        turns: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """只接受能逐字回指用户原话的摘要记忆候选。"""

        if not isinstance(value, list):
            return []

        def turn_identity(turn: Mapping[str, Any], index: int) -> str:
            return str(
                turn.get("requestId")
                or turn.get("turnId")
                or turn.get("id")
                or f"turn_{index + 1}"
            )[:120]

        def evidence_text(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()

        user_turns: dict[str, str] = {}
        correction_turn_ids: set[str] = set()
        for index, turn in enumerate(turns):
            if not isinstance(turn, Mapping):
                continue
            turn_id = turn_identity(turn, index)
            user_text = evidence_text(turn.get("user"))
            if user_text:
                user_turns[turn_id] = user_text
            protection = (
                turn.get("contextProtection")
                if isinstance(turn.get("contextProtection"), Mapping)
                else {}
            )
            reasons = protection.get("reasons")
            if isinstance(reasons, (list, tuple)) and "correction" in reasons:
                correction_turn_ids.add(turn_id)

        accepted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in value[:8]:
            candidate = self.ports.normalize_candidate(raw)
            if (
                candidate is None
                or not candidate["shouldSave"]
                or candidate["importance"] < 0.68
                or candidate["type"] not in self.ports.candidate_types()
            ):
                continue
            evidence = candidate.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                continue
            source_ids: list[str] = []
            exact_quotes: list[str] = []
            valid = True
            for item in evidence:
                if not isinstance(item, Mapping):
                    valid = False
                    break
                turn_id = str(item.get("turnId") or "").strip()
                quote = evidence_text(item.get("quote"))
                source_text = user_turns.get(turn_id, "")
                if not turn_id or not quote or quote not in source_text:
                    valid = False
                    break
                source_ids.append(turn_id)
                exact_quotes.append(quote)
            if not valid:
                continue
            declared_source_ids = [
                str(item).strip()
                for item in candidate.get("sourceTurnIds", ())
                if str(item).strip()
            ]
            if declared_source_ids and set(declared_source_ids) != set(source_ids):
                continue
            if (
                candidate["type"] == "correction"
                and not correction_turn_ids.intersection(source_ids)
            ):
                # 摘要模型不能把普通对比或不赞同升级成纠正；话语行为在写入时已定型。
                continue
            normalized = self.ports.normalize_slot(candidate["content"])
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidate["sourceTurnIds"] = list(dict.fromkeys(source_ids))
            candidate["sourceSpan"] = " | ".join(dict.fromkeys(exact_quotes))[:600]
            accepted.append(candidate)
        return accepted[:6]

    def grounded_updates(
        self,
        updates: list[Any],
        user_message: str,
        *,
        limit: int = 4,
    ) -> list[str]:
        return [
            item["content"]
            for item in self.grounded_candidates(updates, user_message, limit=limit)
        ]
