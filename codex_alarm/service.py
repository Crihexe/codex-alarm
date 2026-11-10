"""Local HTTP API, checker scheduler, and Codex event dispatcher."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import signal
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .appserver import AppServer, AppServerError
from .auth import verified_context
from .paths import api_socket, database_path
from .store import AlarmError, Store


def timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    raise AlarmError("BAD_REQUEST", "timestamp must be RFC 3339 or Unix seconds")


class AlarmService:
    def __init__(self, store: Store | None = None, socket: Path | None = None):
        self.store = store or Store(database_path())
        self.socket = socket or api_socket()
        self.locks: dict[str, asyncio.Lock] = {}
        self.running_checkers: set[str] = set()
        self.dispatching_events: set[str] = set()
        self.checker_tasks: set[asyncio.Task] = set()
        self.delivery_tasks: set[asyncio.Task] = set()
        self.turn_tasks: set[asyncio.Task] = set()
        self.scheduler_task: asyncio.Task | None = None
        self.server: asyncio.AbstractServer | None = None
        self.stopping = asyncio.Event()

    def lock(self, thread_id: str) -> asyncio.Lock:
        return self.locks.setdefault(thread_id, asyncio.Lock())

    async def start(self) -> None:
        self.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.socket.parent, 0o700)
        if self.socket.exists():
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.socket), 0.5)
                writer.close()
                await writer.wait_closed()
                raise RuntimeError("codex-alarm service already running")
            except (ConnectionRefusedError, FileNotFoundError, asyncio.TimeoutError):
                self.socket.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(self.handle_client, self.socket)
        os.chmod(self.socket, 0o600)
        for event in self.store.inflight_events():
            task = asyncio.create_task(self.watch_turn(event["thread_id"], event["event_id"], event["turn_id"]))
            self.turn_tasks.add(task)
            task.add_done_callback(self.turn_tasks.discard)
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())

    async def run(self) -> None:
        await self.start()
        await self.stopping.wait()
        self.server.close()
        await self.server.wait_closed()
        if self.scheduler_task:
            await self.scheduler_task
        for task in list(self.checker_tasks | self.delivery_tasks | self.turn_tasks):
            task.cancel()
        await asyncio.gather(*(self.checker_tasks | self.delivery_tasks | self.turn_tasks), return_exceptions=True)
        self.socket.unlink(missing_ok=True)
        self.store.close()

    def stop(self) -> None:
        self.stopping.set()

    async def scheduler_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                now = time.time()
                self.store.expire_events()
                for alarm in self.store.due_alarms(now):
                    if alarm["deadline_at"] <= now:
                        self.store.deadline_alarm(alarm["alarm_id"])
                    elif alarm["alarm_id"] not in self.running_checkers and len(self.running_checkers) < 8:
                        self.running_checkers.add(alarm["alarm_id"])
                        task = asyncio.create_task(self.run_checker(alarm))
                        self.checker_tasks.add(task)
                        task.add_done_callback(self.checker_tasks.discard)
                for event in self.store.due_events(now):
                    if event["event_id"] not in self.dispatching_events and len(self.dispatching_events) < 8:
                        self.dispatching_events.add(event["event_id"])
                        task = asyncio.create_task(self.dispatch_guarded(event))
                        self.delivery_tasks.add(task)
                        task.add_done_callback(self.delivery_tasks.discard)
            except Exception:
                # One malformed checker or App Server failure must not kill the scheduler.
                import traceback
                traceback.print_exc()
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def run_checker(self, alarm: dict) -> None:
        alarm_id = alarm["alarm_id"]
        command = (["python3", "-c", alarm["source"]] if alarm["language"] == "python"
                   else ["bash", "-c", alarm["source"]])
        state, detail = "error", "checker did not run"
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(*command, cwd=alarm["cwd"],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            async def read_limited(stream: asyncio.StreamReader, limit: int = 4096) -> bytes:
                chunks, total = [], 0
                while chunk := await stream.read(4096):
                    if total < limit:
                        chunks.append(chunk[:limit - total])
                        total += min(len(chunk), limit - total)
                return b"".join(chunks)
            stdout_task = asyncio.create_task(read_limited(process.stdout))
            stderr_task = asyncio.create_task(read_limited(process.stderr))
            try:
                await asyncio.wait_for(process.wait(), timeout=alarm["timeout_seconds"])
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
                if process.returncode:
                    detail = (stderr or stdout).decode(errors="replace")[:2000] or f"checker exited {process.returncode}"
                else:
                    try:
                        result = json.loads(stdout)
                        if not isinstance(result, dict) or result.get("state") not in ("pending", "done", "error"):
                            raise ValueError("invalid checker state")
                        state = result["state"]
                        detail = str(result.get("detail", ""))[:2000]
                    except (json.JSONDecodeError, ValueError):
                        detail = "checker must print one JSON object with state pending, done, or error"
            except asyncio.TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                stdout_task.cancel()
                stderr_task.cancel()
                detail = "checker timed out"
        except (OSError, asyncio.CancelledError) as exc:
            if process and process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            detail = f"checker launch failed: {type(exc).__name__}"
        finally:
            self.running_checkers.discard(alarm_id)
        self.store.check_result(alarm_id, state, detail)

    async def dispatch_guarded(self, event: dict) -> None:
        try:
            await self.dispatch(event)
        except Exception:
            import traceback
            traceback.print_exc()
            if event["policy"] == "defer_until_available":
                self.store.set_event(event["event_id"], "deferred", "DISPATCH_ERROR", retry_after=3)
            else:
                self.store.set_event(event["event_id"], "failed", "DISPATCH_ERROR")
        finally:
            self.dispatching_events.discard(event["event_id"])

    async def availability(self, thread_id: str) -> str:
        try:
            async with AppServer() as app:
                return await app.thread_status(thread_id)
        except (OSError, AppServerError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            return "notLoaded"

    async def pause_goal_for_alarm(self, thread_id: str, alarm_id: str) -> bool:
        try:
            async with AppServer() as app:
                goal = await app.goal(thread_id)
                if goal and goal.get("status") == "active":
                    paused = await app.set_goal_status(thread_id, "paused")
                    self.store.set_goal_pause(alarm_id, paused)
                    return True
                return False
        except AppServerError as exc:
            if "goals feature is disabled" in str(exc):
                return False
            self.store.cancel_alarm(thread_id, alarm_id)
            raise AlarmError("GOAL_PAUSE_FAILED", "could not pause active goal", 500, True) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            self.store.cancel_alarm(thread_id, alarm_id)
            raise AlarmError("GOAL_PAUSE_FAILED", "App Server unavailable while pausing goal", 500, True) from exc

    async def resume_alarm_goal(self, app: AppServer, alarm_id: str) -> None:
        alarm = self.store.alarm(alarm_id)
        if not alarm or alarm["goal_paused_at"] is None:
            return
        thread = self.store.thread(alarm["thread_id"])
        if not thread or thread["interrupted"] or thread["suspended"]:
            return
        try:
            goal = await app.goal(alarm["thread_id"])
            if (goal and goal.get("status") == "paused"
                    and goal.get("updatedAt") == alarm["goal_paused_at"]
                    and goal.get("objective") == alarm["goal_objective"]):
                await app.set_goal_status(alarm["thread_id"], "active")
        except (AppServerError, asyncio.TimeoutError):
            # The event is already in the model turn. A failed goal resume must not duplicate it.
            pass

    async def dispatch(self, event: dict) -> None:
        thread_id = event["thread_id"]
        async with self.lock(thread_id):
            event = self.store.event(event["event_id"])
            if not event or event["state"] not in ("queued", "deferred"):
                return
            thread = self.store.thread(thread_id)
            if not thread:
                self.store.set_event(event["event_id"], "failed", "RECIPIENT_NOT_FOUND")
                return
            if thread["suspended"]:
                if event["policy"] == "defer_until_available":
                    self.store.set_event(event["event_id"], "deferred", "CHAT_SUSPENDED", retry_after=2)
                else:
                    self.store.set_event(event["event_id"], "failed", "CHAT_SUSPENDED")
                return
            if thread["interrupted"]:
                try:
                    async with AppServer() as goal_app:
                        goal = await goal_app.goal(thread_id)
                    if goal and goal.get("status") == "active":
                        self.store.set_interrupted(thread_id, False)
                        thread = self.store.thread(thread_id)
                except (OSError, AppServerError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                    pass
            if thread["interrupted"]:
                if event["policy"] == "defer_until_available":
                    self.store.set_event(event["event_id"], "deferred", "USER_INTERRUPTED", retry_after=2)
                else:
                    self.store.set_event(event["event_id"], "failed", "USER_INTERRUPTED")
                return
            app = AppServer()
            try:
                await app.__aenter__()
                status = await app.thread_status(thread_id)
                if status == "notLoaded" and event["policy"] == "allow_headless":
                    if not thread["headless"]:
                        self.store.set_event(event["event_id"], "failed", "HEADLESS_DISABLED")
                        return
                    await app.resume(thread_id)
                    status = await app.thread_status(thread_id)
                if status == "notLoaded":
                    if event["policy"] == "defer_until_available":
                        self.store.set_event(event["event_id"], "deferred", "THREAD_NOT_LOADED", retry_after=2)
                    else:
                        self.store.set_event(event["event_id"], "failed", "THREAD_NOT_LOADED")
                    return
                # Give a closing TUI a chance to unsubscribe and unload.
                await asyncio.sleep(1)
                if self.store.thread(thread_id)["suspended"] or self.store.thread(thread_id)["interrupted"]:
                    self.store.set_event(event["event_id"], "deferred" if event["policy"] == "defer_until_available" else "failed",
                                         "CHAT_SUSPENDED", retry_after=2)
                    return
                if event["policy"] != "allow_headless" and await app.thread_status(thread_id) == "notLoaded":
                    self.store.set_event(event["event_id"], "deferred" if event["policy"] == "defer_until_available" else "failed",
                                         "THREAD_NOT_LOADED", retry_after=2)
                    return
                output = {"event_id": event["event_id"], "kind": event["kind"],
                          "origin": event["origin"], "payload": json.loads(event["payload"])}
                turn_id = await app.wake(thread_id, output)
                self.store.set_event(event["event_id"], "submitted", turn_id=turn_id, turn_state="in_progress")
                if event["alarm_id"]:
                    await self.resume_alarm_goal(app, event["alarm_id"])
                task = asyncio.create_task(self.watch_turn(thread_id, event["event_id"], turn_id))
                self.turn_tasks.add(task)
                task.add_done_callback(self.turn_tasks.discard)
            except (OSError, AppServerError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                if event["policy"] == "defer_until_available":
                    self.store.set_event(event["event_id"], "deferred", type(exc).__name__, retry_after=3)
                else:
                    self.store.set_event(event["event_id"], "failed", type(exc).__name__)
            finally:
                if app:
                    await app.__aexit__(None, None, None)

    async def watch_turn(self, thread_id: str, event_id: str, turn_id: str) -> None:
        attempt = 0
        while not self.stopping.is_set():
            try:
                async with AppServer() as app:
                    thread_state = await app.thread_status(thread_id)
                    status = (await app.turn_status(thread_id, turn_id)
                              if thread_state != "active" or attempt % 10 == 0 else None)
                if status in ("completed", "interrupted", "failed"):
                    self.store.set_turn_state(event_id, status)
                    return
            except AppServerError as exc:
                if "not found" in str(exc).lower():
                    self.store.set_turn_state(event_id, "failed")
                    return
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass
            attempt += 1
            await asyncio.sleep(3)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line or len(line) > 4096:
                return
            parts = line.decode("ascii", errors="replace").strip().split()
            if len(parts) != 3:
                return
            method, raw_path, _ = parts
            headers = {}
            while header := await asyncio.wait_for(reader.readline(), 5):
                if header == b"\r\n":
                    break
                if len(header) > 8192:
                    raise AlarmError("BAD_REQUEST", "oversized header")
                key, _, value = header.decode("ascii", errors="replace").partition(":")
                headers[key.lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            if length < 0 or length > 65536:
                raise AlarmError("BAD_REQUEST", "request too large")
            body = await asyncio.wait_for(reader.readexactly(length), 10) if length else b""
            data = json.loads(body) if body else {}
            if not isinstance(data, dict):
                raise AlarmError("BAD_REQUEST", "body must be a JSON object")
            path = unquote(urlsplit(raw_path).path)
            status, response = await self.route(method, path, data)
        except AlarmError as exc:
            status, response = exc.status, exc.as_dict()
        except (ValueError, json.JSONDecodeError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            status, response = 400, AlarmError("BAD_REQUEST", "invalid HTTP request").as_dict()
        except Exception:
            import traceback
            traceback.print_exc()
            status, response = 500, AlarmError("INTERNAL_ERROR", "internal service error", 500, True).as_dict()
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
        reason = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden",
                  404: "Not Found", 409: "Conflict", 500: "Internal Server Error"}.get(status, "Error")
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def route(self, method: str, path: str, data: dict) -> tuple[int, dict]:
        if method == "GET" and path == "/health":
            return 200, {"status": "ok", "version": "0.1.3", "pid": os.getpid(),
                         "managed_by_systemd": bool(os.environ.get("INVOCATION_ID"))}
        if method == "POST" and path == "/v1/service/stop":
            self.stop()
            return 200, {"stopping": True}
        if method == "POST" and path == "/v1/threads/register":
            thread_id = data.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise AlarmError("BAD_REQUEST", "thread_id required")
            thread = self.store.register_thread(thread_id, data.get("source", "startup"))
            return 200, {"thread_id": thread_id, "suspended": bool(thread["suspended"])}
        if method == "POST" and path == "/v1/threads/interrupt":
            thread_id = data.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise AlarmError("BAD_REQUEST", "thread_id required")
            self.store.set_interrupted(thread_id, True)
            return 200, {"interrupted": True}
        if method == "POST" and path == "/v1/threads/user-prompt":
            thread_id = data.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise AlarmError("BAD_REQUEST", "thread_id required")
            self.store.set_interrupted(thread_id, False)
            return 200, {"interrupted": False}
        if method == "POST" and path == "/v1/threads/stop":
            thread_id = data.get("thread_id")
            if not isinstance(thread_id, str):
                raise AlarmError("BAD_REQUEST", "thread_id required")
            async with self.lock(thread_id):
                return 200, self.store.stop_chat(thread_id)
        if method == "POST" and path == "/v1/threads/resume":
            thread_id = data.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                raise AlarmError("BAD_REQUEST", "thread_id required")
            return 200, self.store.resume_chat(thread_id)
        if method == "POST" and path == "/v1/threads/headless":
            thread_id = data.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id or not isinstance(data.get("enabled"), bool):
                raise AlarmError("BAD_REQUEST", "thread_id and boolean enabled required")
            return 200, self.store.set_headless(thread_id, data["enabled"])
        if method == "POST" and path == "/v1/threads/alias":
            thread_id, alias = data.get("thread_id"), data.get("alias")
            if not isinstance(thread_id, str) or not isinstance(alias, str):
                raise AlarmError("BAD_REQUEST", "thread_id and alias required")
            self.store.set_alias(thread_id, alias)
            return 200, {"alias": alias, "thread_id": thread_id}
        if method == "GET" and path.startswith("/v1/events/"):
            return 200, self.store.receipt(path[len("/v1/events/"):])
        if method == "POST" and path == "/v1/events":
            thread_id = self.store.resolve_recipient(data.get("recipient"))
            policy = data.get("delivery_policy", "require_available")
            wait = data.get("wait", {})
            if wait:
                if not isinstance(wait, dict) or wait.get("for") not in ("delivery_submitted", "turn_terminal"):
                    raise AlarmError("BAD_REQUEST", "invalid wait.for")
                timeout_ms = wait.get("timeout_ms", 0)
                if not isinstance(timeout_ms, int) or not 0 <= timeout_ms <= 30000:
                    raise AlarmError("BAD_REQUEST", "wait.timeout_ms must be 0..30000")
            if "expires_at" in data:
                data = {**data, "expires_at": timestamp(data["expires_at"])}
            thread = self.store.thread(thread_id)
            existing = self.store.event(data.get("event_id")) if isinstance(data.get("event_id"), str) else None
            if existing:
                receipt = self.store.publish(thread_id, data)
            else:
                if policy == "allow_headless" and not thread["headless"]:
                    raise AlarmError("HEADLESS_DISABLED", "headless mode is disabled for recipient", 403)
                if thread["suspended"] and policy != "defer_until_available":
                    raise AlarmError("AGENT_UNAVAILABLE", "CHAT_SUSPENDED", 409, True)
                if policy == "require_available" and thread["interrupted"]:
                    raise AlarmError("AGENT_UNAVAILABLE", "USER_INTERRUPTED", 409, True)
                availability = (await self.availability(thread_id)
                                if policy in ("require_available", "defer_until_available")
                                and not thread["suspended"] and not thread["interrupted"] else None)
                if policy == "require_available" and availability == "notLoaded":
                    raise AlarmError("AGENT_UNAVAILABLE", "THREAD_NOT_LOADED", 409, True)
                initial_reason = ("CHAT_SUSPENDED" if thread["suspended"] else
                                  "USER_INTERRUPTED" if thread["interrupted"] else
                                  "THREAD_NOT_LOADED" if availability == "notLoaded" else None)
                receipt = self.store.publish(thread_id, data,
                    initial_state="deferred" if policy == "defer_until_available" and initial_reason else "queued",
                    initial_reason=initial_reason if policy == "defer_until_available" else None)
            if wait:
                end = time.monotonic() + timeout_ms / 1000
                while time.monotonic() < end:
                    receipt = self.store.receipt(data["event_id"])
                    if receipt["delivery"]["state"] in ("failed", "expired") or (wait["for"] == "delivery_submitted" and receipt["delivery"]["state"] == "submitted") or (wait["for"] == "turn_terminal" and receipt["turn"]["state"] in ("completed", "interrupted", "failed")):
                        break
                    await asyncio.sleep(0.2)
            return 202, receipt
        if method == "POST" and path == "/v1/alarms":
            try:
                thread_id = verified_context(data, "arm")
            except ValueError as exc:
                raise AlarmError("MISSING_THREAD_CONTEXT", str(exc), 403) from exc
            if "deadline_at" in data:
                data["deadline_at"] = timestamp(data["deadline_at"])
            result = self.store.arm(thread_id, data)
            if data.get("mode") == "sleep" and not result["idempotent"]:
                result["goal_paused"] = await self.pause_goal_for_alarm(thread_id, result["alarm_id"])
            return 200, result
        if method == "POST" and path == "/v1/alarms/list":
            try:
                thread_id = verified_context(data, "list")
            except ValueError as exc:
                raise AlarmError("MISSING_THREAD_CONTEXT", str(exc), 403) from exc
            return 200, self.store.list_alarms(thread_id)
        if method == "POST" and path == "/v1/alarms/status":
            try:
                thread_id = verified_context(data, "status")
            except ValueError as exc:
                raise AlarmError("MISSING_THREAD_CONTEXT", str(exc), 403) from exc
            event_id = data.get("event_id")
            row = self.store.event(event_id) if isinstance(event_id, str) else None
            if not row or row["thread_id"] != thread_id:
                raise AlarmError("EVENT_NOT_FOUND", "event not found in this chat", 404)
            return 200, self.store.receipt(event_id)
        if method == "GET" and path.startswith("/v1/threads/") and path.endswith("/alarms"):
            thread_id = path[len("/v1/threads/"):-len("/alarms")]
            return 200, self.store.list_alarms(thread_id)
        if method == "POST" and path.startswith("/v1/alarms/") and path.endswith("/cancel"):
            alarm_id = path[len("/v1/alarms/"):-len("/cancel")]
            try:
                thread_id = verified_context(data, "cancel")
            except ValueError as exc:
                raise AlarmError("MISSING_THREAD_CONTEXT", str(exc), 403) from exc
            return 200, self.store.cancel_alarm(thread_id, alarm_id)
        raise AlarmError("NOT_FOUND", "route not found", 404)


async def serve() -> None:
    service = AlarmService()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, service.stop)
    await service.run()
