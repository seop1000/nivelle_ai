# Nivelle 0.4.0 architecture

Nivelle is a local-first assistant split across two Windows PCs. `Nivelle Core` runs the
FastAPI Gateway, SQLite persistence, Persona and configuration services, and either a mock
provider or an OpenAI-compatible `llama-server`. `Nivelle Link` runs the PySide6 desktop UI
and communicates only with Core. A locally managed `llama-server` stays bound to loopback.
Long-term memory belongs to `Nivelle Archive`.

## Boundaries

- `packages/nivelle_protocol`: versioned request, response, event, identity, and settings
  models shared by Core and Link.
- `apps/server/nivelle_core`: REST/WebSocket transport, pairing and authentication,
  repositories, configuration transactions, providers, process management, telemetry,
  Archive retrieval, and prompt construction.
- `apps/client/nivelle_link`: connection selection, secure token storage, controllers,
  request-scoped chat state, and Qt windows.
- Runtime data stays outside the source tree through `platformdirs`. The supported
  overrides are `NIVELLE_CORE_DATA_DIR` and `NIVELLE_LINK_DATA_DIR`.

The former package and environment names exist only as one-release migration adapters.
New code and configuration must use the canonical Nivelle names.

## Security and failure model

Pairing is limited to private addresses. A six-digit code expires after ten minutes. Only
PBKDF2-HMAC-SHA256 token hashes and salts are persisted. Tokens never appear in URLs,
configuration files, or logs. Settings are validated before atomic `fsync`/`os.replace`
replacement, and revisions permit rollback. Optional metrics are reported as `null` when
unavailable; they are never guessed. Both transports use the common error envelope.

Core exposes only the Gateway to the trusted LAN. A managed `llama-server` uses loopback.
Do not publish either service directly to the public internet; use a private VPN for remote
access.

## Request flow

Link selects a healthy profile, authenticates, opens `/ws/v1/chat`, and sends a versioned
`chat.request`. Every submission owns a fresh `request_id` and `client_message_id`. Core
persists the user message and one assistant placeholder in a transaction, builds the
Persona/session prompt, streams `assistant.delta` events, persists the final message, and
emits exactly one `assistant.completed` carrying the same assistant `message_id`.

Link isolates delta buffers by request and message ID. History reload, reconnect replay,
and completion handling deduplicate by `message_id`, so a completed assistant message is
rendered once. Cancellation and errors are scoped to the active request. The mock and real
providers follow the same event contract.

## Phase boundaries

Phase 2 provides explicit, user-approved Archive records. Automatic memory extraction is
disabled. Phase 3 tool execution remains deny-by-default: a tool is enabled only after its
schema, policy, approval, audit, timeout, and cancellation gates are implemented and
verified.
