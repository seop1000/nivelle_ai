param([string]$PythonPath)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
& (Join-Path $PSScriptRoot 'bootstrap_python.ps1') `
    -ProjectRoot $ProjectRoot `
    -PythonPath $PythonPath `
    -InstallDev
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $Python -c 'import nivelle_core,nivelle_link,nivelle_protocol; print("Nivelle development environment is ready.")'
