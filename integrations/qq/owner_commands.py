"""Explicit command routing for an already authenticated QQ owner."""
from __future__ import annotations

import re


def persona_command(text: str, assistant_account: str) -> str | None:
    """Exact owner command for this conversation, never inferred from history."""
    current = str(text or '').strip()
    mention = f'@<QQ:{assistant_account}>'
    while assistant_account and current.startswith(mention):
        current = current[len(mention):].lstrip()
    parts = current.casefold().split()
    names = {'game': 'game', '红莉栖': 'game', 'tieba': 'tieba', '贴吧': 'tieba', '孙吧': 'tieba',
             'kangba': 'kangba', '抗吧': 'kangba', '最强攻击性语料': 'kangba',
             'auto': 'auto', '默认': 'auto'}
    if len(parts) == 2 and parts[0] == '/persona':
        return names.get(parts[1])
    return None


def developer_command(text: str, assistant_account: str) -> str | None:
    """Recognize a current-message /dev prefix, never quoted/history content.

    This parser does not authenticate a sender. Its caller must validate the
    owner identity, managed account and enabled policy before using the result.
    """
    current = str(text or "").strip()
    mention = f"@<QQ:{assistant_account}>"
    while assistant_account and current.startswith(mention):
        current = current[len(mention):].lstrip()
    match = re.match(r"/dev(?:\s+|$)", current, flags=re.IGNORECASE)
    return current[match.end():].strip() if match else None
