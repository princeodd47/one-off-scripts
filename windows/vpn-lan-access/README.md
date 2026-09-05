# vpn-lan-access

Lets you reach your LAN (router, NAS, printer, etc.) while ProtonVPN's Windows
kill switch is active and the VPN stays connected — instead of the only
built-in option, which is disconnecting the VPN entirely (LAN access while
connected is normally gated behind a paid "Allow LAN connections" feature).

## The problem

ProtonVPN's free-tier Windows app blocks all LAN access the moment you
connect. The routing table shows why this *isn't* a routing issue: your LAN
subnet's route (e.g. `192.168.0.0/24`, on-link) is untouched by the VPN
connecting — Proton only adds two `/1` routes to redirect general traffic
into the tunnel, and a `/24` route is more specific and still wins for LAN
destinations.

The actual block is a kill-switch firewall rule, enforced via the Windows
Filtering Platform (WFP) — the same subsystem Windows Firewall itself is
built on. Proton installs a filter with no conditions ("block all IPv4") at
the `ALE_AUTH_CONNECT_V4` layer, which governs every outbound connection
attempt at the socket level, before routing is even consulted. There's no
exception for private/LAN address ranges in the free tier's rule set — that
carve-out is exactly what the paid "Allow LAN" toggle adds.

## The fix

`wfp-lan-permit.ps1` adds one narrowly-scoped WFP filter of our own: permit
outbound connections to `192.168.0.0/24` (edit the script if your LAN uses a
different subnet), placed so it wins against Proton's block. Two things were
required to make that actually work — see `INVESTIGATION.md` for the full
story of how these were found:

1. **Its own sublayer, with a higher weight than Proton's.** WFP evaluates
   sublayers by weight, higher first. Proton's kill-switch filters live in
   their own "Dynamic Sublayer" (weight 1001); you can't add filters into it
   directly (it's scoped to their private session), so this script registers
   a separate sublayer with weight 5000, evaluated first.
2. **A "hard permit" flag.** A plain WFP permit is "soft" by default — a
   block encountered later still overrides it. Setting
   `FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT` makes it a hard permit, which an
   ordinary filter block (not a callout) cannot override, regardless of
   evaluation order.

Verified safe: with the filter active, non-LAN traffic still shows the VPN's
exit IP (no leak), and disconnecting/reconnecting the VPN behaves normally —
this only touches the one subnet, nothing else.

## Usage

Run from an **elevated** PowerShell window (admin rights required).

```powershell
# Add - non-persistent (removed on reboot or if the Windows Filtering
# Engine service restarts)
.\wfp-lan-permit.ps1 -Add

# Add - persistent (survives reboot)
.\wfp-lan-permit.ps1 -Add -Persistent

# Remove (works regardless of how it was added)
.\wfp-lan-permit.ps1 -Remove

# Add and dump the resulting filter table for inspection
.\wfp-lan-permit.ps1 -Add -Verify
```

`test-lan-access.ps1` is a quick connectivity check — tries a TCP connect
and tells you whether a failure is an actual local block (the WFP kill
switch) versus anything else (e.g. nothing listening on that port):

```powershell
.\test-lan-access.ps1                                # tests 192.168.0.1:80
.\test-lan-access.ps1 -TargetIp 192.168.0.5 -Port 443
```

**Note:** because PowerShell can't recompile an already-loaded type in the
same session, run each `wfp-lan-permit.ps1` invocation in its own fresh
elevated window after editing the script.

## Scope and caveats

- Only touches outbound `ALE_AUTH_CONNECT_V4` for the one configured subnet.
  Nothing else is modified.
- This is specific to ProtonVPN's current Windows kill-switch implementation
  (observed on v5.1.7). If Proton changes how the kill switch is enforced,
  this may need updating — re-run the diagnostic steps in
  `INVESTIGATION.md` to check.
- Fully reversible via `-Remove`.
