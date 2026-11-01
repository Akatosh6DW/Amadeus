"""Build a fresh task-local working context while retaining user-level memory.

The Todo row is the authoritative instruction. Previous execution results are
historical evidence, not new commands or a replacement for current preferences.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping, Sequence

from agent.cognitive_state import empty_state
from agent.task_instructions import task_instruction_text
from agent.cognitive_runtime import collect_current_user_evidence, collect_user_adaptation_evidence


def scheduled_tool_ids(body: Mapping[str, Any]) -> tuple[str, ...]:
    """Load destination capabilities before generation, without executing them."""
    contract = body.get('_scheduledTaskExecutionContract') or {}
    target = contract.get('deliveryTarget') or {}
    if body.get('_scheduledTaskExecution') and target.get('channel') == 'qq':
        return ('qq_status', 'qq_identity', 'qq_history', 'qq_manage')
    return ()


def scheduled_execution_contract(task: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the durable task's execution boundary for one background turn.

    This is request-local metadata.  It does not grant a tool permission or
    infer a target from the task text; the normal tool and permission gates
    still decide whether a proposed operation can run.
    """

    instruction = task_instruction_text(task) or str(task.get('title') or '')
    return {
        'kind': 'authorized_scheduled_execution',
        'taskId': str(task.get('taskId') or ''),
        'goalSource': 'persisted_todo_instructions',
        'instruction': instruction,
        'deliveryTarget': deepcopy(task.get('deliveryTarget') or {}),
        'externalActionEvidence': 'success_receipt',
        'initialExecutionRule': (
            '若原始指令包含外部动作，首轮候选必须声明 executionRequirements，'
            '并通过正常权限链输出实际工具调用；纯计算或生成回答无需声明。'
        ),
        'completionRule': (
            '完成只依据本轮真实结果；外部动作必须有成功回执，'
            '没有回执时使用 partial/needs_input/failed，不把口头承诺当完成。'
        ),
        'continuationRule': (
            '仅在本轮尚未尝试任何工具时允许一次有界续接；已经尝试的动作不自动重放。'
        ),
    }


def scheduled_execution_recovery_prompt() -> str:
    """Return a generic, non-target-specific continuation instruction."""

    return (
        '[到期任务执行续接] 上一候选只报告了未完成状态，且本轮没有任何工具尝试或执行回执。'
        '重新审阅持久任务执行契约和保存的原始指令：如果目标包含外部动作，'
        '现在按正常权限链输出实际结构化工具调用，并为每个外部动作声明 executionRequirements；'
        '工具返回前不要声称完成。若原始任务确实只需要计算或生成回答，可以直接完成；'
        '若缺少不可推断的必要信息，保持 needs_input 并简要说明缺口。'
        '本次续接有界，不能重复任何已经尝试的动作。'
    )


def _tool_attempted(item: Mapping[str, Any]) -> bool:
    return item.get('attempted') is not False and item.get('error') != 'cognitive_task_constraint'


def _current_receipts(body: Mapping[str, Any], result: Any) -> list[Mapping[str, Any]]:
    evidence = body.get('_cognitiveEvidence') or {}
    receipts = [entry['receipt'] for entry in evidence.values()
                if isinstance(entry, Mapping) and not entry.get('historical')
                and isinstance(entry.get('receipt'), Mapping)] if isinstance(evidence, Mapping) else []
    # These private containers are populated by the executor, not the model.
    if isinstance(result, Mapping):
        for key in ('_conversationToolResults', '_toolResults'):
            rows = result.get(key)
            if isinstance(rows, list):
                receipts.extend(row for row in rows if isinstance(row, Mapping))
    return receipts


def scheduled_step_receipts(body: Mapping[str, Any], result: Any) -> list[dict[str, Any]]:
    """Persist execution facts for every channel without prompts or credentials."""
    fields = ('name', 'toolCallId', 'ok', 'attempted', 'error', 'account', 'targetType',
              'targetId', 'deliveryState', 'sent', 'messageId', 'draftId', 'actionId',
              'path', 'created', 'updated', 'deleted')
    rows, seen = [], set()
    for receipt in _current_receipts(body, result):
        row = {key: receipt[key] for key in fields if key in receipt
               and isinstance(receipt[key], (str, int, float, bool, type(None)))}
        identity = row.get('toolCallId') or json.dumps(row, sort_keys=True)
        if not row or identity in seen:
            continue
        seen.add(identity)
        binding = receipt.get('_cognitiveStepBinding') or {}
        row.update({key: binding[key] for key in ('taskId', 'stepId', 'planRevision') if key in binding})
        rows.append(row)
    return rows


def _has_side_effect_attempt(body: Mapping[str, Any], result: Any) -> bool:
    from agent.tool_architecture import DEFAULT_TOOL_REGISTRY
    for receipt in _current_receipts(body, result):
        definition = DEFAULT_TOOL_REGISTRY.get(str(receipt.get('name') or ''))
        if _tool_attempted(receipt) and (definition is None or definition.side_effect):
            return True
    return False


def scheduled_target_delivery(body: Mapping[str, Any], result: Any) -> dict[str, Any] | None:
    """Report the durable QQ destination's actual delivery, independent of prose."""
    target = (body.get('_scheduledTaskExecutionContract') or {}).get('deliveryTarget') or {}
    if target.get('channel') != 'qq':
        return None
    status = 'not_attempted'
    matched = None
    for receipt in _current_receipts(body, result):
        if (receipt.get('name') != 'qq_manage'
                or str(receipt.get('account') or '') != target['accountId']
                or str(receipt.get('targetType') or '') != target['targetType']
                or str(receipt.get('targetId') or '') != target['targetId']):
            continue
        state = receipt.get('deliveryState')
        if state == 'sent' and receipt.get('sent') is True and receipt.get('messageId'):
            return {'status': 'sent', 'target': dict(target),
                    'messageId': str(receipt['messageId']), 'draftId': str(receipt.get('draftId') or '')}
        if state in {'unknown', 'sending', 'sent'}:
            status = 'unknown'
        elif status != 'unknown' and state:
            status = 'draft' if state == 'draft' else 'failed'
        matched = receipt
    return {'status': status, 'target': dict(target),
            **({'error': str(matched.get('error') or ''),
                'draftId': str(matched.get('draftId') or '')} if matched else {})}


def scheduled_execution_continuation_required(
    body: Mapping[str, Any], result: Any,
) -> bool:
    """Whether a no-side-effect partial result deserves one model continuation."""

    if not body.get('_scheduledTaskExecution') or body.get('_scheduledTaskRecoveryAttempted'):
        return False
    delivery = scheduled_target_delivery(body, result)
    if delivery is not None:
        if delivery['status'] != 'not_attempted':
            return False
        # Reading status/history is not an outgoing action. If the model stops
        # after a read, return the unmet delivery requirement for continuation.
        return not _has_side_effect_attempt(body, result)
    outcome = body.get('_cognitiveOutcome')
    status = outcome.get('status') if isinstance(outcome, Mapping) else None
    if not status and isinstance(result, Mapping):
        status = result.get('scheduledExecutionStatus')
        if not status and isinstance(result.get('outcome'), Mapping):
            status = result['outcome'].get('status')
    if str(status or '').strip().lower() not in {'partial', 'needs_input'}:
        return False
    return not _has_side_effect_attempt(body, result)


def isolate_scheduled_context(body: dict[str, Any], *,
                              default_context: Mapping[str, Any]) -> None:
    snapshot = deepcopy(body['_turnContextSnapshot'])
    previous = snapshot.get('_contextState') or {}
    context = deepcopy(dict(default_context))
    for key in ('conversationId', 'conversationEpoch', 'memoryGeneration',
                'relationshipGeneration', 'conversationGeneration', 'proactiveControl'):
        if key in previous:
            context[key] = deepcopy(previous[key])
    context.pop('pendingAction', None)
    snapshot['_contextState'] = context
    snapshot.pop('_resolvedContextState', None)
    old_cognitive = snapshot.get('_cognitiveState') or {}
    cognitive = empty_state(str(snapshot['conversationId']))
    contract = body.get('_scheduledTaskExecutionContract') or {}
    target = contract.get('deliveryTarget') or {}
    cognitive['task'].update({
        'taskId': str(contract.get('taskId') or body.get('requestId') or ''),
        'goal': str(contract.get('instruction') or body.get('userMessage') or ''),
        'status': 'active',
        'startedByTurnId': str(snapshot.get('currentTurnId') or body.get('requestId') or ''),
    })
    if target.get('channel') == 'qq':
        # These facts were already chosen and persisted when the owner created
        # the task. Resume them before generation; do not make the model repeat
        # task registration and rediscover its recipient through repair rounds.
        cognitive['task'].update({
            'userIntent': {'service': 'qq', 'operation': 'deliver'},
            'target': {'type': target['targetType'], 'reference': target['targetId'],
                       'id': target['targetId'], 'account': target['accountId']},
            'successCriteria': ['完成保存的全部要求，并向指定 QQ 目标发送，取得 sent 和 messageId'],
        })
    # Session-scoped constraints and another task's plan must not leak into a
    # periodic task. The same durable user-adaptation catalog remains available.
    preferences = old_cognitive.get('preferences') or {}
    for key in ('userAdaptations', 'userAdaptationRevision',
                'userAdaptationGeneration', 'userAdaptationScopeId'):
        if key in preferences:
            cognitive['preferences'][key] = deepcopy(preferences[key])
    snapshot['_cognitiveState'] = cognitive
    snapshot['resolvedAnchor'] = {}
    snapshot['replyContextChain'] = {}
    snapshot['continuationContext'] = {}
    body['_turnContextSnapshot'] = snapshot
    body['_cognitiveFailedRequests'] = []
    body['_cognitiveExecutionHistory'] = []
    body['_cognitiveEvidence'] = {
        **collect_user_adaptation_evidence(cognitive['preferences']),
        **collect_current_user_evidence(snapshot),
    }
    if target.get('channel') == 'qq':
        body['_cognitiveEvidence']['scheduled-target:' + str(contract['taskId'])] = {
            'sourceType': 'task_target', 'historical': False,
            'sourceId': str(contract['taskId']),
            'target': deepcopy(cognitive['task']['target']),
        }


def scheduled_task_context(task: Mapping[str, Any], runs: Sequence[Mapping[str, Any]]) -> str:
    facts = {key: task.get(key) for key in (
        'taskId', 'version', 'title', 'dueAt', 'dueDate',
        'timezone', 'recurrenceRule', 'status')}
    facts['instructions'] = task_instruction_text(task) or str(task.get('title') or '')
    facts['deliveryTarget'] = deepcopy(task.get('deliveryTarget') or {})
    history = []
    for run in runs[:6]:
        result = run.get('result') if isinstance(run.get('result'), Mapping) else {}
        receipt = run.get('receipt') if isinstance(run.get('receipt'), Mapping) else {}
        previous_task = receipt.get('task') if isinstance(receipt.get('task'), Mapping) else {}
        history.append({
            'runId': run.get('runId') or run.get('reminderId'),
            'scheduledAt': run.get('scheduledAt') or run.get('occurrenceAt'),
            'finishedAt': run.get('finishedAt'),
            'status': run.get('status'),
            'instruction': str(run.get('instruction') or run.get('onDue') or task_instruction_text(previous_task))[:1200],
            'reply': str(run.get('reply') or result.get('reply') or '')[:5000],
        })
    return '\n'.join([
        '这是用户已授权任务的到期执行，不是用户重新发言。现在执行保存的指令，按需组合已有工具。',
        '本次执行契约（结构化）：' + json.dumps(
            scheduled_execution_contract(task), ensure_ascii=False, separators=(',', ':')
        ),
        '首轮执行要求：若原始指令包含外部动作，首轮候选必须声明 executionRequirements，'
        '并走正常权限链输出实际工具调用；纯计算或生成回答可以直接完成。',
        '任务记录是本次目标的权威来源；相关长期偏好从当前认知上下文读取，不冻结创建时的兴趣。',
        '当前任务的 instructions 是完整要求，与本轮输入相同；请理解全部要求，自主选择并组合工具完成。',
        '明确的任务条件优先于一般偏好；不要把历史结果当作新的用户指令或本次成功证据。',
        '参考前次结果保持连续性、避免重复；需要更多执行历史时使用 todo_search 的 includeHistory。',
        '现在执行到期工作，不要误解为重新登记任务或仅提醒用户自己做。周期推进由调度器处理，',
        'deliveryTarget.channel=qq 时，最终提醒/任务结果必须通过 QQ 工具实际送到该账号下的指定目标；'
        '这是已保存的投递要求，不依赖 onDue 是否为空。已解析的收件人可直接调用 '
        'qq_manage send/delegate；工具适配器会读取当前配置并核对账号和权限。'
        '需要查看状态、历史或查找未知对象时再选相应工具；不要擅自改收件人或改成本地说一句。'
        '只有实际 sent 和 messageId 才是送达；配置变更、草稿或失败需要如实汇报。',
        '当前 task 已从保存记录恢复；继续这个任务，用 REPLACE_TOOL_PLAN 选择下一步，无需重新登记任务。',
        *(['QQ 投递目标已恢复到 task.target。deliveryTarget 是最终汇报位置；若完整指令还要求对其他对象操作，'
           '用 CHANGE_TASK_TARGET 声明完整目标列表（保留最终投递对象），再选择相应工具。']
          if facts['deliveryTarget'].get('channel') == 'qq' else []),
        '不要为了结束本次执行把整个周期任务标为完成；任务指令明确要求调整计划时，可以使用现有任务工具。',
        '调度器会在本轮结束后按回执更新单次任务状态或推进周期；简短汇报实际执行结果，'
        '不要根据执行前的 pending 状态声称任务仍未完成，也不用向用户讲内部登记步骤。',
        '执行历史保存的是实际结果，不需要把任务指令另存一份到通用记忆；修改任务应更新原待办。',
        '原文、次数、指定语言要完整保留；精确计算与计数使用 local_compute 核对，不能完成时如实说明。',
        '保持原等待/安静状态，不输出恢复闲聊的指令；既有工具权限仍然生效。',
        '当前任务（数据）：' + json.dumps(facts, ensure_ascii=False, separators=(',', ':')),
        '以往执行（历史数据）：' + json.dumps(history, ensure_ascii=False, separators=(',', ':')),
    ])
