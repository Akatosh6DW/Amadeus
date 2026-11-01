"""Honor optional, explicitly declared QQ task constraints.

The model owns intent/reference resolution. This module checks its declaration
against the actual contact directory and delivery receipts; it never interprets
user prose, chooses message content, or sends a message.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from agent.dialogue_task import current_dialogue_task


QQ_TOOLS = frozenset({'qq_status', 'qq_activity', 'qq_history', 'qq_manage'})
# Identity lookup/alias writes are scoped identity operations, not delivery goals.
QQ_IDENTITY_TOOLS = frozenset({'qq_identity', 'qq_remember_alias', 'qq_person_memory'})
OPERATIONS = frozenset({'inspect', 'read', 'deliver', 'draft', 'configure'})
DELIVERY_ACTIONS = frozenset({'delegate', 'send', 'approve'})
# A delivery task may temporarily change the delivery scope around its send.
# These actions remain subject to the normal target/account/commit checks; this
# list only prevents the task-level goal gate from treating an explicitly
# requested mute/pause transition as an unrelated configuration goal.
DELIVERY_AUX_ACTIONS = frozenset({'mute', 'unmute', 'pause', 'resume'})


def qq_intent(task):
    # A worker generates a candidate for an already fixed recipient. The
    # gateway owns delivery; an owner-side delivery goal must not leak here.
    if current_dialogue_task() is not None:
        return None
    intent = task.get('userIntent') if isinstance(task, Mapping) else None
    return intent if isinstance(intent, Mapping) and intent.get('service') == 'qq' else None


def _receipts(evidence):
    for ref, entry in evidence.items():
        if isinstance(entry, Mapping) and not entry.get('historical') and isinstance(entry.get('receipt'), Mapping):
            yield ref, entry['receipt']


def _request(receipt):
    try:
        value = json.loads(receipt.get('_cognitiveRequestSignature') or '{}')
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _targets(task):
    target = task.get('target')
    return target if isinstance(target, list) else [target] if target is not None else []


def _key(value):
    return ' '.join(str(value or '').split()).casefold()


def _contact_keys(row):
    values = [row.get("id"), row.get("name"), row.get("remark")]
    aliases = row.get("aliases")
    if isinstance(aliases, str):
        values.append(aliases)
    elif isinstance(aliases, (list, tuple, set)):
        values.extend(aliases)
    return {_key(value) for value in values if value not in (None, "")}


def _issue(code, repair, **details):
    return {'code': code, 'repair': repair, **details}


def _directory(evidence):
    rows = [(ref, receipt) for ref, receipt in _receipts(evidence)
            if receipt.get('name') in {'qq_status', 'qq_activity'} and receipt.get('ok') is True]
    return rows[-1] if rows else (None, None)


def resolve_targets(task, evidence):
    """Return authoritative bindings without rewriting model-selected IDs."""
    ref, status = _directory(evidence)
    targets = _targets(task)
    if not targets:
        return [], []
    problems, resolved = [], []
    for target in targets:
        if (not isinstance(target, Mapping) or target.get('type') not in {'private', 'group'}
                or not isinstance(target.get('reference'), str) or not target['reference'].strip()):
            problems.append(_issue('qq_task_target_invalid',
                'task.target 必须保留用户指定对象：{type:private|group,reference:称呼或数字ID,id:数字ID或null}；多人可用对象列表。'))
            continue
        if status is None:
            # A durable task already carries a host-bound numeric recipient.
            # Requiring another model-driven contacts lookup loses that binding
            # and adds a purely mechanical prerequisite to every scheduled send.
            saved = next(((key, entry['target']) for key, entry in evidence.items()
                          if isinstance(entry, Mapping) and entry.get('sourceType') == 'task_target'
                          and not entry.get('historical') and isinstance(entry.get('target'), Mapping)
                          and all(entry['target'].get(k) == target.get(k)
                                  for k in ('type', 'reference', 'id', 'account'))), None)
            if saved is not None:
                key, binding = saved
                resolved.append({**binding, 'evidenceRef': key})
                continue
            problems.append(_issue('qq_target_directory_required', '先读取 qq_status，使用当前账号的通讯录核对收件人；不能从旧历史猜群号。'))
            continue
        policy = status.get('policy') or {}
        account = str(policy.get('account_id') or '')
        if target.get('account') and str(target['account']) != account:
            problems.append(_issue('qq_target_account_changed', '账号已经变化，需按当前用户意图重新指定目标，不能沿用旧账号的会话。'))
            continue
        kind, reference = target['type'], _key(target['reference'])
        candidates = {str(row.get('id')): row for row in status.get('contacts', [])
                      if isinstance(row, Mapping) and row.get('kind') == kind
                      and reference in _contact_keys(row)}
        # A user-specified number remains usable if nickname refresh is unavailable.
        if reference.isascii() and reference.isdecimal() and reference in [str(item) for item in policy.get(kind + '_ids', [])]:
            candidates = {reference: candidates.get(reference, {'id': reference, 'kind': kind})}
        if len(candidates) != 1:
            problems.append(_issue('qq_target_ambiguous' if candidates else 'qq_target_not_found',
                '称呼没有唯一对应到当前通讯录。不能任选数字ID；核对备注/群名，必要时向主人确认具体对象。',
                reference=target['reference'], candidates=list(candidates.values())))
            continue
        ident, contact = next(iter(candidates.items()))
        if target.get('id') is not None and str(target['id']) != ident:
            problems.append(_issue('qq_task_target_mismatch',
                '登记的数字ID与该称呼的通讯录记录不符。修正 task.target 的ID，不能改称呼来迁就错误ID。',
                reference=target['reference'], expectedTargetId=ident, suppliedTargetId=target['id']))
            continue
        resolved.append({'type': kind, 'reference': target['reference'], 'id': ident, 'account': account,
                         'name': contact.get('name', ''), 'remark': contact.get('remark', ''), 'evidenceRef': ref})
    return resolved, problems


def validate_qq_calls(task, calls, evidence):
    if current_dialogue_task() is not None:
        # Worker tool composition and its scoped executor enforce account,
        # recipient and read/mute-only capability. It has no qq_status tool.
        return []
    selected = [call for call in calls if call.get('name') in QQ_TOOLS]
    if not selected:
        return []
    if all(call.get('name') in {'qq_status', 'qq_activity'} for call in selected):
        # Discovery may support any task (e.g. finding the owner's destination
        # while creating a future reminder). It does not turn that task into an
        # immediate QQ delivery, nor require a second task-registration round.
        return []
    intent = qq_intent(task)
    if not intent:
        # understoodGoal + calls are already an executable decision. Requiring
        # a second START_TASK declaration here blocked the normal continuation
        # from discovery to delivery before the host could append its plan.
        # The tool adapter/gateway still owns account, recipient and approval
        # authorization; absence of this optional contract grants nothing.
        return []
    if intent.get('operation') not in OPERATIONS:
        return [_issue('qq_task_intent_invalid',
            '已声明的 QQ userIntent.operation 无效；使用 inspect/read/deliver/draft/configure 表达该任务的最终效果。')]
    operation = intent['operation']
    targeted = [call for call in selected if call['name'] == 'qq_history'
                or call['name'] == 'qq_manage' and (call.get('args', {}).get('targetType') or call.get('args', {}).get('targetId'))]
    targets = _targets(task)
    if not targets and (operation in {'read', 'deliver', 'draft'} or targeted):
        # Recipient arguments are already the model's selection. The gateway
        # checks actual account/scope; optional task bookkeeping is not a
        # second recipient authorization surface.
        return []
    resolved, problems = resolve_targets(task, evidence)
    # A directory lookup is precisely how an unresolved declaration is resolved.
    if targeted:
        if problems:
            return problems
        for call in targeted:
            args = call.get('args') or {}
            if not any(item['type'] == args.get('targetType') and item['id'] == str(args.get('targetId')) for item in resolved):
                problems.append(_issue('qq_tool_target_mismatch',
                    '工具尚未执行。参数指向的对象与主人指定的对象不同；使用通讯录中绑定的对象重写调用和计划。',
                    tool=call['name'], requestedTarget=args.get('targetId'), expectedTargets=resolved))
    else:
        problems = [item for item in problems if item['code'] == 'qq_task_target_invalid']
    for call in selected:
        name, args = call['name'], call.get('args') or {}
        action = args.get('action')
        if name == 'qq_manage':
            allowed = ((operation == 'deliver' and action in DELIVERY_ACTIONS | DELIVERY_AUX_ACTIONS)
                       or (operation == 'draft' and action == 'draft')
                       or (operation == 'configure' and action not in DELIVERY_ACTIONS | {'draft'}))
            if not allowed:
                problems.append(_issue('qq_goal_operation_mismatch',
                    '当前任务的最终效果不授权这个操作。只读/拟稿不能发送，发送任务不能擅自改授权设置。', operation=operation, action=action))
    return problems


def bind_qq_targets(task, evidence):
    """Enrich the same target with directory facts; don't invalidate lookup steps."""
    if not qq_intent(task):
        return
    resolved, problems = resolve_targets(task, evidence)
    if resolved and not problems:
        task['target'] = resolved if isinstance(task.get('target'), list) else resolved[0]


def qq_completion_violations(task, outcome, evidence, *, unrelated=False, allow_incomplete=False):
    intent = qq_intent(task)
    if not intent or task.get('status') != 'active':
        return []
    if unrelated and outcome.get('taskStatus') != 'completed':
        return []
    # Routing metadata cannot invalidate an actual completed delivery. If the
    # model marks a task completed, check its bound receipts below; without
    # receipts an unrelated chat still cannot complete an old QQ task.
    operation = intent.get('operation')
    if operation not in {'deliver', 'draft', 'read'}:
        return []
    completing = outcome.get('status') == 'completed' or outcome.get('taskStatus') == 'completed'
    if not completing:
        return []
    targets, problems = resolve_targets(task, evidence)
    if problems or not targets:
        return (problems or [_issue('qq_task_target_required', 'QQ任务缺少具体收件人。')]) if completing else []
    receipts = [receipt for _, receipt in _receipts(evidence)
                if (receipt.get('_cognitiveStepBinding') or {}).get('taskId') == task.get('taskId')]
    missing = []
    for target in targets:
        matched = []
        for receipt in receipts:
            args = _request(receipt).get('args') or {}
            kind = receipt.get('targetType') or args.get('targetType')
            ident = receipt.get('targetId') or args.get('targetId')
            if (kind != target['type'] or str(ident) != target['id']
                    or receipt.get('account') and str(receipt['account']) != target['account']):
                continue
            matched.append((receipt, args))
        if operation == 'read':
            satisfied = any(r.get('name') == 'qq_history' and r.get('ok') is True and r.get('attempted') is not False
                            and str(r.get('account')) == target['account'] for r, _ in matched)
        elif operation == 'draft':
            satisfied = any(r.get('name') == 'qq_manage' and r.get('ok') is True and r.get('draftId')
                            and r.get('attempted') is not False and str(r.get('account')) == target['account'] for r, _ in matched)
        else:
            satisfied = any(r.get('name') == 'qq_manage' and a.get('action') in DELIVERY_ACTIONS
                            and r.get('ok') is True and r.get('sent') is True and r.get('messageId')
                            and r.get('attempted') is not False and str(r.get('account')) == target['account']
                            for r, a in matched)
        if satisfied:
            continue
        if not completing:
            # A review draft, failure, or uncertain send can stop, with an honest
            # partial report. History alone is no reason to abandon an explicit reply.
            attempted = any(r.get('name') == 'qq_manage' and a.get('action') in DELIVERY_ACTIONS for r, a in matched)
            _, status = _directory(evidence)
            policy = (status or {}).get('policy') or {}
            blocked = (not (status or {}).get('connected') or policy.get('mode') == 'paused'
                       or target['id'] in policy.get('muted_' + target['type'] + '_ids', [])
                       or target['id'] not in policy.get(target['type'] + '_ids', []))
            if attempted or blocked:
                continue
        missing.append(target)
    if not missing:
        return []
    if operation == 'read':
        return [_issue('qq_goal_unfulfilled',
            '状态查询不等于读取会话。继续调用该目标的qq_history，读取成功后再回答；本任务不授权发消息。',
            operation=operation, targets=missing)]
    if operation == 'draft':
        return [_issue('qq_goal_unfulfilled',
            '尚未取得该目标的草稿结果。使用qq_manage action=draft并取得draftId；只拟稿，不能delegate/send/approve。',
            operation=operation, targets=missing)]
    return [_issue('qq_delivery_unfulfilled' if operation == 'deliver' else 'qq_goal_unfulfilled',
        '读取状态/历史不是向QQ回复。主人明确要求说一句时不能因闲聊或没有问句而自行放弃。'
        '尚未执行请继续正常权限链的qq_manage delegate（instruction保留主人要求），它会读取该子对话历史并回复。'
        '只有该对象sent=true且有messageId才算发送完成；待审核草稿、暂停、失败或结果未知须如实说明并保持任务未完成，不重复未知发送。',
        operation=operation, targets=missing)]
