# Technical findings and design decisions

This design was checked against Codex CLI and App Server 0.167.0 and the `openai/codex` source at commit `eb485f57ba02ca8cdedfcc07aca0f9d75c8faefd`. The App Server protocol may change in later releases, so the protocol client is isolated in `codex_alarm/appserver.py`.

## Verified Codex mechanisms

| Mechanism | Observed behavior | Use in codex-alarm |
| --- | --- | --- |
| [App Server `turn/start` with `toolOutput`](https://learn.chatgpt.com/docs/app-server#start-a-turn) | The daemon accepts a tool event and returns a turn ID. It can also add tool output to an active turn. | Deliver alarms and external events without impersonating the user or injecting a dynamic developer message. |
| [App Server `thread/read`](https://learn.chatgpt.com/docs/app-server#read-a-thread) | Reports `loaded`/`notLoaded` and turn status. An unloaded thread without a rollout can instead return `thread not loaded`. | Approximate availability and inspect turn status without using model turns. |
| [App Server `thread/resume`](https://learn.chatgpt.com/docs/app-server#resume-a-thread) | Can load a thread without a TUI. | Used only when the recipient has explicitly enabled headless delivery. |
| [Hook `PreToolUse`](https://learn.chatgpt.com/docs/hooks#pretooluse) | Receives `session_id` and `tool_use_id` and can rewrite MCP tool arguments. | Attach signed chat context so the model cannot choose another chat's thread ID. |
| [Hooks `Interrupt` and `UserPromptSubmit`](https://learn.chatgpt.com/docs/hooks#interrupt) | Signal a canceled turn and a new user prompt. | Hold ready events after `Ctrl+C` or `Esc`, then permit delivery when the user resumes work. |
| [Goal API](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex) | `thread/goal/get` exposes status, objective, and `updatedAt`. `thread/goal/set` changes status without recreating the goal. | Pause an active goal for `mode: sleep` and resume it if its recorded state is unchanged. |
| [TUI shell command](https://learn.chatgpt.com/docs/developer-commands) | A `!` command receives the current chat's `CODEX_THREAD_ID`, as verified in the TUI source. | Make `!codex-alarm stop-chat` operate on one chat without a model turn. |

Asynchronous hooks are not a persistent scheduler: their output is delivered at a safe point in an existing turn and may be canceled when a session ends. `SessionEnd` also does not immediately indicate that a TUI has closed. The scheduler, backup timers, and event queue therefore run in a local service separate from the MCP process.

## Implemented architecture

The Codex plugin bundles a skill, four MCP tools, four hooks, and a local HTTP service. In the tested Codex version, a root-level Agent Plugin `plugin.json` loads MCP and skills but excludes plugin hooks. The project uses the supported legacy `.codex-plugin/plugin.json` manifest to load both `mcp.json` and `hooks/hooks.json`. A fresh App Server process discovered both components. Codex requires users to review and trust the hooks in the TUI.

SQLite stores alarms, revisions, events, and suspension state by `thread_id`. Each checker runs as a separate Python or Bash process with a timeout shorter than its interval. It must print one JSON object with `state: pending`, `done`, or `error`; captured output and details are bounded. A mandatory backup deadline creates an event even when the checker keeps failing. Internal alarms and external events share the same persistent queue and `toolOutput` dispatcher. Receipts distinguish delivery from Codex turn status. The plugin does not ask the agent to report completion of event-related work.

On Linux, a `systemd --user` unit supervises the service. The startup hook or `codex-alarm setup` creates the local command and HMAC key and sets `thread_unload_delay_secs = 0`. MCP restarts do not lose alarms. Without a user systemd manager, the service starts as a detached process but has no automatic crash restart. External integrations call the Unix-socket API without importing the Python package.

Codex can start MCP subprocesses without `XDG_RUNTIME_DIR`. The client therefore checks the current user's `/run/user/<uid>` directory before falling back to the state directory; systemd calls receive the discovered runtime path as well. This prevents an absent desktop environment variable from closing the MCP transport when it calls the service.

## Compaction and multiple alarms

The database, not the model's context, is authoritative. `SessionStart` can summarize active alarms after compaction, and `list` always reads the database. The MCP tool-call ID makes a retry of the same invocation idempotent. If a chat already has an active alarm, another `arm` returns `ACTIVE_ALARMS_EXIST`. The agent must inspect `list` and explicitly retry with `allow_parallel: true` and the current `expected_revision`.

Checker text is not used to infer whether two alarms observe the same job: similar scripts may monitor different jobs, while different scripts may monitor the same one.

## Goals and interruptions

`mode: continue` leaves the goal unchanged. `mode: sleep` pauses an active goal and records its `updatedAt` value and objective. On wakeup, the service resumes that goal only when those values still match and the chat is neither interrupted nor suspended. Later changes to the goal take precedence. The goal read API does not expose who changed it, and a manual change within the timestamp's resolution remains a limitation. An explicit `/goal resume` can clear a previous interruption because the dispatcher observes that the goal is active again.

The `Interrupt` hook holds ready events after a canceled turn. Checkers and deadlines keep running, but a held event does not start another turn until a new user prompt arrives or the goal is resumed. The interruption flag is scoped to one chat. Goal behavior is covered by tests with a controlled App Server boundary; the visual TUI flow after pausing and resuming a goal still needs a manual check.

## Closing the TUI

The plugin does not use a proxy and does not load an unloaded thread for interactive delivery. [`thread_unload_delay_secs = 0`](https://learn.chatgpt.com/docs/config-file/config-reference) unloads an idle thread promptly after its TUI disconnects. Before delivery, the dispatcher checks that the thread is loaded, waits one second, and checks again. This reduces the closing race but cannot eliminate it: `thread/read` reports thread state, not the number of connected TUI clients. After `turn/start`, the active thread can remain loaded even if its TUI closes, so another `loaded` check cannot reliably stop that turn.

`!codex-alarm stop-chat` is the explicit way to leave a chat without future alarm wakeups. It and event delivery are serialized per chat. The command cancels internal alarms and their undelivered events; external events using `defer_until_available` remain deferred. `safe_to_exit` is false if an event has already started a turn that is still in progress. The command cannot undo an already submitted event or stop the job being observed. Reopening the chat clears the suspension without recreating canceled alarms.

With immediate unloading, a terminal or `unified_exec` process still owned by the Codex session may end when the TUI closes. A build that must survive should run as an independent local or remote job. An alarm checker can inspect a durable result from a remote host over SSH.

## Delivery guarantees and verification

Receipts distinguish event admission, submission to App Server, and Codex turn status. They do not certify that the agent completed any task. `turn/start` and the SQLite update are not one atomic transaction: if the daemon accepts a turn but its response is lost, a retry may deliver the event again. The payload includes a stable `event_id` to make a duplicate recognizable. Exactly-once delivery is not guaranteed in that failure case.

Verification included: direct MCP handshake and tool calls; discovery of the plugin's MCP server and hooks by a fresh Codex App Server; user-systemd bootstrap; delivery of `toolOutput` to a real thread; deferral while a thread was unloaded and delivery after `thread/resume`; and tests for chat isolation, idempotency, backup deadlines, availability policies, hook context, goal changes, and MCP runtime discovery. Both SSH and HTTPS Git marketplace installation commands were tested. Hook approval and TUI rendering are manual installation checks.
