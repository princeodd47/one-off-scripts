<#
Full verification suite for vpn-lan-access: checks that the VPN tunnel is up,
LAN is reachable, non-LAN traffic isn't leaking, and the inbound firewall
rule is still in place. No elevation required - all checks are read-only.

Usage:
    .\test-lan-access.ps1
    .\test-lan-access.ps1 -TargetIp 192.168.0.5 -Port 443

    # Also check for a real-IP leak by comparing against your actual WAN IP.
    # Pass this yourself at runtime - deliberately not a default, since this
    # script lives in a public repo and your real IP shouldn't be committed.
    .\test-lan-access.ps1 -RealWanIp 203.0.113.45
#>
param(
    [string]$TargetIp = "192.168.0.1",
    [int]$Port = 80,
    [string]$RealWanIp = "",
    [string]$TunnelAdapterName = "ProTUN",
    [string]$InboundRuleName = "LAN-while-VPN-IN"
)

$results = @()

# 1. Is the VPN tunnel actually up? (the other checks are meaningless if not)
$adapter = Get-NetAdapter -Name $TunnelAdapterName -ErrorAction SilentlyContinue
if ($adapter -and $adapter.Status -eq "Up") {
    $results += [pscustomobject]@{ Check = "VPN tunnel up"; Result = "PASS"; Detail = "$TunnelAdapterName is Up" }
} else {
    $results += [pscustomobject]@{ Check = "VPN tunnel up"; Result = "WARN"; Detail = "Adapter '$TunnelAdapterName' not found or not Up - are you connected?" }
}

# 2. LAN reachable - the actual fix working
$client = New-Object System.Net.Sockets.TcpClient
$lanOk = $false
try {
    $client.Connect($TargetIp, $Port)
    $lanOk = $true
    $lanDetail = "Connected to $TargetIp`:$Port"
} catch {
    $msg = $_.Exception.InnerException.Message
    $lanDetail = if ($msg -like "*forbidden by its access permissions*") { "BLOCKED locally: $msg" } else { $msg }
} finally {
    $client.Close()
}
$results += [pscustomobject]@{ Check = "LAN reachable ($TargetIp`:$Port)"; Result = if ($lanOk) { "PASS" } else { "FAIL" }; Detail = $lanDetail }

# 3. No leak - external IP should not be your real WAN IP (only checked if you passed one)
if ($RealWanIp) {
    try {
        $extIp = (Invoke-RestMethod -Uri "https://api.ipify.org?format=json" -TimeoutSec 10).ip
        if ($extIp -eq $RealWanIp) {
            $results += [pscustomobject]@{ Check = "No leak (external IP)"; Result = "FAIL"; Detail = "External IP matches your real WAN IP ($extIp) - traffic is NOT tunneled!" }
        } else {
            $results += [pscustomobject]@{ Check = "No leak (external IP)"; Result = "PASS"; Detail = "External IP is $extIp (not your real WAN IP)" }
        }
    } catch {
        $results += [pscustomobject]@{ Check = "No leak (external IP)"; Result = "ERROR"; Detail = $_.Exception.Message }
    }
} else {
    $results += [pscustomobject]@{ Check = "No leak (external IP)"; Result = "SKIPPED"; Detail = "Pass -RealWanIp <your real WAN IP> to enable this check" }
}

# 4. Inbound firewall rule (separate from the WFP outbound filter) still present and enabled
$inboundRule = Get-NetFirewallRule -DisplayName $InboundRuleName -ErrorAction SilentlyContinue
if ($inboundRule -and $inboundRule.Enabled -eq "True") {
    $results += [pscustomobject]@{ Check = "Inbound rule present"; Result = "PASS"; Detail = "'$InboundRuleName' exists and is enabled" }
} else {
    $results += [pscustomobject]@{ Check = "Inbound rule present"; Result = "FAIL"; Detail = "'$InboundRuleName' missing or disabled" }
}

$results | Format-Table -AutoSize

$bad = $results | Where-Object { $_.Result -in @("FAIL", "ERROR") }
if ($bad) {
    Write-Host "One or more checks failed - see above." -ForegroundColor Red
    exit 1
} else {
    Write-Host "All checks passed (or skipped by choice)." -ForegroundColor Green
    exit 0
}
