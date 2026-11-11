"""Delivery policies against a controllable App Server boundary."""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_alarm.hooks import pre_tool_use
from codex_alarm.service import AlarmService
from codex_alarm.store import AlarmError, Store


class FakeAppServer:
    loaded = False
    goal_data = None
    wakes = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def thread_status(self, _):
        return "idle" if self.loaded else "notLoaded"

    async def resume(self, _):
        type(self).loaded = True

    async def wake(self, thread_id, output):
        self.wakes.append((thread_id, output))
        return "turn-1"

    async def turn_status(self, *_):
        return "completed"

    async def goal(self, _):
        return self.goal_data.copy() if self.goal_data else None

    async def set_goal_status(self, _, status):
        self.goal_data["status"] = status
        self.goal_data["updatedAt"] += 1
        return self.goal_data.copy()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.db")
        self.store.register_thread("chat-a")
        self.service = AlarmService(self.store, Path(self.temp.name) / "service.sock")
        FakeAppServer.loaded = False
        FakeAppServer.goal_data = None
        FakeAppServer.wakes = []
        self.patch = patch("codex_alarm.service.AppServer", FakeAppServer)
        self.patch.start()

    async def asyncTearDown(self):
        self.service.stop()
        for task in self.service.turn_tasks:
            task.cancel()
        await asyncio.gather(*self.service.turn_tasks, return_exceptions=True)
        self.patch.stop()
        self.store.close()
        self.temp.cleanup()

    def event(self, policy):
        return {"event_id": "external:1", "recipient": {"thread_id": "chat-a"},
                "kind": "build.finished", "payload": {"message": "ready"},
                "delivery_policy": policy}

    async def arm(self, source, mode="sleep", invocation="call-preflight"):
        with patch.dict(os.environ, {"CODEX_ALARM_STATE_DIR": self.temp.name}):
            call = {"session_id": "chat-a", "tool_use_id": invocation,
                    "tool_name": "mcp__codex_alarm__arm", "tool_input": {
                        "language": "python", "source": source, "cwd": self.temp.name,
                        "interval_seconds": 10, "deadline_at": time.time() + 60,
                        "wake_message": "Inspect result", "mode": mode}}
            signed = pre_tool_use(call)["hookSpecificOutput"]["updatedInput"]
            return await self.service.route("POST", "/v1/alarms", signed)

    async def test_unavailable_reject_or_defer_and_deliver_after_reopen(self):
        with self.assertRaises(AlarmError) as context:
            await self.service.route("POST", "/v1/events", self.event("require_available"))
        self.assertEqual(context.exception.code, "AGENT_UNAVAILABLE")
        self.assertIsNone(self.store.event("external:1"))
        _, receipt = await self.service.route("POST", "/v1/events", self.event("defer_until_available"))
        self.assertEqual(receipt["delivery"]["state"], "deferred")
        self.assertEqual(receipt["delivery"]["reason"], "THREAD_NOT_LOADED")
        FakeAppServer.loaded = True
        await self.service.dispatch(self.store.event("external:1"))
        self.assertEqual(self.store.receipt("external:1")["delivery"]["state"], "submitted")
        self.assertEqual(FakeAppServer.wakes[0][0], "chat-a")
        await asyncio.sleep(0)

    async def test_headless_requires_opt_in(self):
        with self.assertRaises(AlarmError) as context:
            await self.service.route("POST", "/v1/events", self.event("allow_headless"))
        self.assertEqual(context.exception.code, "HEADLESS_DISABLED")
        self.store.set_headless("chat-a", True)
        await self.service.route("POST", "/v1/events", self.event("allow_headless"))
        await self.service.dispatch(self.store.event("external:1"))
        self.assertTrue(FakeAppServer.loaded)
        self.assertEqual(self.store.receipt("external:1")["delivery"]["state"], "submitted")

    async def test_sleep_pauses_and_resumes_unchanged_goal(self):
        FakeAppServer.loaded = True
        FakeAppServer.goal_data = {"status": "active", "objective": "Compile Chromium", "updatedAt": 1}
        alarm = self.store.arm("chat-a", {"language": "python", "source": "print('{}')",
            "cwd": self.temp.name, "interval_seconds": 10, "deadline_at": time.time() + 60,
            "wake_message": "Resume research", "mode": "sleep"})
        self.assertTrue(await self.service.pause_goal_for_alarm("chat-a", alarm["alarm_id"]))
        self.assertEqual(FakeAppServer.goal_data["status"], "paused")
        self.store.check_result(alarm["alarm_id"], "done")
        await self.service.dispatch(self.store.event("alarm:" + alarm["alarm_id"]))
        self.assertEqual(FakeAppServer.goal_data["status"], "active")

    async def test_sleep_respects_later_goal_change(self):
        FakeAppServer.loaded = True
        FakeAppServer.goal_data = {"status": "active", "objective": "Compile Chromium", "updatedAt": 1}
        alarm = self.store.arm("chat-a", {"language": "python", "source": "print('{}')",
            "cwd": self.temp.name, "interval_seconds": 10, "deadline_at": time.time() + 60,
            "wake_message": "Resume research", "mode": "sleep"})
        await self.service.pause_goal_for_alarm("chat-a", alarm["alarm_id"])
        FakeAppServer.goal_data["updatedAt"] += 1
        self.store.check_result(alarm["alarm_id"], "done")
        await self.service.dispatch(self.store.event("alarm:" + alarm["alarm_id"]))
        self.assertEqual(FakeAppServer.goal_data["status"], "paused")

    async def test_hook_context_binds_alarm_to_actual_chat(self):
        with patch.dict(os.environ, {"CODEX_ALARM_STATE_DIR": self.temp.name}):
            call = {"session_id": "chat-a", "tool_use_id": "call-7",
                    "tool_name": "mcp__codex_alarm__arm", "tool_input": {
                        "language": "python", "source": "print('{\"state\":\"pending\"}')", "cwd": self.temp.name,
                        "interval_seconds": 10, "deadline_at": time.time() + 60,
                        "wake_message": "Check result"}}
            signed = pre_tool_use(call)["hookSpecificOutput"]["updatedInput"]
            _, result = await self.service.route("POST", "/v1/alarms", signed)
            self.assertEqual(self.store.alarm(result["alarm_id"])["thread_id"], "chat-a")
            with self.assertRaises(AlarmError) as context:
                await self.service.route("POST", "/v1/alarms", signed | {"_thread_id": "chat-b"})
            self.assertEqual(context.exception.code, "MISSING_THREAD_CONTEXT")
            call["agent_id"] = "subagent-1"
            self.assertEqual(pre_tool_use(call)["hookSpecificOutput"]["permissionDecision"], "deny")

    async def test_failed_initial_check_does_not_arm_or_pause_goal(self):
        FakeAppServer.goal_data = {"status": "active", "objective": "Wait for result", "updatedAt": 1}
        with self.assertRaises(AlarmError) as context:
            await self.arm("import sys; sys.stderr.write('invalid checker syntax'); sys.exit(2)")
        self.assertEqual(context.exception.code, "CHECKER_ERROR")
        self.assertIn("invalid checker syntax", context.exception.reason)
        self.assertEqual(self.store.list_alarms("chat-a")["alarms"], [])
        self.assertEqual(FakeAppServer.goal_data["status"], "active")

    async def test_pending_initial_check_arms_once_and_pauses_goal(self):
        FakeAppServer.goal_data = {"status": "active", "objective": "Wait for result", "updatedAt": 1}
        source = ("from pathlib import Path; import json; p=Path('count'); "
                  "p.write_text(str(int(p.read_text())+1 if p.exists() else 1)); "
                  "print(json.dumps({'state':'pending','detail':'still running'}))")
        _, result = await self.arm(source)
        self.assertEqual(result["state"], "armed")
        self.assertEqual(result["initial_check"], {"state": "pending", "detail": "still running"})
        self.assertTrue(result["goal_paused"])
        self.assertEqual((Path(self.temp.name) / "count").read_text(), "1")
        self.assertGreater(self.store.alarm(result["alarm_id"])["next_check_at"], time.time())
        _, repeated = await self.arm(source)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual((Path(self.temp.name) / "count").read_text(), "1")

    async def test_initial_done_returns_without_alarm_or_goal_pause(self):
        FakeAppServer.goal_data = {"status": "active", "objective": "Wait for result", "updatedAt": 1}
        _, result = await self.arm("import json; print(json.dumps({'state':'done','detail':'result exists'}))")
        self.assertEqual(result["state"], "condition_met")
        self.assertEqual(result["detail"], "result exists")
        self.assertIsNone(result["alarm_id"])
        self.assertEqual(self.store.list_alarms("chat-a")["alarms"], [])
        self.assertEqual(FakeAppServer.goal_data["status"], "active")

    async def test_stop_during_initial_check_prevents_late_arm(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_check(_):
            started.set()
            await release.wait()
            return "pending", "still running"

        with patch.object(self.service, "execute_checker", side_effect=slow_check):
            arming = asyncio.create_task(self.arm("print('{\"state\":\"pending\"}')"))
            await started.wait()
            _, stopped = await self.service.route("POST", "/v1/threads/stop", {"thread_id": "chat-a"})
            self.assertTrue(stopped["safe_to_exit"])
            release.set()
            with self.assertRaises(AlarmError) as context:
                await arming
        self.assertEqual(context.exception.code, "CHAT_SUSPENDED")
        self.assertEqual(self.store.list_alarms("chat-a")["alarms"], [])


if __name__ == "__main__":
    unittest.main()
