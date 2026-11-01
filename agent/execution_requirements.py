"""Model-declared execution obligations, checked against real current-turn receipts."""
from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from agent.cognitive_tools import canonical_tool_arguments, receipt_matches_request
from agent.tool_architecture import DEFAULT_TOOL_REGISTRY


def requirements(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError('executionRequirements must be a list of at most 12 concrete operations')
    output = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError('execution requirement must be an object')
        call = item.get('call')
        if (not isinstance(call, Mapping) or not DEFAULT_TOOL_REGISTRY.get(str(call.get('name', '')))
                or not isinstance(call.get('args', {}), Mapping)):
            raise ValueError('execution requirement needs a known tool call with concrete arguments; read missing targets first')
        completion = item.get('completion', 'success_receipt')
        if completion not in {'success_receipt', 'attempt_receipt'}:
            raise ValueError('invalid execution completion requirement')
        expected = item.get('resultEquals', {})
        if not isinstance(expected, Mapping) or len(expected) > 12 or any(
                not isinstance(key, str) or not key or isinstance(val, (dict, list)) for key, val in expected.items()):
            raise ValueError('resultEquals must contain scalar result paths and values')
        output.append({'call': deepcopy(dict(call)), 'completion': completion, 'resultEquals': dict(expected)})
    return output


def merge_calls(calls, obligations):
    result = []
    for call in [*(calls or []), *(item['call'] for item in obligations)]:
        name = call.get('name')
        args = canonical_tool_arguments(name, call.get('args', {}))
        if not any(old.get('name') == name and canonical_tool_arguments(name, old.get('args', {})) == args for old in result):
            result.append(deepcopy(call))
    return result


def remember(body, obligations):
    saved = body.setdefault('_executionRequirements', [])
    for item in obligations:
        if item not in saved:
            saved.append(deepcopy(item))


def unsatisfied(obligations, receipts):
    def matches(receipt, item):
        if not receipt_matches_request(receipt, item['call']):
            return False
        if item['completion'] == 'success_receipt' and receipt.get('ok') is not True:
            return False
        # A send API can successfully create a review draft; that is not delivery.
        if (item['completion'] == 'success_receipt' and item['call']['name'] == 'qq_manage'
                and item['call'].get('args', {}).get('action') in {'send', 'approve', 'delegate'}
                and not (receipt.get('sent') is True and receipt.get('messageId'))):
            return False
        for path, expected in item['resultEquals'].items():
            actual = receipt
            for key in path.split('.'):
                actual = actual.get(key) if isinstance(actual, Mapping) else None
            if actual != expected:
                return False
        return True
    return [item for item in obligations if not any(matches(receipt, item) for receipt in receipts)]
