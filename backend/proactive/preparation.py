"""主动交流准备：先核对等待和开口许可，再读取话题与可选屏幕证据。

被抑制时立即返回，不能先抓屏或调用话题生成；准备结果供同一轮有界重试复用。
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping
import json
from agent.memory_architecture import MemoryNeed
from agent.visual_grounding import build_visual_evidence_packet
from backend.common.text import unique_strings
from backend.common.values import safe_nonnegative_int as _safe_nonnegative_int
from backend.config.normalization import PROACTIVE_TOPIC_CATEGORIES, PROACTIVE_TOPIC_CATEGORY_LABELS
from backend.chat.reply_fields import SPOKEN_JA_NO_EMOJI_RULE
from backend.conversation.questions import terminal_answerable_question
from backend.proactive.rules import proactive_topic_seeds, redact_private_proactive_context, should_proactive_observe_screen
from backend.proactive.catalog import PROACTIVE_SCREEN_QUESTION
from backend.proactive.persona import PersonaResources
from backend.proactive.prepared import PreparedProactiveTurn
from backend.proactive.ports import ContextOperations, MemoryOperations, ModelOperations, VisualOperations

class ProactivePreparation:
    def __init__(self, context: ContextOperations, memory: MemoryOperations,
                 model: ModelOperations, visual: VisualOperations, persona: PersonaResources,
                 *, assess_availability: Callable[..., Any], now: Callable[[], str],
                 write_log: Callable[..., Any]) -> None:
        self.context = context
        self.memory = memory
        self.model = model
        self.visual = visual
        self.persona = persona
        self.assess_availability = assess_availability
        self.now = now
        self.write_log = write_log

    def recent_assistant_reply_texts(
        self,
        messages: list[dict[str, Any]] | None = None,
        supplied: list[Any] | None = None,
        limit: int = 24,
    ) -> list[str]:
        """Collect every recent assistant reply that a proactive turn must not restate.

        Proactive de-duplication used to compare only against earlier proactive
        bubbles.  A normal answer could therefore finish a topic and be paraphrased
        a few minutes later by the proactive scheduler.  The relevant ownership
        boundary is *assistant speech*, not the UI origin that produced it.
        """

        context = self.context.read_conversation_context()
        values = [
            str(turn.get("assistant") or "").strip()
            for turn in context.get("recentTurns", [])
            if isinstance(turn, dict) and str(turn.get("assistant") or "").strip()
        ]
        values.extend(
            str(item.get("content") or "").strip()
            for item in (messages or [])
            if isinstance(item, dict)
            and item.get("role") == "assistant"
            and str(item.get("content") or "").strip()
        )
        values.extend(str(item).strip() for item in (supplied or []) if str(item).strip())
        return unique_strings(values)[-max(1, limit):]


    def proactive_screen_observation_evidence(
        self,
        result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Expose visual evidence, never local image payloads or internal paths, to the LLM."""

        if not isinstance(result, dict) or not result.get("ok"):
            return None
        vision = result.get("vision") if isinstance(result.get("vision"), dict) else {}
        if not vision.get("available"):
            return None
        caption = str(vision.get("globalDescription") or vision.get("caption") or "").strip()
        visual_tiles = vision.get("visualTiles") if isinstance(vision.get("visualTiles"), list) else []
        visual_regions = vision.get("regions") if isinstance(vision.get("regions"), list) else []
        tiles = [
            {
                "position": str(item.get("position") or "")[:80],
                "caption": str(item.get("caption") or "")[:700],
            }
            for item in visual_tiles[:6]
            if isinstance(item, dict) and str(item.get("caption") or "").strip()
        ]
        regions = [
            {
                "label": str(item.get("label") or item.get("name") or "")[:120],
                "description": str(
                    item.get("description") or item.get("caption") or item.get("text") or ""
                )[:700],
            }
            for item in visual_regions[:16]
            if isinstance(item, dict)
        ]
        ocr_text = str(vision.get("ocrText") or "").strip()[:5000]
        if not any((caption, tiles, regions, ocr_text)):
            return None
        return {
            "observedAt": str(result.get("capturedAt") or self.now())[:80],
            "foregroundWindowTitle": str(result.get("foregroundWindowTitle") or "")[:500],
            "selfExcluded": bool(result.get("selfExcluded")),
            "caption": caption[:5000],
            "visualTiles": tiles,
            "regions": regions,
            "ocrText": ocr_text,
        }


    def prepare(self, body: dict[str, Any]) -> PreparedProactiveTurn | dict[str, Any]:
        """Generate one contextual proactive utterance; never select a local question bank."""

        context = self.context.read_conversation_context()
        waiting = context.get("pendingAssistantQuestion") or {}
        old_questions = context.get("assistantQuestionHistory") or []
        latest_turn = (context.get("recentTurns") or [{}])[-1]
        old_unanswered = bool(old_questions and old_questions[-1].get("status") == "suspended"
                              and old_questions[-1].get("sourceTurnId") == latest_turn.get("turnId"))
        if context.get("waitingForUser") or waiting or (body.get("kind") == "follow_up" and old_unanswered):
            return {"ok": True, "suppressed": True, "reason": "unanswered_question_no_nudge",
                    "waitingForUser": True,
                    "proactiveControl": self.context.normalize_proactive_control(context.get("proactiveControl"))}
        proactive_control = self.context.normalize_proactive_control(context.get("proactiveControl"))
        if proactive_control["status"] == "paused":
            self.write_log(
                "proactive_generation_suppressed",
                reason=proactive_control["reason"],
                resumeMode=proactive_control["resumeMode"],
                sourceTurnId=proactive_control["sourceTurnId"],
            )
            return {
                "ok": True,
                "suppressed": True,
                "reason": "proactive_paused",
                "proactiveControl": proactive_control,
            }
        settings = self.model.load_settings()
        if not self.model.configured(settings):
            return {
                "ok": False,
                "error": "model_not_configured",
                "message": "主动聊天需要可用的大模型；未使用本地固定题目。",
            }
        messages = [
            item for item in (
                body.get("messages") if isinstance(body.get("messages"), list) else []
            )
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        ][-14:]
        if not messages:
            for turn in context.get("recentTurns", [])[-8:]:
                if not isinstance(turn, dict):
                    continue
                if str(turn.get("user") or "").strip():
                    messages.append({"role": "user", "content": str(turn["user"])[:1200]})
                if str(turn.get("assistant") or "").strip():
                    messages.append({"role": "assistant", "content": str(turn["assistant"])[:1600]})
        suppression = self.assess_availability(settings, context, messages)
        if suppression is not None:
            return suppression
        latest_user = next(
            (
                str(item.get("content") or "").strip()
                for item in reversed(messages)
                if item.get("role") == "user"
            ),
            "",
        )
        previous_prompt = str(body.get("previousProactive") or "").strip()[:1200]
        unanswered_count = _safe_nonnegative_int(body.get("unansweredCount"))
        kind = (
            "follow_up"
            if str(body.get("kind") or "").strip().lower() == "follow_up"
            else "new"
        )
        silence_check_in = kind == "follow_up" and unanswered_count > 0
        recent_replies = self.recent_assistant_reply_texts(
            messages,
            body.get("recentProactiveTexts")
            if isinstance(body.get("recentProactiveTexts"), list)
            else [],
        )
        selected_categories = [
            str(item)
            for item in (
                body.get("topicCategories")
                if isinstance(body.get("topicCategories"), list)
                else []
            )
            if str(item) in PROACTIVE_TOPIC_CATEGORIES
        ]
        interests = [
            PROACTIVE_TOPIC_CATEGORY_LABELS[item]
            for item in selected_categories
            if item in PROACTIVE_TOPIC_CATEGORY_LABELS
        ]
        resolution = self.context.contextual_reference_resolution(latest_user, messages)
        # The full deep persona remains available to ordinary /api/chat turns.
        # Idle outreach receives a smaller prompt so private facts cannot become
        # candidate topics merely because a module was loaded.
        loaded_modules: list[str] = []
        proactive_records = self.memory.read_memory_records(settings)
        memories = self.memory.relevant_memories(
            unique_strings(item.content for item in proactive_records if item.active),
            latest_user or "用户最近的话题与长期兴趣",
            messages,
            limit=3,
            need=MemoryNeed.MIXED,
            records=proactive_records,
            settings=settings,
        )
        proactive_persona_policy = self.persona.proactive_persona_topic_policy()
        recent_user_texts = [
            str(item.get("content") or "").strip()
            for item in messages[-10:]
            if item.get("role") == "user"
            and str(item.get("content") or "").strip()
        ]
        topic_seeds = proactive_topic_seeds(
            messages,
            selected_categories,
            memories,
            proactive_persona_policy,
        )
        pending_todo_proactive = self.context.current_pending_action()
        pending_todo_proactive = (
            pending_todo_proactive
            if pending_todo_proactive.get("type") == "todo_mutation"
            and str(pending_todo_proactive.get("status") or "") != "paused"
            else {}
        )
        pending_todo_topic_id = ""
        if pending_todo_proactive and not silence_check_in:
            pending_todo_topic_id = (
                "pending_todo:"
                + str(pending_todo_proactive.get("actionId") or "unknown")[:100]
            )
            topic_seeds.insert(0, {
                "id": pending_todo_topic_id,
                "kind": "pending_todo_clarification",
                "topic": str(
                    pending_todo_proactive.get("originalUserRequest")
                    or "用户已经授权、但仍缺少具体信息的日程操作"
                )[:1000],
                "provenance": "authorized_pending_action",
            })
        screen_result: dict[str, Any] | None = None
        screen_evidence: dict[str, Any] | None = None
        if (
            kind == "new"
            and not pending_todo_proactive
            and should_proactive_observe_screen(settings, body)
        ):
            screen_result = self.visual.tool_capture_screen(settings, PROACTIVE_SCREEN_QUESTION)
            screen_evidence = self.proactive_screen_observation_evidence(screen_result)
            self.write_log(
                "proactive_screen_observation",
                attempted=True,
                ok=bool(screen_evidence),
                captureId=str((screen_result or {}).get("captureId") or ""),
                error=str((screen_result or {}).get("error") or "")[:500],
            )
            if screen_evidence:
                topic_seeds.insert(0, {
                    "id": "screen:current",
                    "kind": "current_screen_observation",
                    "topic": "刚刚取得的当前屏幕工具观测",
                    "provenance": "tool_observation",
                })
        allowed_topic_source_ids = {
            str(seed.get("id") or "")
            for seed in topic_seeds
            if str(seed.get("id") or "")
        }
        silence_topic_id = "interaction:unanswered"
        if silence_check_in:
            topic_seeds.insert(0, {
                "id": silence_topic_id,
                "kind": "unanswered_proactive_check_in",
                "topic": "用户尚未回应上一条主动消息；关心这次沉默本身",
                "provenance": "interaction_state",
            })
            allowed_topic_source_ids.add(silence_topic_id)
        pending_question = context.get("pendingAssistantQuestion")
        pending_question = pending_question if isinstance(pending_question, dict) else {}
        latest_assistant = next(
            (
                str(item.get("content") or "").strip()
                for item in reversed(messages)
                if item.get("role") == "assistant"
            ),
            "",
        )
        pending_question_text = str(pending_question.get("text") or "").strip()
        if (
            not pending_question_text
            or terminal_answerable_question(latest_assistant) != pending_question_text
        ):
            pending_question = {}
            pending_question_text = ""
        latest_role = str(messages[-1].get("role") or "") if messages else ""
        exchange_status = (
            "awaiting_user_answer"
            if pending_question_text or pending_todo_proactive
            else "completed"
            if latest_role == "assistant"
            else "awaiting_assistant"
            if latest_role == "user"
            else "no_recent_exchange"
        )
        generation_rule = "\n".join([
            "[模型即时主动聊天：禁止题库和固定模板]",
            "这是 Amadeus 在用户暂时没有继续输入时主动开口。必须根据最近真实对话、她的人格、关系语境和可靠记忆即时生成，不能从预写题库抽取或改写固定哲学问题。",
            "只有 proactiveContext.exchangeStatus=awaiting_user_answer 时，才可追问上一条真正留给用户的问题；"
            "状态为 completed 时，上一轮已经结束，不得主动重答、改口、纠错、补充结论或重新履行旧请求。",
            (
                "当前存在 authorizedPendingTodo。必须选择它对应的 topicSourceId，围绕原请求只追问完成写入所缺的最少信息；"
                "不得换到随机话题，不得声称日程已经写入，也不得擅自取消这项待续接操作。"
                if pending_todo_proactive and not silence_check_in else
                "当前不存在待用户补充的已授权 Todo 操作。"
            ),
            (
                "proactiveContext.outreachGoal=check_in_on_silence：用户一直没有回应上一条主动消息。"
                "本轮交流目的必须改为关心这次沉默本身，可以自然问为什么没有回答、是不是正在忙、"
                "是否不方便说话或是否不想聊；不得继续追问上一条话题的内容，也不得另开新话题。"
                "语气随关系与 unansweredCount 自然变化，但不要替用户断定原因，不要像客服催回复，"
                "也不要用固定模板。proactiveAct 必须是 check_in_on_silence，topicSourceId 必须是 interaction:unanswered。"
                if silence_check_in else
                "proactiveContext.outreachGoal=start_or_continue_topic：按其余规则自然开启交流。"
            ),
            "allowedTopicSeeds 是本次唯一可选的话题来源。公开人格偏好可以作为话题，"
            "但私密人格事实只供正常对话被用户提及时决定反应，不能用于主动开场、追问、延伸或自曝。",
            "普通对话里用户可以主动谈这些私密内容；本限制只作用于本次 proactive 主动聊天，"
            "不得把它解释为回避用户问题或抹掉人物反应。",
            "recentConversation 中的助手旧发言只能用于避免重复，不是可靠事实来源；"
            "不得把助手以前说过、猜过或纠正过的内容当成用户兴趣。",
            "allowedTopicSeeds.provenance 决定事实权限：user_personal_authority 只证明用户自身状态；"
            "user_assertion 只能表述为‘你刚才说……’，不能当作公共事实确认；topic_direction 只允许提出宽泛问题。",
            "本主动聊天没有自动联网研究回执。不得新增新闻、版本、产品内容、研究结论、角色/物品设定等外部事实；"
            "不得说‘我查了、我看了官网、资料显示’。想聊外部话题时，只能围绕种子中已有内容询问用户，不得补充具体细节。",
            (
                "本轮已经取得 currentScreenObservation。必须选择 topicSourceId=screen:current，"
                "先用一两个当前画面独有的细节概括用户正在做什么，再给出简短、有性格的反应，最后问一个直接相关的问题。"
                "屏幕证据是工具观测，不是用户陈述；不得说成‘你刚才告诉我’，也不要提截图、OCR、视觉模型或工具调用。"
                "Amadeus 自身窗口不是观察对象；只能使用 currentScreenObservation 中实际存在的内容，不确定处要保留不确定。"
                if screen_evidence else
                "本轮没有当前屏幕观测。不得声称看到了用户此刻的屏幕，也不能根据旧截图假装正在观察。"
            ),
            "优先从用户正在推进的任务、明确兴趣或当前可回答的新切口发起交流；不要突然跳到无关的宏大问题。",
            "如果最近话题已经结束，再从用户允许的兴趣方向中选择一个与其经历或当前项目有联系的切口。不能捏造用户经历，不能把 Amadeus 的猜测当成用户事实。",
            "只推进一个交流目的：一句自然观察加一个真正需要用户回答的问题，或直接一个问题即可。"
            "最后一个语义单位必须是开放给用户的问题；反问后由自己解释、自问自答或在问号后继续下结论都不算。",
            (
                "这是连续未获回应后的关心，不是上一话题的内容追问；只询问用户为何没有回应或当前是否方便交流。"
                if silence_check_in
                else
                "这是一次新的主动开口。不得重复近期任何一次助手发言，也不能把刚完成的普通回答换几个词再说一遍。"
            ),
            "recentAssistantSpeechToAvoid 同时包含普通回答与主动发言。若其中已经完成了当前话题，就换一个真正的新切口；不要复述结论、补一句近义总结或再次履行同一个请求。",
            SPOKEN_JA_NO_EMOJI_RULE,
            "只输出 JSON。字段必须包含 reply、spokenTextJa、emotion、gesture、voiceStyle、voiceTone、emotionIntensity、blushIntensity、eyeContact、gazeX、gazeY、tension；"
            "还必须包含 requiresUserResponse=true、expectedResponseType=\"free_text\"、"
            "proactiveAct（只能是 ask_new_question、ask_unanswered_followup、share_observation_and_ask、check_in_on_silence）、"
            "topicSourceId（必须逐字取自 allowedTopicSeeds.id）、"
            "externalFactClaims（数组；列出回复中每条外部公共事实，没有则为空数组）、"
            "researchActionClaims（数组；列出‘我查过/看过/核实过’等行为主张，没有则为空数组）、"
            "toolObservationClaims（数组；只有使用 currentScreenObservation 时列出基于画面的主张，否则为空数组）、"
            "usesPrivatePersonaAsTopic=false、reopensCompletedTurn=false；memoryUpdates 必须为空。",
            "reply 是自然中文口语，spokenTextJa 是语义完整、符合牧濑红莉栖口吻的日语口语；两者都只能包含说出口的话，不能写动作或舞台说明。",
        ])
        prompt_input = {
            "mode": kind,
            "currentTime": self.now(),
            "proactiveControl": self.context.proactive_control_for_model(context),
            "proactiveContext": {
                "exchangeStatus": exchange_status,
                "latestExchangeCompleted": exchange_status == "completed",
                "unansweredCount": unanswered_count,
                "outreachGoal": (
                    "check_in_on_silence" if silence_check_in else "start_or_continue_topic"
                ),
                "privatePersonaMaterialUsage": "reactive_in_normal_chat_only",
            },
            "latestUserTopic": redact_private_proactive_context(
                latest_user,
                proactive_persona_policy,
            ),
            "previousProactive": previous_prompt,
            "pendingAssistantQuestion": str(pending_question.get("text") or ""),
            "authorizedPendingTodo": (
                {
                    "actionId": str(pending_todo_proactive.get("actionId") or ""),
                    "status": str(pending_todo_proactive.get("status") or ""),
                    "originalUserRequest": str(
                        pending_todo_proactive.get("originalUserRequest") or ""
                    )[:1000],
                    "knownCommand": dict(
                        pending_todo_proactive.get("intendedCommand") or {}
                    ),
                }
                if pending_todo_proactive and not silence_check_in else {}
            ),
            "allowedInterestDirections": interests,
            "allowedTopicSeeds": topic_seeds,
            "recentAssistantSpeechToAvoid": [
                redact_private_proactive_context(
                    reply,
                    proactive_persona_policy,
                )
                for reply in recent_replies
            ],
            "recentConversation": [
                {
                    "role": "assistant" if item.get("role") == "assistant" else "user",
                    "content": redact_private_proactive_context(
                        str(item.get("content") or "")[:1600],
                        proactive_persona_policy,
                    ),
                }
                for item in messages[-10:]
            ],
            "relevantLongTermMemory": [
                seed["topic"]
                for seed in topic_seeds
                if seed["kind"] == "reliable_user_memory"
            ],
            "currentScreenObservation": (
                build_visual_evidence_packet(
                    [screen_result],
                    user_question=PROACTIVE_SCREEN_QUESTION,
                )
                if screen_result and screen_evidence else None
            ),
        }
        return PreparedProactiveTurn(
            body=body,
            context=context,
            settings=settings,
            messages=messages,
            previous_prompt=previous_prompt,
            unanswered_count=unanswered_count,
            kind=kind,
            silence_check_in=silence_check_in,
            recent_replies=recent_replies,
            resolution=resolution,
            loaded_modules=loaded_modules,
            proactive_persona_policy=proactive_persona_policy,
            recent_user_texts=recent_user_texts,
            pending_todo_topic_id=pending_todo_topic_id,
            screen_result=screen_result,
            screen_evidence=screen_evidence,
            allowed_topic_source_ids=allowed_topic_source_ids,
            silence_topic_id=silence_topic_id,
            pending_question_text=pending_question_text,
            exchange_status=exchange_status,
            generation_rule=generation_rule,
            prompt_input=prompt_input,
        )
