---
name: codex-alarm
description: Use persistent alarms to wait for long builds or other conditions, including jobs on SSH hosts, without polling with model turns.
---

# Codex Alarm

Use `codex_alarm.arm` when you expect a condition to take long enough that repeated model-driven polling would waste time. Choose a checker that prints one JSON object with `state` equal to `pending`, `done`, or `error`. The checker can call SSH and should inspect a durable result such as a log, status file, or exit code. Give every alarm a backup deadline.

The tool returns an alarm ID. If you can do independent work, use `mode: continue` and keep working. If you need to wait, use `mode: sleep` and finish the turn after arming. Sleep mode pauses an active goal through Codex; on waking, it resumes that same goal only if its recorded state is unchanged. If the goal was edited or manually paused, inspect it and follow the user's latest intent. Do not treat a user interruption as permission to continue.

Use `codex_alarm.list` before creating another alarm after compaction or when you may already have one. Multiple alarms are supported, but creating an additional one requires the revision returned by `list` and `allow_parallel: true`. `codex_alarm.cancel` cancels a specific alarm.

An alarm event contains its ID, reason, and your wake message. A backup deadline can wake you even if the checker has not reported completion; verify the actual job state before acting on that event.

The user can stop all alarms for the current chat from the TUI with `!codex-alarm stop-chat`. Do not restart those alarms unless the user asks.
