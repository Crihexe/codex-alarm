"""Durable per-thread state. Only the service process opens this database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import quote


class AlarmError(Exception):
    def __init__(self, code: str, reason: str = "", status: int = 400, retryable: bool = False):
        self.code, self.reason, self.status, self.retryable = code, reason, status, retryable
        super().__init__(reason or code)

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "reason": self.reason, "retryable": self.retryable}}


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


CHECKER_ERROR_LIMIT = 3


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL DEFAULT 0,
                suspended INTEGER NOT NULL DEFAULT 0,
                interrupted INTEGER NOT NULL DEFAULT 0,
                headless INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(thread_id)
            );
            CREATE TABLE IF NOT EXISTS alarms (
                alarm_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(thread_id),
                invocation_id TEXT UNIQUE,
                language TEXT NOT NULL,
                source TEXT NOT NULL,
                cwd TEXT NOT NULL,
                interval_seconds REAL NOT NULL,
                timeout_seconds REAL NOT NULL,
                deadline_at REAL NOT NULL,
                next_check_at REAL NOT NULL,
                wake_message TEXT NOT NULL,
                mode TEXT NOT NULL,
                state TEXT NOT NULL,
                goal_paused_at INTEGER,
                goal_objective TEXT,
                last_detail TEXT,
                last_error TEXT,
                consecutive_errors INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alarms_due ON alarms(state, next_check_at);
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(thread_id),
                alarm_id TEXT REFERENCES alarms(alarm_id),
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                policy TEXT NOT NULL,
                origin TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                expires_at REAL,
                state TEXT NOT NULL,
                reason TEXT,
                turn_id TEXT,
                turn_state TEXT NOT NULL DEFAULT 'none',
                next_attempt_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_due ON events(state, next_attempt_at);
        """)
        self.db.commit()
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(alarms)")}
        for name, kind in (("goal_paused_at", "INTEGER"), ("goal_objective", "TEXT"),
                           ("consecutive_errors", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE alarms ADD COLUMN {name} {kind}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def thread(self, thread_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM threads WHERE thread_id=?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def register_thread(self, thread_id: str, source: str = "startup") -> dict:
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO threads(thread_id, updated_at) VALUES (?,?)", (thread_id, now))
            if source == "resume":
                self.db.execute("UPDATE threads SET suspended=0, updated_at=? WHERE thread_id=?", (now, thread_id))
        return self.thread(thread_id)

    def set_interrupted(self, thread_id: str, interrupted: bool) -> None:
        self.register_thread(thread_id)
        with self.db:
            self.db.execute("UPDATE threads SET interrupted=?, updated_at=? WHERE thread_id=?",
                            (int(interrupted), time.time(), thread_id))

    def resume_chat(self, thread_id: str) -> dict:
        self.register_thread(thread_id)
        with self.db:
            self.db.execute("UPDATE threads SET suspended=0, interrupted=0, updated_at=? WHERE thread_id=?",
                            (time.time(), thread_id))
        return self.thread(thread_id)

    def set_headless(self, thread_id: str, enabled: bool) -> dict:
        self.register_thread(thread_id)
        with self.db:
            self.db.execute("UPDATE threads SET headless=?, updated_at=? WHERE thread_id=?",
                            (int(enabled), time.time(), thread_id))
        return self.thread(thread_id)

    def set_alias(self, thread_id: str, alias: str) -> None:
        if not alias or len(alias) > 100 or not all(c.isalnum() or c in "-_:." for c in alias):
            raise AlarmError("BAD_REQUEST", "invalid alias")
        if not self.thread(thread_id):
            raise AlarmError("RECIPIENT_NOT_FOUND", "thread is not registered", 404)
        row = self.db.execute("SELECT thread_id FROM aliases WHERE alias=?", (alias,)).fetchone()
        if row and row["thread_id"] != thread_id:
            raise AlarmError("ALIAS_CONFLICT", "alias belongs to another thread", 409)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO aliases(alias,thread_id) VALUES (?,?)", (alias, thread_id))

    def resolve_recipient(self, recipient: dict) -> str:
        if not isinstance(recipient, dict) or len(recipient) != 1:
            raise AlarmError("BAD_REQUEST", "recipient must contain thread_id or alias")
        if "thread_id" in recipient:
            thread_id = recipient["thread_id"]
        elif "alias" in recipient:
            row = self.db.execute("SELECT thread_id FROM aliases WHERE alias=?", (recipient["alias"],)).fetchone()
            thread_id = row["thread_id"] if row else None
        else:
            raise AlarmError("BAD_REQUEST", "recipient must contain thread_id or alias")
        if not isinstance(thread_id, str) or not self.thread(thread_id):
            raise AlarmError("RECIPIENT_NOT_FOUND", "recipient is not registered", 404)
        return thread_id

    def list_alarms(self, thread_id: str) -> dict:
        thread = self.register_thread(thread_id)
        rows = self.db.execute("""SELECT alarm_id, language, cwd, interval_seconds, deadline_at,
                                   wake_message, mode, state, next_check_at, last_detail, last_error,
                                   consecutive_errors
                                   FROM alarms WHERE thread_id=? AND state IN ('armed','ready')
                                   ORDER BY created_at""", (thread_id,)).fetchall()
        return {"revision": thread["revision"], "alarms": [dict(r) for r in rows]}

    def prepare_arm(self, thread_id: str, args: dict) -> dict:
        self.register_thread(thread_id)
        language = args.get("language")
        source = args.get("source")
        cwd = args.get("cwd")
        message = args.get("wake_message")
        mode = args.get("mode", "continue")
        invocation_id = args.get("_invocation_id")
        if language not in ("python", "bash") or not isinstance(source, str) or not source.strip() or len(source) > 30000:
            raise AlarmError("BAD_REQUEST", "language must be python or bash and source must be nonempty (max 30000 chars)")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise AlarmError("BAD_REQUEST", "cwd must be an existing absolute directory")
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise AlarmError("BAD_REQUEST", "wake_message must be nonempty (max 4000 chars)")
        if mode not in ("continue", "sleep"):
            raise AlarmError("BAD_REQUEST", "mode must be continue or sleep")
        try:
            interval = float(args["interval_seconds"])
            timeout = float(args.get("timeout_seconds", min(interval * 0.8, 30)))
            deadline = float(args["deadline_at"])
        except (KeyError, ValueError, TypeError, OverflowError):
            raise AlarmError("BAD_REQUEST", "interval_seconds, timeout_seconds and deadline_at must be numbers") from None
        now = time.time()
        if not 1 <= interval <= 3600 or not 0.1 < timeout < interval or not now + 1 <= deadline <= now + 30 * 86400:
            raise AlarmError("BAD_REQUEST", "interval 1..3600s, timeout < interval, deadline within 30 days")
        if invocation_id is not None and (not isinstance(invocation_id, str) or len(invocation_id) > 200):
            raise AlarmError("BAD_REQUEST", "invalid invocation id")
        if invocation_id:
            prior = self.db.execute("SELECT alarm_id FROM alarms WHERE invocation_id=?", (invocation_id,)).fetchone()
            if prior:
                row = self.db.execute("SELECT thread_id,state FROM alarms WHERE alarm_id=?", (prior["alarm_id"],)).fetchone()
                if row["thread_id"] != thread_id:
                    raise AlarmError("INVOCATION_CONFLICT", "invocation belongs to another thread", 409)
                return {"existing": {"alarm_id": prior["alarm_id"], "state": row["state"], "idempotent": True,
                                     **self.list_alarms(thread_id)}}
        existing = self.list_alarms(thread_id)
        if existing["alarms"] and not (args.get("allow_parallel") is True and
                                       args.get("expected_revision") == existing["revision"]):
            raise AlarmError("ACTIVE_ALARMS_EXIST", canonical(existing), 409)
        return {"existing": None, "language": language, "source": source, "cwd": cwd,
                "interval_seconds": interval, "timeout_seconds": timeout, "deadline_at": deadline,
                "wake_message": message, "mode": mode, "invocation_id": invocation_id}

    def arm(self, thread_id: str, args: dict) -> dict:
        prepared = self.prepare_arm(thread_id, args)
        if prepared["existing"]:
            return prepared["existing"]
        now = time.time()
        with self.db:
            alarm_id = str(uuid.uuid4())
            self.db.execute("""INSERT INTO alarms(alarm_id,thread_id,invocation_id,language,source,cwd,
                interval_seconds,timeout_seconds,deadline_at,next_check_at,wake_message,mode,state,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (alarm_id, thread_id, prepared["invocation_id"], prepared["language"],
                 prepared["source"], prepared["cwd"], prepared["interval_seconds"], prepared["timeout_seconds"],
                 prepared["deadline_at"], now + prepared["interval_seconds"], prepared["wake_message"],
                 prepared["mode"], "armed", now))
            self.db.execute("UPDATE threads SET revision=revision+1, updated_at=? WHERE thread_id=?", (now, thread_id))
        return {"alarm_id": alarm_id, "state": "armed", "idempotent": False,
                "revision": self.thread(thread_id)["revision"]}

    def due_alarms(self, now: float, limit: int = 128) -> list[dict]:
        rows = self.db.execute("SELECT * FROM alarms WHERE state='armed' AND (next_check_at<=? OR deadline_at<=?) ORDER BY next_check_at LIMIT ?",
                               (now, now, limit)).fetchall()
        return [dict(r) for r in rows]

    def set_goal_pause(self, alarm_id: str, goal: dict) -> None:
        with self.db:
            self.db.execute("UPDATE alarms SET goal_paused_at=?, goal_objective=? WHERE alarm_id=?",
                            (goal.get("updatedAt"), goal.get("objective"), alarm_id))

    def alarm(self, alarm_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM alarms WHERE alarm_id=?", (alarm_id,)).fetchone()
        return dict(row) if row else None

    def check_result(self, alarm_id: str, state: str, detail: str = "") -> None:
        row = self.db.execute("SELECT * FROM alarms WHERE alarm_id=?", (alarm_id,)).fetchone()
        if not row or row["state"] != "armed":
            return
        now = time.time()
        detail = detail[:2000]
        if state == "done":
            self._trigger_alarm(dict(row), "condition_met", detail)
        elif state == "error" and row["consecutive_errors"] + 1 >= CHECKER_ERROR_LIMIT:
            self._trigger_alarm(dict(row), "checker_error", detail)
        else:
            with self.db:
                self.db.execute("""UPDATE alarms SET next_check_at=?, last_detail=?, last_error=?,
                                   consecutive_errors=?
                                   WHERE alarm_id=? AND state='armed'""",
                                (now + row["interval_seconds"], detail if state == "pending" else row["last_detail"],
                                 detail if state == "error" else None,
                                 row["consecutive_errors"] + 1 if state == "error" else 0, alarm_id))

    def deadline_alarm(self, alarm_id: str) -> None:
        row = self.db.execute("SELECT * FROM alarms WHERE alarm_id=?", (alarm_id,)).fetchone()
        if row and row["state"] == "armed" and row["deadline_at"] <= time.time():
            self._trigger_alarm(dict(row), "deadline", row["last_error"] or row["last_detail"] or "")

    def _trigger_alarm(self, row: dict, reason: str, detail: str) -> None:
        now = time.time()
        event_id = "alarm:" + row["alarm_id"]
        payload = {"alarm_id": row["alarm_id"], "reason": reason, "message": row["wake_message"],
                   "detail": detail[:2000], "mode": row["mode"]}
        with self.db:
            self.db.execute("""UPDATE alarms SET state='ready', last_detail=?,
                               last_error=CASE WHEN ?='checker_error' THEN ? ELSE last_error END,
                               consecutive_errors=CASE WHEN ?='checker_error' THEN ? ELSE consecutive_errors END
                               WHERE alarm_id=? AND state='armed'""",
                            (detail[:2000], reason, detail[:2000], reason, CHECKER_ERROR_LIMIT, row["alarm_id"]))
            self.db.execute("""INSERT OR IGNORE INTO events(event_id,thread_id,alarm_id,kind,payload,policy,
                origin,fingerprint,expires_at,state,next_attempt_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, row["thread_id"], row["alarm_id"], "alarm.ready", canonical(payload),
                 "defer_until_available", "alarm", hashlib.sha256(canonical(payload).encode()).hexdigest(),
                 None, "queued", now, now, now))
            self.db.execute("UPDATE threads SET revision=revision+1, updated_at=? WHERE thread_id=?", (now, row["thread_id"]))

    def cancel_alarm(self, thread_id: str, alarm_id: str) -> dict:
        row = self.db.execute("SELECT * FROM alarms WHERE alarm_id=? AND thread_id=?", (alarm_id, thread_id)).fetchone()
        if not row:
            raise AlarmError("ALARM_NOT_FOUND", "alarm not found in this chat", 404)
        with self.db:
            self.db.execute("UPDATE alarms SET state='cancelled' WHERE alarm_id=? AND state IN ('armed','ready')", (alarm_id,))
            self.db.execute("UPDATE events SET state='failed', reason='CANCELLED', updated_at=? WHERE alarm_id=? AND state IN ('queued','deferred')",
                            (time.time(), alarm_id))
            self.db.execute("UPDATE threads SET revision=revision+1 WHERE thread_id=?", (thread_id,))
        return {"alarm_id": alarm_id, "state": self.db.execute("SELECT state FROM alarms WHERE alarm_id=?", (alarm_id,)).fetchone()[0]}

    def stop_chat(self, thread_id: str) -> dict:
        if not self.thread(thread_id):
            raise AlarmError("RECIPIENT_NOT_FOUND", "thread is not registered", 404)
        now = time.time()
        with self.db:
            self.db.execute("UPDATE threads SET suspended=1, revision=revision+1, updated_at=? WHERE thread_id=?", (now, thread_id))
            cancelled = self.db.execute("UPDATE alarms SET state='cancelled' WHERE thread_id=? AND state IN ('armed','ready')", (thread_id,)).rowcount
            self.db.execute("UPDATE events SET state='failed', reason='CANCELLED', updated_at=? WHERE thread_id=? AND origin='alarm' AND state IN ('queued','deferred')",
                            (now, thread_id))
            self.db.execute("UPDATE events SET state='deferred', reason='CHAT_SUSPENDED', updated_at=? WHERE thread_id=? AND origin='external' AND policy='defer_until_available' AND state='queued'",
                            (now, thread_id))
            self.db.execute("UPDATE events SET state='failed', reason='CHAT_SUSPENDED', updated_at=? WHERE thread_id=? AND origin='external' AND policy!='defer_until_available' AND state IN ('queued','deferred')",
                            (now, thread_id))
        in_flight = self.db.execute("SELECT COUNT(*) FROM events WHERE thread_id=? AND state='submitted' AND turn_state='in_progress'", (thread_id,)).fetchone()[0]
        return {"thread_id": thread_id, "alarms_cancelled": cancelled, "in_flight": in_flight,
                "safe_to_exit": in_flight == 0}

    def event(self, event_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def receipt(self, event_id: str) -> dict:
        row = self.event(event_id)
        if not row:
            raise AlarmError("EVENT_NOT_FOUND", "event not found", 404)
        return {"event_id": event_id,
                "delivery": {"state": row["state"], "reason": row["reason"], "turn_id": row["turn_id"]},
                "turn": {"state": row["turn_state"], "id": row["turn_id"]},
                "status_url": "/v1/events/" + quote(event_id, safe="")}

    def publish(self, thread_id: str, args: dict, initial_state: str = "queued",
                initial_reason: str | None = None) -> dict:
        event_id = args.get("event_id")
        kind = args.get("kind")
        payload = args.get("payload")
        policy = args.get("delivery_policy", "require_available")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            raise AlarmError("BAD_REQUEST", "event_id must be a string of 1..200 characters")
        if not isinstance(kind, str) or not kind or len(kind) > 100:
            raise AlarmError("BAD_REQUEST", "kind must be a string of 1..100 characters")
        if not isinstance(payload, dict) or len(canonical(payload)) > 16384:
            raise AlarmError("BAD_REQUEST", "payload must be a JSON object of at most 16 KiB")
        if policy not in ("require_available", "defer_until_available", "allow_headless"):
            raise AlarmError("BAD_REQUEST", "unknown delivery_policy")
        expires_at = args.get("expires_at")
        if expires_at is not None:
            if not isinstance(expires_at, (float, int)) or expires_at <= time.time():
                raise AlarmError("BAD_REQUEST", "expires_at must be a future Unix timestamp")
        fingerprint = hashlib.sha256(canonical({"thread_id": thread_id, "kind": kind,
                                                "payload": payload, "policy": policy,
                                                "expires_at": expires_at}).encode()).hexdigest()
        prior = self.event(event_id)
        if prior:
            if prior["fingerprint"] != fingerprint:
                raise AlarmError("EVENT_ID_CONFLICT", "event_id exists with different content", 409)
            return self.receipt(event_id)
        now = time.time()
        with self.db:
            self.db.execute("""INSERT INTO events(event_id,thread_id,kind,payload,policy,origin,fingerprint,
                expires_at,state,next_attempt_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, thread_id, kind, canonical(payload), policy, "external", fingerprint,
                 expires_at, initial_state, now, now, now))
            if initial_reason:
                self.db.execute("UPDATE events SET reason=? WHERE event_id=?", (initial_reason, event_id))
        return self.receipt(event_id)

    def due_events(self, now: float, limit: int = 32) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE state IN ('queued','deferred') AND next_attempt_at<=? ORDER BY created_at LIMIT ?",
                               (now, limit)).fetchall()
        return [dict(r) for r in rows]

    def inflight_events(self) -> list[dict]:
        rows = self.db.execute("SELECT event_id, thread_id, turn_id FROM events WHERE state='submitted' AND turn_state='in_progress' AND turn_id IS NOT NULL").fetchall()
        return [dict(r) for r in rows]

    def set_event(self, event_id: str, state: str, reason: str | None = None,
                  turn_id: str | None = None, turn_state: str | None = None, retry_after: float = 0) -> None:
        now = time.time()
        with self.db:
            self.db.execute("""UPDATE events SET state=?, reason=?, turn_id=COALESCE(?,turn_id),
                turn_state=COALESCE(?,turn_state), next_attempt_at=?, updated_at=? WHERE event_id=?""",
                (state, reason, turn_id, turn_state, now + retry_after, now, event_id))
            if state == "submitted":
                row = self.db.execute("SELECT alarm_id, thread_id FROM events WHERE event_id=?", (event_id,)).fetchone()
                if row and row["alarm_id"]:
                    self.db.execute("UPDATE alarms SET state='delivered' WHERE alarm_id=? AND state='ready'", (row["alarm_id"],))
                    self.db.execute("UPDATE threads SET revision=revision+1 WHERE thread_id=?", (row["thread_id"],))

    def set_turn_state(self, event_id: str, state: str) -> None:
        with self.db:
            self.db.execute("UPDATE events SET turn_state=?, updated_at=? WHERE event_id=?", (state, time.time(), event_id))

    def expire_events(self) -> None:
        with self.db:
            self.db.execute("UPDATE events SET state='expired', reason='EXPIRED', updated_at=? WHERE state IN ('queued','deferred') AND expires_at IS NOT NULL AND expires_at<=?",
                            (time.time(), time.time()))
