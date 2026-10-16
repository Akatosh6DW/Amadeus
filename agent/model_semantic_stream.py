"""Provider-tolerant semantic stream parser.

The model describes semantic events only.  The backend owns transport headers,
sequence numbers and trailers, so a harmless formatting variation cannot break
an otherwise valid answer.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from agent.cognitive_brain import normalize_brain_container
from agent.participant_realization import realize_participant_speech


class ModelSemanticStreamParser:
    """Extract complete JSON values across arbitrary provider chunk boundaries.

    Accepted inputs:
    - semantic V3 records (``kind=speech_unit/turn_end/tool_use/clarification``)
    - semantic V2 objects (``kind=utterance/tool_use/clarification/final_answer``)
    - legacy transport events (``type``/``payload``), with transport metadata ignored
    - an atomic reply object used by the non-stream path

    Output intentionally keeps the current client event shape so the renderer,
    TTS queue and Live2D pipeline do not need a simultaneous migration.
    """

    def __init__(
        self,
        *,
        maximum_buffer_chars: int = 1_000_000,
        maximum_semantic_units: int = 6,
    ) -> None:
        self.buffer = ""
        self.maximum_buffer_chars = maximum_buffer_chars
        self.maximum_semantic_units = max(1, int(maximum_semantic_units))
        self.next_sequence = 0
        self.errors: list[str] = []
        self.accepted_event_count = 0
        self.rejected_event_count = 0
        self._header_emitted = False
        self._trailer_emitted = False
        self._utterance_count = 0
        self._last_utterance: dict[str, Any] = {}
        self._completed_acts: list[str] = []
        self._schedule_intent: dict[str, Any] = {}
        self._cognitive: dict[str, Any] = {}
        self._fatal_protocol_error = False
        self.protocol_version = "semantic-v3"

    def _emit(self, event_type: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "type": event_type,
            "sequence": self.next_sequence,
            "payload": dict(payload or {}),
        }
        self.next_sequence += 1
        self.accepted_event_count += 1
        return event

    def _ensure_header(self, source: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._header_emitted:
            return []
        data = source or {}
        header = {
            "streamProtocol": self.protocol_version,
            "turnEmotion": data.get("emotion") or data.get("turnEmotion") or "neutral",
            "emotionIntensity": data.get("emotionIntensity", data.get("intensity", 0.55)),
        }
        self._header_emitted = True
        return [self._emit("turn_header", header)]

    @staticmethod
    def _utterance_payload(value: Mapping[str, Any]) -> dict[str, Any] | None:
        payload = dict(value.get("payload") or {}) if isinstance(value.get("payload"), Mapping) else dict(value)
        zh = str(
            payload.get("textZh")
            or payload.get("zh")
            or payload.get("reply")
            or payload.get("text")
            or ""
        ).strip()
        ja = str(
            payload.get("spokenTextJa")
            or payload.get("ja")
            or payload.get("spokenJa")
            or ""
        ).strip()
        if not zh:
            return None
        normalized = dict(payload)
        normalized["textZh"] = zh
        # Missing Japanese is a semantic incompleteness, not a transport
        # failure.  The existing normalizer/repair path can fill it later.
        if ja:
            normalized["spokenTextJa"] = ja
        raw_emotion = str(payload.get("emotion") or "").strip()
        if raw_emotion:
            normalized["emotion"] = raw_emotion
            normalized["emotionMissingFromModel"] = False
        else:
            # Preserve absence as data.  The backend can semantically annotate
            # the frozen turn once; converting absence to authoritative neutral
            # here would flatten both Live2D and TTS before recovery can run.
            normalized.pop("emotion", None)
            normalized["emotionMissingFromModel"] = True
        if payload.get("emotionIntensity") is not None or payload.get("intensity") is not None:
            normalized["emotionIntensity"] = payload.get(
                "emotionIntensity", payload.get("intensity")
            )
        boundary = str(payload.get("boundaryAfter") or "continue").strip().lower()
        normalized["boundaryAfter"] = (
            boundary
            if boundary in {"continue", "micro_pause", "phrase_pause", "turn_pause"}
            else "continue"
        )
        return realize_participant_speech(normalized)

    def _emit_utterance(self, value: Mapping[str, Any]) -> list[dict[str, Any]]:
        if isinstance(value.get("scheduleIntent"), Mapping):
            self._schedule_intent = dict(value["scheduleIntent"])
        if self._utterance_count >= self.maximum_semantic_units:
            self.errors.append("semantic_unit_limit_exceeded")
            self.rejected_event_count += 1
            self._fatal_protocol_error = True
            return []
        payload = self._utterance_payload(value)
        if payload is None:
            self.errors.append("semantic_utterance_missing_text")
            self.rejected_event_count += 1
            self._fatal_protocol_error = True
            return []
        events = self._ensure_header(payload)
        events.append(self._emit("utterance", payload))
        self._utterance_count += 1
        self._last_utterance = payload
        return events

    def _emit_trailer(self, value: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._trailer_emitted or not self._utterance_count or self._fatal_protocol_error:
            return []
        data = dict(value or {})
        raw_knowledge_confidence = str(
            data.get("knowledgeConfidence")
            or data.get("knowledge_confidence")
            or ""
        ).strip().lower()
        knowledge_confidence_provided = raw_knowledge_confidence in {
            "high", "medium", "low",
        }
        memory_updates_provided = "memoryUpdates" in data
        raw_memory_action = str(data.get("memoryAction") or "").strip().lower()
        memory_action_provided = raw_memory_action in {"none", "save"}
        relation_event_provided = "relationEvent" in data
        requires_user_response_provided = isinstance(
            data.get("requiresUserResponse"), bool
        )
        payload = {
            "finalEmotion": data.get("finalEmotion") or self._last_utterance.get("emotion") or "neutral",
            "finalEmotionIntensity": data.get(
                "finalEmotionIntensity", self._last_utterance.get("emotionIntensity", 0.55)
            ),
            "emotionDelta": data.get("emotionDelta", 0.0),
            "coldShoulder": bool(data.get("coldShoulder", False)),
            "memoryUpdates": data.get("memoryUpdates") if isinstance(data.get("memoryUpdates"), list) else [],
            "memoryUpdatesProvided": memory_updates_provided,
            "memoryAction": raw_memory_action if memory_action_provided else "none",
            "memoryActionProvided": memory_action_provided,
            "relationEvent": (
                dict(data["relationEvent"])
                if isinstance(data.get("relationEvent"), Mapping)
                else {}
            ),
            "relationEventProvided": relation_event_provided,
            "knowledgeConfidence": (
                raw_knowledge_confidence
                if knowledge_confidence_provided
                else "medium"
            ),
            "knowledgeConfidenceProvided": knowledge_confidence_provided,
            "researchNeeded": False,
            "requiresUserResponse": bool(data.get("requiresUserResponse", False)),
            "requiresUserResponseProvided": requires_user_response_provided,
            "expectedResponseType": str(data.get("expectedResponseType") or ""),
            "proactiveDirective": str(data.get("proactiveDirective") or ""),
            "toolRequests": [],
            "sources": data.get("sources") if isinstance(data.get("sources"), list) else [],
            "completedActs": data.get("completedActs") if isinstance(data.get("completedActs"), list) else self._completed_acts,
            "standalone": bool(data.get("standalone", True)),
            "dependsOnUndeliveredPrefix": False,
            "turnComplete": True,
            "streamProtocol": self.protocol_version,
            "scheduleIntent": (
                dict(data["scheduleIntent"])
                if isinstance(data.get("scheduleIntent"), Mapping)
                else dict(self._schedule_intent)
            ),
        }
        if self._cognitive:
            payload["cognitive"] = dict(self._cognitive)
        self._trailer_emitted = True
        return [self._emit("turn_trailer", payload)]

    def _convert_legacy(self, value: Mapping[str, Any]) -> list[dict[str, Any]]:
        event_type = str(value.get("type") or "").strip()
        payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
        if event_type == "turn_header":
            return self._ensure_header(payload)
        if event_type in {"utterance", "progress_utterance"}:
            return self._emit_utterance(payload)
        if event_type in {"research_escalation", "tool_escalation"}:
            events = self._ensure_header(payload)
            events.append(self._emit(event_type, payload))
            return events
        if event_type == "turn_trailer":
            return self._emit_trailer(payload)
        if event_type in {"turn_abort", "tool_status", "emotion_update"}:
            events = self._ensure_header(payload)
            events.append(self._emit(event_type, payload))
            return events
        self.errors.append(f"unsupported_legacy_type:{event_type[:40]}")
        self.rejected_event_count += 1
        return []

    def _convert(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            events: list[dict[str, Any]] = []
            for item in value:
                events.extend(self._convert(item))
            return events
        if not isinstance(value, Mapping):
            self.errors.append("semantic_value_not_object")
            self.rejected_event_count += 1
            return []
        value = normalize_brain_container(value)
        if isinstance(value.get("cognitive"), Mapping):
            self._cognitive = dict(value["cognitive"])
        if value.get("type"):
            self.protocol_version = "legacy-compatible"
            return self._convert_legacy(value)

        kind = str(value.get("kind") or "").strip().lower()
        if isinstance(value.get("scheduleIntent"), Mapping):
            self._schedule_intent = dict(value["scheduleIntent"])
        if kind in {"speech_unit", "utterance", "message", "answer_part"}:
            if kind == "speech_unit":
                self.protocol_version = "semantic-v3"
            else:
                self.protocol_version = "semantic-v2-compatible"
            return self._emit_utterance(value)
        if kind in {"clarification", "clarify"} and not isinstance(value.get("speechUnits"), list):
            payload = dict(value)
            payload["requiresUserResponse"] = True
            payload.setdefault("expectedResponseType", "free_text")
            events = self._emit_utterance(payload)
            self._completed_acts.append("clarification")
            # A compact clarification is a complete semantic-v3 container in
            # the same way as ``kind=turn``.  The model protocol asks for one
            # JSON object, so waiting for a separate turn_end record would
            # incorrectly turn a valid clarification into a truncated stream.
            events.extend(self._emit_trailer({
                "requiresUserResponse": True,
                "expectedResponseType": payload["expectedResponseType"],
                "completedActs": list(self._completed_acts),
                "standalone": True,
            }))
            return events
        if kind in {"tool_use", "tool_request"}:
            requests = value.get("toolRequests") or value.get("calls") or []
            requests = requests if isinstance(requests, list) else []
            progress = value.get("progress") if isinstance(value.get("progress"), Mapping) else {}
            if not progress and (value.get("progressZh") or value.get("progressJa")):
                progress = {
                    "zh": value.get("progressZh"),
                    "ja": value.get("progressJa"),
                    "emotion": value.get("progressEmotion") or "focused",
                }
            payload = {
                "reason": str(value.get("reason") or "model_discovered_missing_capability"),
                "targetDomain": str(value.get("targetDomain") or "none"),
                "toolAuthorization": str(value.get("toolAuthorization") or "none"),
                "capabilityQuery": str(value.get("capabilityQuery") or ""),
                "toolRequests": [dict(item) for item in requests if isinstance(item, Mapping)],
                "confidence": value.get("confidence", 0.5),
                "progress": dict(progress),
                "scheduleIntent": dict(self._schedule_intent),
            }
            return [*self._ensure_header(value), self._emit("tool_escalation", payload)]
        if kind in {"turn_end", "semantic_end"}:
            self.protocol_version = "semantic-v3"
            return self._emit_trailer(value)
        if (
            kind in {"turn", "final_answer", "answer", "final"}
            or isinstance(value.get("speechUnits"), list)
            or isinstance(value.get("utterances"), list)
        ):
            semantic_v3_units = (
                value.get("speechUnits")
                if isinstance(value.get("speechUnits"), list)
                else None
            )
            self.protocol_version = (
                "semantic-v3" if semantic_v3_units is not None else "semantic-v2-compatible"
            )
            events: list[dict[str, Any]] = []
            utterances = (
                semantic_v3_units
                if semantic_v3_units is not None
                else value.get("utterances")
                if isinstance(value.get("utterances"), list)
                else []
            )
            last_index = len(utterances) - 1
            turn_defaults = {
                key: value.get(key)
                for key in (
                    "emotion",
                    "emotionIntensity",
                    "gesture",
                    "voiceStyle",
                    "voiceTone",
                    "voiceIntensity",
                    "blushIntensity",
                    "eyeContact",
                    "gazeX",
                    "gazeY",
                    "tension",
                )
                if value.get(key) is not None
            }
            for index, item in enumerate(utterances):
                if isinstance(item, Mapping):
                    # The compact V3 contract keeps shared presentation fields
                    # at turn level. A unit may still override them for a real
                    # mixed-emotion turn. Missing boundaries are transport
                    # defaults, not another authoring task for the provider.
                    normalized_item = {**turn_defaults, **dict(item)}
                    normalized_item.setdefault(
                        "boundaryAfter",
                        "turn_pause" if index == last_index else "phrase_pause",
                    )
                    events.extend(self._emit_utterance(normalized_item))
            if not utterances:
                events.extend(self._emit_utterance(value))
            events.extend(self._emit_trailer(value))
            return events
        if value.get("reply") or value.get("textZh") or value.get("zh"):
            self.protocol_version = "atomic-compatible"
            events = self._emit_utterance(value)
            events.extend(self._emit_trailer(value))
            return events
        self.errors.append(f"unsupported_semantic_kind:{kind[:40]}")
        self.rejected_event_count += 1
        return []

    def _extract_values(self, *, final: bool = False) -> list[Any]:
        values: list[Any] = []
        decoder = json.JSONDecoder()
        while True:
            stripped = self.buffer.lstrip()
            self.buffer = stripped
            while self.buffer.startswith("```"):
                newline = self.buffer.find("\n")
                if newline < 0:
                    if final:
                        self.buffer = ""
                    return values
                self.buffer = self.buffer[newline + 1 :].lstrip()
            if not self.buffer:
                return values
            if self.buffer.startswith("```"):
                self.buffer = self.buffer[3:].lstrip()
                continue
            if self.buffer[0] not in "[{":
                starts = [index for index in (self.buffer.find("{"), self.buffer.find("[")) if index >= 0]
                if not starts:
                    if final:
                        self.errors.append("non_json_stream_content")
                        self.rejected_event_count += 1
                        self.buffer = ""
                    return values
                start = min(starts)
                prefix = self.buffer[:start].strip()
                if prefix and prefix != "```":
                    self.errors.append("ignored_non_json_prefix")
                self.buffer = self.buffer[start:]
            try:
                value, end = decoder.raw_decode(self.buffer)
            except json.JSONDecodeError as error:
                if final:
                    self.errors.append(f"incomplete_json:{error.msg}")
                    self.rejected_event_count += 1
                    self.buffer = ""
                return values
            values.append(value)
            self.buffer = self.buffer[end:].lstrip()

    def feed(self, text: str) -> list[dict[str, Any]]:
        self.buffer += str(text or "")
        if len(self.buffer) > self.maximum_buffer_chars:
            raise ValueError("semantic stream exceeded the bounded parser buffer")
        events: list[dict[str, Any]] = []
        for value in self._extract_values():
            events.extend(self._convert(value))
        return events

    def finish(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for value in self._extract_values(final=True):
            events.extend(self._convert(value))
        if (
            self.protocol_version == "semantic-v3"
            and self._utterance_count
            and not self._trailer_emitted
            and not self._fatal_protocol_error
        ):
            self.errors.append("semantic_v3_missing_turn_end")
            self.rejected_event_count += 1
            self._fatal_protocol_error = True
        else:
            # Legacy and atomic compatibility inputs did not promise an
            # explicit V3 turn_end record, so they may still be closed here.
            events.extend(self._emit_trailer())
        self.buffer = ""
        return events


# Compatibility name for external imports while callers migrate.
LLMEventStreamParser = ModelSemanticStreamParser
