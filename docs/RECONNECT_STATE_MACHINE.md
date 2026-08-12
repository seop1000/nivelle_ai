# Nivelle Link Reconnect State Machine

The client owns one `ConnectionManager`, one send task, one health-monitor task, and at most one automatic-reconnect task. Management windows never create network connections.

## States

| State | Meaning | Automatic action |
|---|---|---|
| `DISCONNECTED` | No active Gateway and no attempt in progress | May start saved-profile connection |
| `CONNECTING` | Probing enabled profiles by priority | Continue the current attempt only |
| `AUTHENTICATING` | Gateway health passed; token/pairing and status are being checked | Do not enable mutations yet |
| `CONNECTED` | Health, authentication, and server status succeeded | Poll health every 10 seconds |
| `RECONNECT_WAIT` | Unexpected loss; waiting for the next backoff attempt | Retry at 1, 2, 4, 8, 16, then at most 30 seconds plus bounded jitter |
| `FAILED` | An attempt failed in a way that needs attention | Keep cached information read-only; automatic retry may continue when enabled |
| `MANUAL_OFFLINE` | The user intentionally disconnected | Never reconnect until the user requests connection |

Two consecutive health failures are required before the active profile is released. A single timeout remains visible as a consecutive failure but does not immediately discard a healthy session. The last-success time is not overwritten by a failed probe. A `/health` success does not reset backoff: authentication, typed status, and the one authoritative WebSocket must all succeed before `mark_connected()` resets it to one second.

## Disconnect behavior

- An unexpected WebSocket close or missing terminal event marks the request interrupted/unknown, closes the current chat iterator, and schedules reconnect.
- Nivelle Link never automatically resends the uncertain message.
- The unique `client_message_id` rejects duplicate submission. A controlled retry uses a new ID, references one interrupted ID, and can be created only once.
- Existing chat bubbles, open management windows, last-loaded server data, and unsent draft text remain intact.
- Persona, memory, and server-setting mutation widgets are disabled both visually and at their application handlers while offline.

Application shutdown cancels connection, monitor, reconnect, send, history, Persona,
memory, and administrator tasks, awaits them, then awaits closure of the authoritative
chat socket before the qasync loop closes.

## Manual live test

Run `scripts/test_reconnect.ps1 -ServerHost <private-server-ip>`. The script only observes `/health`; it never terminates a process, VPN, or PC. Stop and restart only Nivelle Core on the server PC, then verify Nivelle Link shows `reconnecting` followed by `online`, retains the draft/windows, and creates no duplicate message.
