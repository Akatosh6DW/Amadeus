"""Public owner-control acknowledgements contain statuses, never private text."""
from __future__ import annotations

import json
from collections.abc import Mapping


CONTROL_ACKS = {
    'configured': '设置已保存',
    'paused': '已暂停自动回复',
    'resumed': '已恢复自动回复，单独暂停的会话保持暂停',
    'muted': '已暂停指定会话的自动回复',
    'unmuted': '已恢复指定会话的回复设置',
    'stopped': '已停止 QQ 后台代管',
    'sent': '消息已发出',
    'drafted': '草稿已保存，还没发送',
}


def control_ack_codes(body: Mapping) -> list[str]:
    """Project current executed QQ control receipts, excluding all content.

    Only a control-classified owner request calls this projection. Private
    reads, unknown tools, failed actions and missing receipts retain private
    delivery. No model-written reply/reason/path/recipient is copied to a group.
    """
    codes = []
    evidence = body.get('_cognitiveEvidence') or {}
    for entry in evidence.values() if isinstance(evidence, Mapping) else []:
        if not isinstance(entry, Mapping) or entry.get('historical'):
            continue
        receipt = entry.get('receipt')
        if not isinstance(receipt, Mapping):
            continue
        name = receipt.get('name')
        if name in {'qq_status', 'qq_identity'}:
            continue
        if name != 'qq_manage' or receipt.get('ok') is not True or receipt.get('attempted') is False:
            return []
        try:
            request = json.loads(receipt.get('_cognitiveRequestSignature') or '{}')
            args = request.get('args') or {}
            action = args.get('action')
        except (ValueError, TypeError, AttributeError):
            return []
        if action == 'configure' and isinstance(receipt.get('policy'), Mapping):
            code = 'configured'
        elif action in {'pause', 'resume', 'mute', 'unmute', 'stop'}:
            code = {'pause': 'paused', 'resume': 'resumed', 'mute': 'muted',
                    'unmute': 'unmuted', 'stop': 'stopped'}[action]
        elif action in {'send', 'delegate', 'approve', 'draft'}:
            if receipt.get('sent') is True and receipt.get('messageId'):
                code = 'sent'
            elif receipt.get('draftId') and receipt.get('deliveryState') == 'draft':
                code = 'drafted'
            else:
                return []
        else:
            return []
        if code not in codes:
            codes.append(code)
    return codes


def control_ack_text(codes) -> str:
    if not isinstance(codes, list) or not codes or any(
            not isinstance(code, str) or code not in CONTROL_ACKS for code in codes):
        return ''
    return '，'.join(CONTROL_ACKS[code] for code in dict.fromkeys(codes))
