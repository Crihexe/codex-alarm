"""Codex MCP subprocesses may omit desktop session environment variables."""

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_alarm.bootstrap import systemd_env
from codex_alarm.mcp_server import handle
from codex_alarm.paths import runtime_dir, state_dir, user_runtime_base


class TransportEnvironmentTests(unittest.TestCase):
    def test_missing_xdg_runtime_uses_owner_runtime_directory(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            base = user_runtime_base()
            if base:
                self.assertEqual(base, Path("/run/user") / str(os.getuid()))
                self.assertEqual(runtime_dir(), base / "codex-alarm")
            else:
                self.assertEqual(runtime_dir(), state_dir() / "run")

    def test_systemd_gets_runtime_even_when_codex_omits_variable(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            with patch("codex_alarm.bootstrap.user_runtime_base", return_value=Path("/run/user/1000")):
                self.assertEqual(systemd_env()["XDG_RUNTIME_DIR"], "/run/user/1000")

    def test_service_start_failure_does_not_close_mcp_transport(self):
        failure = subprocess.CalledProcessError(1, ["systemctl", "--user", "start"])
        with patch("codex_alarm.mcp_server.ensure_service", side_effect=failure):
            response = handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": "list", "arguments": {}}})
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["error"]["code"], -32603)


if __name__ == "__main__":
    unittest.main()
