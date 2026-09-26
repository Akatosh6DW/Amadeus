"""准备完成后的两次有界主动发言生成与交付前复核。

每次仍读取最新关系；生成后及视觉审计后分别复核暂停，避免耗时期间恢复旧决定。
只在已有条件下进行日语对齐和视觉审计，提示、思考参数、重试次数均沿用原协议。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping
import json
import time
from agent.dialogue_architecture import choose_turn_policy
from agent.epistemic_architecture import proactive_external_claim_risks
from backend.chat.parsing import parse_json_object
from backend.chat.completion import visual_grounding_text_fingerprint
from backend.conversation.questions import terminal_answerable_question
from backend.proactive.rules import proactive_private_topic_violation, proactive_reply_duplicate, proactive_outreach_context_key
from backend.proactive.catalog import PROACTIVE_SCREEN_QUESTION
from backend.proactive.persona import PersonaResources
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
        messages = prepared.messages
        previous_prompt = prepared.previous_prompt
        unanswered_count = prepared.unanswered_count
        kind = prepared.kind
        silence_check_in = prepared.silence_check_in
        recent_replies = prepared.recent_replies
        resolution = prepared.resolution
        loaded_modules = prepared.loaded_modules
        proactive_persona_policy = prepared.proactive_persona_policy
        recent_user_texts = prepared.recent_user_texts
        pending_todo_topic_id = prepared.pending_todo_topic_id
        screen_result = prepared.screen_result
        screen_evidence = prepared.screen_evidence
        allowed_topic_source_ids = prepared.allowed_topic_source_ids
        silence_topic_id = prepared.silence_topic_id
        pending_question_text = prepared.pending_question_text
        exchange_status = prepared.exchange_status
        generation_rule = prepared.generation_rule
        prompt_input = prepared.prompt_input
        last_error = ""
        duplicate_candidate = ""
        proactive_policy = choose_turn_policy("你最近想聊什么？")
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
                reply = str(result.get("reply") or "").strip()
                if result.get("emptyReply") or not reply:
                    last_error = "upstream_returned_empty_proactive_reply"
                    continue
                epistemic_risks = proactive_external_claim_risks(
                    reply,
                    declared_external_claims=[
                        *(
                            raw_candidate.get("externalFactClaims")
                            if isinstance(raw_candidate.get("externalFactClaims"), list)
                            else []
                        ),
                        *(
                            raw_candidate.get("researchActionClaims")
                            if isinstance(raw_candidate.get("researchActionClaims"), list)
                            else []
                        ),
                    ],
                )
                if epistemic_risks:
                    last_error = f"proactive_epistemic_risk:{','.join(epistemic_risks)}"
                    duplicate_candidate = reply
                    self.write_log(
                        "proactive_candidate_rejected",
                        attempt=attempt + 1,
                        reason="proactive_unsupported_external_claim",
                        epistemicRisks=list(epistemic_risks),
                    )
                    continue
                if not terminal_answerable_question(reply):
                    last_error = "proactive_reply_missing_terminal_open_question"
                    duplicate_candidate = reply
                    continue
                proactive_act = str(raw_candidate.get("proactiveAct") or "").strip()
                allowed_proactive_acts = {
                    "ask_new_question",
                    "ask_unanswered_followup",
                    "share_observation_and_ask",
                    "check_in_on_silence",
                }
                if proactive_act not in allowed_proactive_acts:
                    last_error = "proactive_reply_missing_valid_act"
                    duplicate_candidate = reply
                    continue
                if silence_check_in and proactive_act != "check_in_on_silence":
                    last_error = "proactive_silence_check_in_not_followed"
                    duplicate_candidate = reply
                    continue
                if (
                    not result.get("requiresUserResponseProvided")
                    or not result.get("requiresUserResponse")
                    or result.get("expectedResponseType") != "free_text"
                ):
                    last_error = "proactive_reply_missing_response_contract"
                    duplicate_candidate = reply
                    continue
                topic_source_id = str(raw_candidate.get("topicSourceId") or "").strip()
                if topic_source_id not in allowed_topic_source_ids:
                    last_error = "proactive_reply_missing_grounded_topic_source"
                    duplicate_candidate = reply
                    self.write_log(
                        "proactive_candidate_rejected",
                        attempt=attempt + 1,
                        reason=last_error,
                        topicSourceId=topic_source_id,
                    )
                    continue
                if silence_check_in and topic_source_id != silence_topic_id:
                    last_error = "proactive_silence_topic_not_followed"
                    duplicate_candidate = reply
                    continue
                if pending_todo_topic_id and topic_source_id != pending_todo_topic_id:
                    last_error = "proactive_pending_todo_not_followed_up"
                    duplicate_candidate = reply
                    self.write_log(
                        "proactive_candidate_rejected",
                        attempt=attempt + 1,
                        reason=last_error,
                        topicSourceId=topic_source_id,
                        requiredTopicSourceId=pending_todo_topic_id,
                    )
                    continue
                if screen_evidence and topic_source_id != "screen:current":
                    last_error = "proactive_screen_observation_not_used"
                    duplicate_candidate = reply
                    self.write_log(
                        "proactive_candidate_rejected",
                        attempt=attempt + 1,
                        reason=last_error,
                        topicSourceId=topic_source_id,
                    )
                    continue
                if screen_evidence and self.visual.tool_grounding_violation(
                    raw_candidate,
                    [screen_result] if screen_result else [],
                    PROACTIVE_SCREEN_QUESTION,
                    self.context.read_relationship_state(),
                ):
                    last_error = "proactive_screen_reply_not_grounded"
                    duplicate_candidate = reply
                    self.write_log(
                        "proactive_candidate_rejected",
                        attempt=attempt + 1,
                        reason=last_error,
                        topicSourceId=topic_source_id,
                    )
                    continue
                if raw_candidate.get("usesPrivatePersonaAsTopic") is not False:
                    last_error = "proactive_private_topic_not_explicitly_excluded"
                    duplicate_candidate = reply
                    self.write_log(
                        "proactive_candidate_rejected",
                        attempt=attempt + 1,
                        reason=last_error,
                        topicSourceId=topic_source_id,
                    )
                    continue
                private_violation, private_rule = proactive_private_topic_violation(
                    reply,
                    recent_user_texts,
                    proactive_persona_policy,
                )
                if private_violation:
                    last_error = f"proactive_private_topic_violation:{private_rule}"
                    duplicate_candidate = reply
                    self.write_log(
                        "proactive_candidate_rejected",
                        attempt=attempt + 1,
                        reason="proactive_private_topic_violation",
                        privacyRule=private_rule,
                        topicSourceId=topic_source_id,
                    )
                    continue
                if (
                    exchange_status == "completed"
                    and raw_candidate.get("reopensCompletedTurn") is not False
                ):
                    last_error = "proactive_reply_reopened_completed_turn"
                    duplicate_candidate = reply
                    continue
                duplicate, matched = proactive_reply_duplicate(
                    reply,
                    recent_replies,
                    previous_prompt=previous_prompt,
                )
                if duplicate:
                    last_error = "proactive_reply_too_similar"
                    duplicate_candidate = matched
                    continue
                style_validation = self.model.dialogue_validation_for_result(
                    result,
                    proactive_policy,
                    messages,
                )
                if style_validation.needs_rewrite:
                    last_error = "proactive_reply_violated_dialogue_policy"
                    duplicate_candidate = reply
                    continue
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
                result = self.model.align_spoken_text_ja(settings, result)
                if screen_result and screen_evidence:
                    try:
                        proactive_visual_audit = self.visual.audit_visual_grounding_with_model(
                            settings,
                            PROACTIVE_SCREEN_QUESTION,
                            result,
                            [screen_result],
                            request_id=str(body.get("requestId") or "proactive-screen"),
                        )
                    except Exception as error:
                        proactive_visual_audit = {
                            "passes": False,
                            "reasons": ["proactive_visual_audit_unavailable"],
                            "errorType": type(error).__name__,
                        }
                    self.write_log(
                        "proactive_visual_semantic_grounding_audit",
                        attempt=attempt + 1,
                        decision=proactive_visual_audit,
                    )
                    if not proactive_visual_audit.get("passes"):
                        last_error = "proactive_screen_reply_failed_semantic_grounding"
                        # Do not feed unsupported visual prose into the next model
                        # attempt; the typed currentScreenObservation is sufficient.
                        duplicate_candidate = ""
                        continue
                    result["visualGroundingAudit"] = proactive_visual_audit
                    result["visualGroundingVerification"] = "proactive_independent_semantic_audit"
                    result["visualGroundingTextFingerprint"] = visual_grounding_text_fingerprint(result)
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
            "message": f"主动聊天没有取得可用的新问题；未使用本地固定题目。{last_error}"[:700],
        }
