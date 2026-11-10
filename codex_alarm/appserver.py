"""Small JSON-RPC client for Codex's local App Server Unix WebSocket."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import struct
from pathlib import Path


class AppServerError(Exception):
    pass


def socket_path() -> Path:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return home / "app-server-control" / "app-server-control.sock"


class AppServer:
    def __init__(self, path: Path | None = None):
        self.path = path or socket_path()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.next_id = 1
        self.notifications: list[dict] = []

    async def __aenter__(self) -> "AppServer":
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_unix_connection(self.path), 3)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n\r\n")
        self.writer.write(request.encode())
        await self.writer.drain()
        header = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), 3)
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if not header.startswith(b"HTTP/1.1 101") or f"Sec-WebSocket-Accept: {expected}".lower().encode() not in header.lower():
            raise AppServerError("WebSocket handshake rejected")
        await self.request("initialize", {"clientInfo": {"name": "codex-alarm", "title": "Codex Alarm", "version": "0.1.1"}})
        await self.send({"method": "initialized", "params": {}})
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.writer:
            try:
                await self._frame(b"", opcode=8)
            except (OSError, ConnectionError):
                pass
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    async def _frame(self, payload: bytes, opcode: int = 1) -> None:
        if not self.writer:
            raise AppServerError("not connected")
        length = len(payload)
        head = bytearray([0x80 | opcode])
        if length < 126:
            head.append(0x80 | length)
        elif length < 65536:
            head.append(0x80 | 126)
            head.extend(struct.pack("!H", length))
        else:
            head.append(0x80 | 127)
            head.extend(struct.pack("!Q", length))
        mask = secrets.token_bytes(4)
        head.extend(mask)
        head.extend(bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
        self.writer.write(head)
        await self.writer.drain()

    async def send(self, message: dict) -> None:
        await self._frame(json.dumps(message, separators=(",", ":")).encode())

    async def receive(self) -> dict:
        if not self.reader:
            raise AppServerError("not connected")
        fragments = bytearray()
        fragmented = False
        while True:
            first = await self.reader.readexactly(2)
            final = bool(first[0] & 0x80)
            opcode = first[0] & 15
            length = first[1] & 127
            masked = bool(first[1] & 128)
            if length == 126:
                length = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if length > 64 * 1024 * 1024 or len(fragments) + length > 64 * 1024 * 1024:
                raise AppServerError("oversized WebSocket frame")
            mask = await self.reader.readexactly(4) if masked else b""
            payload = await self.reader.readexactly(length)
            if masked:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 8:
                raise AppServerError("App Server closed connection")
            if opcode == 9:
                await self._frame(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                if fragmented:
                    raise AppServerError("new WebSocket message before continuation")
                fragmented = True
            elif opcode != 0 or not fragmented:
                raise AppServerError(f"unsupported WebSocket opcode {opcode}")
            fragments.extend(payload)
            if not final:
                continue
            try:
                return json.loads(fragments)
            except json.JSONDecodeError as exc:
                raise AppServerError("invalid JSON from App Server") from exc

    async def request(self, method: str, params: dict | None = None, timeout: float = 10) -> dict:
        req_id = self.next_id
        self.next_id += 1
        await self.send({"id": req_id, "method": method, "params": params or {}})
        async with asyncio.timeout(timeout):
            while True:
                message = await self.receive()
                if message.get("id") == req_id:
                    if "error" in message:
                        raise AppServerError(f"{method}: {message['error']}")
                    return message.get("result", {})
                self.notifications.append(message)

    async def thread_status(self, thread_id: str) -> str:
        try:
            result = await self.request("thread/read", {"threadId": thread_id})
        except AppServerError as exc:
            if "thread not loaded" in str(exc).lower():
                return "notLoaded"
            raise
        return result.get("thread", {}).get("status", {}).get("type", "notLoaded")

    async def wake(self, thread_id: str, output: dict) -> str:
        result = await self.request("turn/start", {
            "threadId": thread_id, "input": [],
            "toolOutput": {"name": "event", "namespace": "codex_alarm", "output": json.dumps(output, ensure_ascii=False)}
        }, timeout=20)
        turn_id = result.get("turn", {}).get("id")
        if not isinstance(turn_id, str):
            raise AppServerError("turn/start returned no turn id")
        return turn_id

    async def resume(self, thread_id: str) -> None:
        await self.request("thread/resume", {"threadId": thread_id}, timeout=20)

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def goal(self, thread_id: str) -> dict | None:
        result = await self.request("thread/goal/get", {"threadId": thread_id})
        return result.get("goal")

    async def set_goal_status(self, thread_id: str, status: str) -> dict:
        result = await self.request("thread/goal/set", {"threadId": thread_id,
                                                        "status": status, "origin": "automatic"})
        return result.get("goal", {})

    async def turn_status(self, thread_id: str, turn_id: str) -> str | None:
        cursor = None
        for _ in range(10):
            params = {"threadId": thread_id, "limit": 50, "itemsView": "notLoaded"}
            if cursor:
                params["cursor"] = cursor
            try:
                result = await self.request("thread/turns/list", params)
            except AppServerError as exc:
                if "not supported" not in str(exc):
                    raise
                # The installed Codex version still uses legacy thread history.
                result = await self.request("thread/read", {"threadId": thread_id, "includeTurns": True})
                for turn in result.get("thread", {}).get("turns", []):
                    if turn.get("id") == turn_id:
                        return turn.get("status")
                return None
            for turn in result.get("data", []):
                if turn.get("id") == turn_id:
                    return turn.get("status")
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return None

    async def wait_turn(self, turn_id: str, timeout: float = 3600) -> str:
        for message in self.notifications:
            if message.get("method") == "turn/completed" and message.get("params", {}).get("turn", {}).get("id") == turn_id:
                return message["params"]["turn"].get("status", "completed")
        async with asyncio.timeout(timeout):
            while True:
                message = await self.receive()
                if message.get("method") == "turn/completed" and message.get("params", {}).get("turn", {}).get("id") == turn_id:
                    return message["params"]["turn"].get("status", "completed")
