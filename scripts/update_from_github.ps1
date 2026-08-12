[CmdletBinding()]
param(
    [string]$Repository = 'seop1000/nozomi_ai',

    [string]$TargetRoot,

    [string]$ApiBaseUrl = 'https://api.github.com',

    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 30,

    [ValidateRange(1048576, 10737418240)]
    [long]$MaxPackageBytes = 2147483648,

    [string]$ProxyUrl,

    [switch]$CheckOnly,

    [switch]$DownloadOnly,

    [switch]$AllowHttpForTesting
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:ProductName = 'Nivelle'
$script:ApiResponseLimit = 2097152
$script:ChecksumLimit = 4096
$script:PartialFiles = New-Object System.Collections.Generic.List[string]
$script:Proxy = $null

function Write-UpdateInfo {
    param([string]$Message)
    Write-Host "[Nivelle 온라인 업데이트] $Message" -ForegroundColor Cyan
}

function Throw-OnlineUpdateError {
    param([string]$Message)
    throw "[Nivelle 온라인 업데이트] $Message"
}

function Test-ChildPath {
    param([string]$Parent, [string]$Child)
    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [IO.Path]::GetFullPath($Child)
    return $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparsePoint {
    param([string]$Path, [string]$Description)
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Throw-OnlineUpdateError "$Description 경로가 링크 또는 연결 지점입니다: $Path"
        }
    }
}

function Resolve-InstallRoot {
    param([string]$RequestedRoot)
    $candidate = $RequestedRoot
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = Split-Path -Parent $PSScriptRoot
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        Throw-OnlineUpdateError "Nivelle 설치 폴더를 찾을 수 없습니다: $candidate"
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path.TrimEnd('\')
    Assert-NoReparsePoint $resolved '설치'
    foreach ($marker in @('VERSION', 'pyproject.toml', 'apps\server', 'apps\client', 'packages')) {
        if (-not (Test-Path -LiteralPath (Join-Path $resolved $marker))) {
            Throw-OnlineUpdateError "올바른 Nivelle 설치본이 아닙니다. 누락: $marker"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $resolved 'nivelle.py') -PathType Leaf) -and
        -not (Test-Path -LiteralPath (Join-Path $resolved 'nozomi.py') -PathType Leaf)) {
        Throw-OnlineUpdateError '설치본에 nivelle.py 또는 0.3.1 호환 nozomi.py가 없습니다.'
    }
    $applyScript = Join-Path $resolved 'scripts\apply_update.ps1'
    if (-not (Test-Path -LiteralPath $applyScript -PathType Leaf)) {
        Throw-OnlineUpdateError '기존 안전 업데이트 적용기 scripts/apply_update.ps1을 찾을 수 없습니다.'
    }
    Assert-NoReparsePoint $applyScript '업데이트 적용기'
    return $resolved
}

function Get-InstalledVersion {
    param([string]$InstallRoot)
    $value = (Get-Content -LiteralPath (Join-Path $InstallRoot 'VERSION') -Raw).Trim()
    Assert-SemVer $value '현재 VERSION' | Out-Null
    return $value
}

function Assert-SemVer {
    param([object]$Value, [string]$Description)
    if (-not ($Value -is [string]) -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        Throw-OnlineUpdateError "$Description 값이 비어 있거나 문자열이 아닙니다."
    }
    $pattern = '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$'
    $match = [regex]::Match([string]$Value, $pattern, [Text.RegularExpressions.RegexOptions]::CultureInvariant)
    if (-not $match.Success) {
        Throw-OnlineUpdateError "$Description SemVer 형식이 올바르지 않습니다: $Value"
    }
    foreach ($index in 1..3) {
        $number = [uint64]0
        if (-not [uint64]::TryParse($match.Groups[$index].Value, [ref]$number)) {
            Throw-OnlineUpdateError "$Description 버전 숫자가 너무 큽니다: $Value"
        }
    }
    if ($match.Groups[4].Success) {
        foreach ($identifier in $match.Groups[4].Value.Split('.')) {
            if ([string]::IsNullOrWhiteSpace($identifier) -or
                ($identifier -match '^\d+$' -and $identifier.Length -gt 1 -and $identifier.StartsWith('0'))) {
                Throw-OnlineUpdateError "$Description 사전 릴리스 형식이 올바르지 않습니다: $Value"
            }
        }
    }
    if ($match.Groups[5].Success -and $match.Groups[5].Value.Split('.') -contains '') {
        Throw-OnlineUpdateError "$Description 빌드 정보 형식이 올바르지 않습니다: $Value"
    }
    return [pscustomobject]@{
        Text = [string]$Value
        Major = [uint64]$match.Groups[1].Value
        Minor = [uint64]$match.Groups[2].Value
        Patch = [uint64]$match.Groups[3].Value
        Prerelease = if ($match.Groups[4].Success) { $match.Groups[4].Value } else { $null }
    }
}

function Compare-NumericIdentifier {
    param([string]$Left, [string]$Right)
    $leftTrimmed = $Left.TrimStart('0')
    $rightTrimmed = $Right.TrimStart('0')
    if ($leftTrimmed.Length -eq 0) { $leftTrimmed = '0' }
    if ($rightTrimmed.Length -eq 0) { $rightTrimmed = '0' }
    if ($leftTrimmed.Length -lt $rightTrimmed.Length) { return -1 }
    if ($leftTrimmed.Length -gt $rightTrimmed.Length) { return 1 }
    return [string]::CompareOrdinal($leftTrimmed, $rightTrimmed)
}

function Compare-SemVer {
    param([string]$Left, [string]$Right)
    $leftVersion = Assert-SemVer $Left '비교할 왼쪽'
    $rightVersion = Assert-SemVer $Right '비교할 오른쪽'
    foreach ($property in @('Major', 'Minor', 'Patch')) {
        if ($leftVersion.$property -lt $rightVersion.$property) { return -1 }
        if ($leftVersion.$property -gt $rightVersion.$property) { return 1 }
    }
    if ($null -eq $leftVersion.Prerelease -and $null -eq $rightVersion.Prerelease) { return 0 }
    if ($null -eq $leftVersion.Prerelease) { return 1 }
    if ($null -eq $rightVersion.Prerelease) { return -1 }
    $leftParts = $leftVersion.Prerelease.Split('.')
    $rightParts = $rightVersion.Prerelease.Split('.')
    $maximum = [Math]::Max($leftParts.Length, $rightParts.Length)
    for ($index = 0; $index -lt $maximum; $index++) {
        if ($index -ge $leftParts.Length) { return -1 }
        if ($index -ge $rightParts.Length) { return 1 }
        $leftNumeric = $leftParts[$index] -match '^\d+$'
        $rightNumeric = $rightParts[$index] -match '^\d+$'
        if ($leftNumeric -and $rightNumeric) {
            $comparison = Compare-NumericIdentifier $leftParts[$index] $rightParts[$index]
        }
        elseif ($leftNumeric) { $comparison = -1 }
        elseif ($rightNumeric) { $comparison = 1 }
        else { $comparison = [string]::CompareOrdinal($leftParts[$index], $rightParts[$index]) }
        if ($comparison -lt 0) { return -1 }
        if ($comparison -gt 0) { return 1 }
    }
    return 0
}

function Assert-RepositoryName {
    param([string]$Value)
    if ($Value -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
        $Value.Contains('..') -or $Value.StartsWith('.') -or $Value.EndsWith('.')) {
        Throw-OnlineUpdateError "GitHub 저장소 이름은 owner/repository 형식이어야 합니다: $Value"
    }
    return $Value
}

function Test-LoopbackHost {
    param([Uri]$Uri)
    return $Uri.IsLoopback -or $Uri.Host.Equals('localhost', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-TransportUri {
    param([Uri]$Uri, [string]$Description)
    if (-not $Uri.IsAbsoluteUri -or -not [string]::IsNullOrWhiteSpace($Uri.UserInfo)) {
        Throw-OnlineUpdateError "$Description 주소가 올바른 절대 주소가 아닙니다: $Uri"
    }
    if ($Uri.Scheme.Equals('https', [StringComparison]::OrdinalIgnoreCase)) { return }
    if ($AllowHttpForTesting -and
        $Uri.Scheme.Equals('http', [StringComparison]::OrdinalIgnoreCase) -and
        (Test-LoopbackHost $Uri)) {
        return
    }
    Throw-OnlineUpdateError "$Description 주소는 HTTPS여야 합니다: $Uri"
}

function Initialize-NetworkSecurity {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if (-not [string]::IsNullOrWhiteSpace($ProxyUrl)) {
        $proxyUri = $null
        if (-not [Uri]::TryCreate($ProxyUrl, [UriKind]::Absolute, [ref]$proxyUri) -or
            ($proxyUri.Scheme -ne 'http' -and $proxyUri.Scheme -ne 'https')) {
            Throw-OnlineUpdateError "프록시 주소가 올바르지 않습니다: $ProxyUrl"
        }
        $webProxy = New-Object Net.WebProxy
        $webProxy.Address = $proxyUri
        $webProxy.BypassProxyOnLocal = $true
        $webProxy.UseDefaultCredentials = $true
        $script:Proxy = $webProxy
    }
}

function New-HttpRequest {
    param([Uri]$Uri, [string]$Accept)
    Assert-TransportUri $Uri '요청'
    $request = [Net.HttpWebRequest]::Create($Uri)
    $request.Method = 'GET'
    $request.UserAgent = 'Nivelle-Updater/0.4'
    $request.Accept = $Accept
    $request.Timeout = $TimeoutSeconds * 1000
    $request.ReadWriteTimeout = $TimeoutSeconds * 1000
    $request.AllowAutoRedirect = $true
    $request.MaximumAutomaticRedirections = 5
    $request.KeepAlive = $false
    $request.Headers['X-GitHub-Api-Version'] = '2026-03-10'
    if ($null -ne $script:Proxy) {
        $request.Proxy = $script:Proxy
    }
    elseif ($null -ne [Net.WebRequest]::DefaultWebProxy) {
        $request.Proxy = [Net.WebRequest]::DefaultWebProxy
        $request.Proxy.Credentials = [Net.CredentialCache]::DefaultCredentials
    }
    return $request
}

function Get-WebExceptionMessage {
    param([Net.WebException]$Exception)
    if ($Exception.Status -eq [Net.WebExceptionStatus]::Timeout) {
        return "요청 시간이 ${TimeoutSeconds}초를 초과했습니다. 네트워크 또는 프록시 설정을 확인하세요."
    }
    $response = $Exception.Response
    if ($null -ne $response -and $response -is [Net.HttpWebResponse]) {
        try {
            $status = [int]$response.StatusCode
            $remaining = $response.Headers['X-RateLimit-Remaining']
            if ($status -eq 403 -and $remaining -eq '0') {
                return 'GitHub 공개 API 요청 한도를 초과했습니다. 잠시 뒤 다시 시도하세요.'
            }
            if ($status -eq 404) {
                return 'GitHub 최신 릴리스를 찾지 못했습니다. 저장소와 공개 릴리스 여부를 확인하세요.'
            }
            return "GitHub 요청이 HTTP $status 상태로 실패했습니다."
        }
        finally {
            $response.Dispose()
        }
    }
    if ($Exception.Status -eq [Net.WebExceptionStatus]::ProxyNameResolutionFailure) {
        return '프록시 주소를 찾을 수 없습니다. 프록시 설정을 확인하세요.'
    }
    if ($Exception.Status -eq [Net.WebExceptionStatus]::TrustFailure) {
        return 'TLS 인증서 검증에 실패했습니다. PC 시간과 보안 프록시 설정을 확인하세요.'
    }
    return "네트워크 요청에 실패했습니다: $($Exception.Message)"
}

function Invoke-HttpDownload {
    param(
        [Uri]$Uri,
        [long]$MaximumBytes,
        [string]$Destination,
        [string]$Accept
    )
    $request = New-HttpRequest $Uri $Accept
    $response = $null
    $inputStream = $null
    $outputStream = $null
    try {
        try { $response = [Net.HttpWebResponse]$request.GetResponse() } catch [Net.WebException] {
            Throw-OnlineUpdateError (Get-WebExceptionMessage $_.Exception)
        }
        Assert-TransportUri $response.ResponseUri '최종 다운로드'
        if ([int]$response.StatusCode -ne 200) {
            Throw-OnlineUpdateError "다운로드가 HTTP $([int]$response.StatusCode) 상태로 실패했습니다."
        }
        if ($response.ContentLength -gt $MaximumBytes) {
            Throw-OnlineUpdateError "다운로드 크기가 허용 한도($MaximumBytes 바이트)를 초과했습니다."
        }
        $inputStream = $response.GetResponseStream()
        $outputStream = New-Object IO.FileStream(
            $Destination,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $buffer = New-Object byte[] 65536
        $total = [long]0
        while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $read
            if ($total -gt $MaximumBytes) {
                Throw-OnlineUpdateError "다운로드 크기가 허용 한도($MaximumBytes 바이트)를 초과했습니다."
            }
            $outputStream.Write($buffer, 0, $read)
        }
        $outputStream.Flush()
        return $total
    }
    finally {
        if ($null -ne $outputStream) { $outputStream.Dispose() }
        if ($null -ne $inputStream) { $inputStream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
    }
}

function Get-GitHubRelease {
    param([string]$RepositoryName, [string]$TemporaryRoot)
    $baseUri = $null
    if (-not [Uri]::TryCreate($ApiBaseUrl.TrimEnd('/'), [UriKind]::Absolute, [ref]$baseUri)) {
        Throw-OnlineUpdateError "GitHub API 주소가 올바르지 않습니다: $ApiBaseUrl"
    }
    Assert-TransportUri $baseUri 'GitHub API'
    if (-not [string]::IsNullOrWhiteSpace($baseUri.Query) -or
        -not [string]::IsNullOrWhiteSpace($baseUri.Fragment)) {
        Throw-OnlineUpdateError 'GitHub API 기본 주소에는 쿼리 또는 fragment를 넣을 수 없습니다.'
    }
    $parts = $RepositoryName.Split('/')
    $endpoint = '{0}/repos/{1}/{2}/releases/latest' -f (
        $ApiBaseUrl.TrimEnd('/'),
        [Uri]::EscapeDataString($parts[0]),
        [Uri]::EscapeDataString($parts[1])
    )
    $temporary = Join-Path $TemporaryRoot ('release-' + [guid]::NewGuid().ToString('N') + '.json.partial')
    $script:PartialFiles.Add($temporary)
    Invoke-HttpDownload ([Uri]$endpoint) $script:ApiResponseLimit $temporary 'application/vnd.github+json' | Out-Null
    try {
        $utf8 = New-Object Text.UTF8Encoding($false, $true)
        $json = [IO.File]::ReadAllText($temporary, $utf8)
        return $json | ConvertFrom-Json
    }
    catch {
        Throw-OnlineUpdateError "GitHub 릴리스 응답을 해석할 수 없습니다: $($_.Exception.Message)"
    }
}

function Resolve-DownloadRoot {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        Throw-OnlineUpdateError 'LOCALAPPDATA를 확인할 수 없어 안전한 다운로드 폴더를 만들 수 없습니다.'
    }
    $localRoot = [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $localRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $localRoot -Force | Out-Null
    }
    Assert-NoReparsePoint $localRoot 'LOCALAPPDATA'
    $current = $localRoot
    foreach ($segment in @('Nivelle', 'Updater', 'downloads')) {
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current -PathType Container)) {
            New-Item -ItemType Directory -Path $current -Force | Out-Null
        }
        Assert-NoReparsePoint $current '다운로드'
    }
    if (-not (Test-ChildPath $localRoot $current)) {
        Throw-OnlineUpdateError '다운로드 폴더가 LOCALAPPDATA를 벗어납니다.'
    }
    return $current
}

function Assert-DownloadSpace {
    param([string]$DownloadRoot, [long]$AssetBytes)
    $driveName = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($DownloadRoot)).TrimEnd('\')
    $drive = New-Object IO.DriveInfo -ArgumentList $driveName
    $required = $AssetBytes + 64MB
    if (-not $drive.IsReady -or $drive.AvailableFreeSpace -lt $required) {
        $requiredMiB = [Math]::Ceiling($required / 1MB)
        Throw-OnlineUpdateError "패치 다운로드 공간이 부족합니다. 최소 ${requiredMiB}MiB가 필요합니다."
    }
}

function Get-ReleaseSelection {
    param([object]$Release, [string]$CurrentVersion)
    if ($null -eq $Release) { Throw-OnlineUpdateError 'GitHub 릴리스 응답이 비어 있습니다.' }
    if (($Release.PSObject.Properties.Name -contains 'draft') -and [bool]$Release.draft) {
        Throw-OnlineUpdateError 'GitHub 최신 릴리스가 초안 상태라서 적용할 수 없습니다.'
    }
    if (($Release.PSObject.Properties.Name -contains 'prerelease') -and [bool]$Release.prerelease) {
        Throw-OnlineUpdateError '시험판 릴리스는 자동 업데이트로 적용하지 않습니다.'
    }
    if (-not ($Release.PSObject.Properties.Name -contains 'tag_name') -or
        -not ($Release.tag_name -is [string])) {
        Throw-OnlineUpdateError 'GitHub 릴리스에 tag_name이 없습니다.'
    }
    $targetVersion = ([string]$Release.tag_name).Trim()
    if ($targetVersion.StartsWith('v', [StringComparison]::OrdinalIgnoreCase)) {
        $targetVersion = $targetVersion.Substring(1)
    }
    Assert-SemVer $targetVersion 'GitHub 릴리스 태그' | Out-Null
    $comparison = Compare-SemVer $targetVersion $CurrentVersion
    if ($comparison -le 0) {
        return [pscustomobject]@{ UpdateAvailable = $false; TargetVersion = $targetVersion }
    }

    $legacyBridge = ($CurrentVersion -ceq '0.3.1' -and $targetVersion -ceq '0.4.0')
    $assetProduct = if ($legacyBridge) { 'Nozomi' } else { $script:ProductName }
    $packageName = "$assetProduct-Update-$CurrentVersion-to-$targetVersion.zip"
    $checksumName = $packageName + '.sha256'
    if (-not ($Release.PSObject.Properties.Name -contains 'assets')) {
        Throw-OnlineUpdateError 'GitHub 릴리스에 업데이트 자산 목록이 없습니다.'
    }
    $packageMatches = @($Release.assets | Where-Object {
        $_.name -is [string] -and ([string]$_.name).Equals($packageName, [StringComparison]::Ordinal)
    })
    $checksumMatches = @($Release.assets | Where-Object {
        $_.name -is [string] -and ([string]$_.name).Equals($checksumName, [StringComparison]::Ordinal)
    })
    if ($packageMatches.Count -ne 1 -or $checksumMatches.Count -ne 1) {
        Throw-OnlineUpdateError "현재 버전용 패치와 SHA-256 자산을 정확히 한 쌍 찾지 못했습니다: $packageName"
    }
    foreach ($asset in @($packageMatches[0], $checksumMatches[0])) {
        if (-not ($asset.PSObject.Properties.Name -contains 'browser_download_url') -or
            -not ($asset.browser_download_url -is [string])) {
            Throw-OnlineUpdateError "릴리스 자산 다운로드 주소가 없습니다: $($asset.name)"
        }
        if (-not ($asset.PSObject.Properties.Name -contains 'size') -or
            -not ($asset.size -is [int]) -and -not ($asset.size -is [long])) {
            Throw-OnlineUpdateError "릴리스 자산 크기가 올바르지 않습니다: $($asset.name)"
        }
        if (($asset.PSObject.Properties.Name -contains 'state') -and $asset.state -cne 'uploaded') {
            Throw-OnlineUpdateError "릴리스 자산 업로드가 완료되지 않았습니다: $($asset.name)"
        }
        $assetUri = $null
        if (-not [Uri]::TryCreate(
                [string]$asset.browser_download_url,
                [UriKind]::Absolute,
                [ref]$assetUri
            )) {
            Throw-OnlineUpdateError "릴리스 자산 다운로드 주소가 올바르지 않습니다: $($asset.name)"
        }
        Assert-TransportUri $assetUri '릴리스 자산'
    }
    if ([long]$packageMatches[0].size -le 0 -or [long]$packageMatches[0].size -gt $MaxPackageBytes) {
        Throw-OnlineUpdateError "패치 자산 크기가 허용 범위를 벗어납니다: $($packageMatches[0].size) 바이트"
    }
    if ([long]$checksumMatches[0].size -le 0 -or [long]$checksumMatches[0].size -gt $script:ChecksumLimit) {
        Throw-OnlineUpdateError 'SHA-256 자산 크기가 허용 범위를 벗어납니다.'
    }
    $packageDigest = Get-OptionalAssetDigest $packageMatches[0]
    $checksumDigest = Get-OptionalAssetDigest $checksumMatches[0]
    return [pscustomobject]@{
        UpdateAvailable = $true
        TargetVersion = $targetVersion
        PackageName = $packageName
        PackageAsset = $packageMatches[0]
        ChecksumName = $checksumName
        ChecksumAsset = $checksumMatches[0]
        PackageDigest = $packageDigest
        ChecksumDigest = $checksumDigest
        ReleaseUrl = if ($Release.PSObject.Properties.Name -contains 'html_url') { [string]$Release.html_url } else { '' }
    }
}

function Get-OptionalAssetDigest {
    param([object]$Asset)
    if (-not ($Asset.PSObject.Properties.Name -contains 'digest') -or
        $null -eq $Asset.digest -or [string]::IsNullOrWhiteSpace([string]$Asset.digest)) {
        return $null
    }
    if (-not ($Asset.digest -is [string])) {
        Throw-OnlineUpdateError "GitHub 자산 digest 형식이 올바르지 않습니다: $($Asset.name)"
    }
    $match = [regex]::Match(
        [string]$Asset.digest,
        '^sha256:([0-9a-fA-F]{64})$',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $match.Success) {
        Throw-OnlineUpdateError "지원하지 않거나 잘못된 GitHub 자산 digest입니다: $($Asset.name)"
    }
    return $match.Groups[1].Value.ToLowerInvariant()
}

function Read-ExpectedChecksum {
    param([string]$ChecksumPath, [string]$PackageName)
    $utf8 = New-Object Text.UTF8Encoding($false, $true)
    $content = [IO.File]::ReadAllText($ChecksumPath, $utf8).Trim()
    $match = [regex]::Match(
        $content,
        '^([0-9a-fA-F]{64}) [ *]([^\r\n]+)$',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $match.Success -or
        -not $match.Groups[2].Value.Equals($PackageName, [StringComparison]::Ordinal)) {
        Throw-OnlineUpdateError 'SHA-256 자산 내용 또는 대상 파일 이름이 올바르지 않습니다.'
    }
    return $match.Groups[1].Value.ToLowerInvariant()
}

function Move-VerifiedDownload {
    param([string]$Source, [string]$Destination)
    $parent = Split-Path -Parent $Destination
    if (-not (Test-ChildPath $parent $Source)) {
        Throw-OnlineUpdateError '임시 다운로드 파일이 안전한 다운로드 폴더를 벗어났습니다.'
    }
    Move-Item -LiteralPath $Source -Destination $Destination -Force
    [void]$script:PartialFiles.Remove($Source)
}

$downloadRoot = $null
try {
    if ($CheckOnly -and $DownloadOnly) {
        Throw-OnlineUpdateError '-CheckOnly와 -DownloadOnly는 함께 사용할 수 없습니다.'
    }
    $installRoot = Resolve-InstallRoot $TargetRoot
    $currentVersion = Get-InstalledVersion $installRoot
    $repositoryName = Assert-RepositoryName $Repository
    Initialize-NetworkSecurity
    $downloadRoot = Resolve-DownloadRoot

    Write-UpdateInfo "$repositoryName 최신 공개 릴리스를 확인합니다. 현재 버전: $currentVersion"
    $release = Get-GitHubRelease $repositoryName $downloadRoot
    $selection = Get-ReleaseSelection $release $currentVersion
    if (-not $selection.UpdateAvailable) {
        Write-Host "이미 최신 버전입니다. 현재 $currentVersion / 릴리스 $($selection.TargetVersion)" -ForegroundColor Green
        exit 0
    }

    Write-Host "업데이트를 찾았습니다: $currentVersion → $($selection.TargetVersion)" -ForegroundColor Green
    if (-not [string]::IsNullOrWhiteSpace($selection.ReleaseUrl)) {
        Write-Host "릴리스: $($selection.ReleaseUrl)"
    }
    if ($CheckOnly) { exit 0 }

    Assert-DownloadSpace $downloadRoot ([long]$selection.PackageAsset.size + [long]$selection.ChecksumAsset.size)

    $checksumPartial = Join-Path $downloadRoot ($selection.ChecksumName + '.' + [guid]::NewGuid().ToString('N') + '.partial')
    $script:PartialFiles.Add($checksumPartial)
    Write-UpdateInfo 'SHA-256 자산을 내려받습니다.'
    $checksumLength = Invoke-HttpDownload (
        [Uri][string]$selection.ChecksumAsset.browser_download_url
    ) $script:ChecksumLimit $checksumPartial 'text/plain'
    if ($checksumLength -ne [long]$selection.ChecksumAsset.size) {
        Throw-OnlineUpdateError 'SHA-256 자산의 실제 크기가 GitHub 메타데이터와 다릅니다.'
    }
    if ($null -ne $selection.ChecksumDigest) {
        $actualChecksumDigest = (Get-FileHash -LiteralPath $checksumPartial -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualChecksumDigest -cne [string]$selection.ChecksumDigest) {
            Throw-OnlineUpdateError 'SHA-256 자산이 GitHub asset digest와 다릅니다.'
        }
    }
    $expectedHash = Read-ExpectedChecksum $checksumPartial $selection.PackageName
    if ($null -ne $selection.PackageDigest -and
        $expectedHash -cne [string]$selection.PackageDigest) {
        Throw-OnlineUpdateError '릴리스 sidecar SHA-256과 GitHub package asset digest가 서로 다릅니다.'
    }

    $packagePartial = Join-Path $downloadRoot ($selection.PackageName + '.' + [guid]::NewGuid().ToString('N') + '.partial')
    $script:PartialFiles.Add($packagePartial)
    Write-UpdateInfo "$($selection.PackageName)을 내려받습니다."
    $packageLength = Invoke-HttpDownload (
        [Uri][string]$selection.PackageAsset.browser_download_url
    ) $MaxPackageBytes $packagePartial 'application/octet-stream'
    if ($packageLength -ne [long]$selection.PackageAsset.size) {
        Throw-OnlineUpdateError '패치 ZIP의 실제 크기가 GitHub 메타데이터와 다릅니다.'
    }
    $actualHash = (Get-FileHash -LiteralPath $packagePartial -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -cne $expectedHash) {
        Throw-OnlineUpdateError '패치 ZIP의 SHA-256이 릴리스 체크섬과 다릅니다. 파일을 적용하지 않습니다.'
    }

    $packagePath = Join-Path $downloadRoot $selection.PackageName
    $checksumPath = Join-Path $downloadRoot $selection.ChecksumName
    Move-VerifiedDownload $packagePartial $packagePath
    Move-VerifiedDownload $checksumPartial $checksumPath
    Write-Host "검증된 패치를 저장했습니다: $packagePath" -ForegroundColor Green
    if ($DownloadOnly) { exit 0 }

    Write-Host '이제 업데이트를 적용합니다. 실행 중인 Nivelle Core·Link·llama-server가 있으면 안전 적용기가 중단합니다.' -ForegroundColor Yellow
    $applyScript = Join-Path $installRoot 'scripts\apply_update.ps1'
    & $applyScript -PackagePath $packagePath -TargetRoot $installRoot
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (-not [string]::IsNullOrWhiteSpace($downloadRoot)) {
        Write-Host "다운로드 폴더: $downloadRoot" -ForegroundColor DarkGray
    }
    exit 1
}
finally {
    foreach ($partial in $script:PartialFiles.ToArray()) {
        if (-not [string]::IsNullOrWhiteSpace($partial) -and
            -not [string]::IsNullOrWhiteSpace($downloadRoot) -and
            (Test-Path -LiteralPath $partial -PathType Leaf) -and
            (Test-ChildPath $downloadRoot $partial)) {
            Remove-Item -LiteralPath $partial -Force
        }
    }
}
