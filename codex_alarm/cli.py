"""Human and integration command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from .bootstrap import ensure_service, setup, uninstall
from .client import APIError, request
from .service import serve


def current_thread(explicit: str | None) -> str:
    thread_id = explicit or os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        raise ValueError("no chat context; run inside Codex with !codex-alarm ... or pass --thread")
    return thread_id


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="codex-alarm", description="Persistent alarms and events for Codex")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help=argparse.SUPPRESS)
    commands.add_parser("setup", help="Set up the plugin and start its service")
    commands.add_parser("uninstall", help="Stop and remove the user service, retaining alarm data")
    commands.add_parser("health", help="Check the local service")
    stop = commands.add_parser("stop-chat", help="Cancel alarms and suspend the current chat")
    stop.add_argument("--thread", help="Thread ID; defaults to the current Codex chat")
    resume = commands.add_parser("resume-chat", help="Lift manual suspension of the current chat")
    resume.add_argument("--thread")
    listed = commands.add_parser("list", help="List active alarms in the current chat")
    listed.add_argument("--thread")
    status = commands.add_parser("status", help="Read the status of one event")
    status.add_argument("event_id")
    headless = commands.add_parser("headless", help="Enable or disable autonomous wakeups for a chat")
    headless.add_argument("state", choices=["on", "off"])
    headless.add_argument("--thread")
    alias = commands.add_parser("alias", help="Bind an external recipient alias to a chat")
    alias.add_argument("name")
    alias.add_argument("--thread")
    publish = commands.add_parser("publish", help="Publish an external event from a JSON file or stdin")
    publish.add_argument("file", nargs="?", help="JSON file; omit or use - for stdin")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            asyncio.run(serve())
            return 0
        if args.command == "setup":
            result = setup()
        elif args.command == "uninstall":
            result = uninstall()
        else:
            ensure_service()
            if args.command == "health":
                result = request("GET", "/health")
            elif args.command == "stop-chat":
                result = request("POST", "/v1/threads/stop", {"thread_id": current_thread(args.thread)})
            elif args.command == "resume-chat":
                result = request("POST", "/v1/threads/resume", {"thread_id": current_thread(args.thread)})
            elif args.command == "list":
                result = request("GET", "/v1/threads/" + quote(current_thread(args.thread), safe="") + "/alarms")
            elif args.command == "status":
                result = request("GET", "/v1/events/" + quote(args.event_id, safe=""))
            elif args.command == "headless":
                result = request("POST", "/v1/threads/headless", {"thread_id": current_thread(args.thread),
                                                                    "enabled": args.state == "on"})
            elif args.command == "alias":
                result = request("POST", "/v1/threads/alias", {"thread_id": current_thread(args.thread),
                                                                 "alias": args.name})
            elif args.command == "publish":
                raw = sys.stdin.read() if args.file in (None, "-") else Path(args.file).read_text()
                data = json.loads(raw)
                result = request("POST", "/v1/events", data)
            else:
                raise ValueError("unknown command")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "stop-chat" and not result["safe_to_exit"]:
            return 2
        return 0
    except (APIError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"codex-alarm: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
