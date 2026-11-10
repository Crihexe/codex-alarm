from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_ALARM_STATE_DIR", str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codex-alarm")))


def user_runtime_base() -> Path | None:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured)
    # Codex may strip XDG_RUNTIME_DIR from MCP subprocesses. The user runtime
    # directory remains available even when the variable is absent.
    candidate = Path("/run/user") / str(os.getuid())
    try:
        if candidate.is_dir() and candidate.stat().st_uid == os.getuid():
            return candidate
    except OSError:
        pass
    return None


def runtime_dir() -> Path:
    override = os.environ.get("CODEX_ALARM_RUNTIME_DIR")
    if override:
        return Path(override)
    base = user_runtime_base()
    return base / "codex-alarm" if base else state_dir() / "run"


def api_socket() -> Path:
    return runtime_dir() / "api.sock"


def database_path() -> Path:
    return state_dir() / "alarms.sqlite3"
