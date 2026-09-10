"""Typed evidence packets for current-turn visual observations.

The dialogue model must not receive a screen caption, OCR dump, window title,
and old conversation as if they had equal evidential weight.  This module
projects visual tool results into a small, explicit hierarchy that can be used
both by generation and by an independent release audit.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence


VISUAL_TOOL_NAMES = frozenset({"capture_screen", "capture_camera", "read_image"})


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def has_current_visual_evidence(results: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        bool(item.get("ok")) and str(item.get("name") or "") in VISUAL_TOOL_NAMES
        for item in results
        if isinstance(item, Mapping)
    )


def build_visual_evidence_packet(
    results: Sequence[Mapping[str, Any]],
    *,
    user_question: str = "",
) -> dict[str, Any]:
    """Build a bounded evidence hierarchy without raw image/runtime payloads.

    Scene captions and regions describe visible objects and actions. OCR and a
    foreground-window title may identify visible text or an application, but
    cannot independently establish the user's broader activity.  Keeping those
    layers separate prevents a stray OCR token from overriding the scene.
    """

    observations: list[dict[str, Any]] = []
    for index, item in enumerate(results):
        if not isinstance(item, Mapping) or not item.get("ok"):
            continue
        tool_name = str(item.get("name") or "")
        if tool_name not in VISUAL_TOOL_NAMES:
            continue
        artifact = item.get("imageArtifact") if isinstance(item.get("imageArtifact"), Mapping) else {}

        if tool_name in {"capture_screen", "capture_camera"}:
            vision = item.get("vision") if isinstance(item.get("vision"), Mapping) else {}
            global_caption = _clean_text(
                vision.get("globalDescription") or vision.get("caption"), 3_500
            )
            raw_tiles = _sequence(vision.get("visualTiles"))
            raw_regions = _sequence(vision.get("regions"))
            raw_ocr_items = _sequence(vision.get("ocrItems"))
            fallback_ocr = _clean_text(vision.get("ocrText"), 2_400)
            observed_at = _clean_text(item.get("capturedAt"), 100)
            foreground_title = _clean_text(item.get("foregroundWindowTitle"), 500)
            self_excluded = bool(item.get("selfExcluded"))
            observation_id = _clean_text(
                item.get("captureId") or artifact.get("artifactId") or f"{tool_name}:{index}",
                160,
            )
        else:
            global_caption = _clean_text(item.get("visualCaption"), 3_500)
            raw_tiles = _sequence(item.get("visualTiles"))
            raw_regions = _sequence(item.get("visualRegions"))
            raw_ocr_items = _sequence(item.get("visualOcrItems"))
            fallback_ocr = _clean_text(item.get("visualOcrText"), 2_400)
            observed_at = _clean_text(artifact.get("acquiredAt") or item.get("observedAt") or item.get("readAt"), 100)
            foreground_title = ""
            self_excluded = False
            observation_id = _clean_text(
                item.get("attachmentId") or item.get("artifactId") or f"{tool_name}:{index}",
                160,
            )

        scene_evidence: list[dict[str, Any]] = []
        if global_caption:
            scene_evidence.append({
                "evidenceId": f"{observation_id}:scene:global",
                "kind": "global_scene_caption",
                "authority": "primary_visual",
                "sceneDescription": global_caption,
            })
        for tile_index, tile in enumerate(raw_tiles[:6]):
            if not isinstance(tile, Mapping):
                continue
            caption = _clean_text(tile.get("caption"), 1_000)
            if not caption:
                continue
            scene_evidence.append({
                "evidenceId": f"{observation_id}:scene:tile:{tile_index}",
                "kind": "scene_tile",
                "authority": "primary_visual",
                "position": _clean_text(tile.get("position"), 80),
                "sceneDescription": caption,
            })
        for region_index, region in enumerate(raw_regions[:16]):
            if not isinstance(region, Mapping):
                continue
            label = _clean_text(region.get("label") or region.get("name"), 160)
            description = _clean_text(
                region.get("description") or region.get("caption") or region.get("text"),
                700,
            )
            if not label and not description:
                continue
            scene_evidence.append({
                "evidenceId": f"{observation_id}:scene:region:{region_index}",
                "kind": "visual_region",
                "authority": "primary_visual",
                "label": label,
                "sceneDescription": description,
            })

        text_evidence: list[dict[str, Any]] = []
        for ocr_index, ocr_item in enumerate(raw_ocr_items[:40]):
            if not isinstance(ocr_item, Mapping):
                continue
            text = _clean_text(ocr_item.get("text"), 500)
            if not text:
                continue
            try:
                confidence = float(ocr_item.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            text_evidence.append({
                "evidenceId": f"{observation_id}:text:ocr:{ocr_index}",
                "kind": "ocr_text",
                "authority": "secondary_text_only",
                "confidence": round(max(0.0, min(1.0, confidence)), 4),
                "position": _clean_text(ocr_item.get("position"), 80),
                "visibleText": text,
            })
        if not text_evidence and fallback_ocr:
            for ocr_index, line in enumerate(fallback_ocr.splitlines()[:24]):
                text = _clean_text(line, 500)
                if text:
                    text_evidence.append({
                        "evidenceId": f"{observation_id}:text:ocr-fallback:{ocr_index}",
                        "kind": "ocr_text",
                        "authority": "secondary_text_only",
                        "confidence": None,
                        "position": "",
                        "visibleText": text,
                    })

        context_evidence = []
        if foreground_title:
            context_evidence.append({
                "evidenceId": f"{observation_id}:context:foreground-title",
                "kind": "foreground_window_title",
                "authority": "context_only",
                "contextLabel": foreground_title,
            })
        observations.append({
            "observationId": observation_id,
            "tool": tool_name,
            "scope": "current_turn",
            "freshness": artifact.get("freshness") or "current",
            "sourceType": artifact.get("sourceType") or tool_name,
            "cacheHit": bool(item.get("cacheHit")),
            "reusedArtifact": bool(item.get("reusedArtifact") or item.get("cacheHit")),
            "observedAt": observed_at,
            "selfWindowExcluded": self_excluded,
            "sceneEvidence": scene_evidence,
            "textEvidence": text_evidence,
            "contextEvidence": context_evidence,
            **({"imageInput": {"available": True, "artifactId": observation_id}}
               if item.get("directImageInput") or (item.get("vision") or {}).get("directImageInput") else {}),
        })

    return {
        "schema": "amadeus.visual-evidence.v1",
        "question": _clean_text(user_question, 1_000),
        "observations": observations,
        "evidencePolicy": {
            "sceneEvidence": (
                "Primary evidence for visible objects, people, actions, and scene type."
            ),
            "textEvidence": (
                "Secondary evidence for text that is visibly present. OCR can be noisy or come "
                "from an overlapping window; it cannot by itself establish a broader activity."
            ),
            "contextEvidence": (
                "Context only. A window title may name an application or file but cannot override "
                "scene evidence or prove what the user is doing."
            ),
            "forbiddenSources": (
                "Persona, long-term memory, unrelated dialogue, and general plausibility must not be used "
                "as evidence for the contents of this current image. Directly relevant recent dialogue may "
                "only resolve what the user is referring to or help interpret visible text; it is not image evidence."
            ),
        },
    }


def visual_evidence_prompt(
    results: Sequence[Mapping[str, Any]],
    *,
    user_question: str = "",
) -> str:
    packet = build_visual_evidence_packet(results, user_question=user_question)
    if not packet["observations"]:
        return ""
    if any(item.get("imageInput") for item in packet["observations"]):
        from agent.multimodal_chat import DIRECT_IMAGE_INSTRUCTION
        return DIRECT_IMAGE_INSTRUCTION + "\n" + json.dumps({"visualEvidencePacket": packet}, ensure_ascii=False)
    return "\n".join([
        "[本轮当前视觉任务：优先级高于人格、记忆、旧对话和通用工具说明]",
        "先从 currentUserRequest 的完整语义判断用户要的是画面描述、文字读取、应用识别、当前活动判断还是进一步分析；"
        "必须完成用户实际要求的那一种结果，不能把它们互相替代。",
        "sceneEvidence 是画面事实的主证据；",
        "textEvidence 只能证明某段文字可能可见，contextEvidence 只能辅助识别窗口。",
        "不得把 OCR 中孤立的‘视频’、文件名、菜单项或窗口标题扩写成 sceneEvidence 没有支持的活动；",
        "不得借用人格设定、长期记忆或旧对话补出当前画面中的衣着、人物、软件、动作或场景。",
        "若用户明确要求判断自己当前在做什么、进行什么活动，或强调要分析而非复述，第一句必须给出最具体且有证据的当前活动判断；"
        "随后用应用、页面、可见内容或操作痕迹说明依据。仅罗列文字、框、按钮或窗口元素不算完成活动判断。",
        "桌面截图中，sceneEvidence 确认屏幕/应用场景后，可以联合一致的 textEvidence 和 contextEvidence 推断用户正在查看、"
        "讨论、编辑、调试或操作什么；证据只够间接推断时要用‘看起来/应该是在’等方式降低确定性。",
        "与本轮直接承接的近期对话可以帮助理解用户所指和 OCR 文字在讨论什么；"
        "它不是当前画面证据。无关上文、较早话题和长期记忆不得用来补当前画面。",
        "用户对当前材料提供的背景或纠正是带来源的上下文，不要忽略，也不要冒称由画面独立验证。"
        "服饰、道具、画风只能说明外观，不能单凭这些断定现实身份、职业、年代或作者立场；艺术化造型可以与用户背景同时成立。"
        "若现有资料不足以辨认用户所问对象，就明确说明具体哪一点认不出，并保留用户提供的背景；"
        "不要编造另一个身份、反过来宣布用户背景错误，或要求重新上传已经可用的图片。",
        "不得越过单帧证据猜测用户的长期目的、心理、身份或画面外动作。若用户只要求描述画面、读取文字或识别应用，"
        "就只完成对应任务，不强行增加活动或目的判断。",
        "用一至三句直接回答，不展示 evidenceId，不追加与问题无关的人格、关系或日程内容。",
        json.dumps({"visualEvidencePacket": packet}, ensure_ascii=False, indent=2),
    ])
