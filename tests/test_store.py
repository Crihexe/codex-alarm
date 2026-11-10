"""Persistence and chat isolation regressions."""

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


if __name__ == "__main__":
    unittest.main()
