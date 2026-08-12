#Requires -Version 5.1

<#
.SYNOPSIS
Builds and smoke-tests a patchable Nivelle Windows x64 portable release.

.DESCRIPTION
Builds the four thin launcher EXEs, packages the external application source,
example configuration, launchers, and updater scripts, then extracts the ZIP to
a temporary directory and runs every EXE with --smoke-test. Runtime/model files,
virtual environments, build output, secrets, caches, and user data are excluded.
#>

[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$OutputPath,
    [switch]$Force,
    [switch]$SkipExecutableBuild,
    [switch]$SkipSmokeTest,
    [switch]$KeepSmokeFiles
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param([string]$Description, [string]$Executable, [string[]]$Arguments)

    Write-Host $Description -ForegroundColor Cyan
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Invoke-ExecutableSmokeTest {
    param([string]$Executable)

    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = '--smoke-test'
    $startInfo.UseShellExecute = $false
    $process = [Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw "Could not start portable EXE smoke test: $Executable"
    }
    try {
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "Portable smoke test failed for $Executable with exit code $($process.ExitCode)."
        }
    }
    finally {
        $process.Dispose()
    }
}

function Test-ChildPath {
    param([string]$Parent, [string]$Child)

    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [IO.Path]::GetFullPath($Child)
    return $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Join-Path $PSScriptRoot '..'
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$PortableBuilder = Join-Path $ProjectRoot 'scripts\build_portable.py'
$ExecutableBuilder = Join-Path $ProjectRoot 'scripts\build_executables.ps1'
$VersionPath = Join-Path $ProjectRoot 'VERSION'

foreach ($required in @($Python, $PortableBuilder, $ExecutableBuilder, $VersionPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required portable build input was not found: $required"
    }
}

$Version = ([IO.File]::ReadAllText($VersionPath)).Trim()
if ($Version -notmatch '^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$') {
    throw "VERSION is not a safe filename component: $Version"
}
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $ProjectRoot "dist\Nivelle-Windows-x64-$Version.zip"
}
elseif (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $ProjectRoot $OutputPath
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
if (-not [IO.Path]::GetExtension($OutputPath).Equals('.zip', [StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputPath must end in .zip: $OutputPath"
}

if (-not $SkipExecutableBuild) {
    Invoke-Checked `
        -Description 'Building thin Windows x64 launcher EXEs' `
        -Executable 'powershell.exe' `
        -Arguments @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $ExecutableBuilder,
            '-ProjectRoot', $ProjectRoot,
            '-OutputRoot', $ProjectRoot
        )
}

$smokeRoot = $null
try {
    $arguments = @(
        $PortableBuilder,
        '--project-root', $ProjectRoot,
        '--output', $OutputPath
    )
    if ($Force) {
        $arguments += '--force'
    }
    if (-not $SkipSmokeTest) {
        $smokeRoot = Join-Path ([IO.Path]::GetTempPath()) (
            'nivelle-portable-smoke-' + [guid]::NewGuid().ToString('N')
        )
        $arguments += @('--extract-to', $smokeRoot)
    }
    Invoke-Checked `
        -Description 'Building and verifying portable ZIP' `
        -Executable $Python `
        -Arguments $arguments

    if (-not $SkipSmokeTest) {
        foreach ($name in @('Nivelle-Core', 'Nivelle-Link', 'Nivelle-Local', 'Nivelle-Updater')) {
            $executable = Join-Path $smokeRoot "$name.exe"
            Write-Host "Portable smoke test: $name.exe" -ForegroundColor Cyan
            Invoke-ExecutableSmokeTest -Executable $executable
        }
    }

    $hash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $size = (Get-Item -LiteralPath $OutputPath).Length
    Write-Host "Portable release: $OutputPath" -ForegroundColor Green
    Write-Host "Size: $size bytes" -ForegroundColor Green
    Write-Host "SHA-256: $hash" -ForegroundColor Green
}
finally {
    if ($null -ne $smokeRoot -and -not $KeepSmokeFiles -and
        (Test-Path -LiteralPath $smokeRoot -PathType Container)) {
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
        $resolvedSmokeRoot = [IO.Path]::GetFullPath($smokeRoot).TrimEnd('\')
        if (-not (Test-ChildPath -Parent $tempRoot -Child $resolvedSmokeRoot) -or
            -not ([IO.Path]::GetFileName($resolvedSmokeRoot)).StartsWith(
                'nivelle-portable-smoke-', [StringComparison]::Ordinal
            )) {
            throw "Refusing to remove unexpected smoke directory: $resolvedSmokeRoot"
        }
        Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force
    }
}
