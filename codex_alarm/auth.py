"""Bind model-facing MCP calls to the actual Codex hook context."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from .paths import state_dir


def key_path():
    return state_dir() / "context.key"


def ensure_key() -> bytes:
    path = key_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
    return path.read_bytes()


def signature(thread_id: str, tool_use_id: str, tool_name: str) -> str:
    message = json.dumps([thread_id, tool_use_id, tool_name], separators=(",", ":")).encode()
    return hmac.new(ensure_key(), message, hashlib.sha256).hexdigest()


def verified_context(data: dict, tool_name: str) -> str:
    thread_id = data.get("_thread_id")
    invocation_id = data.get("_invocation_id")
    supplied = data.get("_context_sig")
    if not all(isinstance(value, str) and value for value in (thread_id, invocation_id, supplied)):
        raise ValueError("missing trusted Codex hook context")
    if not hmac.compare_digest(supplied, signature(thread_id, invocation_id, tool_name)):
        raise ValueError("invalid Codex hook context")
    return thread_id
