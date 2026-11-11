---
name: codex-alarm
description: Use persistent alarms for long jobs and handle external wake events delivered to a Codex chat, including jobs on SSH hosts, without polling with model turns.
---

# Codex Alarm

Use `codex_alarm.arm` when you expect a condition to take long enough that repeated model-driven polling would waste time. Write a read-only checker that prints one JSON object: `pending` while the condition is unresolved, `done` when it is resolved and needs your attention, or `error` when the checker cannot determine its state. Include useful diagnostics in `detail` on errors. Observe a durable result when possible, locally or remotely. Give every alarm a backup deadline later than the expected completion time, with enough margin for normal delays.

`arm` runs the checker once immediately, with a 30-second limit for this initial check. `pending` registers an alarm and returns its ID. `done` returns `condition_met` without an alarm: act on the result now. `error` rejects the alarm without pausing a goal: inspect the diagnostic and correct or retry the checker. A failed observed job can still satisfy a condition such as "the job has ended"; report `done` in that case so you can handle the failure promptly. Reserve `error` for failures to inspect the condition.

If you can do independent work after arming, use `mode: continue` and keep working. If you need to wait, use `mode: sleep` and finish the turn after the tool returns `armed`. Sleep mode pauses an active goal through Codex; on waking, it resumes that same goal only if its recorded state is unchanged. If the goal was edited or manually paused, inspect it and follow the user's latest intent. Do not treat a user interruption as permission to continue.

Use `codex_alarm.list` before creating another alarm after compaction or when you may already have one. Multiple alarms are supported, but creating an additional one requires the revision returned by `list` and `allow_parallel: true`. `codex_alarm.cancel` cancels a specific alarm.

An alarm event contains its ID, reason, and your wake message. If the reason is `checker_error`, inspect the diagnostic and fix the checker or its environment before arming another alarm. A backup deadline does not mean the job is done. If it fires while the checker is still pending, inspect the actual job state and any errors. If the job is healthy and progressing, check `codex_alarm.list`, then arm a new alarm with a later backup deadline and continue or sleep as appropriate. If it has stalled or failed, investigate that instead of blindly extending the timer.

An external integration can also send an event to this chat without a prior `arm` call. It uses the same delivery path and may arrive during your work or wake an idle chat. Read its `origin`, `kind`, and `payload` to decide how it relates to the current goal.

The user can stop all alarms for the current chat from the TUI with `!codex-alarm stop-chat`. Do not restart those alarms unless the user asks.
