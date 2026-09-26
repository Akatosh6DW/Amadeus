"""当前对话指代与来源归因的完整装配。

每轮冻结的 context_state 优先用于结构化账本和旧任务背景；旧近轮用户原文
与可验证工具主张仍按原有时机重新读取 live context。模型自主解析路径
只保留来源账本，不启用确定性的旧任务继承。赛事 kind 仅作旧记录兼容。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Protocol, Sequence

from agent.dialogue_architecture import is_explicit_correction_language
from agent.discourse_reference import (
    DiscourseLedger, ReferenceResolution, build_discourse_ledger,
    extend_with_current_artifacts, reference_requires_context,
    render_discourse_context, resolve_discourse_references,
)
from agent.fingerprints import runtime_fingerprint
from backend.research.current_info import SPORT_SPOKEN_JA
from backend.research.intent import is_external_fact_correction_followup
from backend.research.query import web_search_query_from_message


class ReferenceStore(Protocol):
    """旧近轮与工具主张按调用顺序向现有会话库读取。"""

    def read_context(self) -> dict[str, Any]: ...


class VisualIntent(Protocol):
    """只判断本轮是否要求屏幕/摄像头，视觉执行仍由入口管理。"""

    def is_current_sensor_request(self, text: str) -> bool: ...


SPORT_COMPETITION_PATTERN = re.compile(
    r"世界杯|欧洲杯|欧冠|欧联|英超|西甲|德甲|意甲|法甲|中超|美洲杯|亚洲杯|"
    r"NBA|WNBA|CBA|F1|一级方程式|足球|篮球",
    re.I,
)


SPORT_CONTEXT_PATTERN = re.compile(
    r"比分|赛果|赛程|比赛|对阵|决赛|半决赛|进球|球赛|足球|篮球|世界杯|欧洲杯|"
    r"球队|球员|踢得|打得|战犯|犯规|红牌|黄牌|球风|脏不脏|很脏|"
    r"姆巴佩|姆总|梅西|亚马尔|翻盘|逆转|(?<![A-Za-z])vs?\.?(?![A-Za-z])",
    re.I,
)


SPORT_EVALUATION_FOLLOWUP_PATTERN = re.compile(
    r"怎么看|怎么评价|如何评价|如何看待|你如何看|你的评价|你的看法|你的判断|"
    r"你觉得呢|你认为呢|你怎么想|说说你的看法|我问的是.{0,8}(?:怎么看|看法|评价|判断)|"
    r"别(?:只|光)?(?:复述|念|说).{0,8}(?:结果|资料|新闻)",
    re.I,
)


SPORT_CORRECTION_FOLLOWUP_PATTERN = re.compile(
    r"你在说什么|你说(?:错|反)了|^不对(?:[，,。！!：:]|$)|不是这个意思|我不是这个意思|"
    r"我的意思是|我是说|换句话说|具体来说|重新想|再想想|算错|看错|理解错|哪个大|哪个小|刚才那句",
    re.I,
)


def is_context_meta_message(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return bool(re.search(
        r"看看上下文|看(?:一下|下)?上下文|看看上(?:一)?句|看上文|你没理解上下文|"
        r"结合上下文|回到刚才|你说话看看上下文|联网权限|联网功能",
        compact,
    ))


def is_context_navigation_message(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return bool(re.search(
        r"看看上下文|看(?:一下|下)?上下文|看看上(?:一)?句|看上文|你没理解上下文|"
        r"结合上下文|回到刚才|你说话看看上下文",
        compact,
    ))


def message_needs_recent_context(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return False
    if is_context_navigation_message(compact) or re.search(
        r"这(?:句|场|次|里|样)|那(?:句|场|次)|它|"
        r"(?:他|她|他们|她们)(?:还|也|呢|能|会|是|有|怎么|为什么|现在|后来|到底)|"
        r"刚才|刚刚|上句|上文|上一张|上上张|前一张|前两张|前几张|截图历史|历史截图|"
        r"继续|然后呢|后来呢|结果呢|现在呢|什么意思|怎么样了|"
        r"评价呢|你的评价|你的看法|你的判断|你觉得呢|"
        r"你在说什么|你说(?:错|反)了|不是这个意思|我不是这个意思|重新想|再想想|"
        r"能赢吗|谁能赢|谁会赢|看好谁|赢了吗|结束了吗|最新(?:的)?(?:比分|赛果|结果)|"
        r"猜一个|你猜|猜吧|选一个|一定要|非要|预测|胜率|"
        r"(?:再|重新)查(?:一)?下|(?:再|重新)查查|你查查|重新查|再搜|"
        r"姆总|翻盘|逆转|哪场|什么比赛",
        compact,
    ):
        return True
    if is_explicit_correction_language(compact):
        return True
    # “这个世界”“那个模型”一类“指示词 + 明确名词”已经在当前消息中给出了
    # 对象，不能仅因包含“这个/那个”就强制继承上一轮。只把独立使用、后接
    # 语气词或疑问谓语的指示代词视为省略指代。
    if re.search(
        r"(?:^|[，,。！？?!；;：:])(?:这个|那个)"
        r"(?:$|[呢吗吧啊呀，,。！？?!；;：:]|怎么|为什么|是什么意思|指什么|咋办|怎么办|行不行|可以吗|能不能)",
        compact,
    ):
        return True
    # 普通疑问词本身并不代表缺少上下文。例如“为什么 API Key 不能写进
    # EXE”是完整问题；只有整条消息基本就是一个短追问时才继承上一轮。
    short_followup = compact.strip("，,。！？?!；;：:")
    return bool(re.fullmatch(
        r"(?:那|所以|可是|但是|不过)?(?:你)?(?:为什么|怎么办|怎么看|怎么评价|如何评价|如何看待)"
        r"(?:呢|啊|呀|嘛|吧)?",
        short_followup,
    ))


def extract_sports_matchup(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    patterns = [
        r"([A-Za-z\u3400-\u9fff]{2,24})\s*"
        r"(?:(?<![A-Za-z])vs?\.?(?![A-Za-z])|对阵)\s*"
        r"([A-Za-z\u3400-\u9fff]{2,24})",
        r"([A-Za-z\u3400-\u9fff]{2,12})\s*(?:已经)?被\s*([A-Za-z\u3400-\u9fff]{2,12})\s*\d+\s*[:：-]\s*\d+",
    ]
    # Chinese uses bare "对" and "打" in many non-sports relations. Only treat
    # them as matchup connectors when the sentence supplies a sports anchor or
    # mentions two known teams. This keeps "真由理对红莉栖意味着什么" out of
    # the sports context resolver.
    sports_anchor = bool(SPORT_COMPETITION_PATTERN.search(value) or re.search(
        r"比分|赛果|赛程|比赛|球队|球员|球赛|进球|决赛|半决赛|"
        r"谁能赢|谁会赢|哪队能赢|哪边能赢|看好谁|胜率|预测",
        value,
        re.I,
    ) or sum(name in value for name in SPORT_SPOKEN_JA) >= 2)
    if sports_anchor:
        patterns.insert(
            1,
            r"([A-Za-z\u3400-\u9fff]{2,24})\s*对\s*([A-Za-z\u3400-\u9fff]{2,24})",
        )
        patterns.insert(
            2,
            r"([A-Za-z\u3400-\u9fff]{2,24})\s*打(?!法|得|球|比赛|进攻|防守)\s*([A-Za-z\u3400-\u9fff]{2,24})",
        )
    for pattern in patterns:
        match = re.search(pattern, value, re.I)
        if not match:
            continue
        left, right = match.group(1), match.group(2)
        for competition in SPORT_COMPETITION_PATTERN.findall(value):
            left = re.sub(re.escape(competition), "", left, flags=re.I)
            right = re.sub(re.escape(competition), "", right, flags=re.I)
        left = re.sub(r"^.*(?:今天|今日|刚刚|刚才|这场|一场|场|的)", "", left)
        left = re.sub(r"^.*(?:我的意思是|意思是|我是说|我说的是|至于)", "", left)
        right = re.sub(
            r"(?:的)?(?:比分|比赛|赛果|结果|现在|已经|还能|能否|多少|几比几|"
            r"谁能赢|谁会赢|哪队能赢|哪边能赢|胜率|预测|分析|看好谁|今晚|今天晚上|"
            r"是不是|是否|很脏|脏不脏|踢得|打得).*$",
            "",
            right,
        )
        left = left.strip(" 的这场：:，,。！？?!")
        right = right.strip(" 的这场：:，,。！？?!")
        aliases = {"英国": "英格兰", "英队": "英格兰", "阿根廷队": "阿根廷"}
        left = aliases.get(left, left)
        right = aliases.get(right, right)
        if len(left) >= 2 and len(right) >= 2:
            return f"{left} vs {right}"
    return ""


def extract_sports_entities(text: str) -> list[str]:
    value = re.sub(r"\s+", " ", str(text or ""))
    aliases = {
        "英国": "英格兰",
        "英队": "英格兰",
        "阿根廷队": "阿根廷",
        "法国队": "法国",
        "西班牙队": "西班牙",
    }
    normalized = value
    for alias, canonical in aliases.items():
        normalized = normalized.replace(alias, canonical)
    entities: list[str] = []
    for name in SPORT_SPOKEN_JA:
        if name in normalized and name not in entities:
            entities.append(name)
    return entities


def sports_analysis_focus(text: str) -> list[str]:
    value = re.sub(r"\s+", "", str(text or ""))
    focuses: list[str] = []
    if re.search(r"战犯|责任最大|最大责任|谁的锅|最差球员|表现最差|谁最差|失误最多", value):
        focuses.append("individual_responsibility")
    if re.search(r"评价|复盘|表现|怎么看|看法|分析|主要问题|踢得|打得|战犯", value):
        focuses.append("overall_performance")
    if re.search(r"踢得.{0,4}脏|打得.{0,4}脏|脏不脏|很脏|犯规|红牌|黄牌|恶意动作|假摔", value):
        focuses.append("discipline_fair_play")
    return focuses


def explicit_user_position(text: str) -> str:
    """Extract an explicitly stated user stance without interpreting its meaning."""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    match = re.search(
        r"(?:我的意思是|我是说|我的观点是|我认为|我觉得|在我看来|我的看法是|我的判断是|我感觉|我赞同|我支持|我反对|"
        r"我不赞同|我不同意)\s*([^。！？!?\n]{1,220})",
        value,
        re.I,
    )
    return match.group(1).strip(" ，,：:") if match else ""


def _discourse_message_id(user_message: str, request_id: str = "") -> str:
    stable = str(request_id or "").strip()
    if not stable:
        stable = runtime_fingerprint(user_message)
    return f"request:{stable}:user"


def _reference_evidence(
    ledger: DiscourseLedger,
    resolution: ReferenceResolution,
) -> list[dict[str, str]]:
    by_id = {item.message_id: item for item in ledger.messages}
    return [
        {
            "messageId": message.message_id,
            "speaker": message.speaker,
            "text": message.text[:700],
            "modality": message.modality,
        }
        for message_id in resolution.antecedent_message_ids
        if (message := by_id.get(message_id)) is not None
    ]


def contextual_reference_prompt(resolution: dict[str, Any]) -> str:
    if resolution.get("interpretationOwner") == "model":
        return (
            "[对话与观察的来源归因]\n"
            "消息的 role/speaker 表示实际发送者；引用中的说话者和指代对象由你结合原文判断，不按最近消息自动绑定。"
            "助手旧回答不是用户证词。图片或 OCR 文字的作者默认未知，用户上传图片不等于其中的话是用户说的。"
            "中文回答和日语发声稿须保持同一说话者与指代对象。"
        )
    ledger_payload = resolution.get("referenceLedger")
    structured_prompt = ""
    if isinstance(ledger_payload, Mapping):
        ledger = DiscourseLedger.from_dict(ledger_payload)
        structured_prompt = render_discourse_context(
            ledger,
            resolve_discourse_references(
                ledger,
                message_id=ledger.current_message_id,
            ),
        )
    if not resolution.get("active"):
        return structured_prompt
    evidence = resolution.get("evidence") if isinstance(resolution.get("evidence"), list) else []
    trusted_tool_evidence = (
        resolution.get("trustedToolEvidence")
        if isinstance(resolution.get("trustedToolEvidence"), list)
        else []
    )
    legacy_prompt = "\n".join([
        "[后端确定性短期指代解析：本轮最高优先级]",
        *(f"- 最近用户原文：{item}" for item in evidence),
        *(f"- 已验证工具结果：{item}" for item in trusted_tool_evidence),
        f"- 当前讨论对象：{resolution.get('subject') or '最近一轮对象'}",
        f"- 用户要求的动作：{resolution.get('requestedAction') or '直接回答'}",
        *([f"- 用户本轮明确观点：{resolution.get('userPosition')}"] if resolution.get("userPosition") else []),
        *(f"- 完成条件：{item}" for item in resolution.get("constraints", []) if str(item).strip()),
        f"- 当前完整问题：{resolution.get('resolvedQuestion') or ''}",
        (
            "当前消息是在承接上述最近对话，不是开启新话题。直接回答这个完整问题；"
            if resolution.get("isContinuation") else
            "当前消息本身已经构成完整任务，按当前问题作答，不要引入之前的旧结论；"
        )
        + "不得改谈实验室、长期记忆或其他旧主题，也不要再反问用户指哪一个对象。",
    ])
    return "\n\n".join(item for item in (structured_prompt, legacy_prompt) if item)


def visual_relevant_context_prompt(resolution: Mapping[str, Any]) -> str:
    """只向视觉请求提供已经解析出的近邻上下文。

    近邻对话可用于解析“这个”或画面中文字所指的对象，但不能据此断言当前
    图像里存在某个物体、动作或应用。上下文未激活时完全省略，避免无关旧
    话题泄漏到独立的视觉轮次。
    """

    if not resolution.get("active"):
        return ""
    evidence = [
        re.sub(r"\s+", " ", str(item or "")).strip()[:500]
        for item in (
            resolution.get("evidence")
            if isinstance(resolution.get("evidence"), list) else []
        )[-4:]
        if str(item or "").strip()
    ]
    resolved_question = re.sub(
        r"\s+", " ", str(resolution.get("resolvedQuestion") or "")
    ).strip()[:1_000]
    subject = re.sub(
        r"\s+", " ", str(resolution.get("subject") or "")
    ).strip()[:300]
    if not evidence and not resolved_question and not subject:
        return ""
    return "\n".join([
        "[当前视觉任务的相关近期上下文]",
        *[f"- 近期直接承接内容：{item}" for item in evidence],
        *([f"- 当前所指：{subject}"] if subject else []),
        *([f"- 承接后的完整问题：{resolved_question}"] if resolved_question else []),
        "这些内容只用于理解用户所指、当前任务和可见文字的语境。"
        "它们不能证明当前截图中存在任何事物；与当前问题无关的上文必须忽略。",
    ])


class ReferenceContextService:
    """组合结构化说话人账本与旧任务字段，不持有请求或会话状态。"""

    def __init__(self, store: ReferenceStore, visual: VisualIntent) -> None:
        self.store = store
        self.visual = visual

    def recent_user_utterances(
        self,
        messages: list[dict[str, Any]] | None = None,
        limit: int = 10,
    ) -> list[str]:
        """Return recent user-only evidence, never assistant guesses or summaries."""

        context = self.store.read_context()
        values = [
            re.sub(r"\s+", " ", str(item.get("user") or "")).strip()
            for item in context.get("recentTurns", [])[-limit:]
            if isinstance(item, dict) and str(item.get("user") or "").strip()
        ]
        for item in (messages or [])[-limit * 2:]:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            value = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            if value:
                values.append(value)
        deduplicated: list[str] = []
        for value in values:
            if not deduplicated or deduplicated[-1] != value:
                deduplicated.append(value)
        return deduplicated[-limit:]

    def recent_verified_tool_evidence(self, limit: int = 6) -> list[str]:
        """Return only assistant claims backed by a successful local tool call."""

        context = self.store.read_context()
        evidence: list[str] = []
        for item in context.get("recentTurns", [])[-limit * 2:]:
            if not isinstance(item, dict) or str(item.get("toolStatus") or "") != "ok":
                continue
            tool_name = str(item.get("toolUsed") or "")
            if tool_name not in {"web_search", "read_web_page", "get_system_time", "capture_screen", "capture_camera"}:
                continue
            if tool_name == "web_search":
                web_status = item.get("webEvidenceStatus") if isinstance(item.get("webEvidenceStatus"), dict) else {}
                if not web_status.get("sufficient"):
                    continue
            assistant = re.sub(r"\s+", " ", str(item.get("assistant") or "")).strip()
            if assistant:
                evidence.append(f"{tool_name}: {assistant[:700]}")
        return evidence[-limit:]

    def _legacy_contextual_reference_resolution(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        *,
        context_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = dict(context_state) if isinstance(context_state, Mapping) else self.store.read_context()
        stored_task = context.get("activeTask")
        stored_task = stored_task if isinstance(stored_task, dict) else {}
        if (
            stored_task.get("kind") == "sports"
            and stored_task.get("matchup")
            and stored_task.get("resolvedQuestion")
        ):
            stored_matchup = str(stored_task.get("matchup") or "").strip()
            reparsed_matchup = extract_sports_matchup(str(stored_task.get("resolvedQuestion") or ""))
            if reparsed_matchup != stored_matchup:
                # Structured context can outlive the parser version that created it.
                # Do not keep propagating an old false-positive entity split after
                # the current parser can no longer reproduce it from the source text.
                stored_task = dict(stored_task)
                stored_task.pop("matchup", None)
                stored_task["entities"] = extract_sports_entities(
                    str(stored_task.get("resolvedQuestion") or "")
                )
        prior = self.recent_user_utterances(messages, limit=10)
        trusted_tool_evidence = self.recent_verified_tool_evidence(limit=4)
        while prior and prior[-1] == re.sub(r"\s+", " ", user_message).strip():
            prior.pop()
        needs_context = message_needs_recent_context(user_message)
        if not needs_context and is_external_fact_correction_followup(user_message, messages):
            # A factual correction may be phrased as a complete assertion rather
            # than "you were wrong".  It still belongs to the immediately
            # preceding research task when it disputes that answer's lack-of-
            # evidence conclusion.
            needs_context = True
        nearby = [item for item in prior[-8:] if not is_context_meta_message(item)]
        current_matchup = extract_sports_matchup(user_message)
        current_entities = extract_sports_entities(user_message)
        prediction_request = bool(re.search(
            r"猜|预测|押一个|选一个|一定要|非要|谁能赢|谁会赢|哪队能赢|哪边能赢|看好谁|胜率",
            user_message,
        ))
        sports_followup_request = bool(
            prediction_request
            or SPORT_CONTEXT_PATTERN.search(user_message)
            or SPORT_EVALUATION_FOLLOWUP_PATTERN.search(user_message)
            or SPORT_CORRECTION_FOLLOWUP_PATTERN.search(user_message)
            or re.search(
                r"他们?(?:还|能|会)|她们?(?:还|能|会)|能赢|会赢|赢了吗|结束了吗|"
                r"查查|查一下|重新查|再搜|哪场|什么比赛|"
                r"最新(?:的)?(?:比分|赛果|结果)|翻盘|逆转|赛前|赛后",
                user_message,
            )
        )
        explicit_sensor_request = self.visual.is_current_sensor_request(user_message)
        stored_sports_continuation = bool(
            stored_task.get("kind") == "sports"
            and sports_followup_request
            and not explicit_sensor_request
        )
        combined = "\n".join([*nearby, user_message])
        sports_anchor = bool(
            SPORT_CONTEXT_PATTERN.search(combined)
            or re.search(r"\b\d{1,3}\s*[:：-]\s*\d{1,3}\b", combined)
            or extract_sports_matchup(combined)
            or extract_sports_entities(combined)
        )
        sports_related = bool(not explicit_sensor_request and (
            current_matchup
            or (current_entities and sports_followup_request)
            or stored_sports_continuation
            or (sports_anchor and sports_followup_request)
        ))
        evidence = nearby[-4:]
        if sports_related:
            matchup = current_matchup
            for item in [user_message, *reversed(nearby)]:
                if matchup:
                    break
                matchup = extract_sports_matchup(item)
                if matchup:
                    break
            if not matchup and stored_sports_continuation:
                matchup = str(stored_task.get("matchup") or "")
            competitions = SPORT_COMPETITION_PATTERN.findall(combined)
            competition = competitions[-1] if competitions else ""
            if prediction_request:
                intent = "赛前预测"
            elif re.search(r"翻盘|逆转", user_message):
                intent = "能否翻盘"
            elif re.search(r"比分|赛果|几比几|结果", user_message):
                intent = "最新比分"
            elif SPORT_EVALUATION_FOLLOWUP_PATTERN.search(user_message) or re.search(
                r"评价|复盘|赛后|分析|怎么样|主要问题|战犯|踢得|打得|脏不脏|很脏", user_message
            ):
                intent = "赛后评价"
            elif SPORT_CORRECTION_FOLLOWUP_PATTERN.search(user_message) and stored_task.get("intent"):
                intent = str(stored_task.get("intent"))
            else:
                intent_text = "\n".join(nearby[-3:])
                if re.search(r"比分|赛果|几比几|结果", intent_text):
                    intent = "最新比分"
                elif re.search(r"评价|复盘|赛后|分析|怎么样", intent_text):
                    intent = "赛后评价"
                elif re.search(r"翻盘|逆转", intent_text):
                    intent = "能否翻盘"
                elif stored_sports_continuation and stored_task.get("intent"):
                    intent = str(stored_task.get("intent"))
                else:
                    intent = "最新赛况"
            entities = [item.strip() for item in matchup.split(" vs ") if item.strip()] if matchup else current_entities
            if not entities and stored_sports_continuation:
                entities = [str(item) for item in stored_task.get("entities", []) if str(item).strip()]
            subject_entity = matchup or " ".join(entities)
            subject_parts = [item for item in (competition, subject_entity) if item]
            subject = " ".join(dict.fromkeys(subject_parts)) or str(stored_task.get("subject") or "最近讨论的这场比赛")
            years = re.findall(r"(?:19|20)\d{2}", combined)
            analysis_focus = sports_analysis_focus(user_message)
            if not analysis_focus and stored_sports_continuation:
                analysis_focus = [str(item) for item in stored_task.get("analysisFocus", []) if str(item).strip()]
            requested_action = (
                "choose_winner" if intent == "赛前预测"
                else "evaluate_match" if intent == "赛后评价"
                else "answer_current_sports_question"
            )
            constraints = (
                ["必须明确选择或倾向一方", "先给结论再解释", "角色梗不能替代答案", "说明这是预测而非事实"]
                if requested_action == "choose_winner"
                else ["直接回答当前比赛问题", "不得切换到长期记忆或实验室话题"]
            )
            user_position = explicit_user_position(user_message)
            if user_position:
                constraints.extend([
                    "必须直接回应用户本轮明确表达的观点，可以同意、反驳或作出限定",
                    "先给角色本人的判断，再用赛果、场面或赛报证据解释",
                ])
            if "individual_responsibility" in analysis_focus:
                constraints.extend([
                    "必须回应谁应承担最大责任，并说明具体比赛依据",
                    "若来源不足以支持点名，必须明确说证据不足，不能仅凭比分指定战犯",
                ])
            if "discipline_fair_play" in analysis_focus:
                constraints.extend([
                    "必须单独判断对手是否存在粗野或不公平比赛，并用犯规数、牌数或具体动作支撑",
                    "不能把正常身体对抗直接说成踢得脏；证据不足时要明确保留判断",
                ])
            focus_query = " ".join(
                "player ratings errors worst performer responsibility"
                if item == "individual_responsibility"
                else "fouls yellow red cards disciplinary incidents fair play"
                if item == "discipline_fair_play"
                else "team performance tactical analysis"
                for item in analysis_focus
            )
            clarifies_previous = bool(re.search(
                r"我的意思是|我是说|换句话说|具体来说|不是这个意思|我不是这个意思",
                user_message,
            ))
            self_contained_sports_task = bool(
                not needs_context
                and len(re.sub(r"\s+", "", user_message)) >= 8
                and (SPORT_CONTEXT_PATTERN.search(user_message) or SPORT_COMPETITION_PATTERN.search(user_message))
                and (SPORT_EVALUATION_FOLLOWUP_PATTERN.search(user_message) or re.search(r"[？?]", user_message))
            )
            current_is_specific = bool(
                (
                    current_matchup
                    or current_entities
                    or SPORT_COMPETITION_PATTERN.search(user_message)
                    or self_contained_sports_task
                )
                and not clarifies_previous
            )
            resolved_question = (
                user_message.strip()
                if current_is_specific
                else f"{stored_task.get('resolvedQuestion') or (nearby[-1] if nearby else subject)}；追问：{user_message.strip()}"
            )
            return {
                "active": bool(current_is_specific or needs_context or matchup or stored_sports_continuation),
                "kind": "sports",
                "subject": subject,
                "intent": intent,
                "resolvedQuestion": resolved_question[:700],
                # Preserve the user's retrieval qualifiers (year, event stage,
                # edition, location, and exact object).  A compressed subject such
                # as just "世界杯" is not enough to discover the actual event.
                "searchQuery": f"{resolved_question} {focus_query}"[:500],
                "evidence": [] if current_is_specific else evidence,
                "matchup": matchup,
                "eventYear": years[-1] if years else "",
                "trustedToolEvidence": [] if current_is_specific else trusted_tool_evidence,
                "requestedAction": requested_action,
                "constraints": constraints,
                "entities": entities,
                "analysisFocus": analysis_focus,
                "userPosition": user_position,
                "isContinuation": bool(
                    not current_is_specific
                    and (stored_sports_continuation or (needs_context and not current_matchup))
                ),
                "sourceEvidence": (
                    [] if current_is_specific else [*evidence, *trusted_tool_evidence]
                ),
            }
        if not needs_context or not evidence:
            compact_current = re.sub(r"[\s，。！？?!,.]+", "", user_message)
            passive_ack = bool(re.fullmatch(r"(?:好|好的|嗯+|知道了|明白了|等等|稍等|继续)", compact_current))
            return {
                "active": False,
                "kind": "none",
                "evidence": [],
                "clearActiveTask": bool(stored_task and compact_current and not passive_ack),
            }
        return {
            "active": True,
            "kind": "general",
            "subject": evidence[-1][:300],
            "intent": user_message[:200],
            "resolvedQuestion": f"承接上一条用户原文“{evidence[-1][:300]}”：{user_message[:200]}",
            "searchQuery": web_search_query_from_message(" ".join([*evidence[-2:], user_message])),
            "evidence": evidence,
            "trustedToolEvidence": trusted_tool_evidence,
            "requestedAction": "answer_followup",
            "constraints": ["直接承接最近用户原文", "不得让长期记忆覆盖当前追问"],
            "isContinuation": True,
        }

    def _build_current_discourse_ledger(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        *,
        request_id: str = "",
        context_state: Mapping[str, Any] | None = None,
    ) -> tuple[DiscourseLedger, ReferenceResolution]:
        context = dict(context_state) if isinstance(context_state, Mapping) else self.store.read_context()
        current_message_id = _discourse_message_id(user_message, request_id)
        ledger = build_discourse_ledger(
            recent_turns=(
                context.get("recentTurns")
                if isinstance(context.get("recentTurns"), list) else []
            ),
            client_messages=messages or [],
            current_user_message=user_message,
            current_message_id=current_message_id,
            pending_question=(
                context.get("pendingAssistantQuestion")
                if isinstance(context.get("pendingAssistantQuestion"), Mapping) else None
            ),
            existing_ledger=(
                context.get("referenceLedger")
                if isinstance(context.get("referenceLedger"), Mapping) else None
            ),
        )
        resolution = resolve_discourse_references(
            ledger,
            message_id=current_message_id,
        )
        return ledger, resolution

    def contextual_reference_resolution(
        self,
        user_message: str,
        messages: list[dict[str, Any]] | None = None,
        *,
        request_id: str = "",
        current_artifacts: Sequence[Mapping[str, Any]] = (),
        resolved_task: str = "",
        context_state: Mapping[str, Any] | None = None,
        model_owned: bool = False,
        legacy_resolver: Callable[..., dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """把结构化话语账本投影到旧任务上下文。

        旧解析器继续生成体育任务专用字段，但不再决定说话人和共指事实；
        所有路径都使用这里统一组装、包含消息、引语与 OCR 来源的账本。
        """

        ledger, resolution = self._build_current_discourse_ledger(
            user_message,
            messages,
            request_id=request_id,
            context_state=context_state,
        )
        authoritative_artifacts = [
            item for item in current_artifacts if isinstance(item, Mapping)
        ]
        if authoritative_artifacts:
            continued_task = all(
                bool(item.get("_taskContinuation")) for item in authoritative_artifacts
            )
            ledger = extend_with_current_artifacts(ledger, authoritative_artifacts)
            resolution = resolve_discourse_references(
                ledger,
                message_id=ledger.current_message_id,
            )
        if model_owned:
            # 保留来源记录供追踪，但不把词面先行项提升成检索目标、入选消息
            # 或确定性提示；语义解释仍完全交给模型。
            return {
                "active": False, "kind": "model_owned", "interpretationOwner": "model",
                "referenceLedger": ledger.to_dict(), "referenceResolution": {}, "referenceEvidence": [],
            }
        legacy = (legacy_resolver or self._legacy_contextual_reference_resolution)(
            user_message, messages, context_state=context_state,
        )
        evidence = _reference_evidence(ledger, resolution)
        legacy["referenceLedger"] = ledger.to_dict()
        legacy["referenceResolution"] = resolution.to_dict()
        legacy["referenceEvidence"] = evidence

        if authoritative_artifacts:
            target_message = next((
                item for item in reversed(ledger.messages)
                if item.turn_id == ledger.current_message_id
                and item.modality == "tool_observation"
                and item.source_id.startswith("artifact:")
            ), None)
            artifact_names = [
                str(item.get("name") or item.get("fileName") or "attachment")[:240]
                for item in authoritative_artifacts
            ]
            subject = (
                target_message.text
                if target_message is not None
                else (
                    "最近附件任务续接：" if continued_task else "本轮新上传附件："
                ) + "；".join(artifact_names)
            )
            semantic_request = (resolved_task.strip() or user_message)[:1200]
            legacy.update({
                "active": True,
                "kind": "artifact",
                "subject": subject[:700],
                "intent": semantic_request[:200],
                "resolvedQuestion": semantic_request,
                "requestedAction": (
                    "continue_recent_artifact_task" if continued_task else "answer_current_artifact"
                ),
                "constraints": [
                    (
                        "语义任务解析选中的最近附件是当前任务对象"
                        if continued_task else "本轮新附件是当前任务的权威对象"
                    ),
                    "旧对话只可作为背景，不得替代或覆盖本轮附件",
                    "附件读取成功时依据附件正文回答；读取失败时明确说明失败",
                ],
                "isContinuation": continued_task,
                "evidence": [subject[:700]],
                "sourceEvidence": [subject[:700]],
                "authoritativeTarget": {
                    "kind": "current_artifacts",
                    "artifactIds": [
                        str(item.get("id") or item.get("attachmentId") or "")[:160]
                        for item in authoritative_artifacts
                    ],
                    "names": artifact_names,
                },
                "clearActiveTask": True,
            })

        # If the structured resolver found an actual antecedent, do not let the
        # old user-only history heuristic suppress assistant-side references such
        # as “你刚才说的那个”.  Self-contained phrases such as “这个模型” never
        # enter this branch.
        if reference_requires_context(resolution) and evidence and not legacy.get("active"):
            latest = evidence[-1]
            speaker_label = "AMADEUS" if latest["speaker"] == "assistant" else "用户"
            legacy.update({
                "active": True,
                "kind": "general",
                "subject": latest["text"][:300],
                "intent": user_message[:200],
                "resolvedQuestion": (
                    f"承接上一条{speaker_label}原文“{latest['text'][:300]}”："
                    f"{user_message[:200]}"
                ),
                "requestedAction": "answer_followup",
                "constraints": [
                    "严格按指代账本中的 speaker 和 referenceLinks 理解我、你及引用内容",
                    "不得把 AMADEUS 说过的话改称为用户说过",
                    "不得把用户说过的话改称为 AMADEUS 说过",
                ],
                "isContinuation": True,
                "evidence": [latest["text"]],
                "sourceEvidence": [latest["text"]],
            })
        if not authoritative_artifacts and resolved_task and resolved_task.strip() != user_message.strip():
            # The shared semantic task may have left the artifact topic.  Preserve
            # that completed question instead of letting a legacy reference hint
            # drag an elliptical answer back to the old picture.
            legacy.update({
                "active": True, "kind": "general", "subject": resolved_task[:700],
                "intent": resolved_task[:1200], "resolvedQuestion": resolved_task[:1200],
                "requestedAction": "answer_resolved_dialogue_task",
                "authoritativeTarget": {"kind": "dialogue_task", "artifactIds": []},
                "constraints": ["完成补全后的当前要求；没有绑定图片，不要恢复旧图像任务"],
                "clearActiveTask": True,
            })
        return legacy
