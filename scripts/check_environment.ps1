param([string]$ProjectRoot = (Join-Path $PSScriptRoot '..'))

$ErrorActionPreference = 'Continue'
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)

function Get-CompatiblePythonDescription {
    $candidates = New-Object System.Collections.Generic.List[object]
    $venvPython = Join-Path $script:ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $candidates.Add([pscustomobject]@{ Executable = $venvPython; Arguments = @() })
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $candidates.Add([pscustomobject]@{ Executable = $launcher.Source; Arguments = @('-3') })
    }
    $pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pathPython -and $pathPython.Source -notlike '*\WindowsApps\*') {
        $candidates.Add([pscustomobject]@{ Executable = $pathPython.Source; Arguments = @() })
    }

    foreach ($candidate in $candidates) {
        $result = @(
            & $candidate.Executable @($candidate.Arguments) -c (
                'import platform,sys;print(platform.python_version());print(sys.executable);' +
                'raise SystemExit(0 if sys.version_info>=(3,12) else 1)'
            ) 2>$null
        )
        if ($LASTEXITCODE -eq 0 -and $result.Count -ge 2) {
            return "Python $($result[0]) ($($result[1]))"
        }
    }
    return '사용 가능한 Python 3.12 이상을 찾지 못함 (실행 시 자동 설치됨)'
}

$llama = $env:NIVELLE_LLAMA_SERVER_PATH
if (-not $llama -and $env:NOZOMI_LLAMA_SERVER_PATH) {
    Write-Warning 'NOZOMI_LLAMA_SERVER_PATH is a legacy 0.3.1 compatibility variable. Use NIVELLE_LLAMA_SERVER_PATH.'
    $llama = $env:NOZOMI_LLAMA_SERVER_PATH
}
if (-not $llama -or -not (Test-Path -LiteralPath $llama -PathType Leaf)) {
    $runtimeRoot = Join-Path $ProjectRoot 'runtime\llama.cpp'
    if (Test-Path -LiteralPath $runtimeRoot -PathType Container) {
        $llama = Get-ChildItem -LiteralPath $runtimeRoot -Filter llama-server.exe -Recurse -File |
            Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
}

$probe = Join-Path $env:TEMP ('nivelle-write-test-' + [guid]::NewGuid().ToString('N'))
$dataWrite = try {
    Set-Content -LiteralPath $probe -Value 'ok'
    Remove-Item -LiteralPath $probe -Force
    '정상'
}
catch { '실패' }

Write-Host 'Nivelle 환경 진단' -ForegroundColor Cyan
Write-Host 'Python:' (Get-CompatiblePythonDescription)
Write-Host 'llama-server:' $(if ($llama) { $llama } else { '설정되지 않음 (실행 시 자동 설치됨)' })
Write-Host '데이터 경로 쓰기:' $dataWrite
Write-Host 'Vulkan 도구:' $(
    if (Get-Command vulkaninfo -ErrorAction SilentlyContinue) { '사용 가능' }
    else { '지원 여부 확인 불가' }
)
Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, State, OwningProcess
