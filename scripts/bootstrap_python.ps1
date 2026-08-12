param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [string]$PythonPath,
    [switch]$InstallDev,
    [switch]$SkipPythonInstall
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$VenvRoot = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $VenvRoot 'Scripts\python.exe'
$FingerprintName = '.nivelle-project-fingerprint'
$BootstrapSchema = '3'
$VersionCheck = 'import sys; raise SystemExit(0 if (3,12) <= sys.version_info[:2] < (3,15) else 1)'

function Assert-ProjectRoot {
    if (-not (Test-Path -LiteralPath $script:ProjectRoot -PathType Container)) {
        throw "Nivelle project root does not exist: $($script:ProjectRoot)"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $script:ProjectRoot 'pyproject.toml') -PathType Leaf)) {
        throw "Nivelle project metadata was not found: $($script:ProjectRoot)"
    }
    $expected = [IO.Path]::GetFullPath((Join-Path $script:ProjectRoot '.venv')).TrimEnd('\')
    if ([IO.Path]::GetFullPath($script:VenvRoot).TrimEnd('\') -ne $expected) {
        throw 'The virtual environment target escaped the project root.'
    }
}

function Get-ProjectFingerprint {
    $projectHash = (Get-FileHash -LiteralPath (Join-Path $script:ProjectRoot 'pyproject.toml') -Algorithm SHA256).Hash
    $rootBytes = [Text.Encoding]::UTF8.GetBytes($script:ProjectRoot.ToLowerInvariant())
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $rootHash = ([BitConverter]::ToString($sha.ComputeHash($rootBytes))).Replace('-', '') }
    finally { $sha.Dispose() }
    return "$($script:BootstrapSchema):$rootHash`:$projectHash"
}

function Test-CompatiblePython {
    param([string]$Executable)
    if ([string]::IsNullOrWhiteSpace($Executable) -or
        -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    & $Executable -c $script:VersionCheck 2>$null
    return $LASTEXITCODE -eq 0
}

function Add-PythonCandidate {
    param($List, [string]$Executable, [string]$Source)
    if (-not [string]::IsNullOrWhiteSpace($Executable)) {
        $List.Add([pscustomobject]@{ Executable = $Executable.Trim(); Source = $Source })
    }
}

function Find-CompatiblePython {
    $candidates = [Collections.Generic.List[object]]::new()
    Add-PythonCandidate $candidates $script:PythonPath 'cli'
    Add-PythonCandidate $candidates $env:NIVELLE_PYTHON 'environment'

    $localConfig = Join-Path $script:ProjectRoot '.nivelle\bootstrap.json'
    if (Test-Path -LiteralPath $localConfig -PathType Leaf) {
        try {
            $config = Get-Content -LiteralPath $localConfig -Raw -Encoding UTF8 | ConvertFrom-Json
            Add-PythonCandidate $candidates ([string]$config.python_path) 'local_config'
        } catch {
            throw "Invalid local bootstrap config: $localConfig ($($_.Exception.Message))"
        }
    }

    # Authoritative sources are validated before probing optional launchers.
    foreach ($candidate in $candidates) {
        if (Test-CompatiblePython $candidate.Executable) { return $candidate }
        throw "Python from $($candidate.Source) is missing or outside the supported range >=3.12,<3.15."
    }

    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($selector in @('-3.14', '-3.13', '-3.12', '-3')) {
            $oldPreference = $ErrorActionPreference
            $ErrorActionPreference = 'SilentlyContinue'
            try { $resolved = & $launcher.Source $selector -c 'import sys; print(sys.executable)' 2>$null }
            finally { $ErrorActionPreference = $oldPreference }
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                Add-PythonCandidate $candidates ([string]$resolved) "py_launcher:$selector"
            }
        }
    }
    $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pathPython -and $pathPython.Source -notlike '*\WindowsApps\*') {
        Add-PythonCandidate $candidates $pathPython.Source 'PATH'
    }

    $seen = @{}
    foreach ($candidate in $candidates) {
        try { $key = [IO.Path]::GetFullPath($candidate.Executable).ToLowerInvariant() }
        catch { continue }
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        if (Test-CompatiblePython $candidate.Executable) { return $candidate }
        if ($candidate.Source -in @('cli', 'environment', 'local_config')) {
            throw "Python from $($candidate.Source) is missing or outside the supported range >=3.12,<3.15."
        }
    }
    return $null
}

function Install-CompatiblePython {
    if ($script:SkipPythonInstall) {
        throw 'No compatible Python was found and automatic installation is disabled.'
    }
    Write-Host 'Python >=3.12,<3.15 was not found. Installing Python 3.12...'
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        & $winget.Source install --id Python.Python.3.12 --exact --source winget --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $candidate = Find-CompatiblePython
            if ($candidate) { return $candidate }
        }
    }
    $installer = Join-Path $env:TEMP 'nivelle-python-3.12.10-amd64.exe'
    $uri = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
    Invoke-WebRequest -Uri $uri -OutFile $installer -UseBasicParsing
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*Python Software Foundation*') {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        throw 'The downloaded Python installer signature is invalid.'
    }
    $process = Start-Process -FilePath $installer -ArgumentList @('/quiet','InstallAllUsers=0','PrependPath=1','Include_launcher=1','Include_test=0','Shortcuts=0') -Wait -PassThru -WindowStyle Hidden
    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) { throw "Python installer failed with exit code $($process.ExitCode)." }
    $candidate = Find-CompatiblePython
    if (-not $candidate) { throw 'Python installation completed, but no compatible interpreter was found.' }
    return $candidate
}

function Read-PyvenvConfig {
    param([string]$Root)
    $path = Join-Path $Root 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
        if ($line -match '^\s*([^#=]+?)\s*=\s*(.*?)\s*$') {
            $values[$matches[1].Trim().ToLowerInvariant()] = $matches[2].Trim()
        }
    }
    return $values
}

function Test-NivelleEnvironment {
    param([string]$Root)
    $python = Join-Path $Root 'Scripts\python.exe'
    $cfg = Read-PyvenvConfig $Root
    if ($null -eq $cfg -or -not $cfg.ContainsKey('home') -or -not $cfg.ContainsKey('executable')) { return $false }
    $baseExecutable = [string]$cfg['executable']
    $baseHomePython = Join-Path ([string]$cfg['home']) 'python.exe'
    if (-not (Test-Path -LiteralPath $baseExecutable -PathType Leaf) -and
        -not (Test-Path -LiteralPath $baseHomePython -PathType Leaf)) { return $false }
    if (-not (Test-CompatiblePython $python)) { return $false }
    $prefixCheck = 'import pathlib,sys; expected=pathlib.Path(sys.argv[1]).resolve(); actual=pathlib.Path(sys.prefix).resolve(); raise SystemExit(0 if actual == expected and pathlib.Path(sys.base_prefix).exists() else 1)'
    & $python -c $prefixCheck $Root 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    $fingerprint = Join-Path $Root $script:FingerprintName
    if (-not (Test-Path -LiteralPath $fingerprint -PathType Leaf)) { return $false }
    if ((Get-Content -LiteralPath $fingerprint -Raw).Trim() -ne (Get-ProjectFingerprint)) { return $false }
    & $python -c 'import fastapi, PySide6, nivelle_core, nivelle_link, nivelle_protocol' 2>$null
    return $LASTEXITCODE -eq 0
}

function Remove-StagedDirectory {
    param([string]$Path)
    $parent = [IO.Path]::GetFullPath((Split-Path -Parent $Path)).TrimEnd('\')
    if ($parent -ne $script:ProjectRoot.TrimEnd('\') -or
        -not ([IO.Path]::GetFileName($Path).StartsWith('.tmp.nv-'))) {
        throw "Refusing to remove an unsafe bootstrap path: $Path"
    }
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}

Assert-ProjectRoot
Write-Host "Nivelle project root: $ProjectRoot"
if (Test-NivelleEnvironment $VenvRoot) {
    Write-Host 'Nivelle Python environment is ready.'
    exit 0
}

$selected = Find-CompatiblePython
if (-not $selected) { $selected = Install-CompatiblePython }
$version = & $selected.Executable -c 'import platform; print(platform.python_version())'
Write-Host "Python source: $($selected.Source); version: $version"

$temporary = Join-Path $ProjectRoot ('.tmp.nv-v-' + [guid]::NewGuid().ToString('N').Substring(0,8))
$stale = Join-Path $ProjectRoot ('.tmp.nv-s-' + [guid]::NewGuid().ToString('N').Substring(0,8))
$oldMoved = $false
try {
    & $selected.Executable -m venv $temporary
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the staged virtual environment.' }
    $temporaryPython = Join-Path $temporary 'Scripts\python.exe'
    & $temporaryPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Failed to update pip in the staged environment.' }
    $installTarget = if ($InstallDev) { "$ProjectRoot[dev]" } else { $ProjectRoot }
    & $temporaryPython -m pip install -e $installTarget
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install Nivelle dependencies in the staged environment.' }
    Set-Content -LiteralPath (Join-Path $temporary $FingerprintName) -Value (Get-ProjectFingerprint) -Encoding ASCII -NoNewline

    # The staged prefix differs from its final name, so validate imports now and
    # validate the exact prefix again only after the atomic directory swap.
    & $temporaryPython -c 'import fastapi, PySide6, nivelle_core, nivelle_link, nivelle_protocol'
    if ($LASTEXITCODE -ne 0) { throw 'Staged Nivelle import verification failed.' }
    if (Test-Path -LiteralPath $VenvRoot) {
        Move-Item -LiteralPath $VenvRoot -Destination $stale
        $oldMoved = $true
    }
    try { Move-Item -LiteralPath $temporary -Destination $VenvRoot }
    catch {
        if ($oldMoved -and -not (Test-Path -LiteralPath $VenvRoot)) {
            Move-Item -LiteralPath $stale -Destination $VenvRoot
            $oldMoved = $false
        }
        throw
    }
    if (-not (Test-NivelleEnvironment $VenvRoot)) {
        if ([IO.Path]::GetFullPath($VenvRoot).TrimEnd('\') -ne
            [IO.Path]::GetFullPath((Join-Path $ProjectRoot '.venv')).TrimEnd('\')) {
            throw 'Refusing to remove an unsafe virtual environment path.'
        }
        Remove-Item -LiteralPath $VenvRoot -Recurse -Force
        if ($oldMoved) {
            Move-Item -LiteralPath $stale -Destination $VenvRoot
            $oldMoved = $false
        }
        throw 'Nivelle environment verification failed after installation.'
    }
    if ($oldMoved) { Remove-StagedDirectory $stale; $oldMoved = $false }
} finally {
    if (Test-Path -LiteralPath $temporary) { Remove-StagedDirectory $temporary }
}
Write-Host 'Nivelle Python environment is ready.'
