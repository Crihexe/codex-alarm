#!/usr/bin/env python3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_alarm.mcp_server import main

raise SystemExit(main())
