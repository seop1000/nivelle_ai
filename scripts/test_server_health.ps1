[CmdletBinding()]
param(
    [string]$ServerHost = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidateRange(1, 30)]
    [int]$TimeoutSeconds = 3
)

$ErrorActionPreference = 'Stop'
$uriHost = if ($ServerHost.Contains(':') -and -not $ServerHost.StartsWith('[')) {
    "[$ServerHost]"
} else {
    $ServerHost
}
$healthUri = "http://${uriHost}:$Port/health"
$started = [Diagnostics.Stopwatch]::StartNew()
try {
    $response = Invoke-RestMethod -Uri $healthUri -TimeoutSec $TimeoutSeconds -Method Get
    $started.Stop()
    if ($response.status -ne 'ok') {
        throw "예상하지 못한 health 응답: $($response | ConvertTo-Json -Compress)"
    }
    [pscustomobject]@{
        Result = 'passed'
        Address = "${ServerHost}:$Port"
        Status = $response.status
        LatencyMs = [math]::Round($started.Elapsed.TotalMilliseconds, 2)
        CheckedAt = (Get-Date).ToString('o')
    } | Format-List
    exit 0
}
catch {
    $started.Stop()
    [pscustomobject]@{
        Result = 'failed'
        Address = "${ServerHost}:$Port"
        LatencyMs = [math]::Round($started.Elapsed.TotalMilliseconds, 2)
        CheckedAt = (Get-Date).ToString('o')
        Error = $_.Exception.Message
    } | Format-List
    exit 1
}
