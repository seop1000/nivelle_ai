# Nivelle Lethia 0.4.0 implementation plan

## Release gate

Phase 3 tool execution remains disabled until the chat regression suite proves all of the
following invariants:

- one client submission creates a fresh `request_id` and `client_message_id`;
- one accepted turn owns exactly one assistant `message_id`;
- delta buffers are isolated by request and assistant message identifiers;
- a completed assistant message is finalized and rendered exactly once;
- history reload and reconnect replay cannot duplicate a known `message_id`;
- the persisted assistant identifier equals the identifier announced by the server.

## Canonical 0.4.0 identity

- Product: `Nivelle`
- Character: `Nivelle Lethia` / `레시아 니벨`
- Call name: `Nivelle` / `니벨`
- Server: `Nivelle Core`
- Client: `Nivelle Link`
- Memory: `Nivelle Archive`
- Tool worker: `Nivelle Agent`
- Updater: `Nivelle Updater`

Active UI, logs, package metadata, source package names, launchers, executables, update
artifacts, default settings, and generated prompts use the canonical identity. `Nozomi`
is permitted only in a clearly marked legacy compatibility adapter, migration detector,
rollback bridge, historical release document, lore field, or user-authored historical data.

## Delivery sequence

1. Lock the chat identifiers and rendering invariants with regression tests.
2. Centralize identity and the Nivelle Lethia Persona v1.0 artifact.
3. Introduce canonical `nivelle_protocol`, `nivelle_core`, and `nivelle_link` packages.
4. Keep thin one-release import and launcher adapters for 0.3.1 installations.
5. Migrate local data and credentials before any 0.4.0 database is opened.
6. Migrate only exact legacy default Persona/active-memory values; preserve custom and
   user-authored content.
7. Rename executable, portable, updater, lock, and release artifacts while recognizing
   both old and new process/lock names during the transition.
8. Run unit, integration, type, lint, migration, rollback, mixed-version, and packaging
   validation.
9. Enter Phase 3 only after the release gate is green, starting with a threat model and
   deny-by-default tool policy.

## Compatibility boundary

The REST routes, WebSocket event field names, and protocol 1.0 wire contract remain stable.
The 0.3.1 updater transition artifact retains the legacy bootstrap filename and product
marker required by that updater. All releases created by the 0.4.0 updater after transition
use Nivelle names. The transition process guard recognizes both product generations and
acquires `.nivelle` and `.nozomi` locks in a deterministic order.
