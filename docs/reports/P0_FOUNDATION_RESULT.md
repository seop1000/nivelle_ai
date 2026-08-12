# Nivelle V2.1 P0 Foundation result

Date: 2026-08-12 (Asia/Seoul)

## Root causes found

- The bootstrap treated a moved `.venv` as repairable and ran `venv --upgrade`.
  Its `pyvenv.cfg` contained an absolute Python 3.14 path from the development PC.
- Python selection had no CLI/environment/local-config source contract and accepted
  an open-ended version range.
- Gateway and model configuration used the ambiguous `external_url` name in Core,
  launch code, YAML, and the admin UI.
- Link connection/retry ownership was split between the UI and connection manager;
  connect calls had no shared task or stale-generation guard.
- Shutdown callbacks could still attempt to close/reconnect while teardown was in
  progress.

## Implementation

- Added source-aware configuration resolution: CLI, environment, local config,
  then safe default.
- Link owns `gateway_endpoint`; Core owns `provider_endpoint`. Legacy
  `external_url` has an explicit one-release migration and conflict rejection.
- Changed the safe Core bind default to loopback; LAN/VPN exposure is explicit.
- Rebuilt the `.venv` lifecycle around semantic `pyvenv.cfg` validation, base
  interpreter checks, project fingerprinting, a staged install, verified swap,
  and rollback on failure. Copied venvs are never upgraded.
- Added connection/retry task deduplication, generation guards, bounded backoff,
  shutdown gating, idempotent task collection, and network callback suppression.
- Added CLI propagation through the thin EXE, PowerShell launcher, Python launcher,
  Link, and Core.
- Added unit and real Windows portability acceptance coverage.

## Executables

- Client: `Nivelle-Link.exe`, 9,028,693 bytes,
  SHA-256 `D39B8C96784139A91E298F57A068E61539DAE053967B9A33CC9E76111833E172`
- Server: `Nivelle-Core.exe`, 9,032,203 bytes,
  SHA-256 `5AAA7D8A5D576D8264C8F5CF9A6604CEC639BFAF8DE476AB5B7F818523F3267F`

Both passed the external-install smoke test. Current executable names are Nivelle
Link and Nivelle Core. Files beginning with `Nozomi` remain only where the 0.3.1
update/migration bridge requires them; they are not the active executable names.

## Verification

- Full pytest: `391 passed, 1 skipped, 1 warning`.
- Strict mypy: `Success: no issues found in 34 source files`.
- Changed-file Ruff rules: passed.
- PowerShell parser for bootstrap/run/setup/acceptance scripts: passed.
- Real portability acceptance: `P0_PORTABILITY_ACCEPTANCE_OK`.

The acceptance used Python 3.12.10 at a different installation path, copied the
project without `.venv` into a temporary path containing Korean characters and a
space, injected a broken copied `pyvenv.cfg`, rebuilt the environment, started a
mock Core, and connected Link's `ConnectionManager` to the Gateway.

The single skipped test is an existing optional test. The warning is an upstream
Starlette deprecation warning about its current `httpx` test client integration.

## Remaining compatibility surface

- Legacy `Nozomi` command/import/environment/data names remain for 0.3.1 migration.
- Existing administrator config is preserved; `external_url` is migrated when read
  and new writes use `provider_endpoint`.
- Existing LAN Core installations keep their saved bind address. New installs are
  loopback-only until an administrator configures a LAN/VPN address.
