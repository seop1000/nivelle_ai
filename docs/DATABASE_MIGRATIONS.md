# Nivelle Core database migrations

Nivelle Core records applied versions in `schema_versions`. Startup migrations are
forward-only and preserve existing UUIDs and user-authored content.

## Versions

| Version | Change |
| --- | --- |
| 1 | Phase 1 clients, conversations, messages, settings revisions, runtime samples |
| 2 | Explicit Nivelle Archive memories and indexes |
| 3 | Conversation-history index |
| 4 | `normalized_content`, supersession metadata, `memory_revisions`, duplicate repair |
| 5 | Nullable `messages.client_message_id` and partial unique idempotency index |
| 6 | Nullable `messages.retry_of_client_message_id` and one-child retry index |
| 7 | Nullable user-message `request_id`, safe historical backfill, partial unique index |

Versions 4 through 7 use an explicit `BEGIN IMMEDIATE` transaction. Their schema changes,
data repair, indexes, and version marker commit together. The migration routines are
idempotent at the column and index level so an interrupted older build can be opened
safely.

## Automatic pre-migration backup

Before an existing database moves to the latest schema, `Database.initialize()` uses
SQLite's online backup API. The backup is written under the Core backup folder as
`nivelle.pre-v<version>.<UTC timestamp>.db`. Startup verifies a non-zero file size and
`PRAGMA integrity_check = ok` before applying the migration. A new empty database does not
create a redundant backup.

Create a manual backup with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backup_nivelle_data.ps1
```

An alternate Core data directory can be supplied with `-DataDir`. The script performs an
online SQLite backup, verifies integrity, copies configuration and Persona files, and
writes a SHA-256 manifest. It never modifies or deletes the source. The previous backup
script name is retained only as a 0.3.1 compatibility wrapper.

## v4 Archive duplicate repair

Memory content is canonicalized with NFKC, case folding, and collapsed punctuation and
whitespace. Among active exact duplicates, the record with the highest priority and newest
update timestamp is canonical. Other records keep their IDs and content, become inactive,
and reference the canonical ID through `superseded_by`. A migration revision is recorded.
No duplicate is silently deleted or merged into a different UUID.

Memory deletion remains a hard delete because the existing API contract promises deletion
and the schema has no soft-delete state. FTS delete triggers remove the search row. Revision
rows are internal audit history and are never searched or injected into prompts.

## v5-v7 message durability

`client_message_id` has a partial unique index, so reconnecting cannot persist one client
submission twice. A user message and its assistant `generating` placeholder are allocated
in a transaction. On startup, an assistant left in `generating` after an unclean process
exit becomes `interrupted`; a legacy orphan user turn is also marked `interrupted`.
Recovery metadata contains only a reason and timestamp.

An explicit retry uses a new client message ID and stores the old ID in
`retry_of_client_message_id`. Its partial unique index permits only one retry child for each
interrupted request. Completed and active requests cannot be retry targets. Terminal
assistant transitions use `WHERE state='generating'`, so a late disconnect cannot change a
durably completed message back to interrupted.

Version 7 stores `request_id` on user messages. The partial unique index rejects reuse
across reconnects and Core restarts. Historical metadata is backfilled only when the value
is valid and unique; ambiguous repeated legacy identifiers remain `NULL`. The assistant
message ID persisted in the same turn must equal the ID announced in `chat.accepted` and
`assistant.completed`.

## Audit and rollback

Run the read-only audit after startup:

```powershell
.venv\Scripts\python.exe scripts\audit_runtime_memories.py --json --strict
```

It reports schema version, integrity, state counts, FTS availability, normalized-value
mismatches, duplicate ID groups, message idempotency indexes, and active Archive entries
that contradict configured runtime capabilities. `runtime_conflicts` contains memory IDs
and reason codes only; it never prints memory content.

Rollback is manual because application code and schema must remain compatible:

1. Stop only Nivelle Core.
2. Keep the failed database as forensic evidence; do not overwrite the backup.
3. Verify the selected backup with `PRAGMA integrity_check`.
4. Copy the verified pre-migration database to the configured database path.
5. Restore the matching application version and start Core.
6. Run the audit and health checks before reconnecting Link.

Do not restore an older schema while running code that requires newer message-identity
indexes. Restore a matching application version together with its database.

The 0.3.1-to-0.4.0 product rename is a separate pre-open data migration: it copies the
legacy database through SQLite backup, verifies it, and installs it as `nivelle.db` without
changing conversations or user-authored memories. See
[NIVELLE_RENAME_MIGRATION.md](NIVELLE_RENAME_MIGRATION.md).
