"""Lossless forum import: every source thread and reply occurrence is retained."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from agent.fingerprints import legacy_persistent_fingerprint
from agent.tieba_corpus import _terms


def build_raw_forum_index(source: Path, destination: Path, *, style: str, label: str) -> dict:
    receipt = json.loads((source / 'source.json').read_text(encoding='utf-8'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix('.building.sqlite3')
    if staging.exists():
        raise FileExistsError(staging)
    db = sqlite3.connect(staging)
    counts = Counter()
    lengths = Counter()
    try:
        db.executescript('''
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE threads(id INTEGER PRIMARY KEY, source_file TEXT, source_record INTEGER,
                                 raw_json TEXT NOT NULL);
            CREATE TABLE pairs(id INTEGER PRIMARY KEY, source_line INTEGER,
                               prompt TEXT NOT NULL, reply TEXT NOT NULL, provenance TEXT NOT NULL);
            CREATE VIRTUAL TABLE search USING fts5(prompt_terms, reply_terms, content='');
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        for entry in receipt['files']:
            if not entry['file'].endswith('.json'):
                continue
            raw = (source / entry['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Source bytes changed: ' + entry['file'])
            for position, thread in enumerate(json.loads(raw.decode('utf-8'))):
                counts['sourceThreads'] += 1
                thread_id = counts['sourceThreads']
                # Fail visibly for an unsupported source schema instead of
                # silently discarding a record. Text has no content filter.
                if not isinstance(thread, dict) or not isinstance(thread.get('回复列表'), list):
                    raise ValueError(f'Unsupported source record: {entry["file"]}:{position}')
                db.execute('INSERT INTO threads VALUES (?, ?, ?, ?)',
                    (thread_id, entry['file'], position, json.dumps(thread, ensure_ascii=False)))
                title, body = thread['标题'], thread['楼主内容']
                context = title if not body or title == body else title + '\n' + body
                for reply_index, reply in enumerate(thread['回复列表']):
                    if not isinstance(reply, str):
                        raise ValueError(f'Unsupported reply: {entry["file"]}:{position}:{reply_index}')
                    counts['sourceReplies'] += 1
                    number = counts['sourceReplies']
                    identity = f'{receipt["forum"]}|{receipt["revision"]}|{entry["file"]}|{position}|{reply_index}'
                    origin = {'id': legacy_persistent_fingerprint(identity, length=24),
                        'forum': receipt['forum'], 'revision': receipt['revision'],
                        'sourceFile': entry['file'], 'sourceRecord': position, 'replyIndex': reply_index,
                        'sourceUrl': thread['链接'], 'relation': 'thread_comment', 'threadId': thread_id}
                    db.execute('INSERT INTO pairs VALUES (?, ?, ?, ?, ?)',
                        (number, number, context, reply, json.dumps(origin, ensure_ascii=False)))
                    db.execute('INSERT INTO search(rowid, prompt_terms, reply_terms) VALUES (?, ?, ?)',
                        (number, ' '.join(_terms(context)), ' '.join(_terms(reply))))
                    lengths[len(reply)] += 1
                if thread_id % 200 == 0:
                    db.commit()
        manifest = {**receipt, 'schemaVersion': 3, 'style': style, 'label': label,
            'sourceReceipt': receipt, 'selectionMode': 'full_raw', 'reviewRequired': False,
            **dict(counts), 'indexedThreads': counts['sourceThreads'],
            'indexedPairs': counts['sourceReplies'], 'indexedReplies': counts['sourceReplies'],
            'droppedThreads': 0, 'droppedReplies': 0,
            'replyLengthDistribution': dict(sorted(lengths.items())),
            'scope': 'All raw thread and reply occurrences, unchanged; no semantic review or content filtering. Retrieval, not model fine-tuning.'}
        db.execute('INSERT INTO metadata VALUES (?, ?)', ('manifest', json.dumps(manifest, ensure_ascii=False)))
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Corpus index integrity check failed')
    finally:
        db.close()
    staging.replace(destination)
    destination.with_suffix('.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest
