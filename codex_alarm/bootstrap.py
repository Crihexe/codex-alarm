"""Single-package first-use setup and local service startup."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .auth import ensure_key
from .client import request
from .paths import state_dir


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_cli() -> Path:
    target = plugin_root() / "bin" / "codex-alarm"
    binary_dir = Path.home() / ".local" / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    link = binary_dir / "codex-alarm"
    if link.is_symlink():
        if link.resolve() != target:
            link.unlink()
    elif link.exists():
        # Never overwrite a real user executable.
        return link
    if not link.exists():
        link.symlink_to(target)
    return link


def ensure_unload_zero() -> bool:
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = home / "config.toml"
    home.mkdir(parents=True, exist_ok=True)
    original = path.read_text() if path.exists() else ""
    lines = original.splitlines(keepends=True)
    in_root = True
    replaced = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*\[", line):
            in_root = False
        if in_root and re.match(r"^\s*thread_unload_delay_secs\s*=", line):
            lines[index] = "thread_unload_delay_secs = 0\n"
            replaced = True
            break
    if not replaced:
        lines.insert(0, "thread_unload_delay_secs = 0\n")
    updated = "".join(lines)
    if updated == original:
        return False
    temp = path.with_suffix(".toml.codex-alarm-tmp")
    temp.write_text(updated)
    os.chmod(temp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    temp.replace(path)
    return True


def health() -> bool:
    try:
        return request("GET", "/health", timeout=0.5).get("status") == "ok"
    except (OSError, TimeoutError, ValueError):
        return False


def ensure_service() -> None:
    if health():
        return
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = directory / "startup.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if health():
            return
        log_path = directory / "service.log"
        with log_path.open("ab") as log:
            subprocess.Popen([sys.executable, str(plugin_root() / "bin" / "codex-alarm"), "serve"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, cwd=plugin_root())
        for _ in range(50):
            if health():
                return
            time.sleep(0.1)
    raise RuntimeError(f"codex-alarm service failed to start; inspect {log_path}")


def setup() -> dict:
    ensure_key()
    binary = ensure_cli()
    changed = ensure_unload_zero()
    ensure_service()
    return {"service": "running", "command": str(binary), "unload_delay_set": changed}
