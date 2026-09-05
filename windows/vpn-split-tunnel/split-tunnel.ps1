<#
Split-tunnels specific sites out of ProtonVPN's tunnel so they see your real
ISP IP instead of Proton's exit IP - for sites that block/loop VPN traffic
(anti-bot checks, "suspicious IP" sign-in loops, geo/WAF rules, etc).

Same underlying trick as ../vpn-lan-access/wfp-lan-permit.ps1 (own WFP
sublayer, weighted above ProtonVPN's Dynamic Sublayer, hard-permit flag to
beat their kill-switch block-all filter) - but generalized to arbitrary
resolved site IPs instead of one fixed LAN subnet, plus an explicit /32 host
route per IP so traffic actually goes out your real adapter instead of
Proton's tunnel (unlike your LAN subnet, the open internet has no existing
more-specific route to fall back on - Proton's /1+/1 split-default routes
would otherwise catch it).

Usage (run from an elevated PowerShell window):
    .\split-tunnel.ps1 -Add                      # resolve default domain list, add routes+filters
    .\split-tunnel.ps1 -Add -Domains a.com,b.com # resolve a specific list instead
    .\split-tunnel.ps1 -Add -Persistent          # survives reboot/BFE restart
    .\split-tunnel.ps1 -Refresh                  # re-resolve tracked domains, add new IPs, drop stale ones
    .\split-tunnel.ps1 -Status                   # show what's currently tracked
    .\split-tunnel.ps1 -Remove                   # remove everything this script added

CDN-backed sites resolve to multiple, sometimes-rotating IPs. -Add captures
a snapshot; re-run -Refresh periodically (e.g. via Task Scheduler) if a site
stops working again after a while - that's DNS rotation, not a bug here.
#>
param(
    [switch]$Add,
    [switch]$Remove,
    [switch]$Refresh,
    [switch]$Status,
    [switch]$Verify,
    [switch]$Persistent,
    [string[]]$Domains = @("fabrary.net", "fabtcg.com")
)

$ErrorActionPreference = "Stop"

$StateFile = Join-Path $PSScriptRoot "split-tunnel-state.json"

# Fixed identity for our sublayer - distinct from vpn-lan-access's, so the two
# scripts don't step on each other. Individual filter keys are generated
# per-IP at add time and tracked in the state file.
$SubLayerGuid = [Guid]"e4b0a1c3-7d2e-4b5a-8f1c-000000000002"
$SubLayerWeight = 5000  # must beat ProtonVPN's Dynamic Sublayer (1001) - see ../vpn-lan-access/INVESTIGATION.md
$FilterWeight = [uint64]0x4000000000000000  # 2^62, comfortably above Proton's observed block-filter weight

function Assert-Elevated {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Host "This script must be run from an elevated (Administrator) PowerShell window." -ForegroundColor Red
        exit 1
    }
}

function Get-RealDefaultRoute {
    # The real adapter's default route has an actual gateway; ProtonVPN's
    # split-default routes (0.0.0.0/1, 128.0.0.0/1) are on-link (NextHop 0.0.0.0).
    $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -ne "0.0.0.0" } |
        Sort-Object -Property RouteMetric |
        Select-Object -First 1
    if (-not $route) {
        throw "Could not find a real (non-VPN) default route - are you connected to a network?"
    }
    return $route
}

function Load-State {
    if (Test-Path $StateFile) {
        return Get-Content $StateFile -Raw | ConvertFrom-Json
    }
    return [pscustomobject]@{ persistent = $false; domains = @(); entries = @() }
}

function Save-State($state) {
    $state | ConvertTo-Json -Depth 5 | Set-Content $StateFile -Encoding UTF8
}

$src = @'
using System;
using System.Runtime.InteropServices;

public static class WfpSite
{
    [DllImport("fwpuclnt.dll")]
    static extern uint FwpmEngineOpen0(string serverName, uint authnService, IntPtr authIdentity, IntPtr session, out IntPtr engineHandle);

    [DllImport("fwpuclnt.dll")]
    static extern uint FwpmEngineClose0(IntPtr engineHandle);

    [DllImport("fwpuclnt.dll")]
    static extern uint FwpmFilterAdd0(IntPtr engineHandle, IntPtr filter, IntPtr sd, out ulong id);

    [DllImport("fwpuclnt.dll")]
    static extern uint FwpmFilterDeleteByKey0(IntPtr engineHandle, ref Guid key);

    [DllImport("fwpuclnt.dll")]
    static extern uint FwpmFilterGetByKey0(IntPtr engineHandle, ref Guid key, out IntPtr filter);

    [DllImport("fwpuclnt.dll")]
    static extern uint FwpmSubLayerAdd0(IntPtr engineHandle, IntPtr subLayer, IntPtr sd);

    [DllImport("fwpuclnt.dll")]
    static extern uint FwpmSubLayerDeleteByKey0(IntPtr engineHandle, ref Guid key);

    const uint FWP_E_ALREADY_EXISTS = 0x80320009;
    const uint RPC_C_AUTHN_DEFAULT = 0xFFFFFFFF;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct FWPM_DISPLAY_DATA0 { public IntPtr name; public IntPtr description; }

    [StructLayout(LayoutKind.Sequential)]
    struct FWP_BYTE_BLOB { public uint size; public IntPtr data; }

    [StructLayout(LayoutKind.Explicit, Size = 16)]
    struct FWP_VALUE0
    {
        [FieldOffset(0)] public uint type;
        [FieldOffset(8)] public uint uint32Val;
        [FieldOffset(8)] public IntPtr ptrVal;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct FWP_RANGE0 { public FWP_VALUE0 valueLow; public FWP_VALUE0 valueHigh; }

    [StructLayout(LayoutKind.Sequential)]
    struct FWPM_FILTER_CONDITION0 { public Guid fieldKey; public uint matchType; public FWP_VALUE0 conditionValue; }

    [StructLayout(LayoutKind.Sequential)]
    struct FWPM_ACTION0 { public uint type; public Guid filterTypeOrCalloutKey; }

    [StructLayout(LayoutKind.Sequential)]
    struct FWPM_SUBLAYER0
    {
        public Guid subLayerKey;
        public FWPM_DISPLAY_DATA0 displayData;
        public uint flags;
        public IntPtr providerKey;
        public FWP_BYTE_BLOB providerData;
        public ushort weight;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct FWPM_FILTER0
    {
        public Guid filterKey;
        public FWPM_DISPLAY_DATA0 displayData;
        public uint flags;
        public IntPtr providerKey;
        public FWP_BYTE_BLOB providerData;
        public Guid layerKey;
        public Guid subLayerKey;
        public FWP_VALUE0 weight;
        public uint numFilterConditions;
        public IntPtr filterCondition;
        public FWPM_ACTION0 action;
        public Guid rawContextOrProviderContextKey;
        public IntPtr reserved;
        public ulong filterId;
        public FWP_VALUE0 effectiveWeight;
    }

    static readonly Guid LAYER_ALE_AUTH_CONNECT_V4 = new Guid("c38d57d1-05a7-4c33-904f-7fbceee60e82");
    static readonly Guid COND_IP_REMOTE_ADDRESS    = new Guid("b235ae9a-1d64-49b8-a44c-5ff3d9095045");

    const uint FWP_UINT32 = 3;
    const uint FWP_UINT64 = 4;
    const uint FWP_RANGE_TYPE = 258;
    const uint FWP_MATCH_RANGE = 5;
    const uint FWP_ACTION_PERMIT = 0x00001002;
    const uint FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT = 0x00000008;
    const uint FWPM_FILTER_FLAG_PERSISTENT = 0x00000001;
    const uint FWPM_SUBLAYER_FLAG_PERSISTENT = 0x00000001;

    static uint IpToUint(string ip)
    {
        var parts = ip.Split('.');
        return (uint.Parse(parts[0]) << 24) | (uint.Parse(parts[1]) << 16) | (uint.Parse(parts[2]) << 8) | uint.Parse(parts[3]);
    }

    static IntPtr OpenEngine()
    {
        IntPtr engine;
        uint hr = FwpmEngineOpen0(null, RPC_C_AUTHN_DEFAULT, IntPtr.Zero, IntPtr.Zero, out engine);
        if (hr != 0) throw new Exception("FwpmEngineOpen0 failed: 0x" + hr.ToString("X8"));
        return engine;
    }

    public static void EnsureSubLayer(Guid subLayerKey, ushort weight, bool persistent)
    {
        IntPtr engine = OpenEngine();
        try
        {
            FWPM_SUBLAYER0 sub = new FWPM_SUBLAYER0();
            sub.subLayerKey = subLayerKey;
            sub.displayData.name = Marshal.StringToHGlobalUni("split-tunnel sublayer");
            sub.displayData.description = Marshal.StringToHGlobalUni("Holds per-site split-tunnel permit filters, weight " + weight + " (above ProtonVPN Dynamic Sublayer's 1001)");
            sub.flags = persistent ? FWPM_SUBLAYER_FLAG_PERSISTENT : 0;
            sub.providerKey = IntPtr.Zero;
            sub.providerData = new FWP_BYTE_BLOB { size = 0, data = IntPtr.Zero };
            sub.weight = weight;

            IntPtr subPtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWPM_SUBLAYER0)));
            Marshal.StructureToPtr(sub, subPtr, false);

            uint hr = FwpmSubLayerAdd0(engine, subPtr, IntPtr.Zero);
            if (hr != 0 && hr != FWP_E_ALREADY_EXISTS)
                throw new Exception("FwpmSubLayerAdd0 failed: 0x" + hr.ToString("X8"));
        }
        finally
        {
            FwpmEngineClose0(engine);
        }
    }

    public static void RemoveSubLayer(Guid subLayerKey)
    {
        IntPtr engine = OpenEngine();
        try
        {
            Guid k = subLayerKey;
            FwpmSubLayerDeleteByKey0(engine, ref k);
        }
        finally
        {
            FwpmEngineClose0(engine);
        }
    }

    public static ulong AddIpFilter(string ip, Guid subLayerKey, ulong weightValue, Guid filterKey, string name, string description, bool persistent)
    {
        IntPtr engine = OpenEngine();
        try
        {
            IntPtr weightPtr = Marshal.AllocHGlobal(8);
            Marshal.WriteInt64(weightPtr, (long)weightValue);
            FWP_VALUE0 weight = new FWP_VALUE0 { type = FWP_UINT64, ptrVal = weightPtr };

            FWP_RANGE0 range = new FWP_RANGE0();
            uint ipVal = IpToUint(ip);
            range.valueLow  = new FWP_VALUE0 { type = FWP_UINT32, uint32Val = ipVal };
            range.valueHigh = new FWP_VALUE0 { type = FWP_UINT32, uint32Val = ipVal };
            IntPtr rangePtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWP_RANGE0)));
            Marshal.StructureToPtr(range, rangePtr, false);

            FWPM_FILTER_CONDITION0 cond = new FWPM_FILTER_CONDITION0();
            cond.fieldKey = COND_IP_REMOTE_ADDRESS;
            cond.matchType = FWP_MATCH_RANGE;
            cond.conditionValue = new FWP_VALUE0 { type = FWP_RANGE_TYPE, ptrVal = rangePtr };
            IntPtr condPtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWPM_FILTER_CONDITION0)));
            Marshal.StructureToPtr(cond, condPtr, false);

            FWPM_FILTER0 filter = new FWPM_FILTER0();
            filter.filterKey = filterKey;
            filter.displayData.name = Marshal.StringToHGlobalUni(name);
            filter.displayData.description = Marshal.StringToHGlobalUni(description);
            filter.flags = FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT | (uint)(persistent ? FWPM_FILTER_FLAG_PERSISTENT : 0);
            filter.providerKey = IntPtr.Zero;
            filter.providerData = new FWP_BYTE_BLOB { size = 0, data = IntPtr.Zero };
            filter.layerKey = LAYER_ALE_AUTH_CONNECT_V4;
            filter.subLayerKey = subLayerKey;
            filter.weight = weight;
            filter.numFilterConditions = 1;
            filter.filterCondition = condPtr;
            filter.action = new FWPM_ACTION0 { type = FWP_ACTION_PERMIT, filterTypeOrCalloutKey = Guid.Empty };
            filter.rawContextOrProviderContextKey = Guid.Empty;
            filter.reserved = IntPtr.Zero;
            filter.filterId = 0;
            filter.effectiveWeight = new FWP_VALUE0 { type = 0 };

            IntPtr filterPtr = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(FWPM_FILTER0)));
            Marshal.StructureToPtr(filter, filterPtr, false);

            ulong id;
            uint addHr = FwpmFilterAdd0(engine, filterPtr, IntPtr.Zero, out id);
            if (addHr != 0) throw new Exception("FwpmFilterAdd0 failed: 0x" + addHr.ToString("X8"));
            return id;
        }
        finally
        {
            FwpmEngineClose0(engine);
        }
    }

    public static void RemoveFilter(Guid filterKey)
    {
        IntPtr engine = OpenEngine();
        try
        {
            Guid k = filterKey;
            FwpmFilterDeleteByKey0(engine, ref k);
        }
        finally
        {
            FwpmEngineClose0(engine);
        }
    }

    public static bool Exists(Guid filterKey)
    {
        IntPtr engine = OpenEngine();
        try
        {
            Guid k = filterKey;
            IntPtr filterPtr;
            uint hr = FwpmFilterGetByKey0(engine, ref k, out filterPtr);
            return hr == 0;
        }
        finally
        {
            FwpmEngineClose0(engine);
        }
    }
}
'@

Add-Type -TypeDefinition $src -Language CSharp

function Add-SiteIp {
    param($Ip, $Domain, $RealIfIndex, $RealGateway, $PolicyStore, $Persistent)

    $routeExists = Get-NetRoute -DestinationPrefix "$Ip/32" -ErrorAction SilentlyContinue
    if (-not $routeExists) {
        New-NetRoute -DestinationPrefix "$Ip/32" -InterfaceIndex $RealIfIndex -NextHop $RealGateway -RouteMetric 1 -PolicyStore $PolicyStore | Out-Null
    }

    $filterKey = [Guid]::NewGuid()
    [WfpSite]::AddIpFilter($Ip, $SubLayerGuid, $FilterWeight, $filterKey, "split-tunnel-$Domain", "Permit outbound to $Ip ($Domain) at ALE_AUTH_CONNECT_V4, routed via real adapter", [bool]$Persistent) | Out-Null

    return [pscustomobject]@{ ip = $Ip; domain = $Domain; filterKey = $filterKey.ToString() }
}

function Remove-SiteIp {
    param($Entry, $PolicyStore)

    try { [WfpSite]::RemoveFilter([Guid]$Entry.filterKey) } catch { Write-Host "  filter remove failed for $($Entry.ip): $_" -ForegroundColor Yellow }
    try { Remove-NetRoute -DestinationPrefix "$($Entry.ip)/32" -PolicyStore $PolicyStore -Confirm:$false -ErrorAction Stop } catch { Write-Host "  route remove failed for $($Entry.ip): $_" -ForegroundColor Yellow }
}

if (-not ($Add -or $Remove -or $Refresh -or $Status)) {
    Write-Host "Specify -Add, -Remove, -Refresh, or -Status. See script header for usage." -ForegroundColor Yellow
    exit 1
}

if ($Status) {
    $state = Load-State
    Write-Host "Tracked domains: $($state.domains -join ', ')"
    Write-Host "Persistent: $($state.persistent)"
    $state.entries | Select-Object domain, ip, filterKey | Format-Table -AutoSize
    exit 0
}

Assert-Elevated

if ($Remove) {
    $state = Load-State
    $policyStore = if ($state.persistent) { "PersistentStore" } else { "ActiveStore" }
    foreach ($entry in $state.entries) {
        Write-Host "Removing $($entry.ip) ($($entry.domain))..."
        Remove-SiteIp -Entry $entry -PolicyStore $policyStore
    }
    try { [WfpSite]::RemoveSubLayer($SubLayerGuid); Write-Host "Removed sublayer." -ForegroundColor Green } catch { Write-Host "Sublayer remove failed (may already be gone): $_" -ForegroundColor Yellow }
    Save-State ([pscustomobject]@{ persistent = $false; domains = @(); entries = @() })
    Write-Host "Done." -ForegroundColor Green
    exit 0
}

if ($Add) {
    $state = Load-State
    if ($state.entries.Count -gt 0 -and [bool]$state.persistent -ne [bool]$Persistent) {
        Write-Host "Existing entries were added with -Persistent:$($state.persistent) - run -Remove first if you want to change that." -ForegroundColor Yellow
        exit 1
    }

    $realRoute = Get-RealDefaultRoute
    $policyStore = if ($Persistent) { "PersistentStore" } else { "ActiveStore" }

    [WfpSite]::EnsureSubLayer($SubLayerGuid, $SubLayerWeight, [bool]$Persistent)

    $entries = @($state.entries)
    $allDomains = @($state.domains) + $Domains | Select-Object -Unique
    foreach ($domain in $Domains) {
        Write-Host "Resolving $domain..."
        $ips = (Resolve-DnsName -Name $domain -Type A -ErrorAction Stop | Where-Object { $_.Type -eq "A" }).IPAddress | Select-Object -Unique
        foreach ($ip in $ips) {
            if ($entries | Where-Object { $_.ip -eq $ip }) {
                Write-Host "  $ip already tracked, skipping"
                continue
            }
            Write-Host "  adding $ip"
            $entries += Add-SiteIp -Ip $ip -Domain $domain -RealIfIndex $realRoute.ifIndex -RealGateway $realRoute.NextHop -PolicyStore $policyStore -Persistent:$Persistent
        }
    }
    $Domains = $allDomains

    Save-State ([pscustomobject]@{ persistent = [bool]$Persistent; domains = $Domains; entries = $entries })
    Write-Host "Added $($entries.Count) IP(s) across $($Domains.Count) domain(s)." -ForegroundColor Green
    if ($Verify) {
        Write-Host "Run: netsh wfp show filters file=C:\git\wfp_after_split.xml   (then search for split-tunnel-)" -ForegroundColor Cyan
    }
}

if ($Refresh) {
    $state = Load-State
    if ($state.entries.Count -eq 0 -and $state.domains.Count -eq 0) {
        Write-Host "Nothing tracked yet - run -Add first." -ForegroundColor Yellow
        exit 1
    }

    $realRoute = Get-RealDefaultRoute
    $policyStore = if ($state.persistent) { "PersistentStore" } else { "ActiveStore" }
    [WfpSite]::EnsureSubLayer($SubLayerGuid, $SubLayerWeight, [bool]$state.persistent)

    $newEntries = @()
    foreach ($domain in $state.domains) {
        Write-Host "Resolving $domain..."
        $currentIps = (Resolve-DnsName -Name $domain -Type A -ErrorAction Stop | Where-Object { $_.Type -eq "A" }).IPAddress | Select-Object -Unique
        $trackedForDomain = $state.entries | Where-Object { $_.domain -eq $domain }

        # New IPs: add
        foreach ($ip in $currentIps) {
            $already = $trackedForDomain | Where-Object { $_.ip -eq $ip }
            if ($already) {
                $newEntries += $already
            } else {
                Write-Host "  new IP for $domain : $ip"
                $newEntries += Add-SiteIp -Ip $ip -Domain $domain -RealIfIndex $realRoute.ifIndex -RealGateway $realRoute.NextHop -PolicyStore $policyStore -Persistent:$state.persistent
            }
        }

        # Stale IPs: remove
        foreach ($tracked in $trackedForDomain) {
            if ($tracked.ip -notin $currentIps) {
                Write-Host "  stale IP for $domain : $($tracked.ip) - removing"
                Remove-SiteIp -Entry $tracked -PolicyStore $policyStore
            }
        }
    }

    Save-State ([pscustomobject]@{ persistent = $state.persistent; domains = $state.domains; entries = $newEntries })
    Write-Host "Refreshed. Tracking $($newEntries.Count) IP(s)." -ForegroundColor Green
}
