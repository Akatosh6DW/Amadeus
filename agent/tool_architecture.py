"""Unified tool selection primitives for dialogue, perception, and actions.

The module decides *which capabilities may be relevant* before provider or UI
code runs.  Existing tool executors remain in ``server.py`` during the first
migration phase; this layer supplies one auditable preflight, registry, search,
and target validator instead of adding more phrase-specific gates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import re
from typing import Any, Literal, Mapping, Sequence
from agent.tool_arguments import FILE_TOOL_INPUT_SCHEMAS

PerceptionTarget = Literal[
    "none",
    "screen",
    "physical_user",
    "uploaded_image",
    "document",
    "unknown_visual",
]


@dataclass(frozen=True)
class PerceptionNeed:
    required: bool = False
    target_domain: PerceptionTarget = "none"
    freshness: str = "none"
    existing_artifact_id: str | None = None
    confidence: float = 1.0
    ambiguities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["targetDomain"] = value.pop("target_domain")
        value["existingArtifactId"] = value.pop("existing_artifact_id")
        value["ambiguities"] = list(self.ambiguities)
        return value


@dataclass(frozen=True)
class TurnPreflight:
    reply_mode: str
    speech_act: str
    candidate_capabilities: tuple[str, ...]
    mandatory_capabilities: tuple[str, ...]
    tool_escalation_allowed: bool
    likely_no_tool: bool
    confidence: float
    risk_flags: tuple[str, ...]
    perception: PerceptionNeed
    current_state_evidence_requirement: Mapping[str, Any] | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "replyMode": self.reply_mode,
            "speechAct": self.speech_act,
            "candidateCapabilities": list(self.candidate_capabilities),
            "mandatoryCapabilities": list(self.mandatory_capabilities),
            "toolEscalationAllowed": self.tool_escalation_allowed,
            "likelyNoTool": self.likely_no_tool,
            "confidence": self.confidence,
            "riskFlags": list(self.risk_flags),
            "perception": self.perception.to_dict(),
            "currentStateEvidenceRequirement": (
                dict(self.current_state_evidence_requirement)
                if isinstance(self.current_state_evidence_requirement, Mapping)
                else None
            ),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ToolDef:
    tool_id: str
    family: str
    description: str
    permission_key: str = ""
    side_effect: bool = False
    sensitive: bool = False
    target_domains: tuple[PerceptionTarget, ...] = ()
    aliases: tuple[str, ...] = ()
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_type: str = "object"
    permission_scope: str = ""
    read_only: bool = True
    concurrency_safe: bool = False
    side_effect_class: str = "none"
    estimated_latency_ms: int = 0
    freshness_semantics: str = "not_applicable"

    def __post_init__(self) -> None:
        # Preserve the old constructor flags while exposing the richer ToolDef
        # contract required by the unified runtime.
        if self.side_effect and self.read_only:
            object.__setattr__(self, "read_only", False)
        if self.side_effect and self.side_effect_class == "none":
            object.__setattr__(self, "side_effect_class", "external_mutation")
        if not self.permission_scope and self.permission_key:
            object.__setattr__(self, "permission_scope", self.permission_key)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.tool_id,
            "family": self.family,
            "description": self.description,
            "permissionKey": self.permission_key,
            "sideEffect": self.side_effect,
            "sensitive": self.sensitive,
            "targetDomains": list(self.target_domains),
            "inputSchema": dict(self.input_schema),
            "outputType": self.output_type,
            "permissionScope": self.permission_scope,
            "readOnly": self.read_only,
            "concurrencySafe": self.concurrency_safe,
            "sideEffectClass": self.side_effect_class,
            "estimatedLatencyMs": max(0, int(self.estimated_latency_ms)),
            "freshnessSemantics": self.freshness_semantics,
        }


@dataclass(frozen=True)
class ToolValidation:
    allowed: bool
    reason: str
    requires_clarification: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ToolRegistry:
    def __init__(self, definitions: Sequence[ToolDef]) -> None:
        self._definitions = {item.tool_id: item for item in definitions}

    def get(self, tool_id: str) -> ToolDef | None:
        return self._definitions.get(str(tool_id or "").strip())

    def all(self) -> tuple[ToolDef, ...]:
        return tuple(self._definitions.values())

    def by_families(self, families: Sequence[str]) -> tuple[ToolDef, ...]:
        wanted = {str(item) for item in families}
        return tuple(item for item in self.all() if item.family in wanted)

    def concurrency_safe(self, tool_ids: Sequence[str]) -> bool:
        """Return true only when every requested tool explicitly opts in."""

        definitions = [self.get(tool_id) for tool_id in tool_ids]
        return bool(definitions) and all(
            item is not None
            and item.concurrency_safe
            and item.read_only
            and not item.side_effect
            for item in definitions
        )

    def search(
        self,
        query: str,
        *,
        families: Sequence[str] = (),
        limit: int = 5,
    ) -> tuple[ToolDef, ...]:
        """Search local tool metadata; this never executes a tool."""

        normalized = _compact(query)
        tokens = {
            item
            for item in re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]{1,4}", normalized)
            if item
        }
        family_filter = {str(item) for item in families if str(item)}
        ranked: list[tuple[float, str, ToolDef]] = []
        for definition in self.all():
            if family_filter and definition.family not in family_filter:
                continue
            haystack = _compact(" ".join([
                definition.tool_id,
                definition.family,
                definition.description,
                *definition.aliases,
            ]))
            score = 0.0
            if normalized and normalized in haystack:
                score += 5.0
            for token in tokens:
                if token in haystack:
                    score += min(2.0, 0.4 + len(token) * 0.16)
            if definition.tool_id in normalized:
                score += 6.0
            if score:
                ranked.append((score, definition.tool_id, definition))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked[: max(1, min(12, limit))])


def merge_searched_tool_candidates(
    preflight: TurnPreflight,
    definitions: Sequence[ToolDef],
    *,
    query: str = "",
) -> TurnPreflight:
    """Feed local capability-search results back into the active turn.

    Search only discovers definitions; it never makes a tool mandatory and it
    never bypasses permission or side-effect validation.  Keeping this merge in
    the architecture layer lets every capability family use the same path
    instead of teaching the runtime one special case at a time.
    """

    discovered = tuple(
        dict.fromkeys(
            item.tool_id
            for item in definitions
            if isinstance(item, ToolDef) and item.tool_id
        )
    )
    if not discovered:
        return preflight
    candidates = tuple(dict.fromkeys((
        *preflight.candidate_capabilities,
        *discovered,
    )))
    risk_flags = list(preflight.risk_flags)
    if "capture_camera" in discovered and "camera_sensitive" not in risk_flags:
        risk_flags.append("camera_sensitive")
    compact_query = re.sub(r"\s+", " ", str(query or "")).strip()[:120]
    reason = (
        f"capability_search_result:{compact_query}"
        if compact_query
        else "capability_search_result"
    )
    return replace(
        preflight,
        candidate_capabilities=candidates,
        likely_no_tool=False,
        risk_flags=tuple(risk_flags),
        reasons=tuple(dict.fromkeys((*preflight.reasons, reason))),
    )


DEFAULT_TOOL_REGISTRY = ToolRegistry((
    ToolDef(
        "capture_screen", "visual_perception",
        "读取当前电脑屏幕、窗口、应用界面、网页、游戏或桌面画面",
        "permissionReadScreen", target_domains=("screen",),
        aliases=("屏幕", "截图", "桌面", "窗口", "页面", "我在电脑上做什么"),
        side_effect_class="sensor_capture", estimated_latency_ms=2200,
        freshness_semantics="must_capture_current_unless_existing_artifact_requested",
    ),
    ToolDef(
        "capture_camera", "visual_perception",
        "通过摄像头观察用户本人当前的外表、表情、动作或身后环境",
        "permissionReadCamera", sensitive=True, target_domains=("physical_user",),
        aliases=("摄像头", "相机", "脸", "表情", "穿着", "动作", "手里"),
        side_effect_class="sensor_capture", estimated_latency_ms=2500,
        freshness_semantics="must_capture_current",
    ),
    ToolDef(
        "read_image", "visual_perception",
        "读取用户已经上传或当前消息附带的图片",
        target_domains=("uploaded_image",),
        aliases=("上传图片", "附件图片", "这张图", "刚发的图"),
        concurrency_safe=True, estimated_latency_ms=1800,
        freshness_semantics="bound_attachment_artifact",
    ),
    ToolDef(
        "read_attachment", "document",
        "读取用户已经上传的文档或附件内容",
        target_domains=("document",),
        aliases=("附件", "文件", "文档", "刚发的文件"),
        concurrency_safe=True, estimated_latency_ms=600,
        freshness_semantics="bound_attachment_artifact",
    ),
    ToolDef(
        "web_search", "public_research",
        "检索可能变化、未知或需要来源核验的公网信息",
        "permissionWebSearch", aliases=("联网", "搜索", "核实", "最新", "来源"),
        input_schema={"query": "string", "provider": "optional: duckduckgo|yahoo|brave|wikipedia; omitted uses configured provider",
                      "locale": "optional language code, e.g. zh-CN|ja|en; wikipedia selects this language edition",
                      "freshness": "optional: pd (day), pw (week), pm (month), py (year); dateFilter in the result reports whether the provider applied it"}, concurrency_safe=True,
        estimated_latency_ms=2800, freshness_semantics="provider_result_time",
    ),
    ToolDef(
        "read_web_page", "public_research",
        "读取指定公网网页的正文并形成可引用证据",
        "permissionWebSearch", aliases=("网页正文", "链接内容", "读取网站"),
        input_schema={"url": "public_https_url", "researchQuery": "optional: the question or section to locate in a long document"}, concurrency_safe=True,
        estimated_latency_ms=2200, freshness_semantics="retrieved_at",
    ),
    ToolDef(
        "open_browser", "external_action",
        "在系统默认浏览器打开用户明确指定或确认的公网目标",
        "permissionOpenBrowser", side_effect=True,
        aliases=("打开网页", "打开网站", "浏览器"),
        input_schema={"url": "public_https_url"},
        side_effect_class="external_navigation", estimated_latency_ms=300,
        freshness_semantics="target_resolution_time",
    ),
    ToolDef(
        "get_system_time", "local_information",
        "读取当前系统日期、时间和时区",
        "permissionReadSystemTime", aliases=("几点", "日期", "时间"),
        concurrency_safe=True, estimated_latency_ms=5,
        freshness_semantics="read_at",
    ),
    ToolDef(
        "local_compute", "deterministic_compute",
        "精确执行字符/子串计数、文本长度和基础算术；回答依赖精确结果时使用，不要凭语言估算",
        aliases=("精确计算", "字符计数", "字数", "文本长度", "算术"),
        input_schema={
            "operation": "count_character|count_substring|text_length|calculate",
            "text": "string for text operations",
            "target": "one character for count_character; non-empty string for count_substring",
            "caseSensitive": "boolean; defaults to false",
            "normalization": "none|NFC|NFD|NFKC|NFKD; defaults to NFC",
            "overlapping": "boolean for count_substring; defaults to false",
            "unit": "code_points|non_whitespace|words|lines for text_length",
            "expression": "numeric expression using + - * / // % ** and parentheses",
            "expected": "optional non-negative integer for text operations",
        },
        concurrency_safe=True, estimated_latency_ms=1,
        freshness_semantics="not_applicable",
    ),
    ToolDef(
        "todo_search", "local_productivity",
        "按名称、内容、状态、时间、序号或最近引用检索并解析用户本机待办与日程",
        aliases=("查待办", "查日程", "某项任务", "刚才那个", "第几个任务"),
        input_schema={
            "targetRef": {
                "taskId": "string",
                "title": "string",
                "query": "string",
                "status": "active|completed|all",
                "dueAfter": "ISO-8601 datetime",
                "dueBefore": "ISO-8601 datetime",
                "ordinal": "1-based integer",
                "relative": "last_mentioned|listed_item",
            },
            "limit": "integer 1..20",
        },
        concurrency_safe=True, estimated_latency_ms=10,
        freshness_semantics="sqlite_read_at",
    ),
    ToolDef(
        "todo_list", "local_productivity",
        "读取用户本机的待办、任务与日程列表",
        aliases=("待办", "任务", "日程", "安排", "todo"),
        input_schema={"scope": "active|completed|all"},
        concurrency_safe=True, estimated_latency_ms=10,
        freshness_semantics="sqlite_read_at",
    ),
    ToolDef(
        "todo_manage", "local_productivity",
        "创建、修改、完成或推迟本机任务；支持延迟触发，到点提醒或用 onDue 交给 Amadeus 执行",
        side_effect=True,
        aliases=("添加待办", "修改任务", "完成任务", "删除任务", "推迟提醒"),
        input_schema={
            "action": "create|update|complete|reopen|delete|snooze",
            "taskId": "string",
            "targetRef": "Todo target selector returned through todo_search semantics",
            "title": "string",
            "details": "string",
            "onDue": "optional instruction for Amadeus to execute at the scheduled time using normal conversation/tools; empty means only remind the user",
            "dueAt": "Legacy explicit ISO-8601 datetime; omit when using timeSpec; empty for all-day tasks",
            "timeSpec": {
                "kind": "next_clock|calendar|after|existing_date; model chooses the intended occurrence, backend computes dueAt",
                "clock": "HH:MM[:SS], required except after",
                "date": "YYYY-MM-DD, calendar only; use date OR dayOffset",
                "dayOffset": "nonnegative integer from the frozen local date, calendar only",
                "seconds": "positive integer duration, after only",
                "reason": "brief basis for the chosen occurrence, especially after midnight",
            },
            "dueDate": "YYYY-MM-DD date/start date for an all-day task, or empty; mutually exclusive with dueAt",
            "recurrenceRule": "daily only when daily repetition is requested, otherwise empty",
            "timezone": "IANA timezone name",
            "remindBeforeMinutes": "integer 0..10080",
            "priority": "low|normal|high",
            "snoozeMinutes": "integer 1..43200",
            "snoozeUntil": "ISO-8601 datetime",
            "expectedVersion": "integer from the current-turn Todo database snapshot or todo_search result",
            "commitIntent": "true when the user requests a mutation or clearly reports achieving the corresponding task goal",
        },
        side_effect_class="local_state_mutation", estimated_latency_ms=20,
        freshness_semantics="not_applicable",
    ),
    ToolDef(
        "bookmark_list", "local_productivity",
        "读取或按称呼查询用户保存在本机的网页收藏；称呼只做精确身份解析，不猜相似网站",
        aliases=("网页收藏", "收藏夹", "网址收藏", "已保存网站"),
        input_schema={"query": "optional exact or display filter", "limit": "integer 1..200"},
        estimated_latency_ms=10,
        freshness_semantics="sqlite_read_at",
    ),
    ToolDef(
        "bookmark_manage", "local_productivity",
        "保存、改名、修改网址或删除用户本机网页收藏；只有用户当前明确要求变更时调用",
        side_effect=True,
        aliases=("收藏网址", "保存网页", "修改收藏", "删除收藏"),
        input_schema={
            "action": "create|update|delete",
            "bookmarkId": "required for update/delete; use a real id from bookmark_list",
            "name": "user-chosen display name",
            "url": "public HTTP(S) URL supplied by the user or already bound to this exact name in the local website directory",
            "expectedVersion": "integer from bookmark_list result",
            "commitIntent": "true only when the user explicitly asked to mutate bookmarks",
        },
        side_effect_class="local_state_mutation", estimated_latency_ms=20,
        freshness_semantics="not_applicable",
    ),
    ToolDef(
        "list_directory", "workspace_file",
        "列出授权工作区里的目录内容",
        "permissionFileRead", aliases=("目录", "文件列表"),
        input_schema=FILE_TOOL_INPUT_SCHEMAS["list_directory"],
        concurrency_safe=True, estimated_latency_ms=30,
        freshness_semantics="filesystem_read_at",
    ),
    ToolDef(
        "read_text_file", "workspace_file",
        "读取授权工作区里的文本文件",
        "permissionFileRead", aliases=("读取文件", "查看文件内容"),
        input_schema=FILE_TOOL_INPUT_SCHEMAS["read_text_file"],
        concurrency_safe=True, estimated_latency_ms=40,
        freshness_semantics="filesystem_read_at",
    ),
    ToolDef(
        "create_directory", "workspace_file",
        "在授权工作区内创建目录",
        "permissionFileWrite", side_effect=True, aliases=("创建目录",),
        input_schema=FILE_TOOL_INPUT_SCHEMAS["create_directory"],
        side_effect_class="workspace_write", estimated_latency_ms=30,
        freshness_semantics="not_applicable",
    ),
    ToolDef(
        "write_text_file", "workspace_file",
        "在授权工作区内创建或修改文本文件",
        "permissionFileWrite", side_effect=True, aliases=("写文件", "修改文件"),
        input_schema=FILE_TOOL_INPUT_SCHEMAS["write_text_file"],
        side_effect_class="workspace_write", estimated_latency_ms=80,
        freshness_semantics="not_applicable",
    ),
))


_OBSERVATION_REQUEST = re.compile(
    r"(?:你|助手|amadeus)?.{0,4}"
    r"(?:看|看看|看下|看一下|瞧|瞧瞧|观察|辨认|识别|检查|分析|判断|猜猜|读)"
    r".{0,30}"
    r"(?:我|这里|这儿|这个|这张|那张|画面|屏幕|桌面|窗口|页面|图|图片|"
    r"表情|脸|衣服|穿着|动作|姿势|手里|身后|在做|干什么|干嘛|做啥)",
    re.I,
)
_CURRENT_ACTIVITY = re.compile(
    r"(?:你.{0,4})?(?:看|观察|判断|猜猜).{0,10}"
    r"我(?:现在|正在|这会儿|此刻)?.{0,5}(?:在)?(?:做什么|干什么|干嘛|做啥)",
    re.I,
)
_SCREEN_TARGET = re.compile(
    r"屏幕|截图|截屏|桌面|显示器|电脑|窗口|前台|应用|程序|软件|终端|代码|"
    r"网页|网站|浏览器|页面|论坛|帖子|游戏|文档|编辑器|ide|screen|desktop",
    re.I,
)
_PHYSICAL_TARGET = re.compile(
    r"^(?:你)?(?:看看|看下|看一下|瞧瞧)我(?:吧|呢|呀|啊|嘛)?[？?!！。]*$|"
    r"摄像头|相机|镜头|我的?(?:脸|脸色|表情|气色|精神|眼睛|衣服|穿着|"
    r"姿势|动作|手势|手里|手中|身后|背后|发型|身体)|"
    r"我(?:现在|今天|此刻|这会儿)?的?(?:脸|脸色|表情|气色|精神|眼睛|衣服|"
    r"穿着|姿势|动作|手势|手里|手中|身后|背后|发型|身体)|"
    r"我(?:看起来|显得|长什么样)|camera|webcam",
    re.I,
)
_ATTACHMENT_TARGET = re.compile(
    r"(?:刚刚|刚才|新|最新)?(?:发|传|上传|添加)?(?:的)?"
    r"(?:这张|那张|这个|那个)?(?:图|图片|图像|附件|文件)|"
    r"(?:这张|那张)(?:图|图片)|附件里|文件里",
    re.I,
)
_DOCUMENT_TARGET = re.compile(r"文档|文件|附件|pdf|docx|txt|markdown|md\b", re.I)
_NON_REQUEST = re.compile(
    r"为什么|原理|是什么机制|怎么实现|如何实现|能不能实现|如果|假如|"
    r"昨天|之前|以前|一般(?:来说)?|刷新率|分辨率|摄像头是什么|屏幕是什么",
    re.I,
)
_ABSTRACT_REVIEW = re.compile(
    r"(?:你)?看我(?:说|讲|想|分析|理解|判断|回答|写)(?:得|的|这)?|"
    r"(?:你)?(?:看|看看|分析|检查).{0,6}"
    r"(?:这个|这段|上述|上面)(?:问题|说法|想法|方案|逻辑|回答|观点|文字|描述)|"
    r"(?:你看|我看)(?:，|,|吧|呢)",
    re.I,
)
_FIGURATIVE_OR_EVALUATIVE_SEEING = re.compile(
    r"(?:看出|看透|看穿|看懂|看清).{0,10}(?:想法|心思|心情|情绪|脆弱|"
    r"意图|态度|性格|本质|真心)|(?:高看|小看|看重|看轻|另眼相看)|"
    r"(?:怎么看|如何看待|看法|观点|评价)",
    re.I,
)
_NEGATED_SENSOR_REQUEST = re.compile(
    r"(?:不是|并非)(?:在)?(?:叫|让|要|想让)?你.{0,6}(?:看|读取|观察)"
    r"(?:屏幕|截图|摄像头|相机|画面)|"
    r"(?:不用|不要|无需|别)(?:再)?(?:看|读取|观察|打开)"
    r"(?:屏幕|截图|摄像头|相机|画面)",
    re.I,
)
_HISTORICAL_VISUAL = re.compile(
    r"上一张|前一张|前几张|以前|之前|刚刚|刚才|历史|保存的|截过的|拍过的",
    re.I,
)
_REMOTE_WEB_RESOURCE = re.compile(
    r"https?://|(?:读取|总结|分析|查看).{0,8}(?:这个|该|指定)?(?:网页|网站|链接)|"
    r"(?:网页|网站|链接).{0,8}(?:正文|内容|写了什么|讲了什么)",
    re.I,
)


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _message_texts(messages: Sequence[Mapping[str, Any]] | None) -> list[str]:
    return [
        str(item.get("content") or "")
        for item in (messages or [])[-10:]
        if isinstance(item, Mapping) and str(item.get("content") or "").strip()
    ]


def infer_perception_need(
    user_text: str,
    messages: Sequence[Mapping[str, Any]] | None = None,
    *,
    has_image_attachment: bool = False,
    has_document_attachment: bool = False,
    existing_artifact_id: str | None = None,
) -> PerceptionNeed:
    """Resolve the intended visual target without defaulting to the camera."""

    text = _compact(user_text)
    if not text:
        return PerceptionNeed()

    attachment_reference = bool(_ATTACHMENT_TARGET.search(text))
    document_reference = attachment_reference and bool(_DOCUMENT_TARGET.search(text))
    observation = bool(_OBSERVATION_REQUEST.search(text) or _CURRENT_ACTIVITY.search(text))
    explicit_screen = bool(_SCREEN_TARGET.search(text))
    explicit_physical = bool(_PHYSICAL_TARGET.search(text))

    if _NEGATED_SENSOR_REQUEST.search(text) and not attachment_reference:
        return PerceptionNeed(
            required=False,
            target_domain="none",
            confidence=0.98,
            ambiguities=("sensor_observation_is_explicitly_negated",),
        )

    if _REMOTE_WEB_RESOURCE.search(text):
        return PerceptionNeed(
            required=False,
            target_domain="none",
            freshness="remote_resource",
            confidence=0.97,
            ambiguities=("remote_web_resource_is_not_current_screen",),
        )
    if explicit_screen and _HISTORICAL_VISUAL.search(text):
        return PerceptionNeed(
            required=False,
            target_domain="screen",
            freshness="existing_artifact",
            confidence=0.96,
            ambiguities=("historical_visual_requires_recall_not_new_capture",),
        )

    if has_document_attachment and (
        document_reference or not has_image_attachment
    ):
        return PerceptionNeed(
            required=True,
            target_domain="document",
            freshness="existing_artifact",
            existing_artifact_id=existing_artifact_id,
            confidence=0.98,
        )
    if has_image_attachment or (attachment_reference and existing_artifact_id):
        return PerceptionNeed(
            required=True,
            target_domain="uploaded_image",
            freshness="existing_artifact",
            existing_artifact_id=existing_artifact_id,
            confidence=0.98,
        )
    if has_document_attachment or (document_reference and existing_artifact_id):
        return PerceptionNeed(
            required=True,
            target_domain="document",
            freshness="existing_artifact",
            existing_artifact_id=existing_artifact_id,
            confidence=0.98,
        )

    # Mentions of screen/camera technology or hypothetical observations are not
    # requests to activate a sensor.
    if _NON_REQUEST.search(text) and not observation:
        return PerceptionNeed(
            required=False,
            target_domain="none",
            confidence=0.95,
            ambiguities=("sensor_term_is_discussion_not_observation",),
        )
    if _ABSTRACT_REVIEW.search(text) and not explicit_screen and not explicit_physical:
        return PerceptionNeed(
            required=False,
            target_domain="none",
            confidence=0.9,
            ambiguities=("review_request_uses_conversation_content",),
        )
    if (
        _FIGURATIVE_OR_EVALUATIVE_SEEING.search(text)
        and not explicit_screen
        and not explicit_physical
        and not attachment_reference
    ):
        return PerceptionNeed(
            required=False,
            target_domain="none",
            confidence=0.94,
            ambiguities=("seeing_verb_is_figurative_or_evaluative",),
        )
    if explicit_screen and (observation or re.search(r"屏幕上|截图里|桌面上|窗口里|页面上", text)):
        return PerceptionNeed(
            required=True,
            target_domain="screen",
            freshness="current",
            confidence=0.98,
        )
    if explicit_physical and observation:
        return PerceptionNeed(
            required=True,
            target_domain="physical_user",
            freshness="current",
            confidence=0.98,
        )
    if not observation:
        return PerceptionNeed()

    if _CURRENT_ACTIVITY.search(text):
        context_text = _compact(" ".join(_message_texts(messages)))
        screen_score = len(_SCREEN_TARGET.findall(context_text))
        physical_score = len(_PHYSICAL_TARGET.findall(context_text))
        if screen_score >= physical_score + 1 and screen_score:
            return PerceptionNeed(
                required=True,
                target_domain="screen",
                freshness="current",
                confidence=min(0.92, 0.68 + 0.06 * screen_score),
                ambiguities=("current_activity_resolved_from_recent_screen_context",),
            )
        if physical_score >= screen_score + 2 and physical_score:
            return PerceptionNeed(
                required=True,
                target_domain="physical_user",
                freshness="current",
                confidence=min(0.88, 0.64 + 0.05 * physical_score),
                ambiguities=("current_activity_resolved_from_recent_physical_context",),
            )
        # In a desktop assistant, an otherwise unqualified request to inspect
        # the user's current activity resolves to the current screen. Camera
        # observation remains opt-in through physical/appearance intent. This
        # gives ambiguous wording the least-sensitive useful interpretation
        # instead of exposing both sensors and hoping the model picks one.
        return PerceptionNeed(
            required=True,
            target_domain="screen",
            freshness="current",
            confidence=0.72,
            ambiguities=("current_activity_defaulted_to_desktop_screen",),
        )

    if explicit_screen:
        return PerceptionNeed(True, "screen", "current", confidence=0.9)
    if explicit_physical:
        return PerceptionNeed(True, "physical_user", "current", confidence=0.9)
    if attachment_reference:
        return PerceptionNeed(
            required=True,
            target_domain="uploaded_image",
            freshness="existing_artifact",
            existing_artifact_id=existing_artifact_id,
            confidence=0.76,
            ambiguities=("attachment_reference_without_bound_artifact",),
        )
    return PerceptionNeed(
        # A lexical observation-shaped phrase without a resolved screen,
        # physical-user or attachment target is not authority to activate or
        # even recommend a sensor. Keep the ambiguity for the model's typed
        # capability decision, but do not turn it into a local tool hint.
        required=False,
        target_domain="unknown_visual",
        freshness="none",
        confidence=0.38,
        ambiguities=("visual_object_is_not_resolved",),
    )


def tool_ids_for_perception(need: PerceptionNeed) -> tuple[str, ...]:
    mapping = {
        "screen": ("capture_screen",),
        "physical_user": ("capture_camera",),
        "uploaded_image": ("read_image",),
        # A bound document always needs the attachment reader, while a text
        # transformation may also need to persist a result in the authorized
        # Agent workspace.  Expose the writer as a choice; do not make it an
        # obligation merely because a document exists.
        "document": ("read_attachment", "write_text_file"),
        # Never offer a sensitive camera as an ambiguity resolver. The camera
        # enters the capability envelope only after physical-user intent has
        # been established.
        "unknown_visual": ("capture_screen", "read_image"),
        "none": (),
    }
    return mapping[need.target_domain]


def build_turn_preflight(
    user_text: str,
    messages: Sequence[Mapping[str, Any]] | None = None,
    *,
    reply_mode: str = "normal",
    speech_act: str = "conversation",
    has_image_attachment: bool = False,
    has_document_attachment: bool = False,
    existing_artifact_id: str | None = None,
    schedule_intent: Mapping[str, Any] | None = None,
) -> TurnPreflight:
    perception = infer_perception_need(
        user_text,
        messages,
        has_image_attachment=has_image_attachment,
        has_document_attachment=has_document_attachment,
        existing_artifact_id=existing_artifact_id,
    )
    perception_candidates = (
        tool_ids_for_perception(perception)
        if perception.required else ()
    )
    candidates = perception_candidates
    # Local language matching may propose perception candidates, but it cannot
    # force a sensor.  A capability becomes mandatory only after the model has
    # returned a structured target/authorization and the server has bound it
    # with ``bind_required_capabilities``.
    mandatory_reader = {
        "uploaded_image": "read_image",
        "document": "read_attachment",
    }.get(perception.target_domain)
    mandatory: tuple[str, ...] = (
        (mandatory_reader,)
        if perception.required
        and mandatory_reader
        and perception.confidence >= 0.65
        else ()
    )
    # Schedule capability routing is driven only by the model-produced typed
    # intent.  Natural-language regex classification remains available as a
    # legacy diagnostic, but it has no authority to require a tool.
    schedule = schedule_intent if isinstance(schedule_intent, Mapping) else {}
    intent = schedule.get("intent") if isinstance(schedule.get("intent"), Mapping) else schedule
    todo_kind = str(intent.get("kind") or "none").strip().lower()
    policy = schedule.get("policy") if isinstance(schedule.get("policy"), Mapping) else {}
    policy_action = str(policy.get("action") or "").strip().lower()
    if todo_kind == "query" or policy_action == "read":
        candidates = tuple(dict.fromkeys((*candidates, "todo_search")))
    elif todo_kind in {"mutation", "clarification"}:
        candidates = tuple(dict.fromkeys((*candidates, "todo_search", "todo_manage")))
        if policy_action == "execute":
            # A validated and authorized ScheduleIntent is an execution
            # obligation. Proposals and missing details remain non-mutating.
            mandatory = tuple(dict.fromkeys((*mandatory, "todo_manage")))
    risk_flags: list[str] = []
    if "capture_camera" in candidates:
        risk_flags.append("camera_sensitive")
    reasons = [
        f"perception_target:{perception.target_domain}"
    ] if perception.target_domain != "none" else []
    if todo_kind != "none":
        reasons.append(f"schedule_intent:{todo_kind}:{policy_action or 'pending'}")
    return TurnPreflight(
        reply_mode=reply_mode,
        speech_act=speech_act,
        candidate_capabilities=tuple(candidates),
        mandatory_capabilities=tuple(mandatory),
        tool_escalation_allowed=True,
        likely_no_tool=not bool(candidates),
        confidence=perception.confidence,
        risk_flags=tuple(risk_flags),
        perception=perception,
        reasons=tuple(reasons),
    )


def bind_required_capabilities(
    preflight: TurnPreflight,
    required_capabilities: Sequence[str],
    *,
    source: str = "turn_execution_plan",
) -> TurnPreflight:
    """Make runtime validation consume the finalized execution obligation."""

    required = tuple(dict.fromkeys(
        str(item or "").strip()
        for item in required_capabilities
        if str(item or "").strip()
    ))
    if not required:
        return preflight

    perception = preflight.perception
    visual_targets: set[str] = set()
    for tool_id in required:
        definition = DEFAULT_TOOL_REGISTRY.get(tool_id)
        if definition is not None:
            visual_targets.update(
                set(definition.target_domains)
                & {"screen", "physical_user", "uploaded_image", "document"}
            )
    resolved_visual_target = ""
    if (
        perception.target_domain in {"none", "unknown_visual"}
        and len(visual_targets) == 1
    ):
        target = next(iter(visual_targets))
        resolved_visual_target = target
        perception = replace(
            perception,
            required=True,
            target_domain=target,
            freshness=(
                "current"
                if target in {"screen", "physical_user"}
                else "bound_artifact"
            ),
            confidence=max(0.9, perception.confidence),
            ambiguities=(),
        )

    risk_flags = list(preflight.risk_flags)
    if "capture_camera" in required and "camera_sensitive" not in risk_flags:
        risk_flags.append("camera_sensitive")

    reasons = [
        *preflight.reasons,
        f"required_capabilities_bound:{source}",
    ]
    if resolved_visual_target:
        reasons.append(
            "perception_target_resolved_from_required_capability:"
            f"{source}:{resolved_visual_target}"
        )

    return replace(
        preflight,
        candidate_capabilities=tuple(dict.fromkeys((
            *preflight.candidate_capabilities,
            *required,
        ))),
        mandatory_capabilities=tuple(dict.fromkeys((
            *preflight.mandatory_capabilities,
            *required,
        ))),
        likely_no_tool=False,
        confidence=max(preflight.confidence, perception.confidence),
        risk_flags=tuple(risk_flags),
        perception=perception,
        reasons=tuple(dict.fromkeys(reasons)),
    )


# 将本轮感知目标、用户权限和副作用授权一起校验，防止屏幕请求误用摄像头。
def validate_tool_choice(
    tool_id: str,
    preflight: TurnPreflight,
    settings: Mapping[str, Any] | None = None,
    *,
    explicitly_requested: bool = False,
) -> ToolValidation:
    definition = DEFAULT_TOOL_REGISTRY.get(tool_id)
    if definition is None:
        return ToolValidation(False, "unknown_tool")
    if definition.permission_key and settings is not None and not bool(
        settings.get(definition.permission_key)
    ):
        return ToolValidation(False, f"permission_denied:{definition.permission_key}")
    target = preflight.perception.target_domain
    if target == "screen" and tool_id == "capture_camera":
        return ToolValidation(False, "perception_target_mismatch:screen_vs_camera")
    if target == "physical_user" and tool_id == "capture_screen":
        return ToolValidation(False, "perception_target_mismatch:physical_vs_screen")
    if target == "none" and tool_id in {"capture_screen", "capture_camera"} and not explicitly_requested:
        return ToolValidation(
            False,
            "sensor_use_has_no_grounded_perception_target",
            requires_clarification=True,
        )
    if target in {"uploaded_image", "document"} and tool_id in {
        "capture_screen", "capture_camera",
    } and not explicitly_requested:
        return ToolValidation(False, "existing_artifact_should_be_used_first")
    if target == "unknown_visual" and tool_id == "capture_camera" and not explicitly_requested:
        return ToolValidation(
            False,
            "camera_target_requires_clarification",
            requires_clarification=True,
        )
    if definition.side_effect and not explicitly_requested:
        return ToolValidation(False, "side_effect_requires_explicit_user_intent")
    return ToolValidation(True, "validated")


def assemble_tool_pool(
    preflight: TurnPreflight,
    *,
    research_policy: str = "allowed",
    extra_tool_ids: Sequence[str] = (),
    limit: int = 10,
) -> tuple[ToolDef, ...]:
    """Build a small per-turn pool without erasing escalation/search options."""

    ordered: list[str] = [
        *preflight.mandatory_capabilities,
        # Web schemas must be visible whenever permission allows them.  Put
        # them before heuristic candidates so the pool limit cannot silently
        # remove the model's ability to choose research.
        *(("web_search", "read_web_page") if research_policy != "forbidden" else ()),
        *preflight.candidate_capabilities,
        # Compact local-productivity schemas stay available so the model can
        # decide from semantics rather than a keyword gate whether it needs
        # authoritative schedule state.
        "local_compute",
        "todo_search",
        "todo_manage",
        "bookmark_list",
        "bookmark_manage",
        *extra_tool_ids,
        "get_system_time",
    ]
    result: list[ToolDef] = []
    seen: set[str] = set()
    for tool_id in ordered:
        if tool_id in seen:
            continue
        definition = DEFAULT_TOOL_REGISTRY.get(tool_id)
        if definition is None:
            continue
        seen.add(tool_id)
        result.append(definition)
        if len(result) >= max(1, limit):
            break
    return tuple(result)
