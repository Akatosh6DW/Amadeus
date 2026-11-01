"""Last known display names, strictly scoped to one local QQ account."""
import json
from pathlib import Path


def clean_contacts(rows):
    result = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        kind, ident = row.get('kind'), str(row.get('id', ''))
        if kind not in {'private', 'group'} or not ident.isascii() or not ident.isdigit():
            continue
        result[(kind, ident)] = {'kind':kind, 'id':ident,
            'name':str(row.get('name') or '')[:100], 'remark':str(row.get('remark') or '')[:100]}
    return list(result.values())


def read_contacts(path: Path, account: str):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return clean_contacts(data.get('contacts')) if data.get('account') == account else []
    except (OSError, ValueError, AttributeError):
        return []


def write_contacts(path: Path, account: str, rows):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'account':account,'contacts':clean_contacts(rows)},ensure_ascii=False),encoding='utf-8')
    temporary.replace(path)
