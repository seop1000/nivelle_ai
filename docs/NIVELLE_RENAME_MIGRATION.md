# Nivelle 0.4.0 rename migration

## Safety properties

Migration runs before Core, Link, or the updater opens active state. It never deletes the
legacy installation, never overwrites a non-empty Nivelle destination, and never prints a
credential. A timestamped pre-migration backup and a completed marker make the operation
idempotent and leave an explicit rollback source.

## Data locations

The active 0.4.0 locations are:

- `%LOCALAPPDATA%\Nivelle\NivelleCore`
- `%LOCALAPPDATA%\Nivelle\NivelleLink`
- `%LOCALAPPDATA%\Nivelle\Updater`
- Core database: `database\nivelle.db`

The migrator recognizes the former `Nozomi\NozomiServer`, `Nozomi\NozomiClient`, and
`Nozomi\Updater` locations. Explicit `NIVELLE_*_DATA_DIR` variables take priority. Legacy
`NOZOMI_*_DATA_DIR` variables are accepted for the 0.4.0 transition only and produce a
non-secret compatibility warning.

For Core, SQLite is copied through its backup API, checked with `PRAGMA integrity_check`,
and then installed as `nivelle.db`. Configuration, Persona, pairing state, logs, and runtime
metadata use staged regular-file copies. Symlinks and reparse points are rejected. Link
connection profiles are copied atomically. If both old and new roots contain state, startup
stops with a conflict message instead of merging them.

## Credentials

New credentials use the `NivelleLink` keyring service. On the first lookup for a connection
profile, Link checks the new service, then reads the matching key from `NozomiClient`, writes
and verifies a copy in `NivelleLink`, and leaves the old entry in place for rollback.

## Persona and memory

The canonical Persona is Nivelle Lethia Persona v1.0. Migration updates only fields that are
missing or exactly equal to a released legacy default. Custom Persona fields, conversations,
user messages, assistant history, and user-authored memories remain byte-for-byte intact.
Targeted active identity facts may receive a new revision rather than an in-place rewrite;
the prior revision remains auditable.

## Rollback

Rollback stops both generations, verifies the migration marker and backup, and points the
legacy launcher back to its untouched state. It never copies 0.4.0 data over legacy data
without a separate, explicit export operation.
