[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerHost,
    [ValidateRange(1, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$tcp = Test-NetConnection -ComputerName $ServerHost -Port $Port -InformationLevel Detailed
if (-not $tcp.TcpTestSucceeded) {
    Write-Error "TCP 연결 실패: ${ServerHost}:$Port"
    exit 1
}

& (Join-Path $PSScriptRoot 'test_server_health.ps1') -ServerHost $ServerHost -Port $Port
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "2PC 연결 기본 점검 통과: ${ServerHost}:$Port" -ForegroundColor Green
Write-Host '다음 단계: Nivelle Link에서 같은 프로필로 연결하고 실제 WebSocket 대화를 전송하세요.'
