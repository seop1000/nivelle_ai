#Requires -Version 5.1

<# Legacy 0.3.1 compatibility wrapper. Use backup_nivelle_data.ps1 for 0.4.0. #>

[CmdletBinding()]
param(
    [string]$DataDir,
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
Write-Warning 'backup_nozomi_data.ps1 is a legacy 0.3.1 compatibility wrapper. Use backup_nivelle_data.ps1.'
& (Join-Path $PSScriptRoot 'backup_nivelle_data.ps1') `
    -DataDir $DataDir `
    -Destination $Destination
exit $LASTEXITCODE
