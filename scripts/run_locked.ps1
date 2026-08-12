param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [Parameter(Mandatory = $true)]
    [ValidateSet('all', 'server', 'client')]
    [string]$Mode,
    [string]$PythonPath,
    [string]$GatewayEndpoint,
    [string]$ProviderEndpoint,
    [string]$GatewayBind,
    [string]$GatewayAdvertisedHost,
    [switch]$NetworkDiagnostics
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Launcher = Join-Path $ProjectRoot 'nivelle.py'
if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    # Compatibility with the external source name used by 0.3.1 installs.
    $Launcher = Join-Path $ProjectRoot 'nozomi.py'
}
$Bootstrap = Join-Path $ProjectRoot 'scripts\bootstrap_python.ps1'
$LockDirectories = @(
    (Join-Path $ProjectRoot '.nivelle'),
    (Join-Path $ProjectRoot '.nozomi')
)

if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "Nivelle launcher was not found (nivelle.py or legacy nozomi.py): $Launcher"
}
if (-not (Test-Path -LiteralPath $Bootstrap -PathType Leaf)) {
    throw "Nivelle Python bootstrap was not found: $Bootstrap"
}

$runLocks = New-Object System.Collections.Generic.List[IO.FileStream]
$bootstrapLocks = New-Object System.Collections.Generic.List[IO.FileStream]
$processExitCode = 1

try {
    # Fixed acquisition order prevents deadlocks during the 0.3.1 -> 0.4.0
    # transition. Both locks remain necessary while old launchers can exist.
    foreach ($directory in $LockDirectories) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        $runLocks.Add([IO.File]::Open(
            (Join-Path $directory 'run.lock'),
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::ReadWrite
        ))
    }

    foreach ($directory in $LockDirectories) {
        $bootstrapLock = $null
        $bootstrapDeadline = (Get-Date).AddMinutes(10)
        while ($null -eq $bootstrapLock) {
            try {
                $bootstrapLock = [IO.File]::Open(
                    (Join-Path $directory 'bootstrap.lock'),
                    [IO.FileMode]::OpenOrCreate,
                    [IO.FileAccess]::ReadWrite,
                    [IO.FileShare]::None
                )
            } catch [IO.IOException] {
                if ((Get-Date) -ge $bootstrapDeadline) {
                    throw 'Timed out while another Nivelle launcher was preparing Python.'
                }
                Start-Sleep -Milliseconds 250
            }
        }
        $bootstrapLocks.Add($bootstrapLock)
    }
    try {
        $bootstrapArguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$Bootstrap,'-ProjectRoot',$ProjectRoot)
        if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
            $bootstrapArguments += @('-PythonPath', $PythonPath)
        }
        & powershell.exe @bootstrapArguments
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            throw "Nivelle Python environment preparation failed with exit code $LASTEXITCODE."
        }
    } finally {
        for ($index = $bootstrapLocks.Count - 1; $index -ge 0; $index--) {
            $bootstrapLocks[$index].Dispose()
        }
        $bootstrapLocks.Clear()
    }
    if ([string]::IsNullOrWhiteSpace($env:NIVELLE_EXECUTABLE_PATH) -and
        -not [string]::IsNullOrWhiteSpace($env:NOZOMI_EXECUTABLE_PATH)) {
        $env:NIVELLE_EXECUTABLE_PATH = $env:NOZOMI_EXECUTABLE_PATH
    }
    elseif ([string]::IsNullOrWhiteSpace($env:NOZOMI_EXECUTABLE_PATH) -and
        -not [string]::IsNullOrWhiteSpace($env:NIVELLE_EXECUTABLE_PATH)) {
        # One-release compatibility for the legacy Python bootstrap.
        $env:NOZOMI_EXECUTABLE_PATH = $env:NIVELLE_EXECUTABLE_PATH
    }
    $launcherArguments = @($Launcher, $Mode)
    if (-not [string]::IsNullOrWhiteSpace($GatewayEndpoint)) {
        $launcherArguments += @('--gateway-endpoint', $GatewayEndpoint)
    }
    if (-not [string]::IsNullOrWhiteSpace($ProviderEndpoint)) {
        $launcherArguments += @('--provider-endpoint', $ProviderEndpoint)
    }
    if (-not [string]::IsNullOrWhiteSpace($GatewayBind)) {
        $launcherArguments += @('--gateway-bind', $GatewayBind)
    }
    if (-not [string]::IsNullOrWhiteSpace($GatewayAdvertisedHost)) {
        $launcherArguments += @('--gateway-advertised-host', $GatewayAdvertisedHost)
    }
    if ($NetworkDiagnostics) {
        $launcherArguments += '--network-diagnostics'
    }
    & $Python @launcherArguments
    $processExitCode = $LASTEXITCODE
} finally {
    for ($index = $bootstrapLocks.Count - 1; $index -ge 0; $index--) {
        $bootstrapLocks[$index].Dispose()
    }
    for ($index = $runLocks.Count - 1; $index -ge 0; $index--) {
        $runLocks[$index].Dispose()
    }
}

exit $processExitCode
