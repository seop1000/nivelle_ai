[CmdletBinding(DefaultParameterSetName = 'Latest')]
param(
    [Parameter(ParameterSetName = 'Path', Mandatory = $true)]
    [string]$BackupPath,

    [Parameter(ParameterSetName = 'Latest')]
    [switch]$Latest,

    [string]$TargetRoot
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Throw-RollbackError {
    param([string]$Message)
    throw "[Nivelle 롤백] $Message"
}

function Test-ChildPath {
    param([string]$Parent, [string]$Child)
    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $childFull = [IO.Path]::GetFullPath($Child)
    return $childFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)
}

function Get-SafePath {
    param([string]$Root, [string]$RelativePath)
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or $RelativePath.Contains('\') -or
        $RelativePath.Contains(':') -or $RelativePath.StartsWith('/') -or
        $RelativePath.Contains('//') -or $RelativePath.Contains([char]0)) {
        Throw-RollbackError "안전하지 않은 백업 경로입니다: $RelativePath"
    }
    $segments = $RelativePath.Split('/')
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq '.' -or $segment -eq '..' -or
            $segment.EndsWith('.') -or $segment.EndsWith(' ') -or
            $segment.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
            Throw-RollbackError "안전하지 않은 백업 경로입니다: $RelativePath"
        }
    }
    $destination = [IO.Path]::GetFullPath((Join-Path $Root $RelativePath.Replace('/', '\')))
    if (-not (Test-ChildPath $Root $destination)) {
        Throw-RollbackError "백업 경로가 허용 범위를 벗어납니다: $RelativePath"
    }
    return $destination
}

function Assert-AllowedRestorePath {
    param([string]$RelativePath, [string[]]$DynamicProtectedPrefixes)
    $segments = $RelativePath.Split('/')
    $first = $segments[0]
    $protected = @(
        '.git', '.nivelle', '.nozomi', '.venv', 'backup', 'backups', 'build', 'dist', 'runtime',
        'temp', 'tmp', 'update', 'updates', 'data', 'userdata', 'logs', 'models'
    )
    foreach ($name in $protected) {
        if ($first.Equals($name, [StringComparison]::OrdinalIgnoreCase)) {
            Throw-RollbackError "보호된 경로는 롤백할 수 없습니다: $RelativePath"
        }
    }
    $leaf = $segments[$segments.Length - 1]
    if ($first.StartsWith('.venv.broken-', [StringComparison]::OrdinalIgnoreCase) -or
        $first.StartsWith('.tmp.', [StringComparison]::OrdinalIgnoreCase) -or
        $leaf.Equals('.env', [StringComparison]::OrdinalIgnoreCase) -or
        $leaf -match '(?i)(\.db(?:-(?:wal|shm|journal))?|\.sqlite3?|\.log|\.gguf|\.part|\.tmp|\.bak)$') {
        Throw-RollbackError "사용자 또는 런타임 경로는 롤백할 수 없습니다: $RelativePath"
    }
    if ($first.Equals('config', [StringComparison]::OrdinalIgnoreCase) -and
        ($segments.Length -lt 3 -or -not $segments[1].Equals('examples', [StringComparison]::OrdinalIgnoreCase))) {
        Throw-RollbackError "config에서는 config/examples 아래만 롤백할 수 있습니다: $RelativePath"
    }
    foreach ($prefix in $DynamicProtectedPrefixes) {
        if ($RelativePath.Equals($prefix, [StringComparison]::OrdinalIgnoreCase) -or
            $RelativePath.StartsWith($prefix + '/', [StringComparison]::OrdinalIgnoreCase)) {
            Throw-RollbackError "사용자 데이터 경로는 롤백할 수 없습니다: $RelativePath"
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
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nozomi\NozomiServer'))
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nozomi\NozomiClient'))
        $paths.Add((Join-Path $env:LOCALAPPDATA 'Nozomi\Updater'))
    }
    if (-not [string]::IsNullOrWhiteSpace($env:NIVELLE_CORE_DATA_DIR)) { $paths.Add($env:NIVELLE_CORE_DATA_DIR) }
    if (-not [string]::IsNullOrWhiteSpace($env:NIVELLE_LINK_DATA_DIR)) { $paths.Add($env:NIVELLE_LINK_DATA_DIR) }
    if (-not [string]::IsNullOrWhiteSpace($env:NOZOMI_SERVER_DATA_DIR)) { $paths.Add($env:NOZOMI_SERVER_DATA_DIR) }
    if (-not [string]::IsNullOrWhiteSpace($env:NOZOMI_CLIENT_DATA_DIR)) { $paths.Add($env:NOZOMI_CLIENT_DATA_DIR) }
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($path in $paths) {
        try {
            $full = [IO.Path]::GetFullPath($path).TrimEnd('\')
            if (Test-ChildPath $InstallRoot $full) {
                $relative = $full.Substring([IO.Path]::GetFullPath($InstallRoot).TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
                if (-not [string]::IsNullOrWhiteSpace($relative)) { $result.Add($relative) }
            }
        }
        catch { Throw-RollbackError "사용자 데이터 경로를 확인할 수 없습니다: $path" }
    }
    return $result.ToArray()
}

function Resolve-InstallRoot {
    param([string]$RequestedRoot)
    $candidate = $RequestedRoot
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        if (-not [string]::IsNullOrWhiteSpace($env:NIVELLE_ROLLBACK_ORIGINAL_ROOT)) {
            $candidate = $env:NIVELLE_ROLLBACK_ORIGINAL_ROOT
        }
        elseif (-not [string]::IsNullOrWhiteSpace($env:NOZOMI_ROLLBACK_ORIGINAL_ROOT)) {
            $candidate = $env:NOZOMI_ROLLBACK_ORIGINAL_ROOT
        }
        else {
            $candidate = Split-Path -Parent $PSScriptRoot
        }
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        Throw-RollbackError "Nivelle 설치 폴더를 찾을 수 없습니다: $candidate"
    }
    $root = (Resolve-Path -LiteralPath $candidate).Path.TrimEnd('\')
    foreach ($marker in @('pyproject.toml', 'apps\server', 'apps\client', 'packages')) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $marker))) {
            Throw-RollbackError "올바른 Nivelle 설치본이 아닙니다. 누락: $marker"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'nivelle.py') -PathType Leaf) -and
        -not (Test-Path -LiteralPath (Join-Path $root 'nozomi.py') -PathType Leaf)) {
        Throw-RollbackError '설치본에 nivelle.py 또는 0.3.1 호환 nozomi.py가 없습니다.'
    }
    return $root
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

function Assert-NoReparsePoints {
    param([string]$Root, [string]$RelativePath)
    $item = Get-Item -LiteralPath $Root -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Throw-RollbackError "설치 폴더가 링크 또는 연결 지점입니다: $Root"
    }
    $current = $Root
    foreach ($segment in $RelativePath.Split('/')) {
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Throw-RollbackError "링크 또는 연결 지점을 통과할 수 없습니다: $RelativePath"
            }
        }
    }
}

function Assert-NoNivelleProcesses {
    param([string]$InstallRoot)
    try { $processes = Get-CimInstance Win32_Process -ErrorAction Stop } catch {
        Throw-RollbackError '실행 중인 Nivelle 프로세스를 확인할 수 없어 안전을 위해 중단합니다.'
    }
    $needle = $InstallRoot.ToLowerInvariant()
    foreach ($process in $processes) {
        $name = ([string]$process.Name).ToLowerInvariant()
        $command = ([string]$process.CommandLine).ToLowerInvariant()
        $binary = $name.StartsWith('nivelle') -or $name.StartsWith('nozomi') -or
            $name -eq 'llama-server.exe'
        $python = ($name -eq 'python.exe' -or $name -eq 'pythonw.exe') -and
            $command.Contains($needle) -and
            ($command.Contains('nivelle.py') -or $command.Contains('nivelle_core') -or
             $command.Contains('nivelle_link') -or $command.Contains('nozomi.py') -or
             $command.Contains('nozomi_server') -or $command.Contains('nozomi_client'))
        if ($binary -or $python) {
            Throw-RollbackError "Nivelle이 실행 중입니다(PID $($process.ProcessId)). 모두 종료한 뒤 다시 시도하세요."
        }
    }
}

function Acquire-RunLocks {
    param([string]$InstallRoot)
    $locks = New-Object System.Collections.Generic.List[IO.FileStream]
    try {
        foreach ($name in @('.nivelle', '.nozomi')) {
            $directory = Join-Path $InstallRoot $name
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
            $locks.Add((New-Object IO.FileStream(
                (Join-Path $directory 'run.lock'),
                [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )))
        }
        return $locks.ToArray()
    }
    catch {
        for ($index = $locks.Count - 1; $index -ge 0; $index--) { $locks[$index].Dispose() }
        Throw-RollbackError 'Nivelle이 실행 중입니다. 모두 종료한 뒤 다시 시도하세요.'
    }
}

function Write-JsonAtomic {
    param([string]$Path, [object]$Value)
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText(
        $temporary,
        ($Value | ConvertTo-Json -Depth 12),
        (New-Object Text.UTF8Encoding($false))
    )
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Add-JournalEntry {
    param([string]$JournalPath, [string]$Action, [string]$Path)
    $line = "{0}`t{1}`t{2}`r`n" -f ([DateTime]::UtcNow.ToString('o')), $Action, $Path
    [IO.File]::AppendAllText($JournalPath, $line, (New-Object Text.UTF8Encoding($false)))
}

function Copy-FileAtomically {
    param([string]$Source, [string]$Destination)
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = Join-Path $parent ('.nivelle-rollback-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        Copy-Item -LiteralPath $Source -Destination $temporary
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Resolve-BackupDirectory {
    param(
        [string]$UpdaterBackupRoot,
        [string]$RequestedBackup,
        [switch]$UseLatest,
        [string]$InstallRoot
    )
    if (-not [string]::IsNullOrWhiteSpace($RequestedBackup)) {
        if (-not (Test-Path -LiteralPath $RequestedBackup -PathType Container)) {
            Throw-RollbackError "백업 폴더를 찾을 수 없습니다: $RequestedBackup"
        }
        $resolved = (Resolve-Path -LiteralPath $RequestedBackup).Path
        if (-not (Test-ChildPath $UpdaterBackupRoot $resolved)) {
            Throw-RollbackError '지정한 백업은 Nivelle 업데이트 백업 폴더 안에 있지 않습니다.'
        }
        return $resolved
    }
    if (-not $UseLatest) {
        Throw-RollbackError '-Latest 또는 -BackupPath를 명시해야 합니다.'
    }
    $valid = New-Object System.Collections.Generic.List[object]
    if (Test-Path -LiteralPath $UpdaterBackupRoot -PathType Container) {
        foreach ($directory in Get-ChildItem -LiteralPath $UpdaterBackupRoot -Directory) {
            $metadataPath = Join-Path $directory.FullName 'backup.json'
            if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
                try {
                    $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    $metadataRoot = [IO.Path]::GetFullPath([string]$metadata.install_root).TrimEnd('\')
                    $expectedRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
                    if (($metadata.product -ceq 'Nivelle' -or $metadata.product -ceq 'Nozomi') -and
                        $metadata.kind -ceq 'update-backup' -and
                        $metadata.status -ceq 'applied' -and
                        $metadataRoot.Equals($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
                        $valid.Add([pscustomobject]@{ Directory = $directory.FullName; Metadata = $metadata })
                    }
                }
                catch { }
            }
        }
    }
    if ($valid.Count -eq 0) { Throw-RollbackError '롤백 가능한 성공 백업이 없습니다.' }
    return ($valid | Sort-Object { [DateTime]$_.Metadata.applied_at } -Descending | Select-Object -First 1).Directory
}

function Validate-BackupMetadata {
    param([string]$BackupDirectory, [string]$InstallRoot, [string[]]$DynamicProtectedPrefixes)
    $metadataPath = Join-Path $BackupDirectory 'backup.json'
    try { $metadata = Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch {
        Throw-RollbackError "backup.json을 읽을 수 없습니다: $($_.Exception.Message)"
    }
    if ($metadata.format_version -ne 1 -or
        ($metadata.product -cne 'Nivelle' -and $metadata.product -cne 'Nozomi') -or
        $metadata.kind -cne 'update-backup' -or $metadata.status -cne 'applied') {
        Throw-RollbackError '선택한 백업은 롤백 가능한 Nivelle 업데이트 백업이 아닙니다.'
    }
    if (-not ([IO.Path]::GetFullPath([string]$metadata.install_root).TrimEnd('\')).Equals(
        [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        Throw-RollbackError '백업이 생성된 설치 폴더와 현재 대상 폴더가 다릅니다.'
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($record in @($metadata.files)) {
        $path = [string]$record.path
        Get-SafePath $InstallRoot $path | Out-Null
        Assert-AllowedRestorePath $path $DynamicProtectedPrefixes
        if (-not $seen.Add($path)) { Throw-RollbackError "백업에 중복 경로가 있습니다: $path" }
        if (-not ([string]$record.sha256 -match '^[0-9a-f]{64}$')) {
            Throw-RollbackError "백업 SHA-256이 올바르지 않습니다: $path"
        }
        $source = Get-SafePath (Join-Path $BackupDirectory 'files') $path
        if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or
            (Get-Item -LiteralPath $source).Length -ne [long]$record.size -or
            (Get-Sha256 $source) -cne [string]$record.sha256) {
            Throw-RollbackError "백업 파일이 없거나 손상되었습니다: $path"
        }
    }
    foreach ($pathValue in @($metadata.created_files)) {
        $path = [string]$pathValue
        Get-SafePath $InstallRoot $path | Out-Null
        Assert-AllowedRestorePath $path $DynamicProtectedPrefixes
        if (-not $seen.Add($path)) { Throw-RollbackError "백업에 중복 경로가 있습니다: $path" }
    }
    if (-not ($metadata.PSObject.Properties.Name -contains 'applied_files') -or
        -not ($metadata.PSObject.Properties.Name -contains 'deleted_files')) {
        Throw-RollbackError '백업에 롤백 충돌 검사 정보가 없습니다.'
    }
    foreach ($record in @($metadata.applied_files)) {
        $path = [string]$record.path
        Get-SafePath $InstallRoot $path | Out-Null
        Assert-AllowedRestorePath $path $DynamicProtectedPrefixes
        if (-not ([string]$record.sha256 -match '^[0-9a-f]{64}$')) {
            Throw-RollbackError "적용 파일 SHA-256이 올바르지 않습니다: $path"
        }
    }
    foreach ($pathValue in @($metadata.deleted_files)) {
        $path = [string]$pathValue
        Get-SafePath $InstallRoot $path | Out-Null
        Assert-AllowedRestorePath $path $DynamicProtectedPrefixes
    }
    return $metadata
}

function Assert-PostUpdateState {
    param([string]$InstallRoot, [object]$Metadata)
    foreach ($record in @($Metadata.applied_files)) {
        $path = [string]$record.path
        Assert-NoReparsePoints $InstallRoot $path
        $current = Get-SafePath $InstallRoot $path
        if (-not (Test-Path -LiteralPath $current -PathType Leaf) -or
            (Get-Sha256 $current) -cne [string]$record.sha256) {
            Throw-RollbackError "업데이트 후 파일이 수정되었습니다. 수동 변경을 보호하기 위해 롤백을 중단합니다: $path"
        }
    }
    foreach ($pathValue in @($Metadata.deleted_files)) {
        $path = [string]$pathValue
        Assert-NoReparsePoints $InstallRoot $path
        if (Test-Path -LiteralPath (Get-SafePath $InstallRoot $path)) {
            Throw-RollbackError "업데이트 후 삭제 경로에 새 파일이 생겼습니다. 롤백을 중단합니다: $path"
        }
    }
}

function New-RollbackRecovery {
    param([string]$InstallRoot, [string]$RecoveryRoot, [object]$Metadata)
    New-Item -ItemType Directory -Path $RecoveryRoot -Force | Out-Null
    $filesRoot = Join-Path $RecoveryRoot 'files'
    New-Item -ItemType Directory -Path $filesRoot | Out-Null
    $existing = New-Object System.Collections.Generic.List[object]
    $missing = New-Object System.Collections.Generic.List[string]
    $allPaths = @($Metadata.files | ForEach-Object { [string]$_.path }) + @($Metadata.created_files)
    foreach ($path in $allPaths) {
        Assert-NoReparsePoints $InstallRoot $path
        $source = Get-SafePath $InstallRoot $path
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $destination = Get-SafePath $filesRoot $path
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            Copy-Item -LiteralPath $source -Destination $destination
            $existing.Add([pscustomobject]@{ path = $path; sha256 = Get-Sha256 $destination })
        }
        else { $missing.Add($path) }
    }
    return [pscustomobject]@{ Directory = $RecoveryRoot; Existing = $existing.ToArray(); Missing = $missing.ToArray() }
}

function Restore-RollbackRecovery {
    param([string]$InstallRoot, [object]$Recovery)
    $filesRoot = Join-Path $Recovery.Directory 'files'
    foreach ($record in @($Recovery.Existing)) {
        $source = Get-SafePath $filesRoot ([string]$record.path)
        if ((Get-Sha256 $source) -cne [string]$record.sha256) {
            Throw-RollbackError "롤백 복구 파일이 손상되었습니다: $($record.path)"
        }
        Copy-FileAtomically $source (Get-SafePath $InstallRoot ([string]$record.path))
    }
    foreach ($pathValue in @($Recovery.Missing)) {
        $destination = Get-SafePath $InstallRoot ([string]$pathValue)
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            Remove-Item -LiteralPath $destination -Force
        }
    }
}

$runLocks = @()
$recovery = $null
$metadata = $null
$backupDirectory = $null
try {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        Throw-RollbackError 'LOCALAPPDATA를 확인할 수 없습니다.'
    }
    $installRoot = Resolve-InstallRoot $TargetRoot
    $dynamicProtected = Get-DynamicProtectedPrefixes $installRoot
    $backupRoot = Join-Path $env:LOCALAPPDATA 'Nivelle\Updater\backups'
    $backupDirectory = Resolve-BackupDirectory $backupRoot $BackupPath -UseLatest:$Latest -InstallRoot $installRoot
    $metadata = Validate-BackupMetadata $backupDirectory $installRoot $dynamicProtected

    $currentVersion = if (Test-Path -LiteralPath (Join-Path $installRoot 'VERSION')) {
        (Get-Content -LiteralPath (Join-Path $installRoot 'VERSION') -Raw).Trim()
    } else { '' }
    if ($currentVersion -cne [string]$metadata.to_version) {
        Throw-RollbackError "현재 버전($currentVersion)이 백업의 적용 버전($($metadata.to_version))과 다릅니다."
    }

    Assert-NoNivelleProcesses $installRoot
    $runLocks = @(Acquire-RunLocks $installRoot)
    Assert-PostUpdateState $installRoot $metadata
    $recoveryRoot = Join-Path $env:LOCALAPPDATA ('Nivelle\Updater\rollback-temp\' + [guid]::NewGuid().ToString('N'))
    $recovery = New-RollbackRecovery $installRoot $recoveryRoot $metadata

    Write-Host "[Nivelle 롤백] $($metadata.to_version) → $($metadata.from_version) 복원을 시작합니다." -ForegroundColor Cyan
    $filesRoot = Join-Path $backupDirectory 'files'
    $ordered = @($metadata.files) | Sort-Object @{ Expression = { if ($_.path -eq 'VERSION') { 1 } else { 0 } } }
    foreach ($record in $ordered) {
        $path = [string]$record.path
        if ($path -eq 'VERSION' -or (Test-PersistentRollbackLauncher $path)) { continue }
        Copy-FileAtomically (Get-SafePath $filesRoot $path) (Get-SafePath $installRoot $path)
        Add-JournalEntry (Join-Path $backupDirectory 'journal.log') 'rollback-restore' $path
    }
    foreach ($pathValue in @($metadata.created_files)) {
        $path = [string]$pathValue
        if (Test-PersistentRollbackLauncher $path) {
            Add-JournalEntry (Join-Path $backupDirectory 'journal.log') 'rollback-keep-launcher' $path
            continue
        }
        $destination = Get-SafePath $installRoot $path
        if (Test-Path -LiteralPath $destination -PathType Leaf) { Remove-Item -LiteralPath $destination -Force }
        Add-JournalEntry (Join-Path $backupDirectory 'journal.log') 'rollback-remove' $path
    }
    $versionRecord = @($metadata.files) | Where-Object { $_.path -eq 'VERSION' } | Select-Object -First 1
    if ($null -ne $versionRecord) {
        Copy-FileAtomically (Get-SafePath $filesRoot 'VERSION') (Get-SafePath $installRoot 'VERSION')
    }
    elseif (@($metadata.created_files) -contains 'VERSION') {
        $versionPath = Join-Path $installRoot 'VERSION'
        if (Test-Path -LiteralPath $versionPath) { Remove-Item -LiteralPath $versionPath -Force }
    }

    $metadata.status = 'rolled-back'
    $metadata.rolled_back_at = [DateTime]::UtcNow.ToString('o')
    Write-JsonAtomic (Join-Path $backupDirectory 'backup.json') $metadata
    Add-JournalEntry (Join-Path $backupDirectory 'journal.log') 'rollback-complete' '-'
    Write-Host "롤백이 완료되었습니다: $($metadata.from_version)" -ForegroundColor Green
    exit 0
}
catch {
    $failure = $_.Exception.Message
    if ($null -ne $recovery) {
        Write-Host '롤백 중 오류가 발생하여 롤백 직전 상태를 복구합니다.' -ForegroundColor Yellow
        try { Restore-RollbackRecovery $installRoot $recovery } catch {
            Write-Host "롤백 직전 상태 복구에도 실패했습니다: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    Write-Host $failure -ForegroundColor Red
    exit 1
}
finally {
    for ($index = $runLocks.Count - 1; $index -ge 0; $index--) {
        $runLocks[$index].Dispose()
    }
    if ($null -ne $recovery -and (Test-Path -LiteralPath $recovery.Directory -PathType Container)) {
        $recoveryBase = Join-Path $env:LOCALAPPDATA 'Nivelle\Updater\rollback-temp'
        if (Test-ChildPath $recoveryBase $recovery.Directory) {
            Remove-Item -LiteralPath $recovery.Directory -Recurse -Force
        }
    }
}
