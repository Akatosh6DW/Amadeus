"""SQLite owns wakeups, experience and publication receipts across restarts."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from .config import normalize_config, active_hour
from .rhythm import ACTIVITIES, configured_activity, interval_minutes, next_active_time


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class LifeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS life_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS life_runs (
                        id TEXT PRIMARY KEY, day TEXT NOT NULL, started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                        summary TEXT NOT NULL DEFAULT '', steps TEXT NOT NULL DEFAULT '[]',
                        kind TEXT NOT NULL DEFAULT 'legacy');
                    CREATE TABLE IF NOT EXISTS life_drafts (
                        id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                        account TEXT NOT NULL, visibility TEXT NOT NULL, content TEXT NOT NULL,
                        state TEXT NOT NULL, created_at TEXT NOT NULL,
                        publish_day TEXT NOT NULL DEFAULT '', receipt TEXT NOT NULL DEFAULT '{}');
                    CREATE TABLE IF NOT EXISTS life_chats (
                        id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                        account TEXT NOT NULL, owner TEXT NOT NULL, messages TEXT NOT NULL,
                        state TEXT NOT NULL, created_at TEXT NOT NULL,
                        receipt TEXT NOT NULL DEFAULT '{}');
                """)
                if "kind" not in {row[1] for row in connection.execute("PRAGMA table_info(life_runs)")}:
                    connection.execute("ALTER TABLE life_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'legacy'")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _get(db, key, default):
        row = db.execute("SELECT value FROM life_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT INTO life_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dump(value)))

    def config(self) -> dict:
        with self.db() as db:
            return normalize_config(self._get(db, "config", {}))

    def save_config(self, patch: dict) -> dict:
        with self.db() as db:
            current = normalize_config(self._get(db, "config", {}))
            config = normalize_config({**current, **patch})
            self._set(db, "config", config)
            if config["enabled"] != current["enabled"]:
                self._set(db, "activitySchedule", {})
            return config

    def _rhythm(self, db, config: dict, now: datetime) -> list[dict]:
        """Persist each clock separately; old aggregate budgets never consume a chat slot."""
        saved = self._get(db, "activitySchedule", {})
        schedule, result = {}, []
        counts = {row[0]: row[1] for row in db.execute(
            "SELECT kind,count(*) FROM life_runs WHERE day=? GROUP BY kind", (now.date().isoformat(),))}
        for kind, definition in ACTIVITIES.items():
            enabled = config["enabled"] and configured_activity(config, kind)
            minutes = interval_minutes(config, kind)
            previous = saved.get(kind, {})
            # Changing the bound contact starts a new contact rhythm, not an old recipient's clock.
            scope = [config["publishAccount"], config["qqChatOwner"]] if kind == "chat" else []
            if not enabled:
                due = None
            elif not previous.get("enabled") or previous.get("scope") != scope:
                delay = definition["firstMinutes"] * 100 / config["activityFrequency"]
                due = next_active_time(next_active_time(now, config) + timedelta(minutes=delay), config)
            else:
                due = datetime.fromisoformat(previous["nextAt"])
                if previous["intervalMinutes"] != minutes:
                    remaining = max(0, (due - now).total_seconds())
                    due = now + timedelta(seconds=min(minutes * 60,
                        remaining * minutes / previous["intervalMinutes"]))
                due = next_active_time(due, config)
            schedule[kind] = {"enabled": enabled, "nextAt": due.isoformat() if due else "",
                              "intervalMinutes": minutes, "scope": scope}
            if due and kind == "chat":
                ready = self._next_chat_at(db, config)
                if ready:
                    due = next_active_time(max(due, datetime.fromisoformat(ready)), config)
            result.append({"kind": kind, "label": definition["label"], "enabled": enabled,
                           "nextAt": next_active_time(max(now, due), config).isoformat() if due else "",
                           "intervalMinutes": minutes, "runsToday": counts.get(kind, 0)})
        if schedule != saved:
            self._set(db, "activitySchedule", schedule)
        return result

    def recover(self):
        # Called only after the application has obtained its process lock.
        with self.db() as db:
            db.execute("UPDATE life_runs SET status='interrupted', summary='上次运行中断，下次从日记继续。' WHERE status='running'")
            db.execute("UPDATE life_drafts SET state='unknown', receipt=? WHERE state='publishing'",
                       (dump({"error": "interrupted_delivery", "message": "发送期间进程退出，请核对平台；不会自动重发。"}),))
            db.execute("UPDATE life_chats SET state='unknown', receipt=? WHERE state='sending'",
                       (dump({"error": "interrupted_delivery", "message": "发送期间进程退出，不会自动重发。"}),))

    def chat_delivery_states(self):
        with self.db() as db:
            return {"life-chat-" + row["id"]: row["state"] for row in db.execute("SELECT id,state FROM life_chats")}

    def claim(self, now: datetime, *, manual: bool = False, opportunity_due: bool = False,
              allowed_kinds: set[str] | None = None) -> tuple[str, str]:
        with self.db() as db:
            config = normalize_config(self._get(db, "config", {}))
            if db.execute("SELECT 1 FROM life_runs WHERE status='running'").fetchone():
                return "", "already_running"
            if not manual:
                if not config["enabled"]:
                    return "", "disabled"
                if not active_hour(config, now.hour):
                    return "", "outside_active_hours"
            kind = "manual"
            if not manual:
                rhythm = self._rhythm(db, config, now)
                schedule = self._get(db, "activitySchedule", {})
                candidates = []
                for entry in rhythm:
                    if not entry["enabled"] or (allowed_kinds is not None and entry["kind"] not in allowed_kinds):
                        continue
                    due = datetime.fromisoformat(entry["nextAt"])
                    if opportunity_due and entry["kind"] == "reflect":
                        due = now
                    if due <= now:
                        # Keep original overdue times for fairness when several clocks are due.
                        original = schedule[entry["kind"]]["nextAt"]
                        candidates.append((min(due, datetime.fromisoformat(original)), entry["kind"]))
                if not candidates:
                    return "", "not_due"
                kind = min(candidates)[1]
            day = now.date().isoformat()
            run_id = uuid.uuid4().hex
            db.execute("INSERT INTO life_runs(id,day,started_at,status,kind) VALUES (?,?,?,'running',?)",
                       (run_id, day, now.isoformat(), kind))
            if not manual:
                schedule = self._get(db, "activitySchedule", {})
                schedule[kind]["nextAt"] = next_active_time(
                    now + timedelta(minutes=interval_minutes(config, kind)), config).isoformat()
                self._set(db, "activitySchedule", schedule)
            self._set(db, "lastError", "")
            return run_id, ""

    def run_kind(self, run_id: str) -> str:
        with self.db() as db:
            return db.execute("SELECT kind FROM life_runs WHERE id=?", (run_id,)).fetchone()[0]

    def step(self, run_id: str, item: dict):
        with self.db() as db:
            row = db.execute("SELECT steps FROM life_runs WHERE id=?", (run_id,)).fetchone()
            steps = json.loads(row[0])
            steps.append(item)
            db.execute("UPDATE life_runs SET steps=? WHERE id=?", (dump(steps), run_id))

    def finish(self, run_id: str, now: datetime, status: str, summary: str, *, note: str | None = None,
               decision: str | None = None):
        with self.db() as db:
            db.execute("UPDATE life_runs SET status=?,summary=?,finished_at=? WHERE id=?",
                       (status, summary[:3000], now.isoformat(), run_id))
            if note is not None:
                self._set(db, "note", note[:6000])
            if decision is not None:
                self._set(db, "lastDecision", decision[:600])
            if status == "failed":
                self._set(db, "lastError", summary[:500])
            # Keep useful bounded history, independent of chat/memory retention.
            db.execute("DELETE FROM life_runs WHERE id NOT IN (SELECT id FROM life_runs ORDER BY started_at DESC LIMIT 200) AND status!='running'")

    def remember_source(self, source: str, text: str) -> bool:
        fingerprint = hashlib.sha256((source + text).encode("utf-8")).hexdigest()
        with self.db() as db:
            seen = self._get(db, "seen", [])
            duplicate = fingerprint in seen
            if not duplicate:
                self._set(db, "seen", [*seen[-499:], fingerprint])
            return duplicate

    @staticmethod
    def _draft(row) -> dict:
        return {"id": row["id"], "account": row["account"], "visibility": row["visibility"],
                "content": row["content"], "state": row["state"], "createdAt": row["created_at"],
                "receipt": json.loads(row["receipt"])}

    @staticmethod
    def fingerprint(account: str, visibility: str, content: str) -> str:
        return hashlib.sha256(dump([account, visibility, content.strip()]).encode("utf-8")).hexdigest()

    def create_draft(self, config: dict, content: str, now: datetime) -> dict:
        if not isinstance(content, str) or not content.strip() or len(content) > 2000:
            raise ValueError("动态正文需要 1–2000 字")
        account, visibility = config["publishAccount"], config["publishVisibility"]
        fingerprint = self.fingerprint(account, visibility, content)
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO life_drafts(id,fingerprint,account,visibility,content,state,created_at) VALUES (?,?,?,?,?,'draft',?)",
                       (uuid.uuid4().hex, fingerprint, account, visibility, content, now.isoformat()))
            return self._draft(db.execute("SELECT * FROM life_drafts WHERE fingerprint=?", (fingerprint,)).fetchone())

    def draft(self, draft_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM life_drafts WHERE id=?", (draft_id,)).fetchone()
            if row is None:
                raise ValueError("草稿不存在")
            return self._draft(row)

    def dismiss(self, draft_id: str):
        with self.db() as db:
            db.execute("UPDATE life_drafts SET state='dismissed' WHERE id=? AND state='draft'", (draft_id,))
        return self.draft(draft_id)

    def begin_publish(self, draft_id: str, now: datetime, config: dict, *, content=None, bind_unassigned=False) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM life_drafts WHERE id=?", (draft_id,)).fetchone()
            if not row or row["state"] != "draft":
                raise ValueError("只有待发布草稿可发送；未知结果不会重发")
            account = row["account"] or (config["publishAccount"] if bind_unassigned else "")
            if not account or account != config["publishAccount"]:
                raise ValueError("草稿账号与当前授权账号不同；请重新选择正确账号")
            text = row["content"] if content is None else content
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise ValueError("动态正文需要 1–2000 字")
            day = now.date().isoformat()
            count = db.execute("SELECT count(*) FROM life_drafts WHERE publish_day=? AND state IN ('publishing','sent','unknown','failed')", (day,)).fetchone()[0]
            if count >= config["dailyPublishLimit"]:
                raise ValueError("今天的发布预算已用完")
            fingerprint = self.fingerprint(account, row["visibility"], text)
            existing = db.execute("SELECT id FROM life_drafts WHERE fingerprint=? AND id!=?", (fingerprint, draft_id)).fetchone()
            if existing:
                raise ValueError("同一账号已有相同动态，不能重复发布")
            db.execute("UPDATE life_drafts SET account=?,content=?,fingerprint=?,state='publishing',publish_day=? WHERE id=?", (account, text, fingerprint, day, draft_id))
            return self._draft(db.execute("SELECT * FROM life_drafts WHERE id=?", (draft_id,)).fetchone())

    def finish_publish(self, draft_id: str, receipt: dict) -> dict:
        # 'ok' alone is not a delivery acknowledgement.
        state = "sent" if receipt.get("sent") is True else "draft" if receipt.get("attempted") is False else "unknown"
        provider_receipt = receipt.get("receipt")
        if state == "unknown" and isinstance(provider_receipt, dict) and provider_receipt.get("state") == "failed":
            state = "failed"
        with self.db() as db:
            db.execute("UPDATE life_drafts SET state=?,receipt=? WHERE id=?", (state, dump(receipt), draft_id))
        return self.draft(draft_id)

    def snapshot(self, now: datetime) -> dict:
        with self.db() as db:
            config = normalize_config(self._get(db, "config", {}))
            rhythm = self._rhythm(db, config, now)
            activities = [{"id": r["id"], "startedAt": r["started_at"], "finishedAt": r["finished_at"],
                           "status": r["status"], "kind": r["kind"], "summary": r["summary"], "steps": json.loads(r["steps"])}
                          for r in db.execute("SELECT * FROM life_runs ORDER BY started_at DESC LIMIT 30")]
            drafts = [self._draft(r) for r in db.execute("SELECT * FROM life_drafts ORDER BY created_at DESC LIMIT 40")]
            chats = [self._chat(r) for r in db.execute("SELECT * FROM life_chats ORDER BY created_at DESC LIMIT 30")]
            return {"ok": True, "config": config, "state": {
                "running": any(r["status"] == "running" for r in activities),
                "nextRunAt": min((row["nextAt"] for row in rhythm if row["nextAt"]),
                                 key=datetime.fromisoformat, default=""),
                "rhythm": rhythm, "note": self._get(db, "note", ""),
                "lastError": self._get(db, "lastError", ""),
                "lastDecision": self._get(db, "lastDecision", ""),
                "runsToday": db.execute("SELECT count(*) FROM life_runs WHERE day=?", (now.date().isoformat(),)).fetchone()[0],
                "nextChatAt": next(row["nextAt"] for row in rhythm if row["kind"] == "chat"),
                "chatReadyAt": self._next_chat_at(db, config),
            }, "activities": activities, "drafts": drafts, "chats": chats}

    @staticmethod
    def _chat(row) -> dict:
        return {"id": row["id"], "account": row["account"], "owner": row["owner"],
                "messages": json.loads(row["messages"]), "state": row["state"],
                "createdAt": row["created_at"], "receipt": json.loads(row["receipt"])}

    @staticmethod
    def _next_chat_at(db, config: dict) -> str:
        row = db.execute("SELECT created_at FROM life_chats WHERE account=? AND owner=? AND state!='not_attempted' ORDER BY julianday(created_at) DESC LIMIT 1",
                         (config["publishAccount"], config["qqChatOwner"])).fetchone()
        return ((datetime.fromisoformat(row[0]) + timedelta(minutes=interval_minutes(config, "chat"))).isoformat()
                if row else "")

    def chat_ready(self, config: dict, now: datetime) -> bool:
        with self.db() as db:
            due = self._next_chat_at(db, config)
            return not due or now >= datetime.fromisoformat(due)

    def begin_chat(self, run_id: str, config: dict, messages: list[str], now: datetime) -> dict:
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 8
                or any(not isinstance(part, str) or not part.strip() for part in messages)
                or sum(len(part) for part in messages) > 5000):
            raise ValueError("闲聊需要 1–8 条意思完整的消息，合计不超过 5000 字")
        messages = [part.strip() for part in messages]
        account, owner = config["publishAccount"], config["qqChatOwner"]
        fingerprint = self.fingerprint(account, owner, "".join(messages))
        with self.db() as db:
            existing = db.execute("SELECT * FROM life_chats WHERE id=? OR fingerprint=?", (run_id, fingerprint)).fetchone()
            if existing:
                raise ValueError("这次聊天已记录，不重复发送")
            due = self._next_chat_at(db, config)
            if due and now < datetime.fromisoformat(due):
                raise ValueError("刚主动聊过，过一阵再来找主人")
            db.execute("INSERT INTO life_chats(id,fingerprint,account,owner,messages,state,created_at) VALUES (?,?,?,?,?,'sending',?)",
                       (run_id, fingerprint, account, owner, dump(messages), now.isoformat()))
            return self._chat(db.execute("SELECT * FROM life_chats WHERE id=?", (run_id,)).fetchone())

    def finish_chat(self, run_id: str, receipt: dict) -> dict:
        state = ("sent" if receipt.get("sent") is True and receipt.get("messageId") else
                 "draft" if receipt.get("deliveryState") == "draft" or receipt.get("requiresApproval") else
                 "cancelled" if receipt.get("deliveryState") == "cancelled" else
                 "not_attempted" if receipt.get("attempted") is False else
                 "unknown" if receipt.get("deliveryState") in {"unknown", "sending", "sent"} or receipt.get("sent") else "failed")
        with self.db() as db:
            db.execute("UPDATE life_chats SET state=?,receipt=? WHERE id=?", (state, dump(receipt), run_id))
            row = self._chat(db.execute("SELECT * FROM life_chats WHERE id=?", (run_id,)).fetchone())
            db.execute("DELETE FROM life_chats WHERE id NOT IN (SELECT id FROM life_chats ORDER BY created_at DESC LIMIT 200) AND state!='sending'")
            return row
