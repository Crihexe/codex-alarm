"""Single-package first-use setup and local service startup."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .auth import ensure_key
from .client import request
from .paths import state_dir, user_runtime_base


def systemd_env() -> dict[str, str]:
    env = os.environ.copy()
    runtime = user_runtime_base()
    if runtime and not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = str(runtime)
    return env


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_cli() -> tuple[Path, bool]:
    target = plugin_root() / "bin" / "codex-alarm"
    binary_dir = Path.home() / ".local" / "bin"
    binary_dir.mkdir(parents=True, exist_ok=True)
    link = binary_dir / "codex-alarm"
    changed = False
    if link.is_symlink():
        if link.resolve() != target:
            link.unlink()
            changed = True
    elif link.exists():
        raise RuntimeError(f"{link} already exists and is not managed by codex-alarm")
    if not link.exists():
        link.symlink_to(target)
        changed = True
    return link, changed


def user_service_unit(binary: Path) -> str:
    def quoted(value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("systemd path contains newline")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    env = systemd_env()
    environment = "".join(
        f"Environment={quoted(key + '=' + env[key])}\n"
        for key in ("CODEX_HOME", "CODEX_ALARM_STATE_DIR", "CODEX_ALARM_RUNTIME_DIR",
                    "XDG_RUNTIME_DIR", "XDG_STATE_HOME")
        if key in env
    )
    return ("[Unit]\nDescription=Codex Alarm scheduler\nStartLimitIntervalSec=0\n\n"
            "[Service]\nType=simple\n"
            + environment + f"ExecStart={quoted(str(binary))} serve\n"
            "Restart=always\nRestartSec=2\n\n"
            "[Install]\nWantedBy=default.target\n")


def ensure_user_service(binary: Path, cli_changed: bool = False) -> bool:
    if os.environ.get("CODEX_ALARM_NO_SYSTEMD") == "1" or not shutil.which("systemctl"):
        return False
    probe = subprocess.run(["systemctl", "--user", "is-system-running"], env=systemd_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode not in (0, 1):  # degraded is usable; offline is not.
        return False
    directory = Path.home() / ".config" / "systemd" / "user"
    directory.mkdir(parents=True, exist_ok=True)
    unit = directory / "codex-alarm.service"
    content = user_service_unit(binary)
    changed = not unit.exists() or unit.read_text() != content
    if changed:
        unit.write_text(content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], env=systemd_env(), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    subprocess.run(["systemctl", "--user", "enable", "codex-alarm.service"], env=systemd_env(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    action = "restart" if changed or cli_changed else "start"
    subprocess.run(["systemctl", "--user", action, "codex-alarm.service"], env=systemd_env(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


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
        unit = Path.home() / ".config" / "systemd" / "user" / "codex-alarm.service"
        if unit.exists() and os.environ.get("CODEX_ALARM_NO_SYSTEMD") != "1" and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "start", "codex-alarm.service"], env=systemd_env(), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for _ in range(50):
                if health():
                    return
                time.sleep(0.1)
            raise RuntimeError("systemd started codex-alarm but its API did not become ready")
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
    binary, changed_cli = ensure_cli()
    changed = ensure_unload_zero()
    managed = ensure_user_service(binary, changed_cli)
    ensure_service()
    return {"service": "running", "managed_by_systemd": managed,
            "command": str(binary), "unload_delay_set": changed}


def uninstall() -> dict:
    binary = Path.home() / ".local" / "bin" / "codex-alarm"
    unit = Path.home() / ".config" / "systemd" / "user" / "codex-alarm.service"
    if unit.exists() and str(binary) in unit.read_text() and "Description=Codex Alarm scheduler" in unit.read_text():
        subprocess.run(["systemctl", "--user", "disable", "--now", "codex-alarm.service"], env=systemd_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        unit.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], env=systemd_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    elif health():
        try:
            request("POST", "/v1/service/stop", {})
        except (OSError, ValueError):
            pass
    if binary.is_symlink() and binary.resolve() == plugin_root() / "bin" / "codex-alarm":
        binary.unlink()
    return {"service": "removed", "state_retained": str(state_dir())}
