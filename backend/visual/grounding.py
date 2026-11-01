"""视觉回答的独立审计与最终回执更新。

这里只消费已经采集的视觉证据；不读取人格、长期记忆或重新采集画面。
运行时端口由入口注入；审计结果只作为当前回答的诊断回执。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence

from agent.visual_grounding import build_visual_evidence_packet
from agent.visual_grounding import has_current_visual_evidence as has_typed_visual_evidence
from backend.chat.completion import visual_grounding_text_fingerprint
from backend.chat.parsing import parse_json_object
from backend.providers.errors import UpstreamProtocolError

def normalize_visual_grounding_audit(
    value: Any,
    packet: dict[str, Any],
) -> dict[str, Any]:
    """Validate the semantic auditor's structured verdict fail-closed."""

    parsed = parse_json_object(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise UpstreamProtocolError("visual grounding audit returned no JSON object")
    evidence_authority = {
        str(evidence.get("evidenceId") or ""): str(evidence.get("authority") or "")
        for observation in packet.get("observations", [])
        if isinstance(observation, dict)
        for layer in ("sceneEvidence", "textEvidence", "contextEvidence")
        for evidence in observation.get(layer, [])
        if isinstance(evidence, dict) and str(evidence.get("evidenceId") or "")
    }
    evidence_tool = {
        str(evidence.get("evidenceId") or ""): str(observation.get("tool") or "")
        for observation in packet.get("observations", [])
        if isinstance(observation, dict)
        for layer in ("sceneEvidence", "textEvidence", "contextEvidence")
        for evidence in observation.get(layer, [])
        if isinstance(evidence, dict) and str(evidence.get("evidenceId") or "")
    }
    evidence_observation = {
        str(evidence.get("evidenceId") or ""): str(observation.get("observationId") or "")
        for observation in packet.get("observations", [])
        if isinstance(observation, dict)
        for layer in ("sceneEvidence", "textEvidence", "contextEvidence")
        for evidence in observation.get(layer, [])
        if isinstance(evidence, dict) and str(evidence.get("evidenceId") or "")
    }
    screen_observations_with_scene = {
        str(observation.get("observationId") or "")
        for observation in packet.get("observations", [])
        if isinstance(observation, dict)
        and str(observation.get("tool") or "") == "capture_screen"
        and any(
            isinstance(item, dict)
            and str(item.get("authority") or "") == "primary_visual"
            for item in (
                observation.get("sceneEvidence")
                if isinstance(observation.get("sceneEvidence"), list) else []
            )
        )
    }
    valid_evidence_ids = set(evidence_authority)
    raw_claims = parsed.get("claims") if isinstance(parsed.get("claims"), list) else []
    claims: list[dict[str, Any]] = []
    invalid_references: list[str] = []
    unsupported: list[str] = []
    contradicted: list[str] = []
    for index, item in enumerate(raw_claims[:24]):
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "UNSUPPORTED").upper()
        if status not in {"SUPPORTED", "REASONABLE_INFERENCE", "UNSUPPORTED", "CONTRADICTED", "STYLE_ONLY"}:
            status = "UNSUPPORTED"
        kind = str(item.get("kind") or "visual_fact").lower()
        refs = [
            str(ref) for ref in (
                item.get("evidenceIds") if isinstance(item.get("evidenceIds"), list) else []
            ) if str(ref)
        ][:12]
        bad_refs = [ref for ref in refs if ref not in valid_evidence_ids]
        invalid_references.extend(bad_refs)
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()[:600]
        normalized = {
            "claimId": str(item.get("claimId") or f"visual-claim-{index + 1}")[:100],
            "text": text,
            "kind": kind,
            "status": status,
            "evidenceIds": refs,
            "reason": re.sub(r"\s+", " ", str(item.get("reason") or "")).strip()[:500],
        }
        claims.append(normalized)
        factual = kind in {"visual_fact", "current_activity", "visible_text", "application_identity"}
        primary_refs = [
            ref for ref in refs if evidence_authority.get(ref) == "primary_visual"
        ]
        referenced_tools = {
            evidence_tool.get(ref, "") for ref in refs if evidence_tool.get(ref, "")
        }
        referenced_observations = {
            evidence_observation.get(ref, "")
            for ref in refs
            if evidence_observation.get(ref, "")
        }
        # A desktop screenshot is primarily a text-and-interface artifact.
        # Once the scene establishes that it is a screen/application, OCR and
        # the foreground title may support a narrow summary of the visible app,
        # file, page, or current activity.  Requiring every such claim to cite
        # a generic scene caption discarded the useful evidence and caused the
        # audit to contradict its own SUPPORTED/REASONABLE_INFERENCE labels.
        screen_scoped_claim = bool(
            referenced_tools
            and referenced_tools.issubset({"capture_screen"})
            and referenced_observations
            and referenced_observations.issubset(screen_observations_with_scene)
        )
        if factual and (
            status in {"UNSUPPORTED", "CONTRADICTED", "STYLE_ONLY"}
            or status in {"SUPPORTED", "REASONABLE_INFERENCE"} and not refs
            or kind in {"visual_fact", "current_activity"}
            and status in {"SUPPORTED", "REASONABLE_INFERENCE"}
            and not primary_refs
            and not screen_scoped_claim
        ):
            unsupported.append(text or normalized["claimId"])
            if status == "CONTRADICTED":
                contradicted.append(text or normalized["claimId"])

    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    disposition = str(parsed.get("responseDisposition") or "").strip()
    answers_current_request = (
        disposition in {"answer", "limitation"}
        if "responseDisposition" in parsed
        else parsed.get("answersCurrentRequest") is True
    )
    zh_ja_aligned = parsed.get("zhJaCoreAligned") is True
    verdict_passes = parsed.get("passes") is True
    reported_unsupported = [
        re.sub(r"\s+", " ", str(item or "")).strip()[:600]
        for item in (
            parsed.get("unsupportedClaims")
            if isinstance(parsed.get("unsupportedClaims"), list) else []
        )
        if str(item or "").strip()
    ]
    reported_contradicted = [
        re.sub(r"\s+", " ", str(item or "")).strip()[:600]
        for item in (
            parsed.get("contradictedClaims")
            if isinstance(parsed.get("contradictedClaims"), list) else []
        )
        if str(item or "").strip()
    ]
    structural_reasons: list[str] = []
    if not claims:
        structural_reasons.append("audit_returned_no_claims")
    if not answers_current_request:
        structural_reasons.append("visual_answer_does_not_address_current_request")
    if not zh_ja_aligned:
        structural_reasons.append("visual_zh_ja_core_mismatch")
    if unsupported:
        structural_reasons.append("unsupported_or_contradicted_visual_claim")
    if invalid_references:
        structural_reasons.append("audit_referenced_unknown_visual_evidence")
    # Confidence is diagnostic only.  The atomic status and evidence bindings
    # above decide factual support.  A missing/low global confidence must not
    # negate claims the same audit explicitly marked SUPPORTED or as a bounded
    # REASONABLE_INFERENCE.
    # The backend owns the verdict and summaries from atomic claim bindings.
    # Model-generated global fields are redundant and can contradict the claim
    # records (for example: every claim is SUPPORTED while the same text also
    # appears in unsupportedClaims).  Keep those fields only as diagnostics;
    # never let them override the typed status/evidence binding of a claim.
    passes = not structural_reasons
    summary_conflicts_atomic_claims = bool(
        (reported_unsupported or reported_contradicted) and not unsupported
    )
    return {
        "passes": passes,
        "modelDeclaredPass": verdict_passes,
        "answersCurrentRequest": answers_current_request,
        "responseDisposition": disposition or ("answer" if answers_current_request else "unrelated"),
        "zhJaCoreAligned": zh_ja_aligned,
        "confidence": confidence,
        "claims": claims,
        "unsupportedClaims": list(dict.fromkeys(unsupported)),
        "contradictedClaims": list(dict.fromkeys(contradicted)),
        "modelReportedUnsupportedClaims": list(dict.fromkeys(reported_unsupported)),
        "modelReportedContradictedClaims": list(dict.fromkeys(reported_contradicted)),
        "modelSummaryConflictsAtomicClaims": summary_conflicts_atomic_claims,
        "invalidEvidenceIds": list(dict.fromkeys(invalid_references)),
        "reasons": list(dict.fromkeys([
            *structural_reasons,
            *[
                re.sub(r"\s+", " ", str(item or "")).strip()[:300]
                for item in (
                    parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else []
                )
                if str(item or "").strip()
            ],
        ])),
    }


def native_visual_input_delivered(candidate: Mapping[str, Any], results: list[dict[str, Any]]) -> bool:
    """Original pixels reached the answering model; this is not a semantic audit."""
    delivered = set(candidate.get("_nativeVisualInputIds") or [])
    if not delivered:
        return False
    observations = build_visual_evidence_packet(results).get("observations") or []
    return bool(observations) and all(item["observationId"] in delivered for item in observations)


def current_visual_grounding_verified(
    candidate: Mapping[str, Any], results: list[dict[str, Any]],
) -> bool:
    """A receipt belongs to the exact bilingual text, not just the tool call."""
    audit = candidate.get("visualGroundingAudit")
    return bool(
        has_typed_visual_evidence(results)
        and candidate.get("visualGroundingTextFingerprint")
        == visual_grounding_text_fingerprint(candidate)
        and (
            isinstance(audit, dict) and audit.get("passes")
        )
    )


class VisualGroundingService:
    """拥有审计与修复时序；外部只提供模型传输和请求生命周期端口。"""

    def __init__(self, *, model_call: Callable[..., str],
                 normalize_reply: Callable[..., dict[str, Any]],
                 log: Callable[..., None], progress: Callable[..., None],
                 reply_rule: str) -> None:
        self._model_call = model_call
        self._normalize_reply = normalize_reply
        self._log = log
        self._progress = progress
        self._reply_rule = reply_rule

    def refresh_visual_grounding_for_current_text(self,
        settings: dict[str, Any], user_message: str,
        candidate: dict[str, Any], results: list[dict[str, Any]],
        *, request_id: str, phase: str,
        task_dialogue: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Renew a stale audit before a consumer can reject the repaired answer.

        A speech repair invalidates the old receipt, not the captured observation.
        Only an actual semantic audit may approve changed text; never just restamp it.
        """
        if (not has_typed_visual_evidence(results) or native_visual_input_delivered(candidate, results)
                or current_visual_grounding_verified(candidate, results)):
            return candidate
        previous_fingerprint = str(candidate.get("visualGroundingTextFingerprint") or "")
        current_fingerprint = visual_grounding_text_fingerprint(candidate)
        try:
            audit = self.audit_visual_grounding_with_model(
                settings, user_message, candidate, results, request_id=request_id,
                task_dialogue=task_dialogue,
            )
        except Exception as error:
            audit = {
                "passes": False, "claims": [], "unsupportedClaims": [],
                "contradictedClaims": [], "reasons": ["visual_semantic_audit_unavailable"],
                "errorType": type(error).__name__,
            }
        self._log(
            "visual_grounding_receipt_refreshed", requestId=request_id, phase=phase,
            previousAuditHash=previous_fingerprint, currentTextHash=current_fingerprint,
            decision=audit,
        )
        renewed = dict(candidate)
        if audit.get("passes"):
            renewed.pop("visualGroundingTerminalBoundary", None)
            renewed["visualGroundingVerification"] = f"{phase}_independent_semantic_audit"
        else:
            # A changed visual answer can no longer reuse the previous receipt.
            # Keep the audit as a diagnostic and let the normal response path
            # decide how to answer; this service must not author a fixed reply
            # or silently rewrite the candidate.
            renewed["visualGroundingVerification"] = (
                f"{phase}_semantic_audit_unavailable"
                if audit.get("errorType")
                else f"{phase}_semantic_audit_rejected"
            )
            renewed["visualGroundingDiagnosticOnly"] = True
        renewed["visualGroundingAudit"] = audit
        renewed["visualGroundingTextFingerprint"] = visual_grounding_text_fingerprint(renewed)
        return renewed

    def audit_visual_grounding_with_model(self,
        settings: dict[str, Any],
        user_message: str,
        candidate: dict[str, Any],
        results: list[dict[str, Any]],
        *,
        request_id: str,
        task_dialogue: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Judge each current-visual assertion without persona or conversation priors."""

        packet = build_visual_evidence_packet(results, user_question=user_message)
        if not packet.get("observations"):
            return {
                "passes": True,
                "notApplicable": True,
                "claims": [],
                "reasons": [],
                "confidence": 1.0,
            }
        payload: dict[str, Any] = {
            "model": settings["apiModel"],
            "temperature": 0.0,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": (
                    "你是当前画面的独立证据审计器，不扮演角色，不接收人格或长期记忆。"
                    "recentTaskDialogue 是本次材料任务的分角色对话，只用于理解指代、用户提供的背景和纠正；"
                    "助手先前的猜测不是事实，用户提供的背景也不应被当成独立视觉发现。"
                    "只比较候选回答与 visualEvidencePacket，不用常识补全场景。逐条拆出中文 reply 和日语 spokenTextJa"
                    "中关于当前画面、当前活动、可见文字和应用身份的断言；纯语气、感受或提问标 STYLE_ONLY。"
                    "先按 currentUserRequest 的完整语义判断用户要求的是画面描述、文字读取、应用识别、当前活动判断还是进一步分析。"
                    "responseDisposition 与事实是否有证据是两个独立维度：若用户明确问自己正在做什么、进行什么活动，或强调要分析而非复述，"
                    "候选必须给出最具体且有证据的当前活动结论；仅罗列画面中的文字、框、按钮或应用名称时，"
                    "即使这些元素全都正确，responseDisposition 也必须为 unrelated。若用户只要求描述或 OCR，则不要求额外推断活动。"
                    "同理，用户要解释寓意、讽刺对象或继续上一项分析时，仅复述衣服、颜色、构图并不算回答。"
                    "画面可见事实和解释性推断须区分；结合明确可见元素给出的合理解释可标 REASONABLE_INFERENCE，"
                    "不能把‘分析不等于逐字描述’本身判为错误，也不能把作者真实意图当成可直接观察的事实。"
                    "sceneEvidence 是人物、物体、动作和场景类型的主证据。textEvidence 只能证明文字可能可见，"
                    "contextEvidence 只能辅助识别窗口；二者不能单独支撑更宽的活动，但桌面 sceneEvidence 已确认应用或页面场景时，"
                    "可与一致的可见文字、窗口上下文共同支撑最窄的当前活动推断。不能据此猜测长期目的、心理或画面外动作。"
                    "对 capture_screen，可见界面本来就主要由文字、标签、窗口标题和操作痕迹表达。"
                    "只要它们彼此一致，就可以支持用户正在查看哪个应用、文件、页面或调试什么的有边界总结；"
                    "不要因为这类结论主要引用 textEvidence/contextEvidence 就标成 UNSUPPORTED。"
                    "对 capture_camera 和普通 read_image，人物、动作和场景判断仍必须有 sceneEvidence，不得用 OCR 扩张。"
                    "SUPPORTED 表示证据直接蕴含；REASONABLE_INFERENCE 只用于由主视觉证据自然推出、且回答明确降低确定性的最窄推断；"
                    "UNSUPPORTED 表示证据没有支持，CONTRADICTED 表示与主视觉证据冲突。"
                    "未提及某个细节不等于反证；服饰、道具、造型只能支持外观，不能单独证明人物的现实身份、职业或所属年代。"
                    "用户提供的当前材料背景可以带明确归属地引用，不是候选凭空添加的视觉事实，也不能仅凭造型宣布该背景错误。"
                    "responseDisposition 只从三种选择：answer=给出了所问结果；limitation=切题说明具体哪个细节不能可靠辨认，"
                    "保留用户提供的背景并区分已观察信息与未知信息；unrelated=回答了另一问题、只堆砌不相关描述或泛泛挡话。"
                    "不能因为用户要求确定答案就把具体且诚实的 limitation 判成 unrelated；limitation 不表示已经认出了未知信息。"
                    "只输出 JSON：{responseDisposition,zhJaCoreAligned,confidence,claims,reasons}，不输出 answersCurrentRequest。"
                    "claims 每项必须含 claimId、text、kind、status、evidenceIds、reason；"
                    "kind 只能是 visual_fact/current_activity/visible_text/application_identity/style_only。"
                    "visible_text 只用于候选逐字转述可见文字；只要候选借文字断言这是某个游戏、软件、文件夹或场景，"
                    "就必须拆成 application_identity/visual_fact/current_activity，不能借 visible_text 规避主视觉证据要求。"
                    "SUPPORTED 或 REASONABLE_INFERENCE 必须列出真实 evidenceId。"
                    "不要另行输出 passes、unsupportedClaims 或 contradictedClaims；后端只根据逐条 claims 的状态和证据绑定计算结论。"
                )},
                {"role": "user", "content": json.dumps({
                    "currentUserRequest": user_message,
                    "recentTaskDialogue": list(task_dialogue),
                    "candidate": {
                        "replyZh": str(candidate.get("reply") or ""),
                        "spokenTextJa": str(candidate.get("spokenTextJa") or ""),
                    },
                    "visualEvidencePacket": packet,
                }, ensure_ascii=False)},
            ],
        }
        if settings.get("useResponseFormat"):
            payload["response_format"] = {"type": "json_object"}
        if "deepseek.com" in settings["apiEndpoint"]:
            payload["thinking"] = {"type": "disabled"}
        content = self._model_call(
            settings,
            payload,
            timeout=60,
            request_id=request_id,
            purpose="visual_grounding_audit",
        )
        return normalize_visual_grounding_audit(content, packet)
