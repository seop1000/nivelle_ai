[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$PackagePath,

    [string]$TargetRoot
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:ProductName = 'Nivelle'
$script:LegacyProductName = 'Nozomi'
$script:ManifestName = 'manifest.json'
$script:ProtectedRootNames = @(
    '.git', '.nivelle', '.nozomi', '.venv', 'backup', 'backups', 'build', 'dist', 'runtime',
    'temp', 'tmp', 'update', 'updates', 'data', 'userdata', 'logs', 'models'
)

function Write-Info {
    param([string]$Message)
    Write-Host "[Nivelle 업데이트] $Message" -ForegroundColor Cyan
}

function Throw-UpdateError {
    param([string]$Message)
    throw "[Nivelle 업데이트] $Message"
}

function Test-ChildPath {
    param(
        [string]$Parent,
        [string]$Child
    )

    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [IO.Path]::GetFullPath($Child)
    return $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)
}

function Get-SafeDestinationPath {
    param(
        [string]$Root,
        [string]$RelativePath
    )

    $nativePath = $RelativePath.Replace('/', '\')
    $destination = [IO.Path]::GetFullPath((Join-Path $Root $nativePath))
    if (-not (Test-ChildPath -Parent $Root -Child $destination)) {
        Throw-UpdateError "대상 경로가 설치 폴더를 벗어납니다: $RelativePath"
    }
    return $destination
}

function Assert-SafeRelativePath {
    param(
        [object]$Value,
        [string[]]$DynamicProtectedPrefixes
    )

    if (-not ($Value -is [string]) -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        Throw-UpdateError '매니페스트에 비어 있거나 문자열이 아닌 파일 경로가 있습니다.'
    }

    $path = [string]$Value
    if ($path.Length -gt 240 -or $path.Contains([char]0) -or $path.Contains('\') -or
        $path.StartsWith('/') -or $path.Contains(':') -or $path.Contains('//')) {
        Throw-UpdateError "안전하지 않은 파일 경로입니다: $path"
    }

    $segments = $path.Split('/')
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq '.' -or $segment -eq '..' -or
            $segment.EndsWith('.') -or $segment.EndsWith(' ') -or
            $segment.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
            Throw-UpdateError "안전하지 않은 파일 경로입니다: $path"
        }

        $deviceName = $segment.Split('.')[0]
        if ($deviceName -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
            Throw-UpdateError "Windows 예약 이름은 사용할 수 없습니다: $path"
        }
    }

    $first = $segments[0]
    foreach ($protected in $script:ProtectedRootNames) {
        if ($first.Equals($protected, [StringComparison]::OrdinalIgnoreCase)) {
            Throw-UpdateError "보호된 경로는 업데이트할 수 없습니다: $path"
        }
    }
    if ($first.StartsWith('.venv.broken-', [StringComparison]::OrdinalIgnoreCase) -or
        $first.StartsWith('.tmp.', [StringComparison]::OrdinalIgnoreCase)) {
        Throw-UpdateError "보호된 임시 또는 가상환경 경로는 업데이트할 수 없습니다: $path"
    }
    $leaf = $segments[$segments.Length - 1]
    if ($leaf.Equals('.env', [StringComparison]::OrdinalIgnoreCase)) {
        Throw-UpdateError '.env 파일은 업데이트할 수 없습니다.'
    }
    if ($leaf -match '(?i)(\.db(?:-(?:wal|shm|journal))?|\.sqlite3?|\.log|\.gguf|\.part|\.tmp|\.bak)$') {
        Throw-UpdateError "사용자 데이터 또는 모델 파일은 업데이트할 수 없습니다: $path"
    }
    if ($first.Equals('config', [StringComparison]::OrdinalIgnoreCase)) {
        if ($segments.Length -lt 3 -or
            -not $segments[1].Equals('examples', [StringComparison]::OrdinalIgnoreCase)) {
            Throw-UpdateError "config에서는 config/examples 아래만 업데이트할 수 있습니다: $path"
        }
    }

    foreach ($prefix in $DynamicProtectedPrefixes) {
        if ($path.Equals($prefix, [StringComparison]::OrdinalIgnoreCase) -or
            $path.StartsWith($prefix + '/', [StringComparison]::OrdinalIgnoreCase)) {
            Throw-UpdateError "사용자 데이터 경로는 업데이트할 수 없습니다: $path"
        }
    }

    return $path
}

function Assert-NoReparsePoints {
    param(
        [string]$Root,
        [string]$RelativePath
    )

    $rootItem = Get-Item -LiteralPath $Root -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Throw-UpdateError "설치 폴더가 링크 또는 연결 지점입니다: $Root"
    }

    $current = $Root
    foreach ($segment in $RelativePath.Split('/')) {
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Throw-UpdateError "링크 또는 연결 지점을 통과할 수 없습니다: $RelativePath"
            }
        }
    }
}

function Get-DynamicProtectedPrefixes {
    param([string]$InstallRoot)

    $paths = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nivelle\NivelleCore'))
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nivelle\NivelleLink'))
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nivelle\Updater'))
        # Legacy data remains protected throughout the 0.3.1 bridge.
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nozomi\NozomiServer'))
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nozomi\NozomiClient'))
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nozomi\Updater'))
    }
    if (-not [string]::IsNullOrWhiteSpace($env:NIVELLE_CORE_DATA_DIR)) {
        $paths.Add($env:NIVELLE_CORE_DATA_DIR)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:NIVELLE_LINK_DATA_DIR)) {
        $paths.Add($env:NIVELLE_LINK_DATA_DIR)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:NOZOMI_SERVER_DATA_DIR)) {
        $paths.Add($env:NOZOMI_SERVER_DATA_DIR)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:NOZOMI_CLIENT_DATA_DIR)) {
        $paths.Add($env:NOZOMI_CLIENT_DATA_DIR)
    }

    $result = New-Object System.Collections.Generic.List[string]
    foreach ($dataPath in $paths) {
        try {
            $full = [IO.Path]::GetFullPath($dataPath).TrimEnd('\')
            if (Test-ChildPath -Parent $InstallRoot -Child $full) {
                $relative = $full.Substring([IO.Path]::GetFullPath($InstallRoot).TrimEnd('\').Length)
                $relative = $relative.TrimStart('\').Replace('\', '/')
                if (-not [string]::IsNullOrWhiteSpace($relative)) {
                    $result.Add($relative)
                }
            }
        }
        catch {
            Throw-UpdateError "사용자 데이터 경로를 확인할 수 없습니다: $dataPath"
        }
    }
    return $result.ToArray()
}

function Get-InstalledVersion {
    param([string]$InstallRoot)

    $versionFile = Join-Path $InstallRoot 'VERSION'
    if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
        return (Get-Content -LiteralPath $versionFile -Raw).Trim()
    }

    $projectFile = Join-Path $InstallRoot 'pyproject.toml'
    if (Test-Path -LiteralPath $projectFile -PathType Leaf) {
        $match = [regex]::Match(
            (Get-Content -LiteralPath $projectFile -Raw),
            '(?m)^version\s*=\s*["'']([^"'']+)["'']\s*$'
        )
        if ($match.Success) {
            return $match.Groups[1].Value
        }
    }
    Throw-UpdateError '설치된 버전을 확인할 수 없습니다. VERSION 또는 pyproject.toml이 필요합니다.'
}

function Assert-Version {
    param(
        [object]$Value,
        [string]$Name
    )
    if (-not ($Value -is [string]) -or
        ([string]$Value) -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$') {
        Throw-UpdateError "$Name 버전 형식이 올바르지 않습니다: $Value"
    }
    return [string]$Value
}

function Resolve-InstallRoot {
    param([string]$RequestedRoot)

    $candidate = $RequestedRoot
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $parent = Split-Path -Parent $PSScriptRoot
        if (Test-Path -LiteralPath (Join-Path $parent 'pyproject.toml') -PathType Leaf) {
            $candidate = $parent
        }
        elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'pyproject.toml') -PathType Leaf) {
            $candidate = $PSScriptRoot
        }
        else {
            $candidate = Read-Host '업데이트할 Nivelle 폴더의 전체 경로를 입력하세요'
        }
    }

    if ([string]::IsNullOrWhiteSpace($candidate) -or
        -not (Test-Path -LiteralPath $candidate -PathType Container)) {
        Throw-UpdateError "Nivelle 설치 폴더를 찾을 수 없습니다: $candidate"
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path.TrimEnd('\')
    $markers = @('pyproject.toml', 'apps\server', 'apps\client', 'packages')
    foreach ($marker in $markers) {
        if (-not (Test-Path -LiteralPath (Join-Path $resolved $marker))) {
            Throw-UpdateError "선택한 폴더는 올바른 Nivelle 설치본이 아닙니다. 누락: $marker"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $resolved 'nivelle.py') -PathType Leaf) -and
        -not (Test-Path -LiteralPath (Join-Path $resolved 'nozomi.py') -PathType Leaf)) {
        Throw-UpdateError '선택한 폴더에 nivelle.py 또는 0.3.1 호환 nozomi.py가 없습니다.'
    }
    Assert-NoReparsePoints -Root $resolved -RelativePath 'pyproject.toml'
    return $resolved
}

function Resolve-UpdatePackage {
    param(
        [string]$RequestedPackage,
        [string]$InstallRoot
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedPackage)) {
        if (-not (Test-Path -LiteralPath $RequestedPackage)) {
            Throw-UpdateError "업데이트 패키지를 찾을 수 없습니다: $RequestedPackage"
        }
        return (Resolve-Path -LiteralPath $RequestedPackage).Path
    }

    if ((Test-Path -LiteralPath (Join-Path $PSScriptRoot $script:ManifestName) -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'payload') -PathType Container)) {
        return (Resolve-Path -LiteralPath $PSScriptRoot).Path
    }

    $searchFolders = @($InstallRoot, (Get-Location).Path, $PSScriptRoot)
    if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $searchFolders += (Join-Path $env:USERPROFILE 'Downloads')
    }
    $candidates = New-Object System.Collections.Generic.List[IO.FileInfo]
    $seenFolders = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($folder in $searchFolders) {
        if ((Test-Path -LiteralPath $folder -PathType Container) -and $seenFolders.Add($folder)) {
            foreach ($pattern in @('Nivelle-Update-*.zip', 'Nozomi-Update-0.3.1-to-0.4.0.zip')) {
                foreach ($file in Get-ChildItem -LiteralPath $folder -Filter $pattern -File) {
                    $candidates.Add($file)
                }
            }
        }
    }
    if ($candidates.Count -eq 0) {
        Throw-UpdateError 'Nivelle 업데이트 ZIP을 찾지 못했습니다. -PackagePath로 지정하세요.'
    }
    return ($candidates | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1).FullName
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-PersistentRollbackLauncher {
    param([string]$RelativePath)
    return $RelativePath.Equals('Nivelle-Rollback.cmd', [StringComparison]::OrdinalIgnoreCase) -or
        $RelativePath.Equals('레시아 니벨 롤백.cmd', [StringComparison]::OrdinalIgnoreCase) -or
        $RelativePath.Equals('Nozomi-Rollback.cmd', [StringComparison]::OrdinalIgnoreCase) -or
        $RelativePath.Equals('Nozomi 롤백.cmd', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-FreeSpace {
    param(
        [string]$Path,
        [long]$RequiredBytes,
        [string]$Description
    )
    $driveName = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Path)).TrimEnd('\')
    $drive = New-Object IO.DriveInfo -ArgumentList $driveName
    if (-not $drive.IsReady -or $drive.AvailableFreeSpace -lt $RequiredBytes) {
        $requiredMiB = [Math]::Ceiling($RequiredBytes / 1MB)
        Throw-UpdateError "$Description 디스크 여유 공간이 부족합니다. 최소 ${requiredMiB}MiB가 필요합니다."
    }
}

function Assert-HashString {
    param(
        [object]$Value,
        [string]$Description,
        [switch]$AllowNull
    )
    if ($AllowNull -and $null -eq $Value) {
        return $null
    }
    if (-not ($Value -is [string]) -or ([string]$Value) -notmatch '^[0-9a-f]{64}$') {
        Throw-UpdateError "$Description SHA-256 값이 올바르지 않습니다."
    }
    return [string]$Value
}

function Get-RequiredProperty {
    param(
        [object]$Object,
        [string]$Name
    )
    if ($null -eq $Object -or -not ($Object.PSObject.Properties.Name -contains $Name)) {
        Throw-UpdateError "매니페스트 필드가 누락되었습니다: $Name"
    }
    return $Object.$Name
}

function Read-ManifestTextFromZip {
    param([string]$ZipPath)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        if ($archive.Entries.Count -gt 10000) {
            Throw-UpdateError '업데이트 ZIP의 항목 수가 제한을 초과했습니다.'
        }
        $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        $manifestEntry = $null
        $totalUncompressed = [long]0
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            if ([string]::IsNullOrWhiteSpace($name) -or $name.Contains('\') -or $name.Contains([char]0)) {
                Throw-UpdateError "ZIP에 안전하지 않은 경로가 있습니다: $name"
            }
            $trimmed = $name.TrimEnd('/')
            if (-not [string]::IsNullOrWhiteSpace($trimmed)) {
                Assert-SafeRelativePath -Value $trimmed -DynamicProtectedPrefixes @() | Out-Null
            }
            if (-not $seen.Add($name)) {
                Throw-UpdateError "ZIP에 대소문자만 다른 중복 경로가 있습니다: $name"
            }
            $unixType = (($entry.ExternalAttributes -shr 16) -band 0xF000)
            if ($unixType -eq 0xA000) {
                Throw-UpdateError "ZIP의 심볼릭 링크는 허용되지 않습니다: $name"
            }
            $totalUncompressed += [long]$entry.Length
            if ($totalUncompressed -gt 10739515392) {
                Throw-UpdateError '업데이트 ZIP의 압축 해제 크기가 10GB 제한을 초과했습니다.'
            }
            $allowed = $name.Equals('manifest.json', [StringComparison]::OrdinalIgnoreCase) -or
                $name.Equals('apply_update.ps1', [StringComparison]::OrdinalIgnoreCase) -or
                $name.Equals('Nivelle-Update.cmd', [StringComparison]::OrdinalIgnoreCase) -or
                $name.Equals('Nozomi-Update.cmd', [StringComparison]::OrdinalIgnoreCase) -or
                $name.Equals('payload/', [StringComparison]::OrdinalIgnoreCase) -or
                $name.StartsWith('payload/', [StringComparison]::OrdinalIgnoreCase)
            if (-not $allowed) {
                Throw-UpdateError "ZIP에 허용되지 않은 항목이 있습니다: $name"
            }
            if ($name.Equals('manifest.json', [StringComparison]::OrdinalIgnoreCase)) {
                $manifestEntry = $entry
            }
        }
        if ($null -eq $manifestEntry -or $manifestEntry.Length -gt 1048576) {
            Throw-UpdateError 'ZIP에 유효한 manifest.json이 없습니다.'
        }
        $script:ZipUncompressedBytes = $totalUncompressed
        $stream = $manifestEntry.Open()
        try {
            $utf8 = New-Object Text.UTF8Encoding($false, $true)
            $reader = New-Object IO.StreamReader($stream, $utf8, $true)
            try { return $reader.ReadToEnd() } finally { $reader.Dispose() }
        }
        finally { $stream.Dispose() }
    }
    finally { $archive.Dispose() }
}

function Expand-ValidatedPackage {
    param(
        [string]$ResolvedPackage,
        [string]$StagingRoot
    )

    New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null
    if (Test-Path -LiteralPath $ResolvedPackage -PathType Container) {
        $manifestSource = Join-Path $ResolvedPackage 'manifest.json'
        $payloadSource = Join-Path $ResolvedPackage 'payload'
        if (-not (Test-Path -LiteralPath $manifestSource -PathType Leaf) -or
            -not (Test-Path -LiteralPath $payloadSource -PathType Container)) {
            Throw-UpdateError '압축을 푼 업데이트 폴더에 manifest.json 또는 payload가 없습니다.'
        }
        Assert-NoReparsePoints -Root $ResolvedPackage -RelativePath 'manifest.json'
        Assert-NoReparsePoints -Root $ResolvedPackage -RelativePath 'payload'
        Copy-Item -LiteralPath $manifestSource -Destination (Join-Path $StagingRoot 'manifest.json')
        New-Item -ItemType Directory -Path (Join-Path $StagingRoot 'payload') | Out-Null
        return (Get-Content -LiteralPath $manifestSource -Raw -Encoding UTF8)
    }

    if ([IO.Path]::GetExtension($ResolvedPackage) -ne '.zip') {
        Throw-UpdateError '업데이트 패키지는 ZIP 파일 또는 압축을 푼 패키지 폴더여야 합니다.'
    }
    $manifestText = Read-ManifestTextFromZip -ZipPath $ResolvedPackage
    Assert-FreeSpace -Path $StagingRoot -RequiredBytes ($script:ZipUncompressedBytes + 64MB) -Description '패키지 압축 해제용'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($ResolvedPackage, $StagingRoot)
    return $manifestText
}

function Copy-DirectoryPackagePayload {
    param(
        [string]$PackageRoot,
        [string]$StagingRoot,
        [object[]]$Files
    )

    $payloadRoot = Join-Path $PackageRoot 'payload'
    $actual = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $pending = New-Object System.Collections.Stack
    $pending.Push($payloadRoot)
    while ($pending.Count -gt 0) {
        $directory = [string]$pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Throw-UpdateError "압축 해제 패키지의 링크는 허용되지 않습니다: $($item.FullName)"
            }
            if ($item.PSIsContainer) {
                $pending.Push($item.FullName)
            }
            else {
                $relative = $item.FullName.Substring($payloadRoot.Length).TrimStart('\').Replace('\', '/')
                if (-not $actual.Add($relative)) {
                    Throw-UpdateError "payload에 대소문자만 다른 중복 파일이 있습니다: $relative"
                }
            }
        }
    }
    if ($actual.Count -ne $Files.Count) {
        Throw-UpdateError '압축 해제 패키지의 payload에 manifest에 없는 파일이 있습니다.'
    }

    foreach ($file in $Files) {
        $relative = [string]$file.path
        if (-not $actual.Contains($relative)) {
            Throw-UpdateError "패키지 payload 파일이 없습니다: $relative"
        }
        $sourceRelative = 'payload/' + $relative
        Assert-NoReparsePoints -Root $PackageRoot -RelativePath $sourceRelative
        $source = Get-SafeDestinationPath -Root $PackageRoot -RelativePath $sourceRelative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            Throw-UpdateError "패키지 payload 파일이 없습니다: $relative"
        }
        $destination = Get-SafeDestinationPath -Root (Join-Path $StagingRoot 'payload') -RelativePath $relative
        $parent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
    }
}

function ConvertAndValidate-Manifest {
    param(
        [string]$Json,
        [string[]]$DynamicProtectedPrefixes
    )

    try { $manifest = $Json | ConvertFrom-Json } catch {
        Throw-UpdateError "manifest.json을 읽을 수 없습니다: $($_.Exception.Message)"
    }
    if ($null -eq $manifest) { Throw-UpdateError 'manifest.json이 비어 있습니다.' }

    $format = Get-RequiredProperty -Object $manifest -Name 'format_version'
    if ([int]$format -ne 1) { Throw-UpdateError "지원하지 않는 패치 형식입니다: $format" }
    $product = Get-RequiredProperty -Object $manifest -Name 'product'
    $manifest.from_version = Assert-Version (Get-RequiredProperty $manifest 'from_version') '시작'
    $manifest.to_version = Assert-Version (Get-RequiredProperty $manifest 'to_version') '대상'
    $legacyBridge = (
        $product -is [string] -and
        $product -ceq $script:LegacyProductName -and
        $manifest.from_version -ceq '0.3.1' -and
        $manifest.to_version -ceq '0.4.0'
    )
    if (-not ($product -is [string]) -or
        ($product -cne $script:ProductName -and -not $legacyBridge)) {
        Throw-UpdateError '이 패키지는 Nivelle 업데이트 또는 허용된 0.3.1 호환 브리지가 아닙니다.'
    }
    if ($manifest.from_version -eq $manifest.to_version) {
        Throw-UpdateError '시작 버전과 대상 버전이 같습니다.'
    }

    $filesValue = Get-RequiredProperty $manifest 'files'
    $deletionsValue = Get-RequiredProperty $manifest 'deletions'
    $files = @($filesValue)
    $deletions = @($deletionsValue)
    if ($files.Count -gt 10000 -or $deletions.Count -gt 10000) {
        Throw-UpdateError '매니페스트 파일 수가 제한을 초과했습니다.'
    }

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $totalSize = [long]0
    foreach ($file in $files) {
        $path = Assert-SafeRelativePath (Get-RequiredProperty $file 'path') $DynamicProtectedPrefixes
        if (-not $seen.Add($path)) { Throw-UpdateError "중복된 업데이트 경로입니다: $path" }
        $file.path = $path
        $file.sha256 = Assert-HashString (Get-RequiredProperty $file 'sha256') "$path 대상"
        $file.base_sha256 = Assert-HashString (Get-RequiredProperty $file 'base_sha256') "$path 기존" -AllowNull
        $sizeValue = Get-RequiredProperty $file 'size'
        if (-not ($sizeValue -is [int]) -and -not ($sizeValue -is [long])) {
            Throw-UpdateError "$path 파일 크기가 정수가 아닙니다."
        }
        $size = [long]$sizeValue
        if ($size -lt 0) { Throw-UpdateError "$path 파일 크기가 음수입니다." }
        $file.size = $size
        $totalSize += $size
        if ($totalSize -gt 10737418240) {
            Throw-UpdateError '업데이트 payload 전체 크기가 10GB 제한을 초과했습니다.'
        }
    }
    foreach ($deletion in $deletions) {
        $path = Assert-SafeRelativePath (Get-RequiredProperty $deletion 'path') $DynamicProtectedPrefixes
        if (-not $seen.Add($path)) { Throw-UpdateError "중복된 업데이트/삭제 경로입니다: $path" }
        $deletion.path = $path
        $deletion.base_sha256 = Assert-HashString (
            (Get-RequiredProperty $deletion 'base_sha256')
        ) "$path 기존"
    }

    if (-not ($seen.Contains('VERSION'))) {
        Throw-UpdateError '패치에는 대상 VERSION 파일이 반드시 포함되어야 합니다.'
    }
    return $manifest
}

function Assert-PayloadMatchesManifest {
    param(
        [string]$StagingRoot,
        [object]$Manifest
    )

    $payloadRoot = Join-Path $StagingRoot 'payload'
    $actual = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($item in Get-ChildItem -LiteralPath $payloadRoot -Recurse -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Throw-UpdateError "payload에 링크 또는 연결 지점이 있습니다: $($item.FullName)"
        }
        if (-not $item.PSIsContainer) {
            $relative = $item.FullName.Substring($payloadRoot.Length).TrimStart('\').Replace('\', '/')
            if (-not $actual.Add($relative)) {
                Throw-UpdateError "payload에 중복 파일이 있습니다: $relative"
            }
        }
    }
    if ($actual.Count -ne @($Manifest.files).Count) {
        Throw-UpdateError 'payload 파일 목록과 manifest.json의 files 목록이 다릅니다.'
    }
    foreach ($file in @($Manifest.files)) {
        $path = [string]$file.path
        if (-not $actual.Contains($path)) {
            Throw-UpdateError "payload에 선언된 파일이 없습니다: $path"
        }
        $payloadFile = Get-SafeDestinationPath -Root $payloadRoot -RelativePath $path
        $info = Get-Item -LiteralPath $payloadFile
        if ($info.Length -ne [long]$file.size) {
            Throw-UpdateError "payload 파일 크기가 manifest와 다릅니다: $path"
        }
        if ((Get-Sha256 $payloadFile) -cne [string]$file.sha256) {
            Throw-UpdateError "payload SHA-256이 manifest와 다릅니다: $path"
        }
    }
    $versionPayload = Get-SafeDestinationPath -Root $payloadRoot -RelativePath 'VERSION'
    if ((Get-Content -LiteralPath $versionPayload -Raw).Trim() -cne [string]$Manifest.to_version) {
        Throw-UpdateError 'payload의 VERSION과 manifest의 to_version이 다릅니다.'
    }
}

function Assert-BasePreconditions {
    param(
        [string]$InstallRoot,
        [object]$Manifest
    )

    foreach ($file in @($Manifest.files)) {
        $path = [string]$file.path
        Assert-NoReparsePoints -Root $InstallRoot -RelativePath $path
        $destination = Get-SafeDestinationPath $InstallRoot $path
        $exists = Test-Path -LiteralPath $destination -PathType Leaf
        $currentHash = if ($exists) { Get-Sha256 $destination } else { $null }
        $samePersistentLauncher = $exists -and (Test-PersistentRollbackLauncher $path) -and
            $currentHash -ceq [string]$file.sha256
        if ($null -eq $file.base_sha256) {
            if ($exists -and -not $samePersistentLauncher) {
                Throw-UpdateError "신규 파일이 이미 존재합니다. 사용자 파일 보호를 위해 중단합니다: $path"
            }
        }
        elseif (-not $exists) {
            Throw-UpdateError "업데이트할 기존 파일이 없습니다: $path"
        }
        elseif ($currentHash -cne [string]$file.base_sha256 -and -not $samePersistentLauncher) {
            Throw-UpdateError "기존 파일이 배포본과 다릅니다. 사용자 변경을 덮어쓰지 않습니다: $path"
        }
    }
    foreach ($deletion in @($Manifest.deletions)) {
        $path = [string]$deletion.path
        Assert-NoReparsePoints -Root $InstallRoot -RelativePath $path
        $destination = Get-SafeDestinationPath $InstallRoot $path
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
            Throw-UpdateError "삭제할 기존 파일이 없습니다: $path"
        }
        elseif ((Get-Sha256 $destination) -cne [string]$deletion.base_sha256) {
            Throw-UpdateError "삭제 대상 파일이 배포본과 다릅니다. 사용자 변경을 보호합니다: $path"
        }
    }
}

function Assert-ApplyDiskSpace {
    param(
        [string]$InstallRoot,
        [string]$UpdaterState,
        [object]$Manifest
    )

    $backupBytes = [long]0
    foreach ($item in @($Manifest.files) + @($Manifest.deletions)) {
        $destination = Get-SafeDestinationPath $InstallRoot ([string]$item.path)
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $backupBytes += [long](Get-Item -LiteralPath $destination).Length
        }
    }
    $writeBytes = [long]0
    foreach ($file in @($Manifest.files)) { $writeBytes += [long]$file.size }

    $stateDrive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($UpdaterState))
    $installDrive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($InstallRoot))
    if ($stateDrive.Equals($installDrive, [StringComparison]::OrdinalIgnoreCase)) {
        Assert-FreeSpace $UpdaterState ($backupBytes + $writeBytes + 64MB) '업데이트 및 백업용'
    }
    else {
        Assert-FreeSpace $UpdaterState ($backupBytes + 64MB) '백업용'
        Assert-FreeSpace $InstallRoot ($writeBytes + 64MB) '업데이트 적용용'
    }
}

function Assert-NoNivelleProcesses {
    param([string]$InstallRoot)

    try { $processes = Get-CimInstance Win32_Process -ErrorAction Stop } catch {
        Throw-UpdateError '실행 중인 Nivelle 프로세스를 확인할 수 없어 안전을 위해 중단합니다.'
    }
    $rootNeedle = $InstallRoot.ToLowerInvariant()
    foreach ($process in $processes) {
        $name = [string]$process.Name
        $commandLine = [string]$process.CommandLine
        $lowerName = $name.ToLowerInvariant()
        $lowerCommand = $commandLine.ToLowerInvariant()
        $isNivelleBinary = $lowerName.StartsWith('nivelle') -or
            $lowerName.StartsWith('nozomi') -or $lowerName -eq 'llama-server.exe'
        $isNivellePython = ($lowerName -eq 'python.exe' -or $lowerName -eq 'pythonw.exe') -and
            $lowerCommand.Contains($rootNeedle) -and
            ($lowerCommand.Contains('nivelle.py') -or $lowerCommand.Contains('nivelle_core') -or
             $lowerCommand.Contains('nivelle_link') -or $lowerCommand.Contains('nozomi.py') -or
             $lowerCommand.Contains('nozomi_server') -or $lowerCommand.Contains('nozomi_client'))
        if ($isNivelleBinary -or $isNivellePython) {
            Throw-UpdateError "Nivelle이 실행 중입니다(PID $($process.ProcessId), $name). Core와 Link를 모두 종료한 뒤 다시 시도하세요."
        }
    }
}

function Acquire-RunLocks {
    param([string]$InstallRoot)

    $locks = New-Object System.Collections.Generic.List[IO.FileStream]
    try {
        # Fixed order shared with run_locked.ps1 and rollback_update.ps1.
        foreach ($name in @('.nivelle', '.nozomi')) {
            $lockDirectory = Join-Path $InstallRoot $name
            New-Item -ItemType Directory -Path $lockDirectory -Force | Out-Null
            $locks.Add((New-Object IO.FileStream(
                (Join-Path $lockDirectory 'run.lock'),
                [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )))
        }
        return $locks.ToArray()
    }
    catch {
        for ($index = $locks.Count - 1; $index -ge 0; $index--) { $locks[$index].Dispose() }
        Throw-UpdateError 'Nivelle이 실행 중입니다. Core와 Link를 모두 종료한 뒤 다시 시도하세요.'
    }
}

function Write-JsonAtomic {
    param(
        [string]$Path,
        [object]$Value
    )

    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    $json = $Value | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Add-JournalEntry {
    param(
        [string]$JournalPath,
        [string]$Action,
        [string]$Path
    )
    $line = "{0}`t{1}`t{2}`r`n" -f ([DateTime]::UtcNow.ToString('o')), $Action, $Path
    [IO.File]::AppendAllText($JournalPath, $line, (New-Object Text.UTF8Encoding($false)))
}

function Copy-FileAtomically {
    param(
        [string]$Source,
        [string]$Destination
    )

    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = Join-Path $parent ('.nivelle-update-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        Copy-Item -LiteralPath $Source -Destination $temporary
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function New-UpdateBackup {
    param(
        [string]$InstallRoot,
        [string]$BackupRoot,
        [object]$Manifest,
        [string]$PackageDescription
    )

    $safeFrom = ([string]$Manifest.from_version) -replace '[^0-9A-Za-z._-]', '_'
    $safeTo = ([string]$Manifest.to_version) -replace '[^0-9A-Za-z._-]', '_'
    $name = '{0}-{1}-to-{2}-{3}' -f ([DateTime]::Now.ToString('yyyyMMdd-HHmmss')), $safeFrom, $safeTo, ([guid]::NewGuid().ToString('N').Substring(0, 8))
    $backupDirectory = Join-Path $BackupRoot $name
    $filesDirectory = Join-Path $backupDirectory 'files'
    New-Item -ItemType Directory -Path $filesDirectory -Force | Out-Null

    $records = New-Object System.Collections.Generic.List[object]
    $created = New-Object System.Collections.Generic.List[string]
    $operations = @()
    foreach ($file in @($Manifest.files)) {
        $operations += [pscustomobject]@{ path = [string]$file.path; operation = 'replace' }
    }
    foreach ($deletion in @($Manifest.deletions)) {
        $operations += [pscustomobject]@{ path = [string]$deletion.path; operation = 'delete' }
    }

    foreach ($operation in $operations) {
        $path = [string]$operation.path
        $source = Get-SafeDestinationPath $InstallRoot $path
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $backupFile = Get-SafeDestinationPath $filesDirectory $path
            New-Item -ItemType Directory -Path (Split-Path -Parent $backupFile) -Force | Out-Null
            Copy-Item -LiteralPath $source -Destination $backupFile
            $sourceHash = Get-Sha256 $source
            $backupHash = Get-Sha256 $backupFile
            if ($sourceHash -cne $backupHash) {
                Throw-UpdateError "백업 검증에 실패했습니다: $path"
            }
            $records.Add([pscustomobject]@{
                path = $path
                operation = [string]$operation.operation
                sha256 = $backupHash
                size = [long](Get-Item -LiteralPath $backupFile).Length
            })
        }
        else {
            $created.Add($path)
        }
    }

    $metadata = [pscustomobject]@{
        format_version = 1
        product = $script:ProductName
        kind = 'update-backup'
        install_root = $InstallRoot
        from_version = [string]$Manifest.from_version
        to_version = [string]$Manifest.to_version
        package = $PackageDescription
        created_at = [DateTime]::UtcNow.ToString('o')
        applied_at = $null
        rolled_back_at = $null
        status = 'prepared'
        files = $records.ToArray()
        created_files = $created.ToArray()
        applied_files = @($Manifest.files | ForEach-Object {
            [pscustomobject]@{ path = [string]$_.path; sha256 = [string]$_.sha256; size = [long]$_.size }
        })
        deleted_files = @($Manifest.deletions | ForEach-Object { [string]$_.path })
    }
    Write-JsonAtomic -Path (Join-Path $backupDirectory 'backup.json') -Value $metadata
    Add-JournalEntry -JournalPath (Join-Path $backupDirectory 'journal.log') -Action 'prepared' -Path '-'
    return [pscustomobject]@{
        Directory = $backupDirectory
        Metadata = $metadata
    }
}

function Restore-UpdateBackup {
    param(
        [string]$InstallRoot,
        [object]$Backup,
        [string]$Status
    )

    $filesRoot = Join-Path $Backup.Directory 'files'
    $errors = New-Object System.Collections.Generic.List[string]
    $records = @($Backup.Metadata.files) | Sort-Object @{ Expression = { if ($_.path -eq 'VERSION') { 1 } else { 0 } } }
    foreach ($record in $records) {
        $path = [string]$record.path
        try {
            $source = Get-SafeDestinationPath $filesRoot $path
            $destination = Get-SafeDestinationPath $InstallRoot $path
            if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or
                (Get-Sha256 $source) -cne [string]$record.sha256) {
                Throw-UpdateError "복구 백업이 손상되었습니다: $path"
            }
            if ((Test-Path -LiteralPath $destination -PathType Leaf) -and
                (Get-Sha256 $destination) -ceq [string]$record.sha256) {
                Add-JournalEntry (Join-Path $Backup.Directory 'journal.log') 'restore-skip-unchanged' $path
                continue
            }
            Copy-FileAtomically -Source $source -Destination $destination
            Add-JournalEntry (Join-Path $Backup.Directory 'journal.log') 'restore' $path
        }
        catch {
            $errors.Add("$path`: $($_.Exception.Message)")
        }
    }
    foreach ($pathValue in @($Backup.Metadata.created_files)) {
        $path = [string]$pathValue
        try {
            $destination = Get-SafeDestinationPath $InstallRoot $path
            if (Test-Path -LiteralPath $destination -PathType Leaf) {
                Remove-Item -LiteralPath $destination -Force
            }
            Add-JournalEntry (Join-Path $Backup.Directory 'journal.log') 'remove-created' $path
        }
        catch {
            $errors.Add("$path`: $($_.Exception.Message)")
        }
    }
    if ($errors.Count -gt 0) {
        Throw-UpdateError ("일부 파일을 복구하지 못했습니다: " + ($errors -join ' | '))
    }
    $Backup.Metadata.status = $Status
    Write-JsonAtomic (Join-Path $Backup.Directory 'backup.json') $Backup.Metadata
}

function Invoke-UpdateApply {
    param(
        [string]$InstallRoot,
        [string]$StagingRoot,
        [object]$Manifest,
        [object]$Backup
    )

    $payloadRoot = Join-Path $StagingRoot 'payload'
    $orderedFiles = @($Manifest.files) | Sort-Object @{ Expression = { if ($_.path -eq 'VERSION') { 1 } else { 0 } } }
    foreach ($file in $orderedFiles) {
        $path = [string]$file.path
        if ($path -eq 'VERSION') { continue }
        $source = Get-SafeDestinationPath $payloadRoot $path
        $destination = Get-SafeDestinationPath $InstallRoot $path
        Copy-FileAtomically $source $destination
        Add-JournalEntry (Join-Path $Backup.Directory 'journal.log') 'write' $path
    }
    foreach ($deletion in @($Manifest.deletions)) {
        $path = [string]$deletion.path
        $destination = Get-SafeDestinationPath $InstallRoot $path
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            Remove-Item -LiteralPath $destination -Force
        }
        Add-JournalEntry (Join-Path $Backup.Directory 'journal.log') 'delete' $path
    }
    $versionSource = Get-SafeDestinationPath $payloadRoot 'VERSION'
    $versionDestination = Get-SafeDestinationPath $InstallRoot 'VERSION'
    Copy-FileAtomically $versionSource $versionDestination
    Add-JournalEntry (Join-Path $Backup.Directory 'journal.log') 'write-last' 'VERSION'

    foreach ($file in @($Manifest.files)) {
        $path = [string]$file.path
        $destination = Get-SafeDestinationPath $InstallRoot $path
        if (-not (Test-Path -LiteralPath $destination -PathType Leaf) -or
            (Get-Sha256 $destination) -cne [string]$file.sha256) {
            Throw-UpdateError "적용 후 파일 검증에 실패했습니다: $path"
        }
    }
    foreach ($deletion in @($Manifest.deletions)) {
        if (Test-Path -LiteralPath (Get-SafeDestinationPath $InstallRoot ([string]$deletion.path))) {
            Throw-UpdateError "삭제 대상이 남아 있습니다: $($deletion.path)"
        }
    }
    if ((Get-InstalledVersion $InstallRoot) -cne [string]$Manifest.to_version) {
        Throw-UpdateError '업데이트 후 설치 버전이 대상 버전과 다릅니다.'
    }
}

$stagingDirectory = $null
$runLocks = @()
$backup = $null
$mutationStarted = $false
try {
    $installRoot = Resolve-InstallRoot $TargetRoot
    $dynamicProtected = Get-DynamicProtectedPrefixes $installRoot
    $resolvedPackage = Resolve-UpdatePackage $PackagePath $installRoot

    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        Throw-UpdateError 'LOCALAPPDATA를 확인할 수 없어 안전한 백업을 만들 수 없습니다.'
    }
    $updaterState = Join-Path $env:LOCALAPPDATA 'Nivelle\Updater'
    $backupRoot = Join-Path $updaterState 'backups'
    $stagingRoot = Join-Path $updaterState 'staging'
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
    $stagingDirectory = Join-Path $stagingRoot ([guid]::NewGuid().ToString('N'))

    Write-Info "패키지를 검사합니다: $resolvedPackage"
    $manifestText = Expand-ValidatedPackage $resolvedPackage $stagingDirectory
    $manifest = ConvertAndValidate-Manifest $manifestText $dynamicProtected
    if (Test-Path -LiteralPath $resolvedPackage -PathType Container) {
        Copy-DirectoryPackagePayload $resolvedPackage $stagingDirectory @($manifest.files)
    }
    Assert-PayloadMatchesManifest $stagingDirectory $manifest

    $installedVersion = Get-InstalledVersion $installRoot
    if ($installedVersion -cne [string]$manifest.from_version) {
        Throw-UpdateError "설치 버전($installedVersion)과 패치 시작 버전($($manifest.from_version))이 다릅니다."
    }

    Assert-NoNivelleProcesses $installRoot
    $runLocks = @(Acquire-RunLocks $installRoot)
    Assert-BasePreconditions $installRoot $manifest
    Assert-ApplyDiskSpace $installRoot $updaterState $manifest
    Write-Info '변경되거나 삭제될 기존 파일을 백업합니다.'
    $backup = New-UpdateBackup $installRoot $backupRoot $manifest $resolvedPackage
    $mutationStarted = $true

    Write-Info "$($manifest.from_version) → $($manifest.to_version) 패치를 적용합니다."
    Invoke-UpdateApply $installRoot $stagingDirectory $manifest $backup
    $backup.Metadata.status = 'applied'
    $backup.Metadata.applied_at = [DateTime]::UtcNow.ToString('o')
    Write-JsonAtomic (Join-Path $backup.Directory 'backup.json') $backup.Metadata
    Add-JournalEntry (Join-Path $backup.Directory 'journal.log') 'complete' '-'

    Write-Host "업데이트가 완료되었습니다: $($manifest.to_version)" -ForegroundColor Green
    Write-Host "롤백 백업: $($backup.Directory)"
    exit 0
}
catch {
    $failure = $_.Exception.Message
    if ($mutationStarted -and $null -ne $backup) {
        Write-Host '적용 중 오류가 발생하여 기존 파일을 자동 복구합니다.' -ForegroundColor Yellow
        try {
            Restore-UpdateBackup $installRoot $backup 'failed-restored'
            Write-Host '기존 파일 복구를 완료했습니다.' -ForegroundColor Yellow
        }
        catch {
            $backup.Metadata.status = 'restore-failed'
            try { Write-JsonAtomic (Join-Path $backup.Directory 'backup.json') $backup.Metadata } catch {}
            Write-Host "자동 복구에도 실패했습니다: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "백업을 보존했습니다: $($backup.Directory)" -ForegroundColor Red
        }
    }
    Write-Host $failure -ForegroundColor Red
    exit 1
}
finally {
    for ($index = $runLocks.Count - 1; $index -ge 0; $index--) {
        $runLocks[$index].Dispose()
    }
    if (-not [string]::IsNullOrWhiteSpace($stagingDirectory) -and
        (Test-Path -LiteralPath $stagingDirectory -PathType Container)) {
        $expectedStagingRoot = Join-Path $env:LOCALAPPDATA 'Nivelle\Updater\staging'
        if (Test-ChildPath $expectedStagingRoot $stagingDirectory) {
            Remove-Item -LiteralPath $stagingDirectory -Recurse -Force
        }
    }
}
