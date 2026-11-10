from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_ALARM_STATE_DIR", str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codex-alarm")))


def runtime_dir() -> Path:
    override = os.environ.get("CODEX_ALARM_RUNTIME_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    return Path(xdg) / "codex-alarm" if xdg else state_dir() / "run"


def api_socket() -> Path:
    return runtime_dir() / "api.sock"


def database_path() -> Path:
    return state_dir() / "alarms.sqlite3"
