# Nivelle Windows setup

`Nivelle-Core.cmd` and `Nivelle-Link.cmd` check for Python 3.12 or newer before startup.
If Python is absent, the bootstrap first requests a per-user installation through WinGet
and falls back to the signed python.org 64-bit installer. It then creates or repairs
`.venv`; a virtual environment copied from another PC is not assumed to be portable.

For a development checkout, run the following from the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_dev.ps1
```

On the server PC, start `Nivelle-Core.exe` or `Nivelle-Core.cmd`. If Link runs on another
PC, allow inbound TCP 8765 only on the Windows **Private** firewall profile. This repository
does not alter firewall rules automatically. Never create a public port-forward for the
Gateway or `llama-server`.

On the client PC, start `Nivelle-Link.exe` or `Nivelle-Link.cmd`, enter the Core PC's
private LAN address and Gateway port, and complete the one-time pairing. The Link PC does
not need the model or llama.cpp. For remote access across networks, use a private VPN and
keep the same private-address connection model.

Use the diagnostic scripts when setup fails:

```powershell
.\scripts\check_environment.ps1
.\scripts\test_server_health.ps1 -ServerHost 127.0.0.1
.\scripts\test_client_server_connection.ps1 -ServerHost <Core-PC-LAN-IP>
```

The default active data locations are `%LOCALAPPDATA%\Nivelle\NivelleCore` and
`%LOCALAPPDATA%\Nivelle\NivelleLink`. Override them with `NIVELLE_CORE_DATA_DIR` and
`NIVELLE_LINK_DATA_DIR`. Existing 0.3.1 data is copied through the guarded migration
described in [NIVELLE_RENAME_MIGRATION.md](NIVELLE_RENAME_MIGRATION.md); do not manually
merge the old and new folders.
