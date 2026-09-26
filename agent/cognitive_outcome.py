"""Objective checks for model-owned completion judgments.

This checks execution, observation and task state. Ordinary answer annotations
are optional and never serve as a delivery gate or proof of factual correctness.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from copy import deepcopy
from hashlib import sha256
import json

from .proactive_control import PROACTIVE_DIRECTIVE_INSTRUCTION
from .cognitive_tools import canonical_tool_arguments, receipt_matches_request


CLAIM_KINDS = (
    "inference", "user_fact", "external_fact", "tool_fact", "tool_completion",
    "perceptual_fact", "source_status",
)
_CURRENT_SENSITIVE_CLAIMS = frozenset({"tool_completion", "tool_fact", "perceptual_fact"})
_USER_EVIDENCE_SOURCES = frozenset({"user_message", "user_memory", "user_adaptation"})
ADVISORY_CLAIM_KINDS = frozenset({"inference", "user_fact", "external_fact"})
_CURRENT_STATE_READ_TOOLS = frozenset({"list_directory", "read_text_file"})
OUTCOME_REPAIR_GUIDANCE = (
    "普通身份、知识、观点和解释无需 claims、basis 或 evidenceRefs，不要因普通事实注释缺失而补造引用或调用工具。"
    "只有执行/观察声明需要按真实回执修正：tool_fact 对应 sourceType=tool 且 ok=true、正文非空；"
    "tool_completion 必须有实际回执及与之相符的 resultStatus:success/failed；"
    "perceptual_fact 对应实际 sourceType=visual 的非空识图结果；"
    "source_status 对应 sourceType=web 和真实 fetchStatus。旧操作或画面必须声明 freshness=historical。"
    "任务、产物和状态补丁的实际校验继续生效；回答完成不代表工具成功或整个任务完成。"
)


OUTCOME_PROTOCOL = """[完成情况]
最终 kind=turn/clarification/revise_state 在容器顶层给出 outcome:{status}；claims 仅在报告工具执行或实际观察结果时需要，普通回答可省略。
status 为 completed、partial、needs_input、failed。completed 指当前请求已完成，
不是仅仅工具执行过；需要用户补充信息用 needs_input，工作没做完用 partial 或 failed。
status 只描述本轮请求；“只列计划”的本轮可完成而整体任务仍未执行。packet.task 有 active 任务时，最终 outcome 必须另外带 taskStatus:'active'、'completed'或'failed'，对应当前 TaskFrame；可附 taskId，但若提供必须是真实ID。依据当前目标和成功条件明确判断整体任务是否结束，不要遗漏导致任务一直挂起；仍有工作用 active，达到目标才用 completed，失败且本次不再继续用 failed。工具成败不能直接推导任务成败；例如目标是测试并报告一次操作，实际失败也可以完成该目标。completed 不得有尚未执行的步骤，失败步骤必须如实保留。暂停/取消用状态补丁；后来获准继续已失败任务用 RESUME_TASK。
普通身份、知识、观点和解释直接依据已提供的人格资料、记忆和正文自然回答，不需要提交事实类别、basis 或 evidenceRefs；普通回答不因这些注释缺失或引用格式问题而重试。不要为了填写 claims 新建任务或调用工具。资料不足时如实表达不确定性；用户要求查证时仍按请求使用工具。
claims 用于需要机器核对的执行或观察声明，每项为
{kind:'inference'|'user_fact'|'external_fact'|'tool_fact'|'tool_completion'|'perceptual_fact'|'source_status',claim:命题,evidenceRefs:[后端证据ID]}。
sourceType=tool 且 ok=true、body 有非空正文时，文件或其他工具正文中的直接事实使用 kind=tool_fact，并引用该工具证据；失败或空正文不能作为 tool_fact。tool_completion 只表示工具操作的成功/失败，不能代替正文事实。不要把工具正文标为 perceptual_fact；perceptual_fact 只允许 sourceType=visual，source_status 只允许 sourceType=web。
说明某网页读取成功/失败属于 source_status，附 fetchStatus 与回执一致（例如 fetch_error 或 not_fetched）；这只证明抓取状态，不证明网页内容。不要用 inference 引用空正文来表达抓取失败。
sourceType=user_message/user_memory/user_adaptation 提供用户原话、经历与偏好，供你理解和回答；它们不证明工具已执行或记忆已保存。人格资料描述角色身份与设定。普通事实的可选注释不等于已核验知识，也不能替代状态补丁所需的来源和授权。
每项 tool_completion 必须单独声明 resultStatus:success/failed 并引用真实回执。这个字段只描述该操作，与本轮回答 status、整个任务 taskStatus 相互独立；如实报告一次失败可以完成用户的报告请求，不能因此把失败操作标成成功。画面结论必须引用实际识图证据。
本轮有实际工具回执且声明 completed 时，仍需记录执行或观察结果；普通事实注释不能替代工具结果。未读取的正文、未理解的截图、旧画面均不能冒充本次实际结果。
若工具失败，仍必须输出 cognitive 和 outcome，可 status=failed，解释失败用 inference 或空 claims。
最终结构以本轮收尾合同为准，按证据填写各状态；本次补丁新建或恢复任务时，也必须评估该任务的 taskStatus。
网页资料可跨轮复用，来源时间由后端回执提供；若命题要求当前新核验，声明 freshness:'current'。历史观察不能证明现在；重述旧观察或旧动作结果须声明 freshness:'historical'。
工具调用轮仍使用 kind=tool_use，不需伪造已经完成的 outcome。
任务有 successCriteria 时，宣告 taskStatus=completed 必须给 criteriaAssessment 数组，逐项引用本轮合同给出的 criterionId，并给 status:met/unmet/unknown、reason 和 evidenceRefs。你负责结合当前目标、正文、操作回执和回答内容判断是否达到条件；无法判断用 unknown，尚未达到用 unmet，不能只因工具步骤结束就说达成。所有条件 met 才能结束任务，仍可用 taskStatus=active 继续工作。条件被用户修改后使用新合同中的 ID。
"""


@dataclass(frozen=True)
class CurrentStateEvidenceRequirement:
    """当前本地状态断言所需的只读工具回执。"""

    requirement_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    source_type: str = "workspace"
    freshness: str = "current"
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirementId": self.requirement_id,
            "sourceType": self.source_type,
            "toolName": self.tool_name,
            "arguments": deepcopy(dict(self.arguments)),
            "freshness": self.freshness,
            "readOnly": self.read_only,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CurrentStateEvidenceRequirement | None":
        if not isinstance(value, Mapping):
            return None
        tool_name = str(value.get("toolName") or value.get("tool_name") or "").strip()
        arguments = value.get("arguments")
        if tool_name not in _CURRENT_STATE_READ_TOOLS or not isinstance(arguments, Mapping):
            return None
        canonical = canonical_tool_arguments(tool_name, arguments)
        encoded = json.dumps(
            {"sourceType": "workspace", "toolName": tool_name, "arguments": canonical},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            requirement_id=(
                str(value.get("requirementId") or value.get("requirement_id") or "").strip()
                or "current-state-" + sha256(encoded.encode("utf-8")).hexdigest()[:20]
            ),
            tool_name=tool_name,
            arguments=canonical,
        )


def current_state_evidence_requirement(
    request: Mapping[str, Any] | None,
) -> CurrentStateEvidenceRequirement | None:
    """把已解析的只读工作区请求转成可校验的证据需求。"""

    if not isinstance(request, Mapping):
        return None
    name = str(request.get("name") or request.get("tool") or "").strip()
    args = request.get("args")
    if name not in _CURRENT_STATE_READ_TOOLS or not isinstance(args, Mapping):
        return None
    return CurrentStateEvidenceRequirement.from_mapping({
        "toolName": name,
        "arguments": args,
    })


def current_state_evidence_status(
    requirement: Mapping[str, Any] | CurrentStateEvidenceRequirement | None,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """按工具种类和规范参数核对本轮成功回执。"""

    active = (
        requirement
        if isinstance(requirement, CurrentStateEvidenceRequirement)
        else CurrentStateEvidenceRequirement.from_mapping(requirement)
    )
    if active is None:
        return {"required": False, "satisfied": True, "status": "not_required"}
    request = {"name": active.tool_name, "args": dict(active.arguments)}
    matches: list[str] = []
    successful: list[str] = []
    readable: list[str] = []
    for ref, entry in evidence.items():
        if not isinstance(entry, Mapping) or entry.get("historical"):
            continue
        receipt = entry.get("receipt")
        if (
            entry.get("sourceType") != "tool"
            or not isinstance(receipt, Mapping)
            or not receipt_matches_request(receipt, request)
        ):
            continue
        matches.append(str(ref))
        if (
            entry.get("ok") is True
            and receipt.get("ok") is True
            and entry.get("executionStatus") == "success"
        ):
            successful.append(str(ref))
            if str(entry.get("body") or "").strip():
                readable.append(str(ref))
    status = (
        "success" if readable
        else "unreadable" if successful
        else "failed" if matches
        else "missing"
    )
    return {
        "required": True,
        "satisfied": bool(readable),
        "status": status,
        "requirement": active.to_dict(),
        "matchingEvidenceRefs": matches,
        "successfulEvidenceRefs": successful,
        "readableEvidenceRefs": readable,
    }


def evidence_freshness(entry: Mapping[str, Any]) -> str:
    """Return the freshness value used by the current validator.

    ``historical`` is the backend provenance bit currently enforced by the
    validator.  Missing or false values therefore mean current for this
    contract; a model supplied ``freshness`` field is never trusted here.
    """
    return "historical" if bool(entry.get("historical")) else "current"


def _freshness_requirement(kind: str, *, historical: bool) -> dict[str, Any]:
    required = historical and kind in _CURRENT_SENSITIVE_CLAIMS
    return {
        "required": required,
        "value": "historical" if required else None,
        "currentRejectedWhenExternalEvidenceRequired": False,
    }


def claim_contract_for_evidence(
    entry: Mapping[str, Any], evidence_ref: str | None = None, *, compact: bool = False,
) -> dict[str, Any]:
    """Derive the model-facing claim requirements from one real evidence entry.

    This is deliberately a projection of the checks in :func:`verify_outcome`:
    it reports backend-observed values and required values without changing a
    model claim or turning a receipt into readable tool content.
    """
    if not isinstance(entry, Mapping):
        entry = {}
    source_type = str(entry.get("sourceType") or "") or None
    actual_ok = entry.get("ok")
    body_present = bool(str(entry.get("body") or "").strip())
    receipt_present = isinstance(entry.get("receipt"), Mapping)
    historical = bool(entry.get("historical"))
    freshness = evidence_freshness(entry)
    result_status: str | None
    if actual_ok is True:
        result_status = "success"
    elif actual_ok is False:
        result_status = "failed"
    else:
        result_status = None

    def freshness_for(kind: str) -> dict[str, Any]:
        return _freshness_requirement(kind, historical=historical)

    # ``allowed`` is enforced only for execution/observation claims.
    # Ordinary facts are advisory. ``tool_completion`` follows the validator's
    # receipt check and therefore accepts legacy entries without sourceType;
    # the compact semantic projection only exposes it for tool evidence.
    requirements = {
        "tool_fact": {
            "allowed": source_type == "tool" and actual_ok is True and body_present,
            "required": {"sourceType": "tool", "ok": True, "body": "nonempty"},
            "freshness": freshness_for("tool_fact"),
        },
        "tool_completion": {
            "allowed": receipt_present,
            "required": {
                "receipt": "present",
                "resultStatus": result_status or "must_match_evidence_ok",
            },
            "freshness": freshness_for("tool_completion"),
        },
        "source_status": {
            "allowed": source_type == "web" and bool(entry.get("fetchStatus")),
            "required": {"sourceType": "web", "fetchStatus": entry.get("fetchStatus")},
            "freshness": {"required": False, "value": None},
        },
        "perceptual_fact": {
            "allowed": source_type == "visual" and actual_ok is True and body_present,
            "required": {"sourceType": "visual", "ok": True, "body": "nonempty"},
            "freshness": freshness_for("perceptual_fact"),
        },
    }
    for kind in ADVISORY_CLAIM_KINDS:
        requirements[kind] = {
            "allowed": True, "validation": "advisory", "required": {},
            "freshness": {"required": False, "value": None},
        }
    if source_type == "tool":
        applicable_kinds = ("tool_fact", "tool_completion")
    elif source_type == "web":
        applicable_kinds = ("external_fact", "source_status")
    elif source_type == "visual":
        applicable_kinds = ("perceptual_fact",)
    elif source_type in _USER_EVIDENCE_SOURCES:
        applicable_kinds = ("user_fact",)
    else:
        applicable_kinds = ()
    # A receipt remains reportable even if its visual/web body failed. Keep
    # every backend-supported operation result available in the compact packet.
    if receipt_present and "tool_completion" not in applicable_kinds:
        applicable_kinds = (*applicable_kinds, "tool_completion")
    projected_requirements = (
        {kind: requirements[kind] for kind in applicable_kinds}
        if compact else requirements
    )
    contract: dict[str, Any] = {
        "sourceType": source_type,
        "freshness": freshness,
        "historical": historical,
        "actualOk": actual_ok,
        "bodyPresent": body_present,
        "receiptPresent": receipt_present,
        "ordinaryAnswerEvidenceRequired": False,
        "executionStatus": entry.get("executionStatus"),
        "fetchStatus": entry.get("fetchStatus"),
        "claimRequirements": projected_requirements,
    }
    if compact:
        contract["applicableClaimKinds"] = list(applicable_kinds)
        contract["disallowedClaimKinds"] = [
            kind for kind in sorted(CLAIM_KINDS) if kind not in applicable_kinds and kind not in ADVISORY_CLAIM_KINDS
        ]
    if evidence_ref is not None:
        contract["evidenceRef"] = str(evidence_ref)
    if source_type in _USER_EVIDENCE_SOURCES:
        contract["preferredClaimKind"] = "user_fact"
    elif source_type == "tool":
        if requirements["tool_fact"]["allowed"]:
            contract["preferredClaimKind"] = "tool_fact"
        elif requirements["tool_completion"]["allowed"]:
            contract["preferredClaimKind"] = "tool_completion"
    elif source_type == "web":
        if receipt_present:
            contract["preferredClaimKind"] = "tool_completion"
        elif body_present:
            contract["preferredClaimKind"] = "external_fact"
        elif requirements["source_status"]["allowed"]:
            contract["preferredClaimKind"] = "source_status"
    elif source_type == "visual":
        if requirements["perceptual_fact"]["allowed"]:
            contract["preferredClaimKind"] = "perceptual_fact"
        elif receipt_present:
            contract["preferredClaimKind"] = "tool_completion"
    elif receipt_present:
        contract["preferredClaimKind"] = "tool_completion"
    return contract


def _evidence_diagnostics(contract: Mapping[str, Any], kind: str | None = None) -> dict[str, Any]:
    """Keep validation feedback tied to the same projected evidence facts."""
    requirement = ((contract.get("claimRequirements") or {}).get(kind or "")
                   if isinstance(contract.get("claimRequirements"), Mapping) else {})
    freshness = requirement.get("freshness") if isinstance(requirement, Mapping) else {}
    required = requirement.get("required") if isinstance(requirement, Mapping) else {}
    return {
        "actualSourceType": contract.get("sourceType"),
        "actualFreshness": contract.get("freshness"),
        "bodyPresent": contract.get("bodyPresent"),
        "receiptPresent": contract.get("receiptPresent"),
        "requiredValues": deepcopy(required) if isinstance(required, Mapping) else {},
        "requiredFreshness": freshness.get("value") if isinstance(freshness, Mapping) else None,
        "currentFreshnessRejectedWhenExternalEvidenceRequired": (
            bool(freshness.get("currentRejectedWhenExternalEvidenceRequired"))
            if isinstance(freshness, Mapping) else False
        ),
    }


def success_criteria_catalog(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Bind assessments to this task's exact criteria, without interpreting them."""
    criteria = task.get("successCriteria") or []
    if not isinstance(criteria, list):
        return []
    version = json.dumps([task.get("taskId"), criteria], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    declared_ids = [str(value.get("criterionId") or "").strip() if isinstance(value, Mapping) else "" for value in criteria]
    return [{"criterionId": declared_ids[index] if declared_ids[index] and declared_ids.count(declared_ids[index]) == 1
             else "criterion-" + sha256(f"{version}:{index}".encode("utf-8")).hexdigest()[:20],
             "criterion": deepcopy(value)} for index, value in enumerate(criteria)]


def final_outcome_contract(
    task: Mapping[str, Any], *, evidence: Mapping[str, Any] | None = None,
    current_state_requirement: Mapping[str, Any] | CurrentStateEvidenceRequirement | None = None,
) -> dict[str, Any]:
    """One output contract for initial generation and correction continuations.

    This describes fields, not their answers. Task completion remains the
    model's judgment, while execution claims bind to backend receipts.
    """
    fields: dict[str, Any] = {
        "status": ["completed", "partial", "needs_input", "failed"],
    }
    if task.get("taskId") and task.get("status") == "active":
        fields["taskStatus"] = ["active", "completed", "failed"]
    contract = {
        "requiredFinalContainerFields": ["kind", "cognitive", "outcome", "emotion", "proactiveDirective", "speechUnits"],
        "presentationContract": {
            "emotion": "首轮最终容器必须给出合法情绪标签，按实际回复表达选择；不要留给后置分类器补填。speechUnits 可覆盖顶层情绪。",
            "proactiveDirective": PROACTIVE_DIRECTIVE_INSTRUCTION,
            "voiceIntensity": {"type": "string", "enum": ["subtle", "normal", "strong"], "rule": "轻声使用 subtle；不要输出数字。emotionIntensity 才是连续数值。"},
            "voice": "情绪与说话方式分别表达：平静的安慰可以是 neutral 情绪，同时轻柔发声。用户明确要求轻声、温柔等表达时，在顶层或对应 speechUnits 显式给 voiceStyle、voiceTone、voiceIntensity，且与实际台词及情绪一致。轻柔发声可用 soft / gentle / subtle；不要为了轻声强行改成害羞或亲昵。",
        },
        "requiredOutcomeFields": fields,
        "optionalOutcomeFields": {"claims": "普通回答可省略；有本轮工具执行结果时，按实际回执记录执行/观察声明"},
        "ordinaryAnswerEvidenceRequired": False,
        "currentTaskId": task.get("taskId"),
        "currentTaskStatus": task.get("status"),
        "inactiveTaskStatusRule": "暂停/取消必须通过 PAUSE_TASK/CANCEL_TASK 补丁；补丁后可省略 taskStatus，也可复述实际 paused/cancelled。不能只靠 outcome 改成暂停/取消，不能顺手改成 completed。",
        "responseBoundaries": {
            "currentRequestFirst": "先满足 currentUserRequest 的回答形式和范围，再应用适用的旧经验；用户只要结论时，给结论或说明原因尚未确定，不再推荐步骤。",
            "factsAndHypotheses": "保留用户已经明确说明的现象；没有实际检验就不能改写成相反现象或确定原因。不要顺带补充本轮不需要且尚不确定的背景身份或设定。",
            "conversationalFactScope": "按用户真正要解决的问题区分讨论、安慰与事实查证。引语或背景说法不自动让整轮依赖核实：可讨论用户提供的文字和感受，未核实时把解释表达为你的理解，不宣称作者本意、准确出处或已查证。用户没有要求核对引语时，直接回应其讨论或情感需要，不要因觉得句子熟悉就顺手纠正原话、补作者意图或给出处。用户明确要求查证，或答案关键结论依赖不确定外部事实时，自主调用可用搜索工具；证据不足就保留不确定性。不要为普通讨论新增逐条依据审计。",
            "sources": "引用使用实际读到的页面标题及真实 URL；不要自行重新分配 S 编号。正文读到与否是可读性，官方原始材料/百科转述是来源层级，两者不能混称。两个语言版本不自动等于独立来源。",
        },
        "taskStatusRequiredIfStartingOrResumingTask": True,
        "taskAssessmentScope": "以上任务信息是生成本合同时的状态快照；若标有 uncommittedCandidateTask 则来自未提交候选。按本次补丁应用后的任务评估。有 active 任务时须给 taskStatus；没有任务时省略 taskStatus 和 taskId，只用 status 评估本轮请求。不要为填状态字段新建任务。",
        "statusScopes": {
            "status": "本轮用户请求是否完成；依据请求目标判断，不从工具成败推断",
            "taskStatus": "整个任务是否结束：还有工作 active，达到目标 completed，失败且不再继续 failed",
            "claims[].resultStatus": "该项工具的实际执行成败，与前两个状态独立",
        },
        "evidenceClaimContract": {
            "location": "packet.evidence[].claimContract",
            "rule": "只对执行/观察声明检查真实回执与状态。普通身份、知识、观点和解释无需 claims、basis 或 evidenceRefs；可选的普通事实注释不作为回答交付门槛，也不作为已核验知识。",
            "fields": ["evidenceRef", "sourceType", "freshness", "historical", "actualOk",
                       "bodyPresent", "receiptPresent", "executionStatus", "fetchStatus",
                       "claimRequirements"],
            "claimKinds": list(CLAIM_KINDS),
            "freshnessRule": "历史 tool_completion/tool_fact/perceptual_fact 必须 claim.freshness=historical，不能冒充当前执行或观察结果。",
            "bodyRule": "tool_fact、perceptual_fact 需要非空正文；source_status 检查 web 的真实 fetchStatus；tool_completion 检查真实 receipt 与 resultStatus。普通事实和观点没有强制引用合同。",
            "userEvidenceRule": "sourceType=user_message/user_memory/user_adaptation 映射为 user_fact；user_message 是本轮原话，user_memory/user_adaptation 是用户记录；它们不能证明外部事实或工具执行。",
        },
        "toolCompletionClaim": {
            "required": ["kind", "claim", "evidenceRefs", "resultStatus"],
            "kind": "tool_completion",
            "resultStatus": ["success", "failed"],
            "evidenceRefs": "真实工具回执 ID；resultStatus 必须与这些回执一致",
        },
        "toolFactClaim": {
            "required": ["kind", "claim", "evidenceRefs"],
            "kind": "tool_fact",
            "evidenceRefs": "真实 sourceType=tool 且 ok=true、body 非空的工具证据 ID",
        },
    }
    if evidence is not None:
        contract["availableEvidence"] = [
            claim_contract_for_evidence(item, evidence_ref=ref, compact=True)
            for ref, item in evidence.items() if isinstance(item, Mapping)
        ]
    requirement = (
        current_state_requirement
        if isinstance(current_state_requirement, CurrentStateEvidenceRequirement)
        else CurrentStateEvidenceRequirement.from_mapping(current_state_requirement)
    )
    if requirement is not None:
        contract["currentStateEvidenceRequirement"] = {
            **requirement.to_dict(),
            "completionRule": (
                "outcome.status=completed 前必须取得本轮同一工具和规范参数的成功、可读回执；"
                "历史回执、失败回执、其他路径、其他工具或模型自己的 completed 判断均不满足。"
            ),
            "unavailableRule": (
                "无法取得对应回执时，如实使用 partial、needs_input 或 failed；"
                "不得编造精确的当前文件、目录或内容状态。"
            ),
        }
    catalog = success_criteria_catalog(task)
    if catalog:
        contract["successCriteriaCatalog"] = catalog
        contract["criteriaAssessmentRequiredIfTaskCompleted"] = {
            "fields": ["criterionId", "status", "reason", "evidenceRefs"],
            "status": ["met", "unmet", "unknown"],
            "rule": "按当前目标逐项判断并说明依据；没有实际达到的条件不能标 met。所有条件 met 才能 taskStatus=completed。",
        }
    return contract


def bind_outcome_receipt_metadata(outcome: Any, evidence: Mapping[str, Any]) -> Any:
    """Fill omitted bookkeeping only when the named backend receipts agree.

    Explicit model judgments (including wrong ones) still reach the validator.
    No claim, reference, completion judgment or answer is invented here.
    """
    if not isinstance(outcome, Mapping):
        return outcome
    result = deepcopy(dict(outcome))
    claims = result.get("claims")
    if not isinstance(claims, list):
        return result
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        refs = claim.get("evidenceRefs")
        if not isinstance(refs, list) or not refs or any(
                not isinstance(ref, str) or not isinstance(evidence.get(ref), Mapping) for ref in refs):
            continue
        entries = [evidence[ref] for ref in refs]
        kind = claim.get("kind")
        if kind == "tool_completion" and "resultStatus" not in claim and all(
                isinstance(entry.get("receipt"), Mapping) and isinstance(entry.get("ok"), bool) for entry in entries):
            statuses = {entry["ok"] for entry in entries}
            if len(statuses) == 1:
                claim["resultStatus"] = "success" if statuses == {True} else "failed"
        if kind == "source_status" and "fetchStatus" not in claim and all(
                entry.get("sourceType") == "web" and isinstance(entry.get("fetchStatus"), str) for entry in entries):
            statuses = {entry["fetchStatus"] for entry in entries}
            if len(statuses) == 1 and "" not in statuses:
                claim["fetchStatus"] = next(iter(statuses))
        if kind in _CURRENT_SENSITIVE_CLAIMS and "freshness" not in claim and all(
                entry.get("historical") is True for entry in entries):
            claim["freshness"] = "historical"
    return result


def verify_outcome(
    outcome: Any, *, evidence: Mapping[str, Any], require_external_evidence: bool = False,
    task: Mapping[str, Any] | None = None,
    current_state_requirement: Mapping[str, Any] | CurrentStateEvidenceRequirement | None = None,
) -> dict[str, Any]:
    # Kept as a compatibility keyword for existing callers. Knowledge challenge
    # and research state are handled by their own task/patch contracts, never by
    # requiring ordinary prose to carry an additional reference worksheet.
    violations: list[dict[str, Any]] = []
    evidence_contracts = {
        ref: claim_contract_for_evidence(entry, evidence_ref=ref)
        for ref, entry in evidence.items() if isinstance(entry, Mapping)
    }

    def reject(code: str, **details: Any) -> None:
        violations.append({"code": code, **details})

    if not isinstance(outcome, Mapping):
        return {"provided": False, "valid": False, "violations": [{"code": "outcome_missing"}]}
    status = outcome.get("status")
    if "taskStatus" in outcome and outcome["taskStatus"] not in {"active", "completed", "failed", "paused", "cancelled"}:
        reject("outcome_task_status_invalid")
    if task is not None:
        task_id = task.get("taskId")
        if task_id and task.get("status") == "active" and "taskStatus" not in outcome:
            reject("outcome_task_assessment_missing", taskId=task_id,
                   repair="Declare taskStatus active/completed/failed after reviewing the current goal and success criteria.")
        if "taskStatus" in outcome:
            if not task_id or ("taskId" in outcome and outcome["taskId"] != task_id):
                reject("outcome_task_identity_mismatch", taskId=task_id)
            if (task.get("status") != "active" or outcome["taskStatus"] in {"paused", "cancelled"}) and outcome["taskStatus"] != task.get("status"):
                reject("outcome_task_transition_requires_patch", actualTaskStatus=task.get("status"))
            if outcome["taskStatus"] == "completed":
                unfinished = [str(step.get("stepId") or "") for step in task.get("plan", [])
                              if isinstance(step, Mapping) and step.get("name") and step.get("status") not in {"completed", "failed"}]
                if unfinished:
                    reject("task_steps_unfinished", stepIds=unfinished)
            catalog = {item["criterionId"]: item for item in success_criteria_catalog(task)}
            assessments = outcome.get("criteriaAssessment")
            if catalog and outcome["taskStatus"] == "completed" and not isinstance(assessments, list):
                reject("task_success_criteria_assessment_missing", criteria=list(catalog.values()))
            if assessments is not None:
                if not isinstance(assessments, list):
                    reject("task_success_criteria_assessment_invalid")
                else:
                    seen = set()
                    for assessment in assessments:
                        if not isinstance(assessment, Mapping):
                            reject("task_success_criteria_assessment_invalid")
                            continue
                        ident = assessment.get("criterionId")
                        if not isinstance(ident, str) or ident not in catalog:
                            reject("task_success_criterion_unknown", criterionId=ident)
                            continue
                        if ident in seen:
                            reject("task_success_criterion_duplicate", criterionId=ident)
                        seen.add(ident)
                        if assessment.get("status") not in {"met", "unmet", "unknown"}:
                            reject("task_success_criterion_status_invalid", criterionId=ident)
                        elif outcome["taskStatus"] == "completed" and assessment["status"] != "met":
                            reject("task_success_criterion_not_met", criterionId=ident)
                        if not isinstance(assessment.get("reason"), str) or not assessment["reason"].strip():
                            reject("task_success_criterion_reason_missing", criterionId=ident)
                        refs = assessment.get("evidenceRefs")
                        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                            reject("task_success_criterion_refs_invalid", criterionId=ident)
                        else:
                            for ref in refs:
                                if not isinstance(evidence.get(ref), Mapping):
                                    reject("outcome_evidence_missing", criterionId=ident, ref=ref)
                    if outcome["taskStatus"] == "completed" and set(catalog) - seen:
                        reject("task_success_criteria_incomplete", criterionIds=sorted(set(catalog) - seen))
    if status not in {"completed", "partial", "needs_input", "failed"}:
        reject("outcome_status_invalid")
    current_state_status = current_state_evidence_status(
        current_state_requirement,
        evidence,
    )
    if status == "completed" and not current_state_status["satisfied"]:
        evidence_status = str(current_state_status.get("status") or "missing")
        reject(
            {
                "failed": "outcome_current_state_evidence_failed",
                "unreadable": "outcome_current_state_evidence_unreadable",
            }.get(evidence_status, "outcome_current_state_evidence_missing"),
            requirement=current_state_status.get("requirement"),
            matchingEvidenceRefs=current_state_status.get("matchingEvidenceRefs", []),
            repair=(
                "Use kind=tool_use with the exact required read-only tool and arguments, then answer from its current successful receipt. "
                "If that evidence cannot be obtained, report partial/needs_input/failed without a precise current-state assertion."
            ),
        )
    claims = outcome.get("claims", [])
    if not isinstance(claims, list):
        reject("outcome_claims_must_be_list")
        claims = []
    if status == "completed" and not any(
        isinstance(claim, Mapping) and claim.get("kind") in {"tool_completion", "tool_fact", "perceptual_fact", "source_status"}
        for claim in claims
    ) and any(
        isinstance(entry, Mapping) and not evidence_contracts[ref]["historical"] and evidence_contracts[ref]["receiptPresent"]
        for ref, entry in evidence.items()
    ):
        reject("outcome_tool_conclusions_missing")
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            reject("outcome_claim_must_be_object", claimIndex=index)
            continue
        kind = claim.get("kind")
        if kind in ADVISORY_CLAIM_KINDS:
            # These optional annotations are not proof and cannot block delivery.
            # Execution claims in the same reply still take the strict path below.
            continue
        if kind == "tool_completion" and claim.get("resultStatus") not in {"success", "failed"}:
            reject("outcome_tool_status_required", claimIndex=index,
                   repair="Declare this operation's resultStatus success/failed independently of outcome.status and taskStatus.")
        if kind not in CLAIM_KINDS:
            reject("outcome_claim_kind_invalid", claimIndex=index)
        if not isinstance(claim.get("claim"), str) or not claim["claim"].strip():
            reject("outcome_claim_text_missing", claimIndex=index)
        refs = claim.get("evidenceRefs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            reject("outcome_evidence_refs_invalid", claimIndex=index)
            continue
        requires_evidence = kind in {"tool_completion", "tool_fact", "perceptual_fact"}
        if (requires_evidence or kind == "source_status") and not refs:
            reject("outcome_evidence_required", claimIndex=index)
        for ref in refs:
            entry = evidence.get(ref)
            if not isinstance(entry, Mapping):
                reject("outcome_evidence_missing", claimIndex=index, ref=ref)
                continue
            evidence_contract = evidence_contracts[ref]
            source_type = evidence_contract["sourceType"]
            body_present = evidence_contract["bodyPresent"]
            receipt_present = evidence_contract["receiptPresent"]
            if kind == "source_status":
                if source_type != "web":
                    reject("outcome_source_status_requires_web_receipt", claimIndex=index, ref=ref,
                           **_evidence_diagnostics(evidence_contract, kind))
                if not claim.get("fetchStatus") or claim.get("fetchStatus") != entry.get("fetchStatus"):
                    reject("outcome_source_status_mismatch", claimIndex=index, ref=ref,
                           actualFetchStatus=entry.get("fetchStatus"),
                           **_evidence_diagnostics(evidence_contract, kind))
            if kind == "tool_fact":
                if source_type != "tool":
                    reject("outcome_tool_fact_requires_tool_evidence", claimIndex=index, ref=ref,
                           **_evidence_diagnostics(evidence_contract, kind))
                if entry.get("ok") is not True:
                    reject("outcome_tool_fact_requires_success", claimIndex=index, ref=ref,
                           expectedOk=True, **_evidence_diagnostics(evidence_contract, kind))
                if not body_present:
                    reject("outcome_tool_fact_body_missing", claimIndex=index, ref=ref,
                           **_evidence_diagnostics(evidence_contract, kind))
            if requires_evidence:
                expected_ok: bool | None = True
                if kind == "tool_completion":
                    result_status = claim.get("resultStatus")
                    expected_ok = {"success": True, "failed": False}.get(result_status)
                elif kind == "tool_fact":
                    # The tool_fact branch above reports the precise source and
                    # body failures; do not add a generic status mismatch too.
                    expected_ok = None
                if expected_ok is not None and evidence_contract["actualOk"] is not expected_ok:
                    reject("outcome_evidence_status_mismatch", claimIndex=index, ref=ref, expectedOk=expected_ok,
                           **_evidence_diagnostics(evidence_contract, kind))
            freshness = claim.get("freshness")
            if freshness is not None and freshness not in {"historical", "current"}:
                reject("outcome_freshness_invalid", claimIndex=index,
                       **_evidence_diagnostics(evidence_contract, kind))
            needs_current = freshness == "current" or (
                kind in _CURRENT_SENSITIVE_CLAIMS and freshness != "historical"
            )
            if requires_evidence and evidence_contract["historical"] and needs_current:
                reject("outcome_historical_evidence_for_current_claim", claimIndex=index, ref=ref,
                       **_evidence_diagnostics(evidence_contract, kind))
            if kind == "tool_completion" and not receipt_present:
                reject("outcome_tool_receipt_missing", claimIndex=index, ref=ref,
                       **_evidence_diagnostics(evidence_contract, kind))
            if kind == "perceptual_fact" and not body_present:
                reject("outcome_readable_evidence_missing", claimIndex=index, ref=ref,
                       **_evidence_diagnostics(evidence_contract, kind))
            if kind == "perceptual_fact" and source_type != "visual":
                reject("outcome_visual_evidence_required", claimIndex=index, ref=ref,
                       **_evidence_diagnostics(evidence_contract, kind))
    return {"provided": True, "valid": not violations, "status": status, "violations": violations}
