"""Model-declared answer exclusions, scoped to the current conversation topic.

An excluded answer target is not a false external fact. This module records
the user's intended scope without interpreting their natural-language words.
"""
from collections.abc import Mapping
from copy import deepcopy


ANSWER_SCOPE_RULE = """[回答对象与纠正范围]
纠正分两类：报道/事实本身不真，与这条报道不是用户要找的对象。不能把后者当作前者撤回事实。
用户排除旧答案对象时，即使新对象尚不明确，也已有可保存的信息。必须在 cognitive.answerScope 中声明：
{\"mode\":\"revise\",\"goal\":\"原问题与当前补充合并后的目标\",\"excludedTargets\":[\"用户已排除的具体对象\"],\"sourceSpan\":\"本轮用户原文片段\",\"needsNewSearch\":true或false}。
goal 保留原始时间范围；excludedTargets 写具体对象而不是“这个”。后端保存的 originalGoal 和 userRequirements 是原始目标及用户后续原话，优先于模型的 goal 解释；只有用户后续明确修改才能变更时间范围，不能因旧材料的日期自行缩窄。网页内容不因被排除而变假。needsNewSearch 表示原资料不够、必须重新搜索才能完成当前目标；不能把重新朗读旧结果当新查证。
纠错分析阶段始终声明 cognitive.answerScope：不是对象纠正且无已有范围时用 {\"mode\":\"none\",\"reason\":\"具体原因\"}；不能以需要澄清为由遗漏已经确定的排除条件。
packet.context.answerScope 存在时，每个新用户轮先声明 {\"mode\":\"continue\"} 延续这个话题，或 {\"mode\":\"new_topic\"} 表示用户已换题。仅补充形容词、时间或短语也要结合原目标和排除项，不重新回到已排除答案。真正换题则释放旧范围，不将它用于无关问题。
同轮工具续写可以省略已接受的声明；needsNewSearch 尚未满足时可以追问或说明限制，不能把旧搜索结果当成完成。完成后的同话题追问仍沿用排除项，用户换题时清除。
"""


def apply_answer_scope(context: dict, declaration, *, user_message: str, turn_id: str, original_goal: str = '') -> None:
    existing = context.get('answerScope')
    if declaration is None:
        if isinstance(existing, Mapping) and existing.get('lastAppliedTurnId') != turn_id:
            raise ValueError('answer_scope_relation_required: declare cognitive.answerScope.mode=continue or new_topic for the existing topic; use revise for additional exclusions')
        return
    if not isinstance(declaration, Mapping):
        raise ValueError('answer_scope_must_be_object')
    mode = declaration.get('mode')
    if mode not in {'none', 'revise', 'continue', 'new_topic'}:
        raise ValueError('answer_scope_mode_invalid')
    if mode == 'none':
        if existing:
            raise ValueError('answer_scope_relation_required: use continue or new_topic when a scope already exists')
        if not isinstance(declaration.get('reason'), str) or not declaration['reason'].strip():
            raise ValueError('answer_scope_none_requires_reason')
        return
    if mode == 'new_topic':
        context.pop('answerScope', None)
        return
    if mode == 'continue':
        if not isinstance(existing, Mapping):
            raise ValueError('answer_scope_missing: no previous topic scope to continue')
        requirements = deepcopy(existing.get('userRequirements') or [])
        if not any(item.get('turnId') == turn_id for item in requirements):
            requirements.append({'turnId': turn_id, 'text': user_message})
        context['answerScope'] = {**deepcopy(dict(existing)), 'lastAppliedTurnId': turn_id,
                                  'userRequirements': requirements[-16:]}
        return
    goal = declaration.get('goal')
    targets = declaration.get('excludedTargets')
    source = declaration.get('sourceSpan')
    needs_search = declaration.get('needsNewSearch')
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        raise ValueError('answer_scope_goal_required')
    if not isinstance(targets, list) or not targets or len(targets) > 16 or any(
            not isinstance(target, str) or not target.strip() or len(target) > 1000 for target in targets):
        raise ValueError('answer_scope_excluded_targets_required')
    if not isinstance(source, str) or not source.strip() or source not in user_message:
        raise ValueError('answer_scope_source_span_required: use an exact span of the current user message')
    if not isinstance(needs_search, bool):
        raise ValueError('answer_scope_search_decision_required')
    previous = dict(existing) if isinstance(existing, Mapping) else {}
    exclusions = list(dict.fromkeys([*previous.get('excludedTargets', []), *[t.strip() for t in targets]]))[-16:]
    requirements = deepcopy(previous.get('userRequirements') or [])
    if not any(item.get('turnId') == turn_id for item in requirements):
        requirements.append({'turnId': turn_id, 'text': user_message})
    context['answerScope'] = {
        'scopeId': previous.get('scopeId') or 'answer-scope:' + turn_id,
        'goal': goal.strip(), 'excludedTargets': exclusions,
        'originalGoal': previous.get('originalGoal') or original_goal or goal.strip(),
        'userRequirements': requirements[-16:],
        'sourceSpan': source, 'sourceTurnId': turn_id, 'lastAppliedTurnId': turn_id,
        'needsNewSearch': bool(previous.get('needsNewSearch') or needs_search),
    }


def has_current_search_evidence(evidence: Mapping) -> bool:
    for entry in evidence.values():
        if not isinstance(entry, Mapping) or entry.get('historical'):
            continue
        receipt = entry.get('receipt')
        if (isinstance(receipt, Mapping) and receipt.get('name') == 'web_search'
                and receipt.get('ok') is True and receipt.get('attempted', True) is not False):
            return True
    return False
