#Requires -Version 5.1

<#
.SYNOPSIS
Builds thin Windows x64 Nivelle launcher executables with PyInstaller.

.DESCRIPTION
The EXEs only start scripts/run_locked.ps1 from the external installation.
No application source, configuration, model, user data, or virtual environment
is embedded. This keeps the existing file-level updater and rollback system
authoritative after an EXE build.

.PARAMETER ProjectRoot
Nivelle project root. Defaults to the parent of this scripts directory.

.PARAMETER OutputRoot
Where Nivelle-Core.exe, Nivelle-Link.exe, Nivelle-Local.exe, and
Nivelle-Updater.exe are installed.
Defaults to ProjectRoot so portable/update packages can include them.

.PARAMETER SkipDependencyInstall
Fail instead of installing the pinned PyInstaller build dependency when absent.

.PARAMETER SkipSmokeTest
Do not execute the finished EXEs in external-file validation mode.

.PARAMETER KeepBuildFiles
Keep build/pyinstaller and dist/pyinstaller-stage for diagnosis.
#>

[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [string]$OutputRoot,
    [switch]$SkipDependencyInstall,
    [switch]$SkipSmokeTest,
    [switch]$KeepBuildFiles
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$PyInstallerVersion = '6.21.0'
$ExecutableNames = @('Nivelle-Core', 'Nivelle-Link', 'Nivelle-Local', 'Nivelle-Updater')

function Test-ChildPath {
    param([string]$Parent, [string]$Child)

    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [IO.Path]::GetFullPath($Child)
    return $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)
}

function Remove-ControlledDirectory {
    param([string]$Root, [string]$Target)

    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $resolvedTarget = [IO.Path]::GetFullPath($Target).TrimEnd('\')
    if (-not (Test-ChildPath -Parent $resolvedRoot -Child $resolvedTarget)) {
        throw "Refusing to remove a build directory outside the project: $resolvedTarget"
    }
    if (Test-Path -LiteralPath $resolvedTarget -PathType Container) {
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
}

function Invoke-Checked {
    param(
        [string]$Description,
        [string]$Executable,
        [string[]]$Arguments
    )

    Write-Host $Description -ForegroundColor Cyan
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Invoke-ExecutableSmokeTest {
    param([string]$Executable, [string]$InstallRoot)

    if ($InstallRoot.Contains('"')) {
        throw "InstallRoot cannot contain a quote: $InstallRoot"
    }
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = "--smoke-test --install-root `"$InstallRoot`""
    $startInfo.UseShellExecute = $false
    $process = [Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw "Could not start EXE smoke test: $Executable"
    }
    try {
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "Smoke test failed for $Executable with exit code $($process.ExitCode)."
        }
    }
    finally {
        $process.Dispose()
    }
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Join-Path $PSScriptRoot '..'
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = $ProjectRoot
}
elseif (-not [IO.Path]::IsPathRooted($OutputRoot)) {
    $OutputRoot = Join-Path $ProjectRoot $OutputRoot
}
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$EntryPoint = Join-Path $ProjectRoot 'scripts\nivelle_executable_launcher.py'
$VersionFile = Join-Path $ProjectRoot 'VERSION'
$WorkRoot = Join-Path $ProjectRoot 'build\pyinstaller'
$StageRoot = Join-Path $ProjectRoot 'dist\pyinstaller-stage'
$SpecRoot = Join-Path $WorkRoot 'spec'

foreach ($required in @(
        $Python,
        $EntryPoint,
        $VersionFile,
        (Join-Path $ProjectRoot 'scripts\run_locked.ps1'),
        (Join-Path $ProjectRoot 'scripts\update_from_github.ps1')
    )) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required EXE build input was not found: $required"
    }
}

if (-not (Test-Path -LiteralPath $OutputRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
}

$platformCheck = & $Python -c "import struct,sys; print(f'{sys.platform}:{struct.calcsize(chr(80))*8}')"
if ($LASTEXITCODE -ne 0 -or $platformCheck.Trim() -ne 'win32:64') {
    throw "Windows x64 Python is required to build Nivelle EXEs; found: $platformCheck"
}

$installedPyInstallerVersion = & $Python -c (
    "import importlib.metadata as m; " +
    "print(next((d.version for d in m.distributions() " +
    "if (d.metadata.get('Name') or '').lower() == 'pyinstaller'), ''))"
)
$pyInstallerReady = (
    $LASTEXITCODE -eq 0 -and
    $installedPyInstallerVersion.Trim() -eq $PyInstallerVersion
)
if (-not $pyInstallerReady) {
    if ($SkipDependencyInstall) {
        throw "PyInstaller $PyInstallerVersion is not installed in .venv."
    }
    Invoke-Checked `
        -Description "Installing PyInstaller $PyInstallerVersion" `
        -Executable $Python `
        -Arguments @('-m', 'pip', 'install', "PyInstaller==$PyInstallerVersion")
}

Remove-ControlledDirectory -Root $ProjectRoot -Target $WorkRoot
Remove-ControlledDirectory -Root $ProjectRoot -Target $StageRoot
New-Item -ItemType Directory -Path $WorkRoot, $StageRoot, $SpecRoot -Force | Out-Null

$buildDefinitions = @(
    [PSCustomObject]@{ Name = 'Nivelle-Core'; Console = $true },
    [PSCustomObject]@{ Name = 'Nivelle-Link'; Console = $false },
    [PSCustomObject]@{ Name = 'Nivelle-Local'; Console = $true },
    [PSCustomObject]@{ Name = 'Nivelle-Updater'; Console = $false }
)

$succeeded = $false
try {
    foreach ($definition in $buildDefinitions) {
        $arguments = @(
            '-m', 'PyInstaller',
            '--noconfirm',
            '--clean',
            '--onefile',
            '--noupx',
            '--name', $definition.Name,
            '--distpath', $StageRoot,
            '--workpath', (Join-Path $WorkRoot $definition.Name),
            '--specpath', $SpecRoot
        )
        if ($definition.Console) {
            $arguments += '--console'
        }
        else {
            $arguments += '--windowed'
        }
        $arguments += $EntryPoint
        Invoke-Checked `
            -Description "Building $($definition.Name).exe" `
            -Executable $Python `
            -Arguments $arguments
    }

    foreach ($name in $ExecutableNames) {
        $stagedExecutable = Join-Path $StageRoot "$name.exe"
        if (-not (Test-Path -LiteralPath $stagedExecutable -PathType Leaf)) {
            throw "PyInstaller did not create $stagedExecutable"
        }

        $machine = & $Python -c (
            "import pathlib,struct,sys; p=pathlib.Path(sys.argv[1]); d=p.read_bytes(); " +
            "o=struct.unpack_from('<I',d,0x3c)[0]; " +
            "print(hex(struct.unpack_from('<H',d,o+4)[0]))"
        ) $stagedExecutable
        if ($LASTEXITCODE -ne 0 -or $machine.Trim() -ne '0x8664') {
            throw "$name.exe is not a Windows x64 PE executable (machine=$machine)."
        }

        if (-not $SkipSmokeTest) {
            Write-Host "Smoke testing $name.exe" -ForegroundColor Cyan
            Invoke-ExecutableSmokeTest `
                -Executable $stagedExecutable `
                -InstallRoot $ProjectRoot
        }
    }

    foreach ($name in $ExecutableNames) {
        $source = Join-Path $StageRoot "$name.exe"
        $destination = Join-Path $OutputRoot "$name.exe"
        $temporary = "$destination.new"
        Copy-Item -LiteralPath $source -Destination $temporary -Force
        Move-Item -LiteralPath $temporary -Destination $destination -Force
        $item = Get-Item -LiteralPath $destination
        $sha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Host "$($item.Name): $($item.Length) bytes; SHA-256 $sha256" -ForegroundColor Green
    }
    $succeeded = $true
}
finally {
    if ($succeeded -and -not $KeepBuildFiles) {
        Remove-ControlledDirectory -Root $ProjectRoot -Target $WorkRoot
        Remove-ControlledDirectory -Root $ProjectRoot -Target $StageRoot
    }
}

Write-Host "Nivelle Windows x64 launcher EXEs were built successfully." -ForegroundColor Green
