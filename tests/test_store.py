"""Persistence and chat isolation regressions."""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from codex_alarm.store import AlarmError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.store.register_thread("chat-a")
        self.store.register_thread("chat-b")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def alarm_args(self, invocation="call-1"):
        return {
            "language": "python", "source": "print('{}')", "cwd": self.temp.name,
            "interval_seconds": 10, "deadline_at": time.time() + 60,
            "wake_message": "Check the build", "_invocation_id": invocation,
        }

    def test_compaction_and_parallel_registration_do_not_duplicate_alarms(self):
        first = self.store.arm("chat-a", self.alarm_args())
        self.assertEqual(self.store.arm("chat-a", self.alarm_args())["alarm_id"], first["alarm_id"])
        self.assertTrue(self.store.arm("chat-a", self.alarm_args())["idempotent"])
        self.assertEqual(self.store.list_alarms("chat-b")["alarms"], [])
        with self.assertRaises(AlarmError) as context:
            self.store.arm("chat-a", self.alarm_args("call-2"))
        self.assertEqual(context.exception.code, "ACTIVE_ALARMS_EXIST")
        revision = self.store.list_alarms("chat-a")["revision"]
        extra = self.alarm_args("call-2") | {"allow_parallel": True, "expected_revision": revision}
        self.store.arm("chat-a", extra)
        self.assertEqual(len(self.store.list_alarms("chat-a")["alarms"]), 2)

    def test_stop_affects_only_its_chat_and_blocks_pending_wake(self):
        own = self.store.arm("chat-a", self.alarm_args())
        other = self.store.arm("chat-b", self.alarm_args("call-b"))
        self.store.check_result(own["alarm_id"], "done", "build complete")
        event_id = "alarm:" + own["alarm_id"]
        self.assertEqual(self.store.receipt(event_id)["delivery"]["state"], "queued")
        result = self.store.stop_chat("chat-a")
        self.assertTrue(result["safe_to_exit"])
        self.assertEqual(self.store.receipt(event_id)["delivery"]["reason"], "CANCELLED")
        self.assertEqual(self.store.alarm(other["alarm_id"])["state"], "armed")
        self.assertFalse(self.store.thread("chat-b")["suspended"])

    def test_external_event_idempotency_and_safe_exit_account_for_inflight(self):
        event = {"event_id": "remote:42", "kind": "build.finished", "payload": {"ok": True},
                 "delivery_policy": "defer_until_available"}
        self.store.publish("chat-a", event)
        self.store.publish("chat-a", event)
        with self.assertRaises(AlarmError) as context:
            self.store.publish("chat-a", event | {"payload": {"ok": False}})
        self.assertEqual(context.exception.code, "EVENT_ID_CONFLICT")
        self.store.set_event("remote:42", "submitted", turn_id="turn-1", turn_state="in_progress")
        result = self.store.stop_chat("chat-a")
        self.assertFalse(result["safe_to_exit"])
        self.assertEqual(result["in_flight"], 1)

    def test_backup_deadline_wakes_after_checker_errors(self):
        alarm = self.store.arm("chat-a", self.alarm_args())
        self.store.check_result(alarm["alarm_id"], "error", "SSH unavailable")
        self.assertEqual(self.store.alarm(alarm["alarm_id"])["state"], "armed")
        self.store.db.execute("UPDATE alarms SET deadline_at=? WHERE alarm_id=?",
                              (time.time() - 1, alarm["alarm_id"]))
        self.store.db.commit()
        self.store.deadline_alarm(alarm["alarm_id"])
        self.assertEqual(self.store.receipt("alarm:" + alarm["alarm_id"])["delivery"]["state"], "queued")
        self.assertEqual(self.store.alarm(alarm["alarm_id"])["state"], "ready")

    def test_three_consecutive_checker_errors_wake_with_diagnostic(self):
        alarm = self.store.arm("chat-a", self.alarm_args())
        alarm_id = alarm["alarm_id"]
        self.store.check_result(alarm_id, "error", "temporary failure")
        self.store.check_result(alarm_id, "pending", "job is still running")
        self.assertEqual(self.store.alarm(alarm_id)["consecutive_errors"], 0)
        self.assertIsNone(self.store.alarm(alarm_id)["last_error"])
        self.store.check_result(alarm_id, "error", "first failure")
        self.store.check_result(alarm_id, "error", "second failure")
        self.assertEqual(self.store.alarm(alarm_id)["state"], "armed")
        self.store.check_result(alarm_id, "error", "third failure")
        self.assertEqual(self.store.alarm(alarm_id)["state"], "ready")
        self.assertEqual(self.store.alarm(alarm_id)["last_error"], "third failure")
        row = self.store.event("alarm:" + alarm_id)
        payload = json.loads(row["payload"])
        self.assertEqual(payload["reason"], "checker_error")
        self.assertEqual(payload["detail"], "third failure")

    def test_existing_database_gains_error_counter(self):
        path = Path(self.temp.name) / "legacy.db"
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE alarms (
            alarm_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, invocation_id TEXT UNIQUE,
            language TEXT NOT NULL, source TEXT NOT NULL, cwd TEXT NOT NULL,
            interval_seconds REAL NOT NULL, timeout_seconds REAL NOT NULL,
            deadline_at REAL NOT NULL, next_check_at REAL NOT NULL,
            wake_message TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL,
            goal_paused_at INTEGER, goal_objective TEXT, last_detail TEXT,
            last_error TEXT, created_at REAL NOT NULL
        )""")
        db.execute("""INSERT INTO alarms(alarm_id, thread_id, language, source, cwd,
            interval_seconds, timeout_seconds, deadline_at, next_check_at,
            wake_message, mode, state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("old-alarm", "chat-a", "python", "print('{}')", self.temp.name,
             10, 5, time.time() + 60, time.time() + 10, "Wait", "sleep", "armed", time.time()))
        db.commit()
        db.close()
        upgraded = Store(path)
        try:
            upgraded.register_thread("chat-a")
            self.assertEqual(upgraded.alarm("old-alarm")["consecutive_errors"], 0)
            upgraded.check_result("old-alarm", "error", "network error")
            self.assertEqual(upgraded.alarm("old-alarm")["consecutive_errors"], 1)
        finally:
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
