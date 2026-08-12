# Configuration

The YAML files under `config/examples` are reference templates; the current
`setup_dev.ps1` bootstraps Python and validates imports but does not copy them
into the Core data directory. When no local YAML exists, Core uses the validated
Pydantic defaults. Saving through the management UI or `ConfigService` creates
the real files below the Core data directory. `server.yaml` owns the Core
Gateway listener, `models.yaml` owns the Core-to-provider connection, and
`inference.yaml` owns validated generation limits. Link connection profiles are
stored below the Link data directory. Secrets never belong in YAML.

## Gateway bind and advertised addresses

Listening and connecting are separate concerns:

```yaml
# server.yaml
host: 0.0.0.0          # bind address: listen on all local IPv4 interfaces
advertised_host: null  # null: select the active Windows LAN IPv4 automatically
port: 8765
log_level: INFO
mock_mode: false
```

Core binds to `0.0.0.0:8765` by default so a Link on another PC can reach it.
The wildcard `0.0.0.0` is not a destination and is never shown to Link as the
server address. Core separately advertises a concrete address such as
`192.168.219.100:8765`.

Automatic selection inspects Windows adapters, IPv4 addresses, default routes,
and metrics. It prefers an active physical Ethernet adapter, then physical
Wi-Fi. Disconnected, loopback, APIPA (`169.254.0.0/16`), multicast, Wi-Fi Direct,
Hyper-V/WSL, and other virtual adapters are rejected. Host-name resolution is
not used. If no usable address exists, Core reports that detection failed rather
than guessing an address.

Bind address priority is:

1. `--gateway-bind`
2. `NIVELLE_GATEWAY_BIND`
3. `server.yaml` `host`
4. safe default `0.0.0.0`

Advertised address priority is:

1. `--gateway-advertised-host`
2. `NIVELLE_GATEWAY_ADVERTISED_HOST`
3. `server.yaml` `advertised_host`
4. Windows LAN IPv4 auto-detection
5. explicit unavailable state (no host-name or stale-address fallback)

An explicit advertised override must be a connectable address. Wildcard,
loopback-only remote configurations, APIPA, multicast, and broadcast addresses
must not be given to a Link on another PC. Leave `advertised_host` as `null`
unless a multi-NIC/VPN installation intentionally needs a fixed address.

## Link Gateway endpoint

Link knows only the Core Gateway endpoint. On a second PC enter the concrete
address printed by Core diagnostics, for example:

```text
http://192.168.219.100:8765
```

Do not enter `0.0.0.0`. `127.0.0.1` and `localhost` refer to the Link PC itself,
so they are valid only when Link and Core run on the same PC. A saved endpoint is
never silently rewritten when the server changes networks; reconnect with the
new diagnostic address instead.

Link endpoint priority is `--gateway-endpoint`,
`NIVELLE_GATEWAY_ENDPOINT`, then its saved connection profile. Link does not
discover or connect directly to llama.cpp.

## Provider endpoint

The model provider is Core-private. Its default remains:

```yaml
# models.yaml
provider_endpoint: http://127.0.0.1:8080
```

Resolution priority is `--provider-endpoint`, `NIVELLE_PROVIDER_ENDPOINT`,
`models.yaml`, then `http://127.0.0.1:8080`. Port `8080` is the local llama.cpp
provider port; port `8765` is the Gateway port used by Link. Do not publish the
provider endpoint to Link or expose it as the Gateway address.

## Network diagnostics

Run diagnostics on the Core PC before pairing a remote Link:

```powershell
.\Nivelle-Core.exe --network-diagnostics
```

The command exits after printing the bind endpoint, advertised endpoint and its
source, every candidate adapter, default gateway/metric data, and the reason each
candidate was selected or rejected. It runs before model download or provider
startup. A successful result should resemble:

```text
bind=http://0.0.0.0:8765
advertised=http://192.168.219.100:8765 source=auto_detection
```

If diagnostics selects the expected address but another PC cannot connect,
verify that both PCs can route to that subnet and that Windows Defender Firewall
allows inbound TCP `8765` on the Core PC's active network profile.
