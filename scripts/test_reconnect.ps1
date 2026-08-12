[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerHost,
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidateRange(30, 900)]
    [int]$ObservationSeconds = 180
)

$ErrorActionPreference = 'Stop'
$uriHost = if ($ServerHost.Contains(':') -and -not $ServerHost.StartsWith('[')) {
    "[$ServerHost]"
} else {
    $ServerHost
}
$healthUri = "http://${uriHost}:$Port/health"

Write-Host '안전한 수동 재연결 테스트를 시작합니다.' -ForegroundColor Cyan
Write-Host '1. Nivelle Link를 연결한 채로 둡니다.'
Write-Host '2. 서버 PC에서 Nivelle Core만 정상 종료했다가 다시 실행합니다.'
Write-Host '3. VPN, 다른 서비스, PC 전원은 종료하지 마세요.'
Write-Host "최대 $ObservationSeconds 초 동안 Gateway 상태 전환을 관찰합니다."

$deadline = (Get-Date).AddSeconds($ObservationSeconds)
$sawOffline = $false
$sawRecovery = $false
$previous = $null
while ((Get-Date) -lt $deadline) {
    $online = $false
    try {
        $response = Invoke-RestMethod -Uri $healthUri -TimeoutSec 2 -Method Get
        $online = $response.status -eq 'ok'
    }
    catch {
        $online = $false
    }
    $state = if ($online) { 'online' } else { 'offline' }
    if ($state -ne $previous) {
        Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] Gateway: $state"
        $previous = $state
    }
    if (-not $online) {
        $sawOffline = $true
    }
    elseif ($sawOffline) {
        $sawRecovery = $true
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $sawOffline) {
    Write-Error '관찰 시간 동안 서버 중단을 확인하지 못했습니다.'
    exit 1
}
if (-not $sawRecovery) {
    Write-Error '서버 중단은 확인했지만 재시작 후 health 복구를 확인하지 못했습니다.'
    exit 2
}

Write-Host 'Gateway 중단과 복구를 확인했습니다.' -ForegroundColor Green
Write-Host '클라이언트에서 reconnecting→online, 창/초안 유지, 중복 메시지 없음도 확인하세요.'
