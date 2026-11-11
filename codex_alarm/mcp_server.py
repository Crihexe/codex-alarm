"""Minimal dependency-free MCP stdio adapter for the local service."""

from __future__ import annotations

import json
import sys
from urllib.parse import quote

from .bootstrap import ensure_service
from .client import APIError, request


CONTEXT = {
    "_thread_id": {"type": "string", "description": "Injected by the Codex PreToolUse hook; do not set."},
    "_invocation_id": {"type": "string", "description": "Injected by the Codex PreToolUse hook; do not set."},
    "_context_sig": {"type": "string", "description": "Injected by the Codex PreToolUse hook; do not set."},
}


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": {**properties, **CONTEXT},
            "required": required or [], "additionalProperties": False}


TOOLS = [
    {"name": "arm", "description": "Check once immediately, then register a durable alarm with a mandatory backup deadline if pending. Done returns condition_met without an alarm; error rejects with diagnostics. Checker stdout must be one JSON object with state pending, done, or error.",
     "inputSchema": schema({
         "language": {"type": "string", "enum": ["python", "bash"]},
         "source": {"type": "string", "description": "Python or Bash checker source; print JSON state."},
         "cwd": {"type": "string", "description": "Existing absolute working directory."},
         "interval_seconds": {"type": "number", "minimum": 1, "maximum": 3600},
         "timeout_seconds": {"type": "number", "description": "Maximum runtime of later checks; must be below interval. The initial check is capped at 30 seconds."},
         "deadline_at": {"type": "string", "description": "Mandatory RFC 3339 backup deadline with timezone."},
         "wake_message": {"type": "string", "description": "What you need to know when woken."},
         "mode": {"type": "string", "enum": ["continue", "sleep"], "description": "Continue independent work, or pause an active goal and finish the turn to await the event."},
         "allow_parallel": {"type": "boolean", "description": "Required with expected_revision when active alarms already exist."},
         "expected_revision": {"type": "integer", "description": "Revision returned by list, to confirm an additional alarm."},
     }, ["language", "source", "cwd", "interval_seconds", "deadline_at", "wake_message"])},
    {"name": "list", "description": "List active alarms in this Codex chat and obtain the revision for an additional alarm.",
     "inputSchema": schema({})},
    {"name": "cancel", "description": "Cancel one alarm in this Codex chat.",
     "inputSchema": schema({"alarm_id": {"type": "string"}}, ["alarm_id"])},
    {"name": "status", "description": "Read delivery and turn status of one alarm or external event in this Codex chat.",
     "inputSchema": schema({"event_id": {"type": "string"}}, ["event_id"])},
]


def call_tool(name: str, args: dict) -> dict:
    if not isinstance(args, dict):
        return {"content": [{"type": "text", "text": "tool arguments must be an object"}], "isError": True}
    ensure_service()
    if name == "arm":
        path, data = "/v1/alarms", args
    elif name == "list":
        path, data = "/v1/alarms/list", args
    elif name == "cancel":
        alarm_id = args.get("alarm_id")
        if not isinstance(alarm_id, str):
            raise ValueError("alarm_id required")
        path, data = "/v1/alarms/" + quote(alarm_id, safe="") + "/cancel", args
    elif name == "status":
        path, data = "/v1/alarms/status", args
    else:
        raise ValueError("unknown tool")
    try:
        result = request("POST", path, data)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
    except APIError as exc:
        return {"content": [{"type": "text", "text": json.dumps(exc.response, ensure_ascii=False)}],
                "isError": True}


def handle(message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "codex-alarm", "version": "0.1.4"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params", {})
            result = call_tool(params.get("name"), params.get("arguments", {}))
        else:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except (ValueError, OSError, RuntimeError) as exc:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32603, "message": str(exc)}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32603, "message": f"codex-alarm operation failed: {type(exc).__name__}"}}


def main() -> int:
    for line in sys.stdin.buffer:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            response = handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except (ValueError, UnicodeDecodeError):
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
