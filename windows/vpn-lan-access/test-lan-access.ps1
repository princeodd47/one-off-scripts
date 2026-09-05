<#
Quick TCP connect test - confirms whether LAN traffic is currently blocked or reaching
its target, distinguishing "blocked locally" (access-permissions error) from
"no response from target" (any other error, or a clean connect).

Usage:
    .\test-lan-access.ps1                       # tests 192.168.0.1:80
    .\test-lan-access.ps1 -TargetIp 192.168.0.5 -Port 443
#>
param(
    [string]$TargetIp = "192.168.0.1",
    [int]$Port = 80
)

$client = New-Object System.Net.Sockets.TcpClient
try {
    $client.Connect($TargetIp, $Port)
    Write-Host "Connected to $TargetIp`:$Port - no block." -ForegroundColor Green
} catch {
    $msg = $_.Exception.InnerException.Message
    if ($msg -like "*forbidden by its access permissions*") {
        Write-Host "BLOCKED locally: $msg" -ForegroundColor Red
    } else {
        Write-Host "Not blocked locally, but no connection: $msg" -ForegroundColor Yellow
    }
} finally {
    $client.Close()
}
