# Nivelle GitHub Releases online updates

`Nivelle-Update-Online.cmd` and `Nivelle-Updater.exe` query the latest GitHub Release from
the public repository. Authentication is not required. The repository URL retains its
pre-0.4.0 slug solely for release compatibility:

- Latest release: <https://github.com/seop1000/nozomi_ai/releases/latest>
- GitHub REST documentation:
  <https://docs.github.com/en/rest/releases/releases?apiVersion=2026-03-10>

The updater compares the installation's `VERSION` with the release tag. For a normal
0.4.0-or-newer update, it downloads only when both matching assets exist:

```text
Nivelle-Update-<current-version>-to-<target-version>.zip
Nivelle-Update-<current-version>-to-<target-version>.zip.sha256
```

Downloads are staged in `%LOCALAPPDATA%\Nivelle\Updater\downloads`. The updater verifies
the ZIP size, SHA-256 sidecar format and digest, and the GitHub `sha256:` asset digest when
provided. Only a package that passes every check is sent to `apply_update.ps1`.

Stop Nivelle Core, Link, Local, and `llama-server`, then run:

```powershell
.\Nivelle-Update-Online.cmd
```

Use the following modes to check without applying or to stop after downloading:

```powershell
.\Nivelle-Update-Online.cmd -CheckOnly
.\Nivelle-Update-Online.cmd -DownloadOnly
```

When an enterprise proxy must be specified explicitly:

```powershell
.\Nivelle-Update-Online.cmd -ProxyUrl "http://proxy.example:8080"
```

If omitted, the Windows system proxy configuration is used. API and download connections
require TLS 1.2 and operating-system certificate validation. The HTTP test override accepts
loopback hosts only. Requests, redirects, API bodies, checksum files, and patch ZIPs all
have bounded limits; failed partial downloads are deleted.

SHA-256 detects corruption and asset mismatches. It is not a publisher signature. If the
repository or release account is compromised, an attacker may replace both the ZIP and its
checksum.

## 0.3.1 transition bridge

The one-time 0.3.1-to-0.4.0 transition keeps the exact legacy bootstrap asset names required
by the old updater:

```text
Nozomi-Update-0.3.1-to-0.4.0.zip
Nozomi-Update-0.3.1-to-0.4.0.zip.sha256
```

Those names are compatibility identifiers, not active product branding. The bridge starts
the guarded migration, leaves legacy data intact for rollback, and installs a sibling
Nivelle 0.4.0 root. Every package produced by the 0.4.0 updater after that transition uses
the canonical `Nivelle-Update-*` convention.

## EXE installation-root selection

`Nivelle-Updater.exe` never treats the current shell directory or a source checkout as the
installation target. In a frozen PyInstaller process, the updater uses the parent folder of
the running executable and passes that exact path to
`update_from_github.ps1 -TargetRoot` as one argument.

For example, if the executable is `D:\Nivelle\Nivelle-Updater.exe`, the installation root
is `D:\Nivelle`. Before update, the launcher verifies `VERSION`, `pyproject.toml`,
`nivelle.py`, and required scripts. If validation fails, it stops without guessing another
folder. Tests cover paths containing spaces and apostrophes.

Structured startup logs include `executable_path`, allowing operators to confirm which EXE
selected the installation. Source mode records the Python executable. When the build
pipeline supplies `NIVELLE_BUILD_COMMIT` and `NIVELLE_BUILD_TIME`, those values are logged;
missing values remain `null` rather than being inferred.

The transition process guard recognizes both Nivelle executables and explicitly labeled
0.3.1 legacy executables. It blocks patching while either generation is running.
