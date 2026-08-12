#Requires -Version 5.1

<#
.SYNOPSIS
Builds a small, self-contained Nivelle update package.

.DESCRIPTION
Compares an older portable ZIP (or an extracted portable directory) with the
current project. Only new and changed application files are put in payload/.
The manifest also records deleted files and the old SHA-256 for every operation
so the updater can refuse to overwrite locally modified files.

Runtime files, virtual environments, caches, logs, databases, models, secrets,
and user-owned configuration are never compared or packaged. Under config/,
only config/examples/ is considered distributable application content.

.PARAMETER BasePath
Path to the previous Nivelle portable ZIP or its extracted root directory.

.PARAMETER ProjectRoot
Root of the current Nivelle source tree. Defaults to the parent of scripts/.

.PARAMETER FromVersion
Version expected in the installation being updated. If omitted, VERSION is
read from the base directory or ZIP. It must be supplied for legacy packages
that did not contain VERSION.

.PARAMETER ToVersion
Version written by the update. Defaults to the current project's VERSION file.

.PARAMETER OutputPath
Destination ZIP. Relative paths are resolved below ProjectRoot. Defaults to
dist/Nivelle-Update-<from>-to-<to>.zip. The one-time 0.3.1 -> 0.4.0
transition intentionally uses Nozomi-Update-0.3.1-to-0.4.0.zip so the old
updater can discover it.

.PARAMETER Force
Replace an existing output ZIP.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\scripts\build_update.ps1 `
  -BasePath .\dist\Nivelle-Windows-x64-0.4.0.zip `
  -FromVersion 0.1.0
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$BasePath,

    [string]$ProjectRoot,

    [string]$FromVersion,

    [string]$ToVersion,

    [string]$OutputPath,

    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$script:RootExcludedDirectories = @(
    ".git",
    ".hg",
    ".svn",
    ".nivelle",
    ".nozomi",
    ".venv",
    "venv",
    "runtime",
    "dist",
    "build",
    "updates",
    "update",
    "backup",
    "backups",
    "temp",
    "tmp",
    "data",
    "userdata",
    "logs",
    "models"
)

$script:CacheDirectoryNames = @(
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    ".cache"
)

$script:FixedZipTimestamp = [DateTimeOffset]::Parse("2000-01-01T00:00:00Z")

function ConvertTo-SafeRelativePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "An empty package path is not allowed."
    }

    $normalized = $Path.Replace("\", "/")
    while ($normalized.StartsWith("./", [StringComparison]::Ordinal)) {
        $normalized = $normalized.Substring(2)
    }

    if (
        [string]::IsNullOrWhiteSpace($normalized) -or
        $normalized.StartsWith("/", [StringComparison]::Ordinal) -or
        $normalized -match "^[A-Za-z]:" -or
        $normalized.IndexOf([char]0) -ge 0
    ) {
        throw "Unsafe package path: '$Path'."
    }

    $safeSegments = New-Object "System.Collections.Generic.List[string]"
    foreach ($segment in $normalized.Split('/')) {
        if (
            [string]::IsNullOrEmpty($segment) -or
            $segment -eq "." -or
            $segment -eq ".." -or
            $segment.EndsWith(" ", [StringComparison]::Ordinal) -or
            $segment.EndsWith(".", [StringComparison]::Ordinal) -or
            $segment.IndexOfAny([char[]]'<>:"|?*') -ge 0
        ) {
            throw "Unsafe package path: '$Path'."
        }

        foreach ($character in $segment.ToCharArray()) {
            if ([int]$character -lt 32) {
                throw "Unsafe package path: '$Path'."
            }
        }

        $deviceName = $segment.Split('.')[0]
        if ($deviceName -match "^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$") {
            throw "Windows device names are not allowed in package paths: '$Path'."
        }
        $safeSegments.Add($segment)
    }

    return [string]::Join("/", $safeSegments)
}

function Test-IsExcludedDirectoryPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativePath
    )

    $segments = $RelativePath.Replace("\", "/").Split('/')
    $top = $segments[0]
    if ($script:RootExcludedDirectories -contains $top) {
        return $true
    }
    if (
        $top.StartsWith(".venv.broken-", [StringComparison]::OrdinalIgnoreCase) -or
        $top.StartsWith(".tmp.", [StringComparison]::OrdinalIgnoreCase) -or
        $top.StartsWith("Nivelle-Portable-", [StringComparison]::OrdinalIgnoreCase) -or
        $top.StartsWith("Nivelle-Windows-", [StringComparison]::OrdinalIgnoreCase) -or
        $top.StartsWith("Nivelle-Update-", [StringComparison]::OrdinalIgnoreCase) -or
        $top.StartsWith("Nozomi-Portable-", [StringComparison]::OrdinalIgnoreCase) -or
        $top.StartsWith("Nozomi-Windows-", [StringComparison]::OrdinalIgnoreCase) -or
        $top.StartsWith("Nozomi-Update-", [StringComparison]::OrdinalIgnoreCase)
    ) {
        return $true
    }

    foreach ($segment in $segments) {
        if (
            $script:CacheDirectoryNames -contains $segment -or
            $segment.StartsWith(".venv.broken-", [StringComparison]::OrdinalIgnoreCase)
        ) {
            return $true
        }
    }

    if (
        $segments.Count -ge 1 -and
        $segments[0].Equals("config", [StringComparison]::OrdinalIgnoreCase)
    ) {
        if ($segments.Count -ge 2 -and
            -not $segments[1].Equals("examples", [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }

    return $false
}

function Test-IsExcludedFilePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RelativePath
    )

    $normalized = $RelativePath.Replace("\", "/")
    $parent = [IO.Path]::GetDirectoryName($normalized).Replace("\", "/")
    if (-not [string]::IsNullOrEmpty($parent)) {
        if (Test-IsExcludedDirectoryPath -RelativePath $parent) {
            return $true
        }
    }

    $segments = $normalized.Split('/')
    if (
        $segments.Count -ge 1 -and
        $segments[0].Equals("config", [StringComparison]::OrdinalIgnoreCase) -and
        ($segments.Count -lt 3 -or
            -not $segments[1].Equals("examples", [StringComparison]::OrdinalIgnoreCase))
    ) {
        return $true
    }

    $leaf = $segments[$segments.Count - 1]
    if (
        $leaf.Equals(".env", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".pyc", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".pyo", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".db", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf -match "(?i)\.db-(wal|shm|journal)$" -or
        $leaf.EndsWith(".sqlite", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".sqlite3", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".log", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".gguf", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".part", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".tmp", [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.EndsWith(".bak", [StringComparison]::OrdinalIgnoreCase)
    ) {
        return $true
    }

    return $false
}

function Get-Sha256FromStream {
    param(
        [Parameter(Mandatory = $true)]
        [IO.Stream]$Stream
    )

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash($Stream)
        return ([BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-FileSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    $stream = [IO.File]::OpenRead($LiteralPath)
    try {
        return Get-Sha256FromStream -Stream $stream
    }
    finally {
        $stream.Dispose()
    }
}

function Get-IncludedFiles {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath
    )

    $root = [IO.Path]::GetFullPath($RootPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $rootPrefix = $root + [IO.Path]::DirectorySeparatorChar
    $pending = New-Object "System.Collections.Generic.Stack[System.IO.DirectoryInfo]"
    $pending.Push((Get-Item -LiteralPath $root))

    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory.FullName -Force) {
            if (-not $item.FullName.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "A source item escaped the project root: '$($item.FullName)'."
            }
            $rawRelativePath = $item.FullName.Substring($rootPrefix.Length)
            $relativePath = ConvertTo-SafeRelativePath -Path $rawRelativePath

            if ($item.PSIsContainer) {
                if (Test-IsExcludedDirectoryPath -RelativePath $relativePath) {
                    continue
                }
                if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    Write-Warning "Skipping reparse-point directory: $relativePath"
                    continue
                }
                $pending.Push($item)
                continue
            }

            if (Test-IsExcludedFilePath -RelativePath $relativePath) {
                continue
            }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Write-Warning "Skipping reparse-point file: $relativePath"
                continue
            }
            Write-Output ([PSCustomObject]@{
                Path = $relativePath
                FullName = $item.FullName
                Size = [long]$item.Length
            })
        }
    }
}

function Get-DirectorySnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath
    )

    $snapshot = @{}
    foreach ($file in Get-IncludedFiles -RootPath $RootPath) {
        if ($snapshot.ContainsKey($file.Path)) {
            throw "Duplicate source path (case-insensitive): '$($file.Path)'."
        }
        $snapshot[$file.Path] = [PSCustomObject]@{
            Path = $file.Path
            FullName = $file.FullName
            Size = $file.Size
            Sha256 = Get-FileSha256 -LiteralPath $file.FullName
        }
    }
    return $snapshot
}

function Get-ZipSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ZipPath
    )

    $snapshot = @{}
    $archive = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($entry in $archive.Entries) {
            if ([string]::IsNullOrEmpty($entry.Name)) {
                continue
            }
            $relativePath = ConvertTo-SafeRelativePath -Path $entry.FullName
            if (Test-IsExcludedFilePath -RelativePath $relativePath) {
                continue
            }
            if ($snapshot.ContainsKey($relativePath)) {
                throw "Duplicate base ZIP path (case-insensitive): '$relativePath'."
            }

            $stream = $entry.Open()
            try {
                $sha256 = Get-Sha256FromStream -Stream $stream
            }
            finally {
                $stream.Dispose()
            }
            $snapshot[$relativePath] = [PSCustomObject]@{
                Path = $relativePath
                FullName = $null
                Size = [long]$entry.Length
                Sha256 = $sha256
            }
        }
    }
    finally {
        $archive.Dispose()
    }
    return $snapshot
}

function Get-VersionFromZip {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ZipPath
    )

    $archive = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $matches = @(
            $archive.Entries | Where-Object {
                -not [string]::IsNullOrEmpty($_.Name) -and
                (ConvertTo-SafeRelativePath -Path $_.FullName).Equals(
                    "VERSION", [StringComparison]::OrdinalIgnoreCase
                )
            }
        )
        if ($matches.Count -eq 0) {
            return $null
        }
        if ($matches.Count -gt 1) {
            throw "The base ZIP contains more than one root VERSION file."
        }

        $stream = $matches[0].Open()
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
        try {
            return $reader.ReadToEnd().Trim()
        }
        finally {
            $reader.Dispose()
            $stream.Dispose()
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Assert-Version {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Version,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if ($Version -notmatch "^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$") {
        throw "$Label '$Version' is not a safe version identifier."
    }
}

function Assert-ProjectVersionConsistency {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedVersion
    )

    $pyprojectPath = Join-Path $RootPath "pyproject.toml"
    if (Test-Path -LiteralPath $pyprojectPath -PathType Leaf) {
        $pyprojectText = [IO.File]::ReadAllText($pyprojectPath)
        $projectVersionMatch = [regex]::Match(
            $pyprojectText,
            '(?m)^\s*version\s*=\s*["'']([^"'']+)["'']'
        )
        if ($projectVersionMatch.Success -and -not $projectVersionMatch.Groups[1].Value.Equals(
            $ExpectedVersion, [StringComparison]::Ordinal
        )) {
            throw (
                "VERSION ($ExpectedVersion) does not match pyproject.toml " +
                "($($projectVersionMatch.Groups[1].Value))."
            )
        }
        if (-not $projectVersionMatch.Success -and -not [regex]::IsMatch(
            $pyprojectText,
            '(?ms)^\[tool\.hatch\.version\]\s*$.*?^\s*path\s*=\s*["'']VERSION["'']\s*$'
        )) {
            throw "pyproject.toml does not derive its version from VERSION."
        }
    }

    $applicationVersionPath = Join-Path $RootPath "packages\nivelle_protocol\version.py"
    if (-not (Test-Path -LiteralPath $applicationVersionPath -PathType Leaf)) {
        $applicationVersionPath = Join-Path $RootPath "packages\nozomi_protocol\version.py"
    }
    if (Test-Path -LiteralPath $applicationVersionPath -PathType Leaf) {
        $applicationVersionText = [IO.File]::ReadAllText($applicationVersionPath)
        $applicationVersionMatch = [regex]::Match(
            $applicationVersionText,
            '(?m)^APP_VERSION\s*=\s*["'']([^"'']+)["'']'
        )
        if ($applicationVersionMatch.Success -and -not $applicationVersionMatch.Groups[1].Value.Equals(
            $ExpectedVersion, [StringComparison]::Ordinal
        )) {
            throw (
                "VERSION ($ExpectedVersion) does not match APP_VERSION " +
                "($($applicationVersionMatch.Groups[1].Value))."
            )
        }
        if (-not $applicationVersionMatch.Success -and -not [regex]::IsMatch(
            $applicationVersionText,
            '(?m)^APP_VERSION\s*=\s*_load_app_version\(\)\s*$'
        )) {
            throw "APP_VERSION does not derive from the canonical VERSION source."
        }
    }
}

function Write-Utf8WithoutBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,

        [Parameter(Mandatory = $true)]
        [string]$Content
    )

    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($LiteralPath, $Content, $encoding)
}

function Add-FileToZip {
    param(
        [Parameter(Mandatory = $true)]
        [IO.Compression.ZipArchive]$Archive,

        [Parameter(Mandatory = $true)]
        [string]$SourcePath,

        [Parameter(Mandatory = $true)]
        [string]$EntryName
    )

    $safeEntryName = ConvertTo-SafeRelativePath -Path $EntryName
    $entry = $Archive.CreateEntry($safeEntryName, [IO.Compression.CompressionLevel]::Optimal)
    $entry.LastWriteTime = $script:FixedZipTimestamp
    $entry.ExternalAttributes = 0

    $input = [IO.File]::OpenRead($SourcePath)
    $output = $entry.Open()
    try {
        $input.CopyTo($output)
    }
    finally {
        $output.Dispose()
        $input.Dispose()
    }
}

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Join-Path $PSScriptRoot ".."
}
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).ProviderPath
if (-not (Test-Path -LiteralPath $resolvedProjectRoot -PathType Container)) {
    throw "ProjectRoot must be a directory: '$ProjectRoot'."
}
$resolvedProjectRoot = [IO.Path]::GetFullPath($resolvedProjectRoot).TrimEnd(
    [IO.Path]::DirectorySeparatorChar
)

$resolvedBasePath = (Resolve-Path -LiteralPath $BasePath).ProviderPath
$baseIsDirectory = Test-Path -LiteralPath $resolvedBasePath -PathType Container
$baseIsZip = (
    (Test-Path -LiteralPath $resolvedBasePath -PathType Leaf) -and
    [IO.Path]::GetExtension($resolvedBasePath).Equals(".zip", [StringComparison]::OrdinalIgnoreCase)
)
if (-not $baseIsDirectory -and -not $baseIsZip) {
    throw "BasePath must be a portable ZIP or an extracted portable directory: '$BasePath'."
}

if ([string]::IsNullOrWhiteSpace($ToVersion)) {
    $currentVersionPath = Join-Path $resolvedProjectRoot "VERSION"
    if (-not (Test-Path -LiteralPath $currentVersionPath -PathType Leaf)) {
        throw "ToVersion was not provided and the current project has no VERSION file."
    }
    $ToVersion = ([IO.File]::ReadAllText($currentVersionPath)).Trim()
}

if ([string]::IsNullOrWhiteSpace($FromVersion)) {
    if ($baseIsDirectory) {
        $baseVersionPath = Join-Path $resolvedBasePath "VERSION"
        if (Test-Path -LiteralPath $baseVersionPath -PathType Leaf) {
            $FromVersion = ([IO.File]::ReadAllText($baseVersionPath)).Trim()
        }
    }
    else {
        $FromVersion = Get-VersionFromZip -ZipPath $resolvedBasePath
    }
}

if ([string]::IsNullOrWhiteSpace($FromVersion)) {
    throw (
        "The base package has no VERSION file. Supply -FromVersion explicitly, " +
        "for example: -FromVersion 0.1.0"
    )
}
Assert-Version -Version $FromVersion -Label "FromVersion"
Assert-Version -Version $ToVersion -Label "ToVersion"
Assert-ProjectVersionConsistency `
    -RootPath $resolvedProjectRoot `
    -ExpectedVersion $ToVersion
if ($FromVersion.Equals($ToVersion, [StringComparison]::OrdinalIgnoreCase)) {
    throw "FromVersion and ToVersion must be different."
}
$isLegacyBridge = (
    $FromVersion.Equals('0.3.1', [StringComparison]::Ordinal) -and
    $ToVersion.Equals('0.4.0', [StringComparison]::Ordinal)
)

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $packageProduct = if ($isLegacyBridge) { 'Nozomi' } else { 'Nivelle' }
    $OutputPath = Join-Path $resolvedProjectRoot (
        "dist\{0}-Update-{1}-to-{2}.zip" -f $packageProduct, $FromVersion, $ToVersion
    )
}
elseif (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $resolvedProjectRoot $OutputPath
}
$resolvedOutputPath = [IO.Path]::GetFullPath($OutputPath)
$checksumPath = $resolvedOutputPath + ".sha256"
if (-not [IO.Path]::GetExtension($resolvedOutputPath).Equals(
        ".zip", [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "OutputPath must end in .zip: '$resolvedOutputPath'."
}

$outputDirectory = Split-Path -Parent $resolvedOutputPath
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}
if (Test-Path -LiteralPath $resolvedOutputPath) {
    if (-not $Force) {
        throw "Output already exists. Use -Force to replace it: '$resolvedOutputPath'."
    }
    Remove-Item -LiteralPath $resolvedOutputPath -Force
}
if (Test-Path -LiteralPath $checksumPath) {
    if (-not $Force) {
        throw "Checksum sidecar already exists. Use -Force to replace it: '$checksumPath'."
    }
    Remove-Item -LiteralPath $checksumPath -Force
}

$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
    "nivelle-update-build-" + [Guid]::NewGuid().ToString("N")
)
$payloadRoot = Join-Path $temporaryRoot "payload"
New-Item -ItemType Directory -Path $payloadRoot -Force | Out-Null

try {
    Write-Host "Reading base package: $resolvedBasePath"
    if ($baseIsDirectory) {
        $baseSnapshot = Get-DirectorySnapshot -RootPath $resolvedBasePath
    }
    else {
        $baseSnapshot = Get-ZipSnapshot -ZipPath $resolvedBasePath
    }

    Write-Host "Reading current project: $resolvedProjectRoot"
    $currentSnapshot = Get-DirectorySnapshot -RootPath $resolvedProjectRoot

    [string[]]$changedPaths = @(
        $currentSnapshot.Keys | Where-Object {
            -not $baseSnapshot.ContainsKey($_) -or
            -not $currentSnapshot[$_].Sha256.Equals(
                $baseSnapshot[$_].Sha256, [StringComparison]::OrdinalIgnoreCase
            )
        }
    )
    [Array]::Sort($changedPaths, [StringComparer]::Ordinal)
    [string[]]$deletedPaths = @(
        $baseSnapshot.Keys | Where-Object { -not $currentSnapshot.ContainsKey($_) }
    )
    [Array]::Sort($deletedPaths, [StringComparer]::Ordinal)

    $manifestFiles = New-Object "System.Collections.Generic.List[object]"
    foreach ($relativePath in $changedPaths) {
        $source = $currentSnapshot[$relativePath]
        $stagedPath = Join-Path $payloadRoot $relativePath.Replace('/', '\')
        $stagedParent = Split-Path -Parent $stagedPath
        if (-not (Test-Path -LiteralPath $stagedParent -PathType Container)) {
            New-Item -ItemType Directory -Path $stagedParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $source.FullName -Destination $stagedPath -Force

        $stagedItem = Get-Item -LiteralPath $stagedPath
        $baseSha256 = $null
        if ($baseSnapshot.ContainsKey($relativePath)) {
            $baseSha256 = $baseSnapshot[$relativePath].Sha256
        }
        $manifestFiles.Add([PSCustomObject][ordered]@{
            path = $relativePath
            sha256 = Get-FileSha256 -LiteralPath $stagedPath
            size = [long]$stagedItem.Length
            base_sha256 = $baseSha256
        })
    }

    $manifestDeletions = New-Object "System.Collections.Generic.List[object]"
    foreach ($relativePath in $deletedPaths) {
        $manifestDeletions.Add([PSCustomObject][ordered]@{
            path = $relativePath
            base_sha256 = $baseSnapshot[$relativePath].Sha256
        })
    }

    $dependencyHash = $null
    $pyprojectPath = Join-Path $resolvedProjectRoot "pyproject.toml"
    if (Test-Path -LiteralPath $pyprojectPath -PathType Leaf) {
        $dependencyHash = Get-FileSha256 -LiteralPath $pyprojectPath
    }

    $manifest = [PSCustomObject][ordered]@{
        format_version = 1
        product = $(if ($isLegacyBridge) { 'Nozomi' } else { 'Nivelle' })
        from_version = $FromVersion
        to_version = $ToVersion
        min_updater_version = "1.0"
        architecture = "windows-x64"
        dependency_hash = $dependencyHash
        files = $manifestFiles.ToArray()
        deletions = $manifestDeletions.ToArray()
    }
    $manifestPath = Join-Path $temporaryRoot "manifest.json"
    $manifestJson = $manifest | ConvertTo-Json -Depth 6
    Write-Utf8WithoutBom -LiteralPath $manifestPath -Content ($manifestJson + "`n")

    $bootstrapMappings = @(
        [PSCustomObject]@{
            SourcePath = "scripts/apply_update.ps1"
            EntryName = "apply_update.ps1"
        }
    )
    if ($isLegacyBridge) {
        $bootstrapMappings += [PSCustomObject]@{
            SourcePath = "Nozomi-Update.cmd"
            EntryName = "Nozomi-Update.cmd"
        }
    }
    else {
        $bootstrapMappings += [PSCustomObject]@{
            SourcePath = "Nivelle-Update.cmd"
            EntryName = "Nivelle-Update.cmd"
        }
    }
    $bootstrapFiles = New-Object "System.Collections.Generic.List[object]"
    foreach ($mapping in $bootstrapMappings) {
        $sourcePath = Join-Path $resolvedProjectRoot $mapping.SourcePath.Replace('/', '\')
        if (Test-Path -LiteralPath $sourcePath -PathType Leaf) {
            $bootstrapFiles.Add([PSCustomObject]@{
                Path = $mapping.EntryName
                FullName = $sourcePath
            })
        }
    }
    if (-not ($bootstrapFiles | Where-Object { $_.Path -eq "apply_update.ps1" })) {
        throw (
            "The update bootstrap is missing scripts/apply_update.ps1. " +
            "It is required so legacy installations can apply the package."
        )
    }
    if (-not $isLegacyBridge -and
        -not ($bootstrapFiles | Where-Object { $_.Path -eq "Nivelle-Update.cmd" })) {
        throw (
            "The update bootstrap is missing Nivelle-Update.cmd."
        )
    }
    if ($isLegacyBridge -and
        -not ($bootstrapFiles | Where-Object { $_.Path -eq "Nozomi-Update.cmd" })) {
        throw (
            "The 0.3.1 compatibility bridge is missing Nozomi-Update.cmd. " +
            "The legacy updater requires that exact bootstrap name."
        )
    }

    $archiveStream = [IO.File]::Open(
        $resolvedOutputPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
    $archive = New-Object IO.Compression.ZipArchive(
        $archiveStream,
        [IO.Compression.ZipArchiveMode]::Create,
        $false
    )
    try {
        Add-FileToZip -Archive $archive -SourcePath $manifestPath -EntryName "manifest.json"
        foreach ($bootstrap in $bootstrapFiles | Sort-Object Path) {
            Add-FileToZip `
                -Archive $archive `
                -SourcePath $bootstrap.FullName `
                -EntryName $bootstrap.Path
        }
        foreach ($relativePath in $changedPaths) {
            $stagedPath = Join-Path $payloadRoot $relativePath.Replace('/', '\')
            Add-FileToZip `
                -Archive $archive `
                -SourcePath $stagedPath `
                -EntryName ("payload/" + $relativePath)
        }
    }
    finally {
        $archive.Dispose()
        $archiveStream.Dispose()
    }

    $packageHash = Get-FileSha256 -LiteralPath $resolvedOutputPath
    $packageSize = (Get-Item -LiteralPath $resolvedOutputPath).Length
    $checksumLine = "$packageHash *$([IO.Path]::GetFileName($resolvedOutputPath))`r`n"
    Write-Utf8WithoutBom -LiteralPath $checksumPath -Content $checksumLine
    Write-Host ""
    Write-Host "Update package created: $resolvedOutputPath"
    Write-Host "From: $FromVersion"
    Write-Host "To: $ToVersion"
    Write-Host "Changed/new files: $($changedPaths.Count)"
    Write-Host "Deleted files: $($deletedPaths.Count)"
    Write-Host "Size: $packageSize bytes"
    Write-Host "SHA-256: $packageHash"
    Write-Host "Checksum file: $checksumPath"
}
catch {
    if (Test-Path -LiteralPath $resolvedOutputPath -PathType Leaf) {
        Remove-Item -LiteralPath $resolvedOutputPath -Force
    }
    if (Test-Path -LiteralPath $checksumPath -PathType Leaf) {
        Remove-Item -LiteralPath $checksumPath -Force
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
