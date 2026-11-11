# External Event API

The `v1` API is part of the codex-alarm plugin. It serves HTTP over a local Unix socket that only the current user can access. A remote integration can call it through SSH or an authenticated gateway without changing the event format.

## Availability and delivery

Interactive delivery is the default. The dispatcher checks whether the Codex thread is loaded and does not call `thread/resume` for an unloaded thread unless headless delivery has been explicitly enabled. A loaded thread is not proof that its TUI window is still open: an active thread can remain loaded after the TUI exits. This leaves a small race between the availability check and event submission. The plugin sets `thread_unload_delay_secs = 0` to shorten that window for idle threads.

Before closing a chat, the user can run `!codex-alarm stop-chat` in the TUI. This cancels that chat's alarms and suspends delivery until the chat is reopened. It does not affect other chats. The response includes `alarms_cancelled`, `in_flight`, and `safe_to_exit`. Wait for `safe_to_exit: true` before exiting; `false` means a turn started by an alarm or external event is still in progress. Events already submitted cannot be withdrawn. While the chat is suspended, `require_available` and `allow_headless` requests receive `409 AGENT_UNAVAILABLE` with reason `CHAT_SUSPENDED`; `defer_until_available` events remain queued for later delivery.

## Publish an event

```http
POST /v1/events
Content-Type: application/json
```

```json
{
  "event_id": "bounty:scan-42:finished",
  "recipient": {"thread_id": "01a12386-3da3-77d2-a458-f5f6e1e2c248"},
  "kind": "scan.finished",
  "payload": {"message": "The scan finished", "report_id": "scan-42"},
  "delivery_policy": "defer_until_available",
  "expires_at": "2027-11-11T12:00:00Z",
  "wait": {"for": "delivery_submitted", "timeout_ms": 30000}
}
```

Choose a stable `event_id` for each event. `recipient` must contain exactly one registered `thread_id` or `alias`; it resolves to one chat. `kind` identifies the event type, and `payload` is a JSON object delivered as `codex_alarm.event` tool output. `expires_at` is optional and prevents delivery after the event becomes stale. It accepts an RFC 3339 timestamp or Unix seconds.

`delivery_policy` controls what happens when the recipient is unavailable:

| Policy | If the thread is not loaded | If it becomes unavailable before submission |
| --- | --- | --- |
| `require_available` (default for external calls) | Return `409 AGENT_UNAVAILABLE`; do not register the event. | Mark an accepted event `failed` with reason `THREAD_NOT_LOADED`; do not retry automatically. |
| `defer_until_available` | Return `202` with `delivery.state = deferred`. Keep the event until the chat is available or the event expires. | Keep it `deferred` and retry later. |
| `allow_headless` | Call `thread/resume` and submit the event without a TUI. | Continue in headless mode. This requires per-recipient opt-in; otherwise return `403 HEADLESS_DISABLED`. |

Alarms created by the agent use `defer_until_available` internally. External integrations can choose the same policy. Accepted events from both sources enter the same persistent queue and use the same `toolOutput` dispatcher.

`wait` is optional. Set `for` to `delivery_submitted` or `turn_terminal`, with `timeout_ms` from 0 to 30000. With zero or no `wait`, the API returns immediately. When a wait times out, the API returns the latest receipt; it does not cancel the event. You can query the receipt again later.

## Receipt and status

An accepted event returns `202 Accepted` with a receipt, including when `wait` observed its submission:

```json
{
  "event_id": "bounty:scan-42:finished",
  "delivery": {"state": "deferred", "reason": "THREAD_NOT_LOADED", "turn_id": null},
  "turn": {"state": "none", "id": null},
  "status_url": "/v1/events/bounty%3Ascan-42%3Afinished"
}
```

`GET /v1/events/{event_id}` returns the current receipt. `delivery.state` is `queued`, `deferred`, `submitted`, `failed`, or `expired`. `submitted` means Codex App Server accepted the tool output. `turn.state` is `none`, `in_progress`, `completed`, `interrupted`, or `failed`, with a turn ID when available. These states report delivery and Codex turn progress. If an event is added to a turn already in progress, `in_progress` does not prove that the model has read that particular event. `completed` means the turn ended; it does not report whether the agent completed any work related to the event.

## Errors and retries

Errors use this shape:

```json
{"error":{"code":"AGENT_UNAVAILABLE","reason":"THREAD_NOT_LOADED","retryable":true}}
```

| HTTP | Code | Meaning |
| --- | --- | --- |
| `400` | `BAD_REQUEST` | Invalid fields or unknown delivery policy. |
| `403` | `HEADLESS_DISABLED` | The recipient has not enabled `allow_headless`. |
| `404` | `RECIPIENT_NOT_FOUND` | Unknown thread or alias. |
| `409` | `AGENT_UNAVAILABLE` | A `require_available` recipient is unloaded (`THREAD_NOT_LOADED`), interrupted (`USER_INTERRUPTED`), or manually suspended (`CHAT_SUSPENDED`). A suspended recipient also rejects `allow_headless`. |
| `409` | `EVENT_ID_CONFLICT` | The same `event_id` was used with different content. |

Sending the same `event_id` with identical content returns its existing receipt while its `expires_at` value is still in the future. An error before admission does not reserve the ID, so the sender can retry when the chat becomes available. If a `require_available` event was accepted and later failed during the closing race, inspect its receipt and use a new `event_id` for another delivery attempt. A synchronous response cannot guarantee that the TUI stays open afterward.

If the API process is not running, the client gets a socket connection error rather than a JSON `AGENT_UNAVAILABLE` response. `codex-alarm health` starts the service if needed and checks that it is ready.

## Agent alarm checks

The MCP `arm` tool runs its checker once before admitting an alarm. Its result determines the response:

| Initial checker state | `arm` result | Goal |
| --- | --- | --- |
| `pending` | `200` with `state: "armed"`, an `alarm_id`, and `initial_check` containing the checker detail. | Paused only when `mode: "sleep"` and an active goal exists. |
| `done` | `200` with `state: "condition_met"`, `detail`, and `alarm_id: null`. No alarm is registered. | Unchanged; the agent can act immediately. |
| `error`, invalid output, nonzero exit, or timeout | `400 CHECKER_ERROR` with the diagnostic in `reason`. No alarm is registered. | Unchanged. |

The checker runs with the requested `cwd` and is expected to be safe to run more than once. The initial check has a 30-second cap so the MCP call can return within the local client's 35-second timeout; later checks use the requested `timeout_seconds`. Once armed, three consecutive `error` results produce an `alarm.ready` event with `reason: "checker_error"` and the latest diagnostic in `detail`. A `pending` result resets the error count. The backup deadline still fires first if it arrives sooner. `done` produces `reason: "condition_met"` immediately. A terminal failure of the observed task should be reported as `done` when the alarm's condition is that the task has ended; `error` means the checker could not determine the condition.

The MCP `arm` endpoint returns `400 CHECKER_ERROR` when its initial check fails and `409 CHAT_SUSPENDED` if the user stops the chat while that check is running. Neither response registers an alarm.

## Socket and management endpoints

The API listens at `$XDG_RUNTIME_DIR/codex-alarm/api.sock`. If `XDG_RUNTIME_DIR` is missing, it uses `/run/user/<uid>/codex-alarm/api.sock` when that directory exists and belongs to the user. Otherwise it uses `~/.local/state/codex-alarm/run/api.sock`. The socket has mode `0600`; no TCP port is opened. When `XDG_RUNTIME_DIR` is set, you can call the API directly:

```sh
curl --unix-socket "$XDG_RUNTIME_DIR/codex-alarm/api.sock" \
  -H 'Content-Type: application/json' -d @event.json \
  http://localhost/v1/events
```

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Read service version, PID, and supervisor status. |
| `POST /v1/threads/alias` | Register `{"thread_id":"...","alias":"..."}`. An alias belongs to one thread. |
| `POST /v1/threads/headless` | Set `{"thread_id":"...","enabled":true}` or `false` for headless delivery. |
| `POST /v1/threads/stop` | Suspend a chat, cancel its undelivered alarms, and return `safe_to_exit`. |
| `POST /v1/threads/resume` | Lift manual suspension without recreating canceled alarms. |
| `GET /v1/threads/{thread_id}/alarms` | List active alarms and their revision. |
| `GET /v1/events/{event_id}` | Read an event receipt. |

The plugin hooks use the internal `register`, `interrupt`, and `user-prompt` endpoints. Alarm MCP endpoints require HMAC context supplied by the `PreToolUse` hook. External clients should publish through `POST /v1/events`.
