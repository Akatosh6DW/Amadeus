"""继续查看已保存的屏幕或摄像头画面。

本模块只复用已有理解或读取原帧，没有截图、拍摄能力。上传附件有自己的
存储与续接服务，不能将两种来源混成一个默认画面。
"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


def refresh_saved_visual_artifacts(
    body: dict[str, Any],
    settings: dict[str, Any],
    user_message: str,
    *,
    screen_root: Path,
    camera_root: Path,
    read_image: Callable[[Path, dict[str, Any], str], dict[str, Any]],
    build_receipt: Callable[[dict[str, Any]], dict[str, Any]],
    now: Callable[[], str],
    log: Callable[..., None],
) -> list[dict[str, Any]]:
    """复用理解或重读已选原帧，把回执缓存到本轮 body。

    同一轮恢复生成时返回已有回执，避免重复视觉请求；重新分析只更新临时
    材料副本，原图和采集时间保持不变。目录由入口明确传入。
    """
    existing = body.get("_continuedVisualToolResults")
    if isinstance(existing, list):
        return existing
    task = body.get("_artifactTaskContext") or {}
    receipts: list[dict[str, Any]] = []
    for value in body.get("_continuedVisualArtifacts") or []:
        item = deepcopy(value)
        vision = item.get("vision") or {}
        has_understanding = bool(str(vision.get("caption") or vision.get("ocrText") or "").strip())
        should_read = bool(settings.get("_directVisualChat")) or task.get("visualAction") == "reanalyze" or not has_understanding
        if should_read:
            path = Path(str(item.get("path") or "")).resolve()
            expected_root = (screen_root if item.get("sourceType") == "screen_capture" else camera_root).resolve()
            # 只接受所属来源目录中的原帧，缺失时返回失败，不重新采集替代图片。
            if path.parent != expected_root or not path.is_file():
                vision = {"available": False, "error": "saved_visual_frame_missing", "caption": "", "ocrText": ""}
            else:
                vision = read_image(path, settings, str(task.get("inspectionQuestion") or task.get("question") or user_message))
            item["vision"] = vision
            item["understandingUpdatedAt"] = now()
        receipt = build_receipt(item)
        receipt["cacheHit"] = not should_read
        receipt["visionRequestMade"] = should_read and not vision.get("directImageInput") and vision.get("error") != "saved_visual_frame_missing"
        if should_read:
            receipt["message"] = "重新检查已保存的原画面；没有重新截图或拍摄。"
        if not vision.get("available", vision.get("ok", has_understanding)):
            receipt["ok"] = False
            receipt["error"] = str(vision.get("error") or "saved_visual_understanding_unavailable")
            receipt["message"] = "保存的画面无法重新读取；没有擅自拍摄新画面。"
        receipts.append(receipt)
        log(
            "visual_artifact_reanalyzed" if should_read else "visual_artifact_cache_reused",
            artifactId=item["id"], sourceType=item["sourceType"],
            visionRequestMade=receipt["visionRequestMade"], available=receipt["ok"],
        )
    body["_continuedVisualToolResults"] = receipts
    return receipts
