# Nivelle Phase 2.1 Implementation Plan (historical 0.3.1 baseline)

> This is a historical 0.3.1 plan. Former package, executable, and product identifiers below
> are retained only to document and reproduce the released baseline.

## Existing architecture

- `apps/server/nozomi_server`: FastAPI Gateway, WebSocket chat pipeline, SQLite repositories, Persona prompt builder, llama.cpp adapter, status/telemetry, and versioned migrations.
- `apps/client/nozomi_client`: one PySide6/qasync application, connection manager, authenticated HTTP/WebSocket client, chat/history UI, and separate Persona, memory, server-management, and conversation-information windows.
- `packages/nozomi_protocol`: shared Pydantic request/event/settings models and protocol metadata. Application release version is loaded from the canonical root `VERSION` artifact (or the same artifact bundled in an installed wheel).
- `scripts` plus the thin PyInstaller launchers: portable execution, dependency/runtime bootstrap, transactional patching, online GitHub release discovery, rollback, and package verification.

`Nozomi-Updater.exe` resolves its install root to the executable's parent. It passes that exact root to `scripts/update_from_github.ps1`, which validates `VERSION`, `pyproject.toml`, `nozomi.py`, and the patch script before applying a version-matched package. The live server still reports 0.2.1 because that separate installation has not received the newer local 0.3.0 build; the source tree and launcher do not update a remote installation automatically.

## Confirmed root causes

1. Prompt memory selection used a global priority-sorted top three instead of query relevance.
2. FTS5 `unicode61` treats Korean inflected strings such as `히냥이이다` as one token; exact-token queries therefore miss prefixes.
3. Memory rows have no canonical content key, revision trail, or database-enforced duplicate policy.
4. WebSocket context events did not expose retrieval candidates, scores, inclusion decisions, or reasons.
5. Client connection backoff existed as a dormant generator; no health monitor or production reconnect task called it.
6. Management windows did not bind mutation controls to authoritative connection state.
7. The prompt lacked a separate project glossary, live connection/runtime facts, and explicit network safety policy.
8. llama.cpp usage/timing data was ignored, and runtime request metrics were not surfaced.
9. Message persistence had no client-generated idempotency key.
10. Version strings were synchronized in source but runtime/build identity was incomplete; the remote installation was simply older.

## Files and components to change

- Protocol: typed runtime context, retrieval candidate/context event, generation metrics, version/build identity, and compatibility helpers.
- Database/repositories: backup-before-migration, canonical memory content, duplicate repair/index, memory revisions, and message idempotency metadata.
- Memory: Korean-friendly SQLite hybrid candidate generation and a dedicated relevance-dominant retriever returning selected and rejected decisions.
- Prompt/chat server: explicit hierarchy, project glossary, runtime block, selected memories only, context-before-delta event, idempotent request handling, and reliable metrics.
- Client: authoritative reconnect/health state, no automatic resend, protocol warning, offline read-only binding, runtime/context rendering, and generation diagnostics.
- Updater/version: 0.3.1 synchronization, runtime build metadata, executable-path diagnostics, and installation-root verification tests.
- Operations/docs: safe backup/audit/health/connection/reconnect scripts and Phase 2.1 test/result documentation.

## Database migrations

- Add a normalized memory-content column and populate it with Unicode NFKC, case, whitespace, and punctuation normalization.
- Detect existing canonical duplicates without deleting user data. Keep one canonical active record, deactivate duplicates, and record the repair in revisions/audit metadata.
- Add a partial unique index for eligible active explicit memories.
- Add `memory_revisions` for content/state changes.
- Add `client_message_id` and request correlation needed for idempotent chat persistence (schema v5).
- Add `retry_of_client_message_id` and a partial unique index that permits at most one controlled retry child for an interrupted original request (schema v6).
- Allocate each user/assistant turn in one immediate transaction, recover unfinished generations on startup, and allow terminal transitions only from `generating`.
- Preserve UUIDs, categories, priorities, flags, and timestamps. Rebuild FTS after migration and verify row counts.
- Create and validate a timestamped SQLite backup before a migration that changes stored user data. Migration tests start from the prior schema.

## Protocol changes

- Keep protocol 1.x backward-compatible where possible; patch application-version differences are warnings, not connection failures.
- Add optional validated client runtime context to `chat.request`; never use it for authorization.
- Emit typed `assistant.context` before the first delta with backend information and selected/rejected retrieval decisions.
- Add optional typed generation metrics to `assistant.completed`; unavailable values remain null rather than estimated.
- Include component/app/protocol/build identity in status and expose separate Gateway, LLM, memory, and embedding states.

## Compatibility risks and mitigations

- Older clients ignore unknown events; the server retains existing accepted/delta/completed events.
- New request fields are optional so an older client continues to chat.
- The client accepts compatible protocol-major versions and warns on runtime app-version differences.
- SQLite feature variance is handled by detecting FTS5 and using bounded normalized substring fallback, never claiming vector search.
- Reconnect never resends an uncertain in-flight message. One send task and one reconnect task remain authoritative.
- Offline windows retain last-loaded values while handlers repeat the online/authentication guard at mutation time.

## Test strategy

- Unit tests: normalization, Korean prefixes/substrings/synonyms, scoring weights, exclusions/reasons, conflicts, duplicate policy, revisions, prompt hierarchy, version compatibility, metrics parsing, and reconnect transitions.
- Integration tests: old-schema migration, REST duplicate 409, CRUD/FTS update, context ordering/correlation, message idempotency, status shape, and local WebSocket flow.
- Headless Qt tests: separate context window, selected/rejected memory rows, offline mutation lock, online re-enable, singleton windows, status states, and draft preservation.
- Validation: full pytest, Ruff, mypy, updater/build tests, local server smoke test, then real LAN/Qwen tests only after the server installation is updated to 0.3.1.
- Every unexecuted live restart or real-model check is reported as skipped with its concrete reason; automated success is not presented as live acceptance.

## Rollback considerations

- Code rollback uses the existing transactional update backup and `Nozomi-Rollback` path.
- User data and model/runtime directories stay outside update payload deletion scope.
- A pre-migration SQLite backup is retained and its path is reported; restoration is manual and requires the server to be stopped.
- Schema additions are forward-only. Rolling application code back while retaining the added nullable columns/tables is supported; restoring the backup is the fallback for a full data rollback.
- Live memory correction is audited separately and never exposes tokens or other credentials.
