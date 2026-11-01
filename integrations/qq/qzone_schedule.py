"""Durable per-account cadence for optional autonomous QQ Space visits."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path


class QzoneSchedule:
    def __init__(self, runtime: Path):
        self.runtime = runtime

    def _path(self, account: str) -> Path:
        if not account.isascii() or not account.isdigit():
            raise ValueError('Invalid QQ Space account')
        return self.runtime / 'qzone' / account / 'schedule.json'

    def read(self, account: str) -> dict:
        try:
            value = json.loads(self._path(account).read_text(encoding='utf-8'))
            return value if isinstance(value, dict) and value.get('account') == account else {}
        except FileNotFoundError:
            return {}

    def _save(self, account: str, value: dict) -> None:
        path = self._path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)

    def claim(self, account: str, interval_minutes: int, *, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        state = self.read(account)
        if state.get('state') == 'running':
            # After a crash the last write might already have happened. Never
            # replay that run; defer a fresh visit for another full interval.
            state.update(state='unknown', nextRun=now + interval_minutes * 60)
            self._save(account, state)
            return None
        if now < state.get('nextRun', 0):
            return None
        run_id = 'qzone-' + uuid.uuid4().hex
        self._save(account, {'account': account, 'runId': run_id, 'state': 'running',
                            'startedAt': now, 'nextRun': now + interval_minutes * 60})
        return run_id

    def finish(self, account: str, run_id: str, state: str) -> None:
        value = self.read(account)
        if value.get('runId') == run_id:
            value.update(state=state, finishedAt=time.time())
            self._save(account, value)
