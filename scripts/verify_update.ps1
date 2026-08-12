#Requires -Version 5.1

<#
.SYNOPSIS
Verifies a Nivelle update against a real previous portable package.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BasePath,

    [Parameter(Mandatory = $true)]
    [string]$UpdatePath,

    [string]$ProjectRoot,

    [switch]$KeepWorkDirectory
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-IsPersistentRollbackLauncher {
    param([string]$RelativePath)
    return $RelativePath.Equals(
        'Nivelle-Rollback.cmd', [StringComparison]::OrdinalIgnoreCase
    ) -or $RelativePath.Equals(
        '레시아 니벨 롤백.cmd', [StringComparison]::OrdinalIgnoreCase
    ) -or $RelativePath.Equals(
        'Nozomi-Rollback.cmd', [StringComparison]::OrdinalIgnoreCase
    ) -or $RelativePath.Equals(
        'Nozomi 롤백.cmd', [StringComparison]::OrdinalIgnoreCase
    )
}

function Read-UpdateManifest {
    param([string]$Path)
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry = $archive.GetEntry('manifest.json')
        if ($null -eq $entry) { throw 'The update ZIP has no manifest.json.' }
        $stream = $entry.Open()
        try {
            $reader = New-Object IO.StreamReader(
                $stream,
                (New-Object Text.UTF8Encoding($false, $true)),
                $true
            )
            try { return ($reader.ReadToEnd() | ConvertFrom-Json) }
            finally { $reader.Dispose() }
        }
        finally { $stream.Dispose() }
    }
    finally { $archive.Dispose() }
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Join-Path $PSScriptRoot '..'
}
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
$baseZip = (Resolve-Path -LiteralPath $BasePath).Path
$updateZip = (Resolve-Path -LiteralPath $UpdatePath).Path
Assert-True (Test-Path -LiteralPath $root -PathType Container) 'ProjectRoot was not found.'

$localizedPrefix = 'Nivelle ' + [char]0xAC80 + [char]0xC99D + ' '
$workRoot = Join-Path ([IO.Path]::GetTempPath()) (
    $localizedPrefix + [guid]::NewGuid().ToString('N')
)
$installRoot = Join-Path $workRoot 'server PC installation'
$stateRoot = Join-Path $workRoot 'Local App Data'
$packageRoot = Join-Path $workRoot 'extracted update package'
$oldLocalAppData = $env:LOCALAPPDATA
$oldServerData = $env:NOZOMI_SERVER_DATA_DIR
$oldNoPause = $env:NOZOMI_NO_PAUSE
$oldCoreData = $env:NIVELLE_CORE_DATA_DIR
$oldNivelleNoPause = $env:NIVELLE_NO_PAUSE

try {
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($baseZip, $installRoot)
    [IO.Compression.ZipFile]::ExtractToDirectory($updateZip, $packageRoot)

    $manifest = Read-UpdateManifest $updateZip
    $expectedVersion = [string]$manifest.to_version
    Assert-True (-not [string]::IsNullOrWhiteSpace($expectedVersion)) (
        'The update manifest has no target version.'
    )

    $baseVersionPath = Join-Path $installRoot 'VERSION'
    $baseHadVersion = Test-Path -LiteralPath $baseVersionPath -PathType Leaf
    $baseVersion = if ($baseHadVersion) {
        (Get-Content -LiteralPath $baseVersionPath -Raw).Trim()
    }
    else {
        $null
    }

    $baseHashes = @{}
    foreach ($file in Get-ChildItem -LiteralPath $installRoot -Recurse -File) {
        $relative = $file.FullName.Substring($installRoot.Length).TrimStart('\').Replace('\', '/')
        $baseHashes[$relative] = Get-Sha256 $file.FullName
    }

    $sentinels = [ordered]@{
        'runtime\models\Qwen3.5-9B-Q4_K_M.gguf' = 'model-sentinel'
        '.venv\pyvenv.cfg' = 'venv-sentinel'
        '.env' = 'NOZOMI_SECRET=sentinel'
        'config\private-user.yaml' = 'user-setting-sentinel'
        'data\user.db' = 'database-sentinel'
        'user data\settings.yaml' = 'server-setting-sentinel'
    }
    $sentinelHashes = @{}
    foreach ($relative in $sentinels.Keys) {
        $path = Join-Path $installRoot $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
        [IO.File]::WriteAllText(
            $path,
            [string]$sentinels[$relative],
            (New-Object Text.UTF8Encoding($false))
        )
        $sentinelHashes[$relative] = Get-Sha256 $path
    }

    $env:LOCALAPPDATA = $stateRoot
    $env:NIVELLE_CORE_DATA_DIR = Join-Path $installRoot 'user data'
    $env:NOZOMI_SERVER_DATA_DIR = $env:NIVELLE_CORE_DATA_DIR
    $env:NIVELLE_NO_PAUSE = '1'
    $env:NOZOMI_NO_PAUSE = '1'
    $isLegacyBridge = (
        [string]$manifest.product -ceq 'Nozomi' -and
        [string]$manifest.from_version -ceq '0.3.1' -and
        [string]$manifest.to_version -ceq '0.4.0'
    )
    $updateBootstrap = if ($isLegacyBridge) { 'Nozomi-Update.cmd' } else { 'Nivelle-Update.cmd' }
    $updateCommand = Join-Path $packageRoot $updateBootstrap
    Assert-True (Test-Path -LiteralPath $updateCommand -PathType Leaf) (
        "The update ZIP has no $updateBootstrap bootstrap."
    )
    & $updateCommand -TargetRoot $installRoot
    Assert-True ($LASTEXITCODE -eq 0) "Update apply failed with exit code $LASTEXITCODE."
    Assert-True (
        (Get-Content -LiteralPath (Join-Path $installRoot 'VERSION') -Raw).Trim() -eq
            $expectedVersion
    ) "VERSION was not updated to $expectedVersion."

    foreach ($record in @($manifest.files)) {
        $destination = Join-Path $installRoot ([string]$record.path).Replace('/', '\')
        Assert-True (Test-Path -LiteralPath $destination -PathType Leaf) (
            "Missing applied file: $($record.path)"
        )
        Assert-True ((Get-Sha256 $destination) -ceq [string]$record.sha256) (
            "Applied hash mismatch: $($record.path)"
        )
    }
    foreach ($relative in $sentinels.Keys) {
        Assert-True (
            (Get-Sha256 (Join-Path $installRoot $relative)) -ceq $sentinelHashes[$relative]
        ) "Protected file changed during apply: $relative"
    }

    $rollbackCommand = Join-Path $installRoot 'Nivelle-Rollback.cmd'
    Assert-True (Test-Path -LiteralPath $rollbackCommand -PathType Leaf) (
        'The installed update has no Nivelle-Rollback.cmd.'
    )
    & $rollbackCommand
    Assert-True ($LASTEXITCODE -eq 0) "Rollback failed with exit code $LASTEXITCODE."
    if ($baseHadVersion) {
        Assert-True (Test-Path -LiteralPath $baseVersionPath -PathType Leaf) (
            'Rollback removed the VERSION file from a versioned base installation.'
        )
        Assert-True (
            (Get-Content -LiteralPath $baseVersionPath -Raw).Trim() -eq $baseVersion
        ) "Rollback did not restore VERSION $baseVersion."
    }
    else {
        Assert-True (-not (Test-Path -LiteralPath $baseVersionPath)) (
            'Rollback did not remove the VERSION file added to the legacy installation.'
        )
    }

    foreach ($relative in $baseHashes.Keys) {
        if (Test-IsPersistentRollbackLauncher $relative) {
            continue
        }
        $destination = Join-Path $installRoot $relative.Replace('/', '\')
        Assert-True (Test-Path -LiteralPath $destination -PathType Leaf) (
            "Base file missing after rollback: $relative"
        )
        Assert-True ((Get-Sha256 $destination) -ceq $baseHashes[$relative]) (
            "Base hash mismatch after rollback: $relative"
        )
    }
    foreach ($record in @($manifest.files)) {
        $persistentLauncher = Test-IsPersistentRollbackLauncher ([string]$record.path)
        if ($null -eq $record.base_sha256 -and -not $persistentLauncher) {
            $destination = Join-Path $installRoot ([string]$record.path).Replace('/', '\')
            Assert-True (-not (Test-Path -LiteralPath $destination -PathType Leaf)) (
                "New update file remained after rollback: $($record.path)"
            )
        }
    }
    foreach ($relative in $sentinels.Keys) {
        Assert-True (
            (Get-Sha256 (Join-Path $installRoot $relative)) -ceq $sentinelHashes[$relative]
        ) "Protected file changed during rollback: $relative"
    }

    $metadataFiles = @(
        Get-ChildItem -LiteralPath (Join-Path $stateRoot 'Nivelle\Updater\backups') `
            -Filter backup.json -Recurse -File
    )
    Assert-True ($metadataFiles.Count -eq 1) 'Expected exactly one isolated update backup.'
    $metadata = Get-Content -LiteralPath $metadataFiles[0].FullName -Raw -Encoding UTF8 |
        ConvertFrom-Json
    Assert-True ($metadata.status -ceq 'rolled-back') 'Backup status is not rolled-back.'

    & $updateCommand -TargetRoot $installRoot
    Assert-True ($LASTEXITCODE -eq 0) (
        "Reapply after rollback failed with exit code $LASTEXITCODE."
    )
    Assert-True (
        (Get-Content -LiteralPath (Join-Path $installRoot 'VERSION') -Raw).Trim() -eq
            $expectedVersion
    ) "Reapply after rollback did not restore VERSION $expectedVersion."

    Write-Host 'REAL_PORTABLE_APPLY_ROLLBACK_OK' -ForegroundColor Green
    Write-Host (
        'Base files: {0}; patch files: {1}; protected sentinels: {2}' -f `
            $baseHashes.Count, @($manifest.files).Count, $sentinels.Count
    )
}
finally {
    $env:LOCALAPPDATA = $oldLocalAppData
    $env:NOZOMI_SERVER_DATA_DIR = $oldServerData
    $env:NOZOMI_NO_PAUSE = $oldNoPause
    $env:NIVELLE_CORE_DATA_DIR = $oldCoreData
    $env:NIVELLE_NO_PAUSE = $oldNivelleNoPause
    if ($KeepWorkDirectory) {
        Write-Host "Verification directory kept: $workRoot"
    }
    elseif (Test-Path -LiteralPath $workRoot -PathType Container) {
        $resolvedWork = [IO.Path]::GetFullPath($workRoot)
        $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $resolvedWork.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove an unsafe verification path: $resolvedWork"
        }
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force
    }
}
