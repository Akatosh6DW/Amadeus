"""Separate belief content from backend-only dependency bookkeeping."""
from copy import deepcopy
from typing import Any, Mapping


def belief_in_current_scope(value: Mapping[str, Any], state: Mapping[str, Any],
                            snapshot: Mapping[str, Any], evidence: Mapping[str, Any]) -> bool:
    """An assistant's inference belongs to its originating task, not its persona.

    Preserve durable records for trace retrieval. A retrieved original turn can
    explicitly bring its inference back into view without promoting it to a
    permanent user fact.
    """
    turn_id = snapshot.get('currentTurnId')
    if not turn_id or value.get('domain') != 'assistant_inference':
        return True
    origin = value.get('sourceTurnId')
    if origin == turn_id:
        return True
    task = state.get('task') or {}
    if task.get('status') not in {'idle', 'completed', 'failed', 'cancelled', 'superseded'}:
        if value.get('taskId') and value['taskId'] == task.get('taskId'):
            return True
        if origin and origin == task.get('startedByTurnId'):
            return True
    return any(isinstance(item, Mapping) and item.get('selectedForCurrentTurn') is True
               and origin and item.get('sourceTurnId') == origin
               for item in evidence.values())


def belief_prompt_view(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep semantic/evidence fields exact; large context ID sets stay on disk.

    contextIds is a dependency set used by the state owner, not the text of a
    source or the evidenceRefs needed to verify a claim. Older versions attached
    the entire visible context to each inference, including repeated IDs. Do not
    require a destructive state migration to make those conversations usable.
    """
    result = deepcopy(dict(value))
    ids = result.get('contextIds')
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        return result
    distinct = list(dict.fromkeys(ids))
    if len(distinct) <= 8 and sum(map(len, distinct)) <= 600:
        result['contextIds'] = distinct
    else:
        result.pop('contextIds')
        result['_promptContextSupport'] = {
            'distinctContextCount': len(distinct),
            'fullListInStoredBelief': True,
            'note': '历史依赖编号留在本地记录；此计数不是事实证据，判断仍以原话和 evidenceRefs 为准',
        }
    return result
