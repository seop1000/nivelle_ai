param(
    [string]$PythonPath,
    [switch]$KeepTemporaryCopy
)

$ErrorActionPreference = 'Stop'
$SourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$KoreanPathSegment = -join @([char]0xB2C8,[char]0xBCA8)
$AcceptancePrefix = "$KoreanPathSegment P0 "
$AcceptanceRoot = Join-Path $env:TEMP ($AcceptancePrefix + [guid]::NewGuid().ToString('N').Substring(0,8))
$CopiedRoot = Join-Path $AcceptanceRoot 'moved project'
$CoreData = Join-Path $AcceptanceRoot 'core data'
$server = $null

function Find-DifferentPython {
    if (-not [string]::IsNullOrWhiteSpace($script:PythonPath)) { return $script:PythonPath }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($selector in @('-3.12', '-3.13', '-3.14')) {
            $candidate = & $launcher.Source $selector -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $candidate) { return [string]$candidate }
        }
    }
    throw 'A separate compatible Python is required for the portability acceptance test.'
}

try {
    New-Item -ItemType Directory -Path $CopiedRoot -Force | Out-Null
    foreach ($directoryName in @('apps','config','docs','packages','scripts','tests')) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $directoryName) -Destination $CopiedRoot -Recurse -Force
    }
    foreach ($fileName in @('pyproject.toml','VERSION','nivelle.py','nivelle_runtime.py','README.md')) {
        Copy-Item -LiteralPath (Join-Path $SourceRoot $fileName) -Destination $CopiedRoot -Force
    }

    # Simulate a venv copied from a developer PC whose base interpreter no longer exists.
    $broken = Join-Path $CopiedRoot '.venv'
    New-Item -ItemType Directory -Path (Join-Path $broken 'Scripts') -Force | Out-Null
    @'
home = C:\Users\developer\AppData\Local\Programs\Python\Missing
executable = C:\Users\developer\AppData\Local\Programs\Python\Missing\python.exe
version = 3.14.0
'@ | Set-Content -LiteralPath (Join-Path $broken 'pyvenv.cfg') -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $broken 'copied-venv-sentinel.txt') -Value 'must disappear'

    $selectedPython = Find-DifferentPython
    Write-Host "Portability Python: $selectedPython"
    & (Join-Path $CopiedRoot 'scripts\bootstrap_python.ps1') `
        -ProjectRoot $CopiedRoot `
        -PythonPath $selectedPython `
        -SkipPythonInstall
    if ($LASTEXITCODE -ne 0) { throw "Bootstrap failed with exit code $LASTEXITCODE." }
    if (Test-Path -LiteralPath (Join-Path $broken 'copied-venv-sentinel.txt')) {
        throw 'The copied virtual environment was reused instead of replaced.'
    }

    $python = Join-Path $CopiedRoot '.venv\Scripts\python.exe'
    $port = & $python -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"
    if ($LASTEXITCODE -ne 0 -or -not $port) { throw 'Could not allocate an acceptance-test port.' }
    $config = Join-Path $CoreData 'config'
    New-Item -ItemType Directory -Path $config -Force | Out-Null
    "host: 127.0.0.1`nport: $port`nlog_level: WARNING`nmock_mode: true" |
        Set-Content -LiteralPath (Join-Path $config 'server.yaml') -Encoding UTF8
    "mode: mock`nprovider_endpoint: http://127.0.0.1:8080`nfallback_enabled: false`nmodels: []" |
        Set-Content -LiteralPath (Join-Path $config 'models.yaml') -Encoding UTF8

    $previousData = $env:NIVELLE_CORE_DATA_DIR
    $env:NIVELLE_CORE_DATA_DIR = $CoreData
    $server = Start-Process -FilePath $python -ArgumentList @('-m','nivelle_core.main') `
        -WorkingDirectory $CopiedRoot -WindowStyle Hidden -PassThru
    $probe = @'
import asyncio, sys
from nivelle_link.network import ConnectionManager
from nivelle_protocol.settings import ConnectionProfile

async def main():
    manager = ConnectionManager(
        [ConnectionProfile(id="acceptance-gateway", host="127.0.0.1", port=int(sys.argv[1]))],
        probe_timeout=1,
    )
    for _ in range(60):
        if await manager.connect() is not None:
            assert manager.base_url() == f"http://127.0.0.1:{sys.argv[1]}"
            await manager.shutdown()
            return
        await asyncio.sleep(0.25)
    raise SystemExit("Link could not reach the copied Core Gateway")

asyncio.run(main())
'@
    $probePath = Join-Path $AcceptanceRoot 'gateway_probe.py'
    Set-Content -LiteralPath $probePath -Value $probe -Encoding UTF8
    & $python $probePath $port
    if ($LASTEXITCODE -ne 0) { throw 'Link-to-Gateway portability probe failed.' }
    Write-Host 'P0_PORTABILITY_ACCEPTANCE_OK' -ForegroundColor Green
} finally {
    if ($null -ne $server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        $server.WaitForExit(10000) | Out-Null
    }
    if ($null -ne $previousData) { $env:NIVELLE_CORE_DATA_DIR = $previousData }
    else { Remove-Item Env:NIVELLE_CORE_DATA_DIR -ErrorAction SilentlyContinue }
    if (-not $KeepTemporaryCopy -and (Test-Path -LiteralPath $AcceptanceRoot)) {
        $resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
        $resolvedAcceptance = [IO.Path]::GetFullPath($AcceptanceRoot)
        if (-not $resolvedAcceptance.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -or
            -not ([IO.Path]::GetFileName($resolvedAcceptance)).StartsWith($AcceptancePrefix)) {
            throw "Refusing to remove an unsafe acceptance path: $resolvedAcceptance"
        }
        Remove-Item -LiteralPath $resolvedAcceptance -Recurse -Force
    }
}
