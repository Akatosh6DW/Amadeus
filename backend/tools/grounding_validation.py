"""工具结果 grounding 校验与网页答案形状诊断。

本模块只消费本轮结果和候选回复，不采集视觉、不执行工具，也不读取 server 全局。
视觉证据的成人 profile 通过窄端口接入，模型与对话编排继续由上层持有。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable

from backend.chat.completion import reply_integrity_issues
from backend.chat.parsing import parse_json_object
from backend.context.reference import SPORT_CONTEXT_PATTERN


def extract_scheduled_event_datetime(text: str) -> datetime | None:
    value = re.sub(r"\s+", " ", str(text or ""))
    value = re.sub(
        r"\b(?:GMT|UTC)\s+(?=on\b)",
        "UTC +0 ",
        value,
        flags=re.I,
    )
    match = re.search(
        r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)"
        r"(?:\s*(?:GMT|UTC)\s*([+-]\d{1,2})(?::?(\d{2}))?)?"
        r".{0,32}?\bon\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})\s*,?\s*((?:19|20)\d{2})",
        value,
        re.I,
    )
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "pm":
        hour += 12
    minute = int(match.group(2) or 0)
    try:
        month = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }[match.group(6).lower()]
        if match.group(4):
            offset_hour = int(match.group(4))
            offset_minute = int(match.group(5) or 0)
            offset = timedelta(hours=offset_hour, minutes=offset_minute if offset_hour >= 0 else -offset_minute)
            tz = timezone(offset)
        else:
            tz = datetime.now().astimezone().tzinfo or timezone.utc
        return datetime(int(match.group(8)), month, int(match.group(7)), hour, minute, tzinfo=tz)
    except (TypeError, ValueError):
        return None

def sports_temporal_grounding_violation(
    reply: str,
    results: list[dict[str, Any]],
    now: datetime | None = None,
) -> bool:
    source_text = " ".join(
        web_result_evidence_text(item)
        for item in results
        if item.get("ok") and item.get("name") in {"web_search", "read_web_page"}
    )
    scheduled_at = extract_scheduled_event_datetime(source_text)
    if not scheduled_at:
        return False
    current = now or datetime.now(scheduled_at.tzinfo)
    if current.tzinfo is None:
        current = current.replace(tzinfo=scheduled_at.tzinfo)
    else:
        current = current.astimezone(scheduled_at.tzinfo)
    claims_not_started = bool(re.search(
        r"(?:还没|尚未|没有|未)(?:正式)?(?:开始|开打|开赛|踢)|"
        r"比赛.{0,10}(?:还没|尚未|没有|未).{0,6}(?:开始|开打|开赛)",
        reply,
    ))
    claims_finished = bool(re.search(r"已经|早已|刚刚", reply) and re.search(r"踢完|打完|结束|完赛|终场", reply))
    return bool((current >= scheduled_at and claims_not_started) or (current < scheduled_at and claims_finished))

def image_caption_required_markers(caption: str) -> list[tuple[str, ...]]:
    """Map distinctive local-caption concepts to words expected in a grounded reply."""

    value = str(caption or "").lower()
    concepts: list[tuple[str, ...]] = []
    mappings = (
        (r"warrior|armor|armour|helmet|horns?", ("战士", "武士", "铠甲", "盔甲", "战甲", "头盔", "角", "warrior", "armor", "helmet")),
        (r"anime|manga|cartoon", ("动漫", "动画", "二次元", "漫画", "角色", "anime")),
        (r"cat|kitten", ("猫", "小猫", "cat")),
        (r"dog|puppy", ("狗", "小狗", "犬", "dog")),
        (r"car|vehicle|motorcycle", ("汽车", "车辆", "车", "摩托", "car")),
        (r"water|splash|ocean|river|lake", ("水", "水花", "浪", "海", "河", "湖", "water")),
        (r"mountain|rock|cliff", ("山", "岩石", "悬崖", "石头", "rock")),
        (r"computer screen|screenshot|desktop", ("屏幕", "截图", "桌面", "窗口", "screen")),
    )
    for pattern, markers in mappings:
        if re.search(pattern, value):
            concepts.append(markers)
    return concepts

def image_attachment_grounding_violation(reply: str, results: list[dict[str, Any]]) -> bool:
    images = [item for item in results if item.get("ok") and item.get("name") == "read_image"]
    for item in images:
        tile_captions = " ".join(
            str(tile.get("caption") or "")
            for tile in item.get("visualTiles", [])
            if isinstance(tile, dict)
        )
        caption = f"{item.get('visualCaption') or ''} {tile_captions}".strip()
        if not caption:
            continue
        adult_evidence = bool(re.search(
            r"sexual|sex scene|intercourse|nude|naked|genitals?|explicit|pornographic|adult content|"
            r"breasts?|裸体|裸露|成人内容|性行为|情色",
            caption,
            flags=re.I,
        ))
        if adult_evidence and (
            not re.search(r"r18|成人|色情|情色|裸|性内容|性行为|露骨|限制级", reply, flags=re.I)
            or (
                re.search(r"不能|无法|不便|拒绝", reply)
                and re.search(r"分析|描述|评价|讨论", reply)
                and re.search(r"r18|成人|色情|裸|性", reply, flags=re.I)
            )
        ):
            return True
        concepts = image_caption_required_markers(caption)
        # The first matching group is intentionally the most distinctive one.
        if concepts and not any(marker.lower() in reply for marker in concepts[0]):
            return True
        if not concepts and not re.search(r"图|图片|图像|画面|主体|人物|场景|背景|颜色|看到|看起来", reply):
            return True
    return False


def web_result_evidence_text(result: dict[str, Any]) -> str:
    """Join public source text used by the temporal grounding rule."""

    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    return " ".join(
        " ".join(str(source.get(key) or "") for key in ("title", "snippet", "excerpt", "body"))
        for source in sources
        if isinstance(source, dict)
    )


_WEB_PROCESS_ONLY_CLAUSE = re.compile(
    r"^(?:好|好的|可以|没问题|稍等|等一下|等等|先等一下)$|"
    r"^(?:我)?(?:现在|马上|这就|先|去)?(?:联网|上网|在线)?"
    r"(?:查(?:一下|下|查)|搜(?:一下|下|搜)|搜索一下|检索一下|查询一下|查找一下).*$|"
    r"^(?:我)?(?:已经|已)?(?:查到|搜到)(?:了|的)?"
    r"(?:相关|一些|若干|可用|新的|最新的)?(?:资料|来源|结果|信息|内容|网页|页面|链接|公告)(?:了)?$|"
    r"^(?:联网|上网|在线)?(?:搜索|检索|查询)(?:已经|已)?"
    r"(?:完成|结束|成功|失败|取得(?:了)?(?:相关|一些|若干)?(?:资料|来源|结果|信息|内容)?)$|"
    r"^(?:证据综合|检索结果)(?:没有|未能|尚未|已经|已)?(?:完成|失败|结束)?$|"
    r"^(?:最相关|最接近|首个)(?:的)?来源(?:是|为)?$",
    re.I,
)


def web_answer_has_substantive_statement(reply: str, source_titles: list[str]) -> bool:
    """Return whether a Web answer contains something beyond retrieval metadata."""

    compact = re.sub(r"\s+", "", str(reply or ""))
    compact = re.sub(r"https?://[^\s，,。；;！？!?]+", "", compact, flags=re.I)
    compact = re.sub(r"\[(?:S\d+)\]", "", compact, flags=re.I)
    for title in source_titles:
        normalized_title = re.sub(r"\s+", "", str(title or ""))
        if normalized_title:
            compact = compact.replace(normalized_title, "")
    clauses = [
        re.sub(r"^(?:但|不过|然而|另外|以及|并且)", "", item).strip()
        for item in re.split(r"[，,。；;！？!?：:\n]+", compact)
        if item.strip()
    ]
    return any(
        re.search(r"[A-Za-z0-9\u4e00-\u9fff]", clause)
        and not _WEB_PROCESS_ONLY_CLAUSE.fullmatch(clause)
        for clause in clauses
    )


@dataclass(frozen=True)
class ToolGroundingValidationPorts:
    """工具 grounding 只借用视觉证据摘要，不拥有视觉状态。"""

    visual_adult_evidence_profile: Callable[[list[dict[str, Any]]], dict[str, bool]]


class ToolGroundingValidationService:
    """Validate claims against actual tool outcomes and evidence shape."""

    def __init__(self, ports: ToolGroundingValidationPorts) -> None:
        self.ports = ports

    def web_answer_diagnostics(self, value: Any, results: list[dict[str, Any]]) -> list[str]:
        """Detect answer-shape failures without knowing the research domain.

        This deliberately does not look for football, products, politics, or any
        other subject vocabulary.  It only checks whether a web-backed response is
        an actual answer with a verified citation instead of a retrieval status or
        a source-title inventory.
        """

        web_sources = [
            source
            for item in results
            if item.get("ok") and item.get("name") in {"web_search", "read_web_page"}
            for source in (item.get("sources") if isinstance(item.get("sources"), list) else [])
            if isinstance(source, dict)
        ]
        if not web_sources:
            return []
        parsed = parse_json_object(value) if isinstance(value, str) else value if isinstance(value, dict) else {}
        reply = str((parsed or {}).get("reply") or value or "").strip()
        if not reply:
            return ["empty_answer"]
        diagnostics: list[str] = reply_integrity_issues(value)
        valid_ids = {str(source.get("id") or "").upper() for source in web_sources if source.get("id")}
        cited_ids = {item.upper() for item in re.findall(r"\[(S\d+)\]", reply, re.I)}
        web_statuses = [
            item.get("evidenceStatus")
            for item in results
            if item.get("ok")
            and item.get("name") in {"web_search", "read_web_page"}
            and isinstance(item.get("evidenceStatus"), dict)
        ]
        citations_required = bool(
            valid_ids
            and (
                not web_statuses
                or any(status.get("sufficient") for status in web_statuses)
            )
        )
        if citations_required and not (valid_ids & cited_ids):
            diagnostics.append("missing_verified_citation")
        research_plan = next(
            (
                item.get("researchPlan") for item in results
                if item.get("ok") and isinstance(item.get("researchPlan"), dict)
            ),
            {},
        )
        comparison_task = bool(re.search(
            r"相比|比较|对比|区别|差异|强弱|优劣|(?:哪个|哪一个|谁|孰).{0,6}更|"
            r"\b(?:vs\.?|versus|compare|comparison)\b",
            str(research_plan.get("resolvedQuestion") or ""),
            flags=re.I,
        ))
        source_identity_by_id = {
            str(source.get("id") or "").upper(): str(
                source.get("sourceUid")
                or source.get("canonicalUrl")
                or source.get("url")
                or source.get("id")
                or ""
            ).strip().casefold()
            for source in web_sources
            if source.get("id")
        }
        distinct_source_identities = {
            identity for identity in source_identity_by_id.values() if identity
        }
        cited_source_identities = {
            source_identity_by_id[source_id]
            for source_id in valid_ids & cited_ids
            if source_identity_by_id.get(source_id)
        }
        if (
            citations_required
            and comparison_task
            and len(distinct_source_identities) >= 2
            and len(cited_source_identities) < 2
        ):
            diagnostics.append("insufficient_cross_source_citations")

        compact = re.sub(r"\s+", "", reply)
        progress_only = bool(re.search(
            r"^(?:好|好的|可以|没问题|稍等|等一下|等等|先等一下)?[，,。.!！]*"
            r"(?:我)?(?:现在|马上|这就|先|去)?"
            r"(?:联网|上网|在线)?(?:查|搜|搜索|检索|查询|查找)(?:一下|下)?",
            compact,
            re.I,
        ))
        retrieval_status = bool(re.search(
            r"^(?:联网|搜索|检索|查询)(?:已经|已|后|结果|到了|发现|显示|返回)|"
            r"^(?:我)?(?:查到|搜到)(?:了|的)|"
            r"(?:证据综合|检索结果).{0,18}(?:没有|未能|失败|完成)|"
            r"(?:最相关|最接近|首个)(?:的)?来源(?:是|为)",
            compact,
            re.I,
        ))
        titles = [re.sub(r"\s+", "", str(source.get("title") or "")) for source in web_sources]
        copied_title_chars = sum(len(title) for title in titles if len(title) >= 6 and title in compact)
        has_substantive_statement = web_answer_has_substantive_statement(reply, titles)
        if retrieval_status and not has_substantive_statement:
            diagnostics.append("retrieval_status_instead_of_answer")
        if progress_only and not has_substantive_statement:
            diagnostics.append("search_progress_instead_of_answer")
        if copied_title_chars >= 12 and not has_substantive_statement:
            diagnostics.append("source_inventory_instead_of_synthesis")
        return list(dict.fromkeys(diagnostics))

    def tool_grounding_violation(self,
        value: Any,
        results: list[dict[str, Any]],
        user_message: str = "",
        relationship_state: dict[str, Any] | None = None,
    ) -> bool:
        successful_names = {
            str(item.get("name") or "")
            for item in results
            if item.get("ok")
        }
        if not successful_names:
            return False
        parsed = parse_json_object(value) if isinstance(value, str) else value if isinstance(value, dict) else {}
        reply = str((parsed or {}).get("reply") or value or "").strip().lower()
        visual_names = successful_names & {"capture_screen", "capture_camera", "read_image"}
        if visual_names and re.search(
            r"本地视觉模型|视觉模型(?:确认|识别|判断)|ocr\s*(?:模型|服务)|"
            r"(?:capture_screen|capture_camera|read_image)|工具调用(?:结果|过程)?",
            reply,
            flags=re.I,
        ):
            return True
        if image_attachment_grounding_violation(reply, results):
            return True
        if sports_temporal_grounding_violation(reply, results):
            return True
        if "capture_camera" in successful_names:
            camera_denial = re.search(
                r"(?:没有|不具备|无法|不能|没法|并无).{0,18}(?:摄像头|相机|镜头|物理接口)|"
                r"(?:摄像头|相机|镜头).{0,18}(?:不存在|没有|无法|不能|怎么打开)|"
                r"(?:看不到|无法看到|不能看到).{0,10}(?:你|画面)",
                reply,
            )
            if camera_denial:
                return True
            camera_detail = re.search(
                r"看到你|画面里|镜头里|看起来|穿|衣|脸|表情|头发|眼镜|坐|站|手|姿势|"
                r"背景|房间|光线|身后|旁边|拿着|戴着|正在",
                reply,
            )
            if not camera_detail:
                return True
        if "capture_screen" in successful_names and re.search(
            r"(?:没有|无法|不能|没法).{0,16}(?:截图|屏幕|桌面)|(?:看不到|无法看到).{0,10}(?:屏幕|桌面|画面)",
            reply,
        ):
            return True
        screen = next(
            (item for item in results if item.get("name") == "capture_screen" and item.get("ok")),
            None,
        )
        if screen:
            vision = screen.get("vision") if isinstance(screen.get("vision"), dict) else {}
            tile_captions = " ".join(
                str(item.get("caption") or "")
                for item in vision.get("visualTiles", [])
                if isinstance(item, dict)
            )
            caption = f"{vision.get('caption') or ''} {tile_captions}".lower()
            ocr_text = str(vision.get("ocrText") or "").lower()
            visible_characters = bool(re.search(r"anime|character|人物|角色|男性|女性|男.*女|girl|woman|man", caption))
            game_ui_markers = sum(
                marker in ocr_text
                for marker in (
                    "game", "galgame", "返回", "历史记录", "跳过", "自动", "保存", "快速保存", "偏好设置",
                    "save", "load", "auto", "back", "sys", "quit", "設定",
                )
            )
            game_scene_evidence = bool(re.search(r"game|visual novel|游戏", caption)) or (
                visible_characters and game_ui_markers >= 2
            )
            adult_scene_evidence = bool(re.search(
                r"sexual|sex scene|intercourse|nude|naked|genitals?|explicit|pornographic|adult content|"
                r"suggestive pose|partially clothed|underwear|breasts?|裸体|裸露|成人内容|性行为|情色",
                caption,
            ))
            denies_game_scene = bool(re.search(
                r"(?:没有|没看到|看不到|并非|不是).{0,14}(?:游戏|galgame).{0,8}(?:画面|界面|场景|运行)|"
                r"(?:只|仅).{0,8}(?:看到|显示).{0,8}(?:文件管理器|文件夹)|"
                r"(?:显示的是|只有).{0,8}(?:文件管理器|文件夹).{0,12}(?:不是|没有).{0,8}(?:游戏|画面)",
                reply,
            ))
            if game_scene_evidence and denies_game_scene:
                return True
            if visible_characters and not re.search(
                r"动漫|动画|二次元|角色|人物|女孩|女性|女人|少女|男人|男性|游戏画面|立绘|anime",
                reply,
            ):
                return True
            if game_scene_evidence and game_ui_markers >= 2 and not re.search(
                r"游戏|galgame|视觉小说|save|load|auto|back|sys|保存|读取|自动",
                reply,
            ):
                return True
            if adult_scene_evidence and not re.search(
                r"r18|成人|色情|情色|裸|性内容|性行为|露骨|少儿不宜|限制级",
                reply,
            ):
                return True
        # This function owns evidence grounding, not dialogue style.  Persona
        # interaction and subjective commentary may be encouraged by the visual
        # prompt, but an accurate objective description must never be rejected for
        # omitting words such as “评价”“观感”“我觉得”, an emotional lead-in, or a
        # direct address.  Factual support is enforced by the typed, claim-by-claim
        # visual semantic gate below the generation layer.
        adult_profile = self.ports.visual_adult_evidence_profile(results)
        if relationship_state is not None and adult_profile["adultContent"] and not adult_profile["ageUnclear"]:
            emotion = str((parsed or {}).get("emotion") or "").strip().lower()
            gesture = str((parsed or {}).get("gesture") or "").strip().lower()
            stage = str(relationship_state.get("stage") or "close_companion")
            if "变态" not in reply:
                return True
            if stage == "close_companion" and (emotion != "flustered" or gesture != "recoil"):
                return True
            if stage == "trusted_intimacy" and (
                emotion not in {"flustered", "shy"} or gesture not in {"tilt", "lean"}
            ):
                return True
            if stage not in {"close_companion", "trusted_intimacy"} and (
                emotion not in {"shy", "affectionate"} or gesture not in {"soften", "lean"}
            ):
                return True
        if successful_names & {"web_search", "read_web_page"}:
            if re.search(r"(?:没有|无法|不能|不具备).{0,16}(?:联网|上网|搜索|网页)|(?:无法|不能).{0,10}获取实时", reply):
                return True
            if self.web_answer_diagnostics(value, results):
                return True
        return False
