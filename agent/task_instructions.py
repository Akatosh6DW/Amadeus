"""One task text across legacy Todo fields, editing and scheduled execution.

``details`` and ``onDue`` remain wire-compatible with older clients. For an
executable task they are write-through aliases, not independently authored
instructions. Old rows are read losslessly; no model, regex or action inference
is needed to carry their complete text into the execution turn.
"""
from __future__ import annotations

from typing import Any, Mapping


def _text(value: Any) -> str:
    return str(value or '').strip()


def task_instruction_text(task: Mapping[str, Any]) -> str:
    details, execution = _text(task.get('details')), _text(task.get('onDue'))
    if not details or details == execution:
        return execution or details
    if not execution:
        return details
    # Legacy tasks can contain additional requirements in either field. Keep
    # both verbatim. Let the model reconcile meaning, not a keyword heuristic.
    return (f'任务要求：\n{details}\n\n'
            f'执行补充（如有冲突，以任务要求为准）：\n{execution}')


def normalize_instruction_changes(
    current: Mapping[str, Any], changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve the actual edited alias before saving and before readback checks."""
    result = dict(changes)
    fields = ('details', 'onDue')
    if not any(key in result for key in fields):
        return result
    # Empty onDue deliberately switches back to a reminder. Editing reminder
    # notes alone must never grant permission to execute a task.
    if 'onDue' in result and not _text(result['onDue']):
        return result
    if not _text(result.get('onDue', current.get('onDue'))):
        return result
    changed = [key for key in fields if key in result
               and _text(result[key]) != _text(current.get(key))]
    if len(changed) == 1:
        instruction = _text(result[changed[0]])
    elif changed:
        instruction = task_instruction_text(result)
    else:
        instruction = task_instruction_text(current)
    # Clearing a redundant notes field does not silently disable execution.
    instruction = instruction or task_instruction_text(current)
    result.update(details=instruction, onDue=instruction)
    return result
