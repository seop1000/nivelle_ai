# P0 Foundation

## Runtime architecture

```text
Nivelle Link -> Nivelle Core Gateway -> Model Router -> Provider -> llama.cpp
```

`Nivelle-Link.exe` is the client launcher. It knows only the Gateway endpoint and
uses Core HTTP/WebSocket APIs. `Nivelle-Core.exe` is the server launcher. Core
owns provider routing and the `provider_endpoint`; llama.cpp is never a Link
fallback. The Link admin window may edit Core-owned model settings through the
Gateway, but it does not connect to those endpoints itself.

## Configuration ownership and priority

All endpoint resolution follows one order: CLI override, environment variable,
local configuration, then safe default.

Link owns `gateway_endpoint` (`--gateway-endpoint` or
`NIVELLE_GATEWAY_ENDPOINT`). It has no guessed network default: when no saved
profile exists, Link opens the connection dialog.

Core owns `provider_endpoint` (`--provider-endpoint` or
`NIVELLE_PROVIDER_ENDPOINT`). Its safe default is the Core-local
`http://127.0.0.1:8080`. The 0.3.1 `external_url` field is accepted once as a
legacy input, rejected when it conflicts with `provider_endpoint`, and serialized
only under the new name. Diagnostics report the selected source and redact the
value.

Core's Gateway listener and its advertised Link address are separate. Core binds
to `0.0.0.0:8765` by default, while the provider remains Core-local at
`127.0.0.1:8080`. A wildcard is useful for listening but is never a connectable
Link endpoint.

The advertised Gateway address follows CLI
`--gateway-advertised-host`, environment
`NIVELLE_GATEWAY_ADVERTISED_HOST`, local `server.yaml`, then deterministic
Windows IPv4 detection. Detection evaluates adapter state, address state,
physical/virtual metadata, the default route, and effective route/interface
metric. It prefers physical Ethernet, then physical Wi-Fi. It rejects loopback,
APIPA, multicast, disconnected and virtual adapters and never falls back to
host-name resolution. No eligible interface produces an explicit unavailable
state instead of a guessed endpoint.

The bind address independently follows `--gateway-bind`,
`NIVELLE_GATEWAY_BIND`, `server.yaml` `host`, then `0.0.0.0`. Therefore the
normal two-PC topology is:

```text
Nivelle Link -> http://<Core-LAN-IPv4>:8765
                  Core bind 0.0.0.0:8765
                  provider  127.0.0.1:8080
```

`Nivelle-Core.exe --network-diagnostics` prints the effective bind and
advertised addresses plus candidate/rejection details, then exits without
starting the model provider. Link must be configured with the concrete advertised
address. `0.0.0.0` is never valid in Link; `127.0.0.1` means the Link PC and is
only appropriate for a same-PC installation. Persisted Link endpoints are not
silently changed when the Core PC's address changes.

The Gateway is intended for a trusted LAN or an explicitly configured VPN. The
public internet is not a safe default; Windows Firewall should permit inbound
TCP `8765` only on the intended profile/subnet.

## Python and disposable `.venv`

The supported Python range is `>=3.12,<3.15`. Selection order is CLI
`-PythonPath` / EXE `--python`, `NIVELLE_PYTHON`, local
`.nivelle/bootstrap.json`, Windows `py`, then compatible `python.exe` on PATH.

`.venv` is an artifact, not portable state. The bootstrap checks `pyvenv.cfg`,
the recorded base Python, the real interpreter prefix, project-path fingerprint,
dependency imports, and supported version. A copied, incomplete, or stale venv is
never upgraded or path-edited. A complete replacement is built under a unique
temporary directory; only after install and import validation does it replace
`.venv`. A failed build leaves the prior environment untouched. `.venv` must not
be committed, packaged, copied to another PC, or synchronized.

```powershell
.\scripts\bootstrap_python.ps1 -ProjectRoot $PWD
.\scripts\bootstrap_python.ps1 -ProjectRoot $PWD -PythonPath 'C:\Python312\python.exe'
```

Paths are derived from the launcher/script location. Spaces, Korean characters,
and other drive letters are supported; subprocesses receive argument lists and
explicit working directories.

## Connection state machine

Normal transitions are
`DISCONNECTED -> CONNECTING -> AUTHENTICATING -> CONNECTED -> DISCONNECTING -> DISCONNECTED`.
Failures use `CONNECTING -> FAILED -> WAITING_RETRY -> CONNECTING`.

The manager owns at most one connection task and one retry task. Concurrent
connect calls share one task. Retry uses bounded exponential backoff with jitter
and an interruptible shutdown wait. A generation counter prevents stale work from
publishing state. Once shutdown begins, no new connection/retry task can start.
Shutdown is idempotent, excludes its current task, cancels outstanding work, and
awaits task collection. Network readers clear callbacks before final socket close
so a shutdown cannot schedule a reconnect.

Gateway transport failure is a Link connection failure. A healthy Gateway with
an unavailable model remains a connected Gateway and reports the provider/model
state from `/api/v1/status`; Link does not bypass Core.

## Validation

```powershell
.\scripts\test_p0_portability.ps1
.\scripts\run_tests.ps1
```

The portability acceptance creates a project copy under a temporary path with
spaces and Korean characters, injects a venv pointing at a missing developer
Python, bootstraps with a different compatible Python, starts Core in mock mode,
and proves Link reaches its Gateway. The test excludes the source `.venv`.
