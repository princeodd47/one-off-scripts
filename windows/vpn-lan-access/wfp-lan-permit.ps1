<#
Adds/removes ONE raw Windows Filtering Platform (WFP) filter that permits
outbound connections to a specific LAN subnet, at the exact layer
(ALE_AUTH_CONNECT_V4) where ProtonVPN's kill-switch "block all IPv4" filter
lives - bypassing the normal Windows Firewall rule engine, which we already
confirmed does not materialize a competing filter here.

Usage (run from an elevated PowerShell window):
    .\wfp-lan-permit.ps1 -Add                       # add, non-persistent (gone on reboot/BFE restart)
    .\wfp-lan-permit.ps1 -Add -Persistent           # add, survives reboot/BFE restart
    .\wfp-lan-permit.ps1 -Remove                    # remove it (works regardless of how it was added)
    .\wfp-lan-permit.ps1 -Add -Verify               # add, then dump the resulting filter for inspection

Scope: only 192.168.0.0/24, outbound (ALE_AUTH_CONNECT_V4), IPv4.
Nothing else is touched. Fully reversible via -Remove.
#>
param(
    [switch]$Add,
    [switch]$Remove,
    [switch]$Verify,
    [switch]$Persistent
)

# Fixed identity for our filter so -Remove can find exactly this one, and only this one.
$FilterKeyGuid = [Guid]"a1e5b2b0-4f3e-4a3a-9c1e-000000000001"

$RemoteSubnetLow  = "192.168.0.0"
$RemoteSubnetHigh = "192.168.0.255"

# Comfortably above Proton's observed block-filter weight (2^60 = 1,152,921,504,606,846,976),
# well below core OS filters we observed near UINT64_MAX - deliberately not touching those.
$ExplicitWeight = [uint64]0x4000000000000000  # 2^62

$src = @'
using System;
using System.Runtime.InteropServices;

public static class WfpLan
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
        public Guid rawContextOrProviderContextKey; // union placeholder, zeroed/unused
        public IntPtr reserved;
        public ulong filterId;
        public FWP_VALUE0 effectiveWeight;
    }

    static readonly Guid LAYER_ALE_AUTH_CONNECT_V4 = new Guid("c38d57d1-05a7-4c33-904f-7fbceee60e82");
    // Our OWN sublayer. Docs: "higher-numbered weights have higher priorities and will be
    // evaluated before lower-weighted" - so this must be HIGHER than ProtonVPN's Dynamic
    // Sublayer (1001) to be evaluated first. We can't add filters into Proton's own sublayer
    // (FWP_E_WRONG_SESSION - it's scoped to their private session), so we register our own.
    static readonly Guid OUR_SUBLAYER              = new Guid("d3a9f1e2-6b7c-4a1d-9e3f-000000000001");
    const ushort OUR_SUBLAYER_WEIGHT = 5000;
    static readonly Guid COND_IP_REMOTE_ADDRESS    = new Guid("b235ae9a-1d64-49b8-a44c-5ff3d9095045");

    const uint FWP_UINT32 = 3;
    const uint FWP_UINT64 = 4;
    const uint FWP_RANGE_TYPE = 258;
    const uint FWP_MATCH_RANGE = 5;
    const uint FWP_ACTION_PERMIT = 0x00001002;
    // Makes this a "hard permit" - cannot be overridden by an ordinary filter block in a
    // lower-priority sublayer (only a callout Veto could override it). Without this flag,
    // a permit is "soft" and yields to any later block - which is why our first attempt failed.
    const uint FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT = 0x00000008;
    // Survives BFE (Base Filtering Engine) stop/start, i.e. reboots. Filter and sublayer must
    // both be persistent together, or FwpmFilterAdd0 returns FWP_E_LIFETIME_MISMATCH.
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

    public static void EnsureSubLayer(bool persistent)
    {
        IntPtr engine = OpenEngine();
        try
        {
            FWPM_SUBLAYER0 sub = new FWPM_SUBLAYER0();
            sub.subLayerKey = OUR_SUBLAYER;
            sub.displayData.name = Marshal.StringToHGlobalUni("LAN-while-VPN sublayer");
            sub.displayData.description = Marshal.StringToHGlobalUni("Holds the LAN-while-VPN permit filter, weight " + OUR_SUBLAYER_WEIGHT + " (higher than ProtonVPN Dynamic Sublayer's 1001, so evaluated first)");
            sub.flags = persistent ? FWPM_SUBLAYER_FLAG_PERSISTENT : 0;
            sub.providerKey = IntPtr.Zero;
            sub.providerData = new FWP_BYTE_BLOB { size = 0, data = IntPtr.Zero };
            sub.weight = OUR_SUBLAYER_WEIGHT;

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

    public static void RemoveSubLayer()
    {
        IntPtr engine = OpenEngine();
        try
        {
            Guid k = OUR_SUBLAYER;
            uint hr = FwpmSubLayerDeleteByKey0(engine, ref k);
            if (hr != 0) throw new Exception("FwpmSubLayerDeleteByKey0 failed: 0x" + hr.ToString("X8"));
        }
        finally
        {
            FwpmEngineClose0(engine);
        }
    }

    public static ulong Add(string lowIp, string highIp, ulong weightValue, Guid filterKey, string name, string description, bool persistent)
    {
        IntPtr engine = OpenEngine();
        try
        {
            IntPtr weightPtr = Marshal.AllocHGlobal(8);
            Marshal.WriteInt64(weightPtr, (long)weightValue);
            FWP_VALUE0 weight = new FWP_VALUE0 { type = FWP_UINT64, ptrVal = weightPtr };

            FWP_RANGE0 range = new FWP_RANGE0();
            range.valueLow  = new FWP_VALUE0 { type = FWP_UINT32, uint32Val = IpToUint(lowIp) };
            range.valueHigh = new FWP_VALUE0 { type = FWP_UINT32, uint32Val = IpToUint(highIp) };
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
            filter.subLayerKey = OUR_SUBLAYER;
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

    public static void Remove(Guid filterKey)
    {
        IntPtr engine = OpenEngine();
        try
        {
            Guid k = filterKey;
            uint hr = FwpmFilterDeleteByKey0(engine, ref k);
            if (hr != 0) throw new Exception("FwpmFilterDeleteByKey0 failed: 0x" + hr.ToString("X8"));
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

if (-not $Add -and -not $Remove) {
    Write-Host "Specify -Add or -Remove (optionally with -Verify). See script header for usage." -ForegroundColor Yellow
    exit 1
}

if ($Remove) {
    try {
        [WfpLan]::Remove($FilterKeyGuid)
        Write-Host "Removed filter $FilterKeyGuid" -ForegroundColor Green
    } catch {
        Write-Host "Remove filter failed (may already be gone): $_" -ForegroundColor Yellow
    }
    try {
        [WfpLan]::RemoveSubLayer()
        Write-Host "Removed our sublayer" -ForegroundColor Green
    } catch {
        Write-Host "Remove sublayer failed (may already be gone): $_" -ForegroundColor Yellow
    }
    exit 0
}

if ($Add) {
    if ([WfpLan]::Exists($FilterKeyGuid)) {
        Write-Host "Filter $FilterKeyGuid already exists - remove it first with -Remove." -ForegroundColor Yellow
        exit 1
    }
    $mode = if ($Persistent) { "persistent - survives reboot/BFE restart" } else { "non-persistent - gone on reboot/BFE restart" }
    try {
        [WfpLan]::EnsureSubLayer([bool]$Persistent)
    } catch {
        Write-Host "Sublayer creation failed: $_" -ForegroundColor Red
        exit 1
    }
    try {
        $id = [WfpLan]::Add($RemoteSubnetLow, $RemoteSubnetHigh, $ExplicitWeight, $FilterKeyGuid, "LAN-while-VPN-OUT-raw", "Raw WFP permit for $RemoteSubnetLow/24 outbound at ALE_AUTH_CONNECT_V4, weight $ExplicitWeight, $mode", [bool]$Persistent)
        Write-Host "Added filter, id=$id, key=$FilterKeyGuid ($mode)" -ForegroundColor Green
    } catch {
        Write-Host "Add failed: $_" -ForegroundColor Red
        exit 1
    }
    if ($Verify) {
        Write-Host "Run: netsh wfp show filters file=C:\git\wfp_after_raw.xml   (then check filterId $id)" -ForegroundColor Cyan
    }
}
