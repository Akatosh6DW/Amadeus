"""Per-conversation login checkpoints; history remains the source of messages."""
from __future__ import annotations

import time
import json


def enabled_conversations(policy):
    return {tuple(key.split(':', 1)) for key, value in policy.get('conversation_overrides', {}).items()
            if value.get('reply_on_login') is True}


class LoginCheckpoints:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.lock, ledger.db:
            ledger.db.execute('''CREATE TABLE IF NOT EXISTS login_checkpoints (
                account TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
                checked_at REAL NOT NULL, checked_ids TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY(account,kind,target))''')
            if 'checked_ids' not in {row['name'] for row in ledger.db.execute('PRAGMA table_info(login_checkpoints)')}:
                ledger.db.execute("ALTER TABLE login_checkpoints ADD COLUMN checked_ids TEXT NOT NULL DEFAULT '[]'")

    def sync(self, policy, *, now=None):
        account = policy['account_id']
        scopes = enabled_conversations(policy)
        now = time.time() if now is None else now
        with self.ledger.lock, self.ledger.db:
            existing = self.ledger.db.execute(
                'SELECT kind,target FROM login_checkpoints WHERE account=?', (account,)).fetchall()
            for row in existing:
                if (row['kind'], row['target']) not in scopes:
                    self.ledger.db.execute('DELETE FROM login_checkpoints WHERE account=? AND kind=? AND target=?',
                                           (account, row['kind'], row['target']))
            for kind, target in scopes:
                self.ledger.db.execute('INSERT OR IGNORE INTO login_checkpoints(account,kind,target,checked_at) VALUES(?,?,?,?)',
                                       (account, kind, target, int(now)))

    def since(self, account, kind, target):
        with self.ledger.lock:
            row = self.ledger.db.execute(
                'SELECT checked_at FROM login_checkpoints WHERE account=? AND kind=? AND target=?',
                (account, kind, target)).fetchone()
            return float(row['checked_at']) if row else None

    def boundary_ids(self, account, kind, target):
        with self.ledger.lock:
            row = self.ledger.db.execute('SELECT checked_ids FROM login_checkpoints WHERE account=? AND kind=? AND target=?',
                                        (account, kind, target)).fetchone()
            return set(json.loads(row['checked_ids'])) if row else set()

    def advance(self, account, kind, target, timestamp, *, message_ids=()):
        with self.ledger.lock, self.ledger.db:
            previous = self.since(account, kind, target)
            if previous is None or int(timestamp) < previous:
                return
            ids = set(map(str, message_ids))
            if int(timestamp) == previous:
                ids.update(self.boundary_ids(account, kind, target))
            self.ledger.db.execute('''UPDATE login_checkpoints SET checked_at=?,checked_ids=?
                WHERE account=? AND kind=? AND target=?''',
                (int(timestamp), json.dumps(sorted(ids)), account, kind, target))


def pending_messages(rows, *, account, since, seen):
    """An actual later outgoing message closes the preceding backlog."""
    pending = []
    for row in rows:
        if row['time'] < since:
            continue
        if row.get('senderId') == account:
            pending.clear()
        elif not seen(row['messageId']):
            pending.append(row)
    return pending
