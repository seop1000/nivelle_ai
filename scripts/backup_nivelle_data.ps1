#Requires -Version 5.1

<#
.SYNOPSIS
Creates a consistent, non-destructive backup of Nivelle Core data.

.DESCRIPTION
SQLite's online backup API is used so a live WAL database cannot produce a
torn backup. Configuration and Persona files are copied afterwards. The source
directory is never changed or deleted.
#>

[CmdletBinding()]
param(
    [string]$DataDir,
    [string]$Destination
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($DataDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:NIVELLE_CORE_DATA_DIR)) {
        $DataDir = $env:NIVELLE_CORE_DATA_DIR
    }
    elseif (-not [string]::IsNullOrWhiteSpace($env:NOZOMI_SERVER_DATA_DIR)) {
        Write-Warning 'Using legacy NOZOMI_SERVER_DATA_DIR for 0.3.1 compatibility. Set NIVELLE_CORE_DATA_DIR instead.'
        $DataDir = $env:NOZOMI_SERVER_DATA_DIR
    }
    else {
        $DataDir = Join-Path $env:LOCALAPPDATA 'Nivelle\NivelleCore'
    }
}
$DataDir = [IO.Path]::GetFullPath($DataDir)

$DatabasePath = Join-Path $DataDir 'database\nivelle.db'
if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
    $legacyDatabasePath = Join-Path $DataDir 'database\nozomi.db'
    if (Test-Path -LiteralPath $legacyDatabasePath -PathType Leaf) {
        Write-Warning 'Backing up a legacy 0.3.1 database. The source remains unchanged.'
        $DatabasePath = $legacyDatabasePath
    }
    else {
        throw "Nivelle Core database was not found: $DatabasePath"
    }
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $Destination = Join-Path $DataDir "backups\manual-$timestamp"
}
$Destination = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Destination) {
    throw "Backup destination already exists: $Destination"
}
New-Item -ItemType Directory -Path $Destination | Out-Null

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonCandidates = @(
    (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
    (Join-Path $ProjectRoot 'runtime\python\python.exe')
)
$Python = $pythonCandidates | Where-Object {
    Test-Path -LiteralPath $_ -PathType Leaf
} | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($Python)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw 'Python was not found. Run the Nivelle bootstrap once, then retry the backup.'
    }
    $Python = $pythonCommand.Source
}

$BackupDatabase = Join-Path $Destination 'nivelle.db'
$backupCode = @'
import sqlite3
import sys

source_path, destination_path = sys.argv[1:3]
source = sqlite3.connect('file:' + source_path + '?mode=ro', uri=True)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
verification = sqlite3.connect('file:' + destination_path + '?mode=ro', uri=True)
try:
    result = verification.execute('PRAGMA integrity_check').fetchone()
finally:
    verification.close()
if result != ('ok',):
    raise SystemExit(f'backup integrity_check failed: {result}')
'@

& $Python -c $backupCode $DatabasePath $BackupDatabase
if ($LASTEXITCODE -ne 0) {
    throw "SQLite backup failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $BackupDatabase -PathType Leaf) -or
    (Get-Item -LiteralPath $BackupDatabase).Length -le 0) {
    throw "SQLite backup is empty: $BackupDatabase"
}

$ConfigPath = Join-Path $DataDir 'config'
if (Test-Path -LiteralPath $ConfigPath -PathType Container) {
    Copy-Item -LiteralPath $ConfigPath -Destination (Join-Path $Destination 'config') -Recurse
}
$hash = (Get-FileHash -LiteralPath $BackupDatabase -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    created_at_utc = [DateTime]::UtcNow.ToString('o')
    source_data_dir = $DataDir
    source_database_file = [IO.Path]::GetFileName($DatabasePath)
    database_file = 'nivelle.db'
    database_size = (Get-Item -LiteralPath $BackupDatabase).Length
    database_sha256 = $hash
    integrity_check = 'ok'
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (
    Join-Path $Destination 'backup-manifest.json'
) -Encoding UTF8

Write-Host "Nivelle Core backup completed: $Destination" -ForegroundColor Green
Write-Host "Database SHA-256: $hash" -ForegroundColor Green
