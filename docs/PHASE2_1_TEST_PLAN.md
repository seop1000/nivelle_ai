# Nivelle Phase 2.1 Test Plan (historical 0.3.1 baseline)

> This is a historical 0.3.1 validation artifact. Former package, executable, workspace,
> and product identifiers below are preserved only to make that released baseline reproducible.

## Automated validation

1. Run `python -m pytest -q` for all unit, integration, migration, WebSocket, updater, and headless Qt tests.
2. Run `python -m ruff check .` and `python -m mypy` using the repository environment.
3. Verify version-source consistency and protocol-major compatibility tests.
4. Exercise a temporary old-schema database and confirm backup, migration, row/UUID preservation, duplicate repair, revisions, and FTS rebuild.
5. Verify Korean searches for `히냥이`, server RAM/GPU variants, client specifications, model/fallback, and 2PC terminology.
6. Verify relevant facts outrank unrelated priority-100 memories; inspect selected and rejected reasons and score components.
7. Verify `assistant.context` precedes the first delta and correlates request, conversation, and message identifiers.
8. Verify the same `client_message_id` does not persist or generate twice.
9. Simulate two health failures, exponential backoff, successful reconnect, manual offline, cancellation on shutdown, and no automatic resend.
10. Verify cached management data stays visible while every mutation control and handler is locked offline, then re-enables online.
11. Allocate the user message and assistant `generating` placeholder atomically; inject a failed retry allocation and verify that no partial turn remains.
12. Restart against rows left in `generating` or legacy orphan-user state and verify deterministic recovery to `interrupted`.
13. Race completion commit against cancellation/delivery failure and verify that durable `completed` state never regresses.
14. Close a real loopback WebSocket transport during generation (not only a mocked iterator) and verify the assistant row becomes `interrupted` within a bounded timeout.
15. Submit two controlled retries for the same interrupted target and verify the persisted v6 unique relation permits only one child.
16. Verify ordinary chat retrieval is active-only while the memory library includes inactive rows only after `include_inactive=true` is explicitly requested.

## Local integration validation

- Start a temporary mock Gateway with a temporary data directory.
- Connect through HTTP and WebSocket, create/update/search/deactivate/delete memories, stream a conversation, inspect context/metrics, and reload persisted conversation data.
- Keep `/health` unauthenticated and lightweight; use authenticated `/api/v1/status` for component detail.
- Use a real loopback Uvicorn/WebSocket connection for the transport-disconnect regression. Stop only the Uvicorn task and use a temporary database.

## Isolated real-model smoke validation

After automated validation passes, run the eight acceptance questions against the installed Qwen model without touching the user's runtime database:

```powershell
.\.venv\Scripts\python.exe .\scripts\smoke_phase21_real_model.py `
  --output .\build\phase21-real-smoke-final.json
```

The script starts only its own loopback llama-server process, creates a temporary Gateway database, records answers and `assistant.context`, and removes temporary data on exit. A missing/incomplete JSON file, non-zero exit, failed context check, or failed answer check is not a pass.

## Two-PC LAN validation

1. Update both installations to 0.3.1 and confirm client/server/protocol identity in the UI.
2. Run `scripts/test_client_server_connection.ps1 -ServerHost <private-server-address> -Port <configured-port>`.
3. Ask the eight real-model smoke questions from the Phase 2.1 specification in new conversations.
4. For each answer, inspect `assistant.context`, runtime profile/address, selected and rejected memory decisions, and generation metrics.
5. Run the guided reconnect test. Confirm two consecutive failures lead to reconnecting, recovery returns online, no draft/window is lost, and no message is duplicated.
6. Restart server and client normally and recheck Persona, memory IDs/counts/content, active flags, and conversation persistence.

## Safety and reporting

- Never print tokens, Authorization headers, pairing codes, passwords, private keys, or full private prompts.
- Do not stop VPN services, reboot either PC, expose a public port, or require the real model in automated tests.
- Record each required validation as passed, failed, skipped with reason, or not applicable with reason. Automated success does not substitute for an unexecuted live restart or Qwen smoke test.
- Do not infer the final pytest count from an earlier targeted run. Copy the exact final summary into `PHASE2_1_RESULT.md`.

## Exact automated commands

```powershell
Set-Location D:\Nozomi
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy packages\nozomi_protocol apps\server\nozomi_server apps\client\nozomi_client
```
