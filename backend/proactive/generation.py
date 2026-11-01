"""准备完成后的两次有界主动发言生成与交付前复核。

每次仍读取最新关系；生成后及视觉审计后分别复核暂停，避免耗时期间恢复旧决定。
只在已有条件下进行日语对齐和视觉审计，提示、思考参数、重试次数均沿用原协议。
"""
from __future__ import annotations
from typing import Any, Callable
import json
import time
from backend.chat.delivery import DeliveryError, require_delivery
from backend.chat.parsing import parse_json_object
from backend.proactive.rules import proactive_outreach_context_key
from backend.proactive.persona import PersonaResources
from backend.proactive.catalog import PROACTIVE_SCREEN_QUESTION as PROACTIVE_SCREEN_QUESTION
from backend.proactive.prepared import PreparedProactiveTurn
from backend.proactive.ports import ContextOperations, ModelOperations, VisualOperations

class ProactiveGenerator:
    def __init__(self, context: ContextOperations, model: ModelOperations,
                 visual: VisualOperations, persona: PersonaResources, *,
                 dialogue_style: Callable[[], str], write_log: Callable[..., Any]) -> None:
        self.context = context
        self.model = model
        self.visual = visual
        self.persona = persona
        self.dialogue_style = dialogue_style
        self.write_log = write_log

    def generate(self, prepared: PreparedProactiveTurn) -> dict[str, Any]:
        body = prepared.body
        context = prepared.context
        settings = prepared.settings
        unanswered_count = prepared.unanswered_count
        kind = prepared.kind
        silence_check_in = prepared.silence_check_in
        recent_replies = prepared.recent_replies
        resolution = prepared.resolution
        loaded_modules = prepared.loaded_modules
        screen_result = prepared.screen_result
        screen_evidence = prepared.screen_evidence
        allowed_topic_source_ids = prepared.allowed_topic_source_ids
        pending_question_text = prepared.pending_question_text
        exchange_status = prepared.exchange_status
        generation_rule = prepared.generation_rule
        prompt_input = prepared.prompt_input
        last_error = ""
        duplicate_candidate = ""
        started = time.perf_counter()
        for attempt in range(2):
            retry_rule = ""
            if duplicate_candidate:
                retry_rule = (
                    "\n上一候选未通过校验。"
                    + (
                        "它把不愿主动公开的人格事实当成了话题；不要继续、暗示或换说法复述，"
                        "必须从 allowedTopicSeeds 选择另一个公开来源。"
                        if last_error.startswith("proactive_private_topic")
                        else
                        "必须更换交流切口和问题结构，不能只替换同义词。"
                    )
                    + f"\n未通过候选：{duplicate_candidate[:500]}"
                )
            payload: dict[str, Any] = {
                "model": settings["apiModel"],
                "temperature": 0.78 if attempt == 0 else 0.9,
                "max_tokens": 520,
                "messages": [
                    {"role": "system", "content": self.persona.read_proactive_persona_prompt()},
                    {"role": "system", "content": self.dialogue_style()},
                    {"role": "system", "content": self.context.relationship_context_prompt(self.context.read_relationship_state())},
                    {"role": "system", "content": generation_rule + retry_rule},
                    {"role": "user", "content": json.dumps(prompt_input, ensure_ascii=False)},
                ],
            }
            if settings.get("useResponseFormat"):
                payload["response_format"] = {"type": "json_object"}
            if "deepseek.com" in settings["apiEndpoint"]:
                payload["thinking"] = {"type": "disabled"}
            try:
                content = self.model.send_upstream_chat_payload(
                    settings,
                    payload,
                    timeout=35,
                    purpose="proactive_generation",
                )
                raw_candidate = parse_json_object(content)
                raw_candidate = raw_candidate if isinstance(raw_candidate, dict) else {}
                result = self.model.normalize_model_reply(content, settings=settings)
                result = require_delivery(result, settings=settings, logger=self.write_log,
                                          request_id=str(body.get("requestId") or "proactive"))
                canonical_reply = result["reply"]
                proactive_act = str(raw_candidate.get("proactiveAct") or "share_observation")
                topic_source_id = str(raw_candidate.get("topicSourceId") or "")
                if topic_source_id not in allowed_topic_source_ids:
                    result["proactiveTopicUnverified"] = True
                # Topic relevance and question/style shape are generation guidance,
                # never permission to discard a valid answer or generate repeatedly.
                latest_proactive_control = self.context.normalize_proactive_control(
                    self.context.read_conversation_context().get("proactiveControl")
                )
                if latest_proactive_control["status"] == "paused":
                    self.write_log(
                        "proactive_generation_discarded_after_pause",
                        reason=latest_proactive_control["reason"],
                        resumeMode=latest_proactive_control["resumeMode"],
                        sourceTurnId=latest_proactive_control["sourceTurnId"],
                    )
                    return {
                        "ok": True,
                        "suppressed": True,
                        "reason": "proactive_paused_during_generation",
                        "proactiveControl": latest_proactive_control,
                    }
                try:
                    result = self.model.align_spoken_text_ja(settings, result)
                except Exception:
                    result["speechUnavailableReason"] = "spoken_alignment_failed"
                result["reply"] = canonical_reply
                result = require_delivery(result, settings=settings, logger=self.write_log,
                                          request_id=str(body.get("requestId") or "proactive"))
                latest_proactive_control = self.context.normalize_proactive_control(
                    self.context.read_conversation_context().get("proactiveControl")
                )
                if latest_proactive_control["status"] == "paused":
                    self.write_log(
                        "proactive_generation_discarded_before_delivery",
                        reason=latest_proactive_control["reason"],
                        resumeMode=latest_proactive_control["resumeMode"],
                        sourceTurnId=latest_proactive_control["sourceTurnId"],
                    )
                    return {
                        "ok": True,
                        "suppressed": True,
                        "reason": "proactive_paused_before_delivery",
                        "proactiveControl": latest_proactive_control,
                    }
                result.update({
                    "ok": True,
                    "source": "api-proactive",
                    "proactiveAfterUserTurnId": str(proactive_outreach_context_key(context)[2] or ""),
                    "proactiveKind": kind,
                    "proactiveSubject": str(
                        resolution.get("subject")
                        if resolution.get("active")
                        else pending_question_text
                        if pending_question_text
                        else ""
                    )[:240],
                    "proactiveAct": proactive_act,
                    "proactiveOutreachGoal": (
                        "check_in_on_silence" if silence_check_in else "start_or_continue_topic"
                    ),
                    "proactiveInputUnansweredCount": unanswered_count,
                    "proactiveTopicSourceId": topic_source_id,
                    "proactiveExchangeStatus": exchange_status,
                    "proactiveScreenObserved": bool(screen_evidence),
                    "proactivePersonaModules": loaded_modules,
                    "memoryUpdates": [],
                    "holdMs": max(42000, int(result.get("holdMs") or 0)),
                    "latencyMs": round((time.perf_counter() - started) * 1000),
                })
                if screen_result and screen_evidence:
                    result.update({
                        "proactiveObservation": self.visual.compact_screen_capture_memory(screen_result),
                        "toolUsed": "capture_screen",
                        "toolStatus": "ok",
                        "_conversationToolResults": [screen_result],
                    })
                return result
            except DeliveryError as error:
                last_error = error.code
                if error.code == "leak_blocked":
                    break
            except Exception as error:
                last_error = f"{type(error).__name__}: {error}"[:500]
        self.write_log(
            "proactive_generation_failed",
            error=last_error,
            kind=kind,
            unansweredCount=unanswered_count,
            outreachGoal=(
                "check_in_on_silence" if silence_check_in else "start_or_continue_topic"
            ),
            recentCount=len(recent_replies),
        )
        return {
            "ok": False,
            "error": "proactive_generation_failed",
            "message": f"主动聊天没有取得可用的回复；未使用本地固定题目。{last_error}"[:700],
        }
