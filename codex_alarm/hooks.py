"""Codex lifecycle hook entry points."""

from __future__ import annotations

import json
import sys

from .auth import signature
from .bootstrap import ensure_service, setup
from .client import request


def session_start(message: dict) -> dict:
    thread_id = message.get("session_id")
    source = message.get("source", "startup")
    if not isinstance(thread_id, str) or not thread_id:
        return {}
    setup()
    request("POST", "/v1/threads/register", {"thread_id": thread_id, "source": source})
    listed = request("GET", "/v1/threads/" + thread_id + "/alarms")
    alarms = listed.get("alarms", [])
    if not alarms:
        return {}
    short = [{"alarm_id": a["alarm_id"], "state": a["state"], "wake_message": a["wake_message"],
              "deadline_at": a["deadline_at"]} for a in alarms[:10]]
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": "Active codex-alarm entries for this chat: " + json.dumps(short, ensure_ascii=False) +
                                                        ". Use codex_alarm.list before arming another alarm."}}


def pre_tool_use(message: dict) -> dict:
    name = message.get("tool_name", "")
    if not isinstance(name, str) or not name.startswith("mcp__codex_alarm__"):
        return {}
    if message.get("agent_id"):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "codex-alarm tools are scoped to the main chat"}}
    thread_id = message.get("session_id")
    invocation_id = message.get("tool_use_id")
    tool = name.rsplit("__", 1)[-1]
    if not isinstance(thread_id, str) or not isinstance(invocation_id, str) or not thread_id or not invocation_id:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "missing Codex thread or tool call id"}}
    args = message.get("tool_input")
    if not isinstance(args, dict):
        args = {}
    args = {**args, "_thread_id": thread_id, "_invocation_id": invocation_id,
            "_context_sig": signature(thread_id, invocation_id, tool)}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "allow", "updatedInput": args}}


def interrupt(message: dict) -> dict:
    thread_id = message.get("session_id")
    if isinstance(thread_id, str) and thread_id:
        ensure_service()
        request("POST", "/v1/threads/interrupt", {"thread_id": thread_id}, timeout=1)
    return {}


def user_prompt(message: dict) -> dict:
    thread_id = message.get("session_id")
    if isinstance(thread_id, str) and thread_id:
        ensure_service()
        request("POST", "/v1/threads/user-prompt", {"thread_id": thread_id}, timeout=1)
    return {}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        return 2
    try:
        message = json.load(sys.stdin)
        if not isinstance(message, dict):
            return 2
        if argv[0] == "session-start":
            result = session_start(message)
        elif argv[0] == "pre-tool-use":
            result = pre_tool_use(message)
        elif argv[0] == "interrupt":
            result = interrupt(message)
        elif argv[0] == "user-prompt":
            result = user_prompt(message)
        else:
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # A failed hook must never disguise a successful alarm operation.
        print(json.dumps({"systemMessage": f"codex-alarm hook error: {type(exc).__name__}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
