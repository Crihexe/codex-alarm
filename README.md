# codex-alarm

Codex can wait for a build, test run, or any other long job without spending model turns checking its progress. An alarm runs a small Python or Bash checker in the background and sends a tool event to the right chat when the condition is met. Every alarm also has a backup deadline. Other programs can send events to the same chat through a local API.

## Install

Requires Linux, Python 3.11+, and Codex CLI 0.167.0 or newer. Keep `~/.local/bin` on your `PATH`. A running `systemd --user` is recommended so the alarm service restarts automatically.

1. Add the marketplace and install the plugin:

   ```sh
   codex plugin marketplace add https://github.com/Crihexe/codex-alarm.git
   codex plugin add codex-alarm@crihexe-codex-alarm
   ```

   If you use GitHub over SSH, `git@github.com:Crihexe/codex-alarm.git` works as the marketplace source too.

2. Open a new Codex TUI, run `/hooks`, and approve the four `codex-alarm` hooks. Close and reopen the TUI once so its startup hook can finish setup.

3. Check the local service:

   ```sh
   codex-alarm health
   ```

The startup hook creates the `codex-alarm` command, starts its local service, and sets `thread_unload_delay_secs = 0` in `~/.codex/config.toml`. If the command is still missing after reopening Codex, check that `~/.local/bin` is on your `PATH`. You can also run `python3 <installed-plugin-root>/bin/codex-alarm setup`; `codex plugin add` prints the installed plugin root.

Codex's App Server daemon must be running for wakeups. If needed, start it with `codex app-server daemon start`.

## Use an alarm

Ask Codex to watch a long job with `codex-alarm`. The plugin gives it MCP tools to arm, list, cancel, and inspect alarms. A checker reports `pending` while the condition is unresolved, `done` when it needs attention, or `error` when the checker cannot determine its state. It can inspect local or remote state.

- `mode: continue` lets Codex do other work while the checker runs.
- `mode: sleep` lets Codex finish its turn and wait. If a goal is active, the plugin pauses it and resumes it on wakeup only if it has not changed.

The backup deadline should be later than the expected job duration, with room for normal delays. If it fires while the job is still making progress, Codex can check the job and arm another alarm. The checker and deadline run without model polling turns.

`arm` runs the checker once before registering an alarm or pausing a goal. This initial check must finish within 30 seconds. If it returns `error`, the tool reports the diagnostic and creates no alarm. If it returns `done`, the tool reports `condition_met` immediately and creates no alarm. An armed checker that later fails three times in a row wakes Codex with reason `checker_error`; an intervening `pending` result resets that count. A failed job can still meet a condition such as "the job has ended" and should return `done` so Codex can inspect the failure promptly.

For remote builds, start the build so it survives the SSH connection and writes a durable result, such as an exit-code file. The checker runs on the machine hosting Codex and can read that result over SSH.

Alarms belong to individual chats. They survive compaction and service restarts. Codex checks the existing list before adding another alarm; an additional alarm needs explicit confirmation using the current list revision.

## Close a chat with active alarms

Run this inside the Codex TUI before leaving the chat:

```text
!codex-alarm stop-chat
```

It cancels that chat's alarms and suspends further delivery until you reopen the chat. `safe_to_exit: true` means no alarm or external event has already started a turn that is still running. If it is `false`, the response includes the number of turns in flight. Reopening the chat removes the suspension; canceled alarms stay canceled.

Without this command, the plugin keeps an event pending when Codex has unloaded the chat. There is still a small race if the TUI closes exactly as an event is delivered: Codex does not expose a reliable TUI-presence signal. Setting `thread_unload_delay_secs = 0` can also end processes still managed by that Codex session when the TUI closes. Run builds that must survive as independent jobs.

## Send an event from another program

An external program can wake a chat without the agent having armed an alarm. Write an event to `event.json`:

```json
{
  "event_id": "scan-42:finished",
  "recipient": {"thread_id": "THREAD_ID"},
  "kind": "scan.finished",
  "payload": {"report": "path/to/report.json"},
  "delivery_policy": "defer_until_available"
}
```

Then publish it:

```sh
codex-alarm publish event.json
```

`defer_until_available` keeps the event until the chat is available. The default policy, `require_available`, rejects it if the chat is not loaded. `allow_headless` can resume an unloaded chat only after explicit opt-in with `codex-alarm headless on --thread THREAD_ID`. The API reports delivery and Codex turn status; it does not ask the agent to report whether its work is complete. See the [API contract](docs/API.md) for requests, responses, and error codes.

The API listens on an owner-only Unix socket under `$XDG_RUNTIME_DIR/codex-alarm/api.sock`. A remote system can publish through SSH to the machine hosting Codex; the socket does not need a network port.

## Update or uninstall

To update an installation from Git:

```sh
codex plugin marketplace upgrade crihexe-codex-alarm
codex plugin remove codex-alarm@crihexe-codex-alarm
codex plugin add codex-alarm@crihexe-codex-alarm
```

Reopen the chat after updating so it uses the new MCP connection. Review changed hooks with `/hooks` if Codex asks. To remove the service and command while retaining alarm data, run `codex-alarm uninstall` before removing the plugin.

## Development

From a local checkout, run `codex plugin marketplace add .` and then install the plugin as above. Run the tests with `python3 -m unittest discover -s tests -v`. The [technical analysis](RESEARCH.md) records the protocol choices and limitations.
