# Investigation log: ProtonVPN kill switch vs. LAN access

How we found and fixed the "can't reach my LAN while ProtonVPN is connected"
problem. Kept for the next time this needs revisiting (Proton updates their
client, the fix stops working, or the same technique is needed for a
different VPN client's kill switch).

## Starting point

ProtonVPN's free Windows app: connect the VPN, lose all access to the local
network (router, NAS, etc.) until you disconnect. The paid tier has an
"Allow LAN connections" toggle; free tier doesn't. The question was whether
this was fixable with a routing tweak, or something deeper.

## Step 1 — rule out routing

Captured `route print -4` before and after connecting the VPN. Proton adds
two `/1` routes (`0.0.0.0/1` and `128.0.0.0/1` via the tunnel adapter) — the
classic "split default route" trick, more specific than a plain `0.0.0.0/0`
so they win the tiebreak without literally replacing it. Critically, the
existing LAN route (`192.168.0.0/24`, on-link via the real adapter) was
**untouched** — still present, still more specific than the tunnel's `/1`
routes. So at the routing layer, LAN traffic should have worked. It didn't.
That ruled out routing and pointed at the firewall layer instead.

## Step 2 — find the actual block

Dumped the live Windows Filtering Platform (WFP) rule set with
`netsh wfp show filters` before/after connecting. Found it:

- `ProtonVPN block IPv4` — layer `ALE_AUTH_CONNECT_V4`, **no conditions**
  (matches everything), action `BLOCK`.
- A handful of narrow exceptions above it (tunnel interface traffic, DHCP,
  Proton's own processes) — but nothing for private/LAN address ranges.

`ALE_AUTH_CONNECT_V4` fires at socket-connect time, before routing is
consulted — so it doesn't matter that the LAN route was intact. This is
Proton's kill switch, default-deny, with no LAN carve-out on the free tier.

## Step 3 — the standard Windows Firewall route (dead end)

First attempt: `New-NetFirewallRule` outbound Allow rule for
`192.168.0.0/24`. It reported success but **did nothing** — the connect
attempt still failed identically.

Dumped the filter table again and found why: the outbound rule never
materialized into an actual WFP filter at all (searched the whole dump for
`192.168.0.0` — zero matches at `ALE_AUTH_CONNECT_V4`). Windows' default
outbound policy is already "allow," so Windows Firewall's rule compiler
treats an explicit outbound Allow as redundant and skips creating a real
filter for it. (The matching *inbound* rule, by contrast, did materialize,
since default inbound policy is "block.") The only way to force Windows
Firewall itself to compile a real competing outbound filter is to flip the
whole profile's default outbound action to Block — a huge blast-radius
change (every other app loses network access until individually allowed) —
so this path was abandoned in favor of talking to WFP directly.

## Step 4 — raw WFP filter, attempt 1 (failed)

Wrote a small PowerShell/C# script (`wfp-lan-permit.ps1`) that P/Invokes
`fwpuclnt.dll` directly — `FwpmEngineOpen0`/`FwpmFilterAdd0`/etc. — to add a
permit filter at `ALE_AUTH_CONNECT_V4` for `192.168.0.0/24`, with an
explicit large weight (`2^62`), placed in the built-in
`FWPM_SUBLAYER_UNIVERSAL`.

Added cleanly, no errors. Verified the resulting filter's `effectiveWeight`
was indeed higher than Proton's block (`2^62` vs `2^60`). Still blocked —
identical `WSAEACCES` ("forbidden by access permissions") error on connect.

## Step 5 — diagnosing why: sublayer weight, not filter weight

Dumped the full engine state (`netsh wfp show state`), which lists sublayer
registrations and their weights — not shown in the plain filter dump. Found:

| Sublayer | Weight |
|---|---|
| ProtonVPN Dynamic Sublayer (their block lives here) | 1001 |
| FWPM_SUBLAYER_UNIVERSAL (where our filter landed) | 32768 |

Sublayers are evaluated in priority order before individual filter weight
matters within them — our filter's big weight number was meaningless because
it was in the wrong sublayer's evaluation pass relative to Proton's.

Tried adding directly into Proton's own Dynamic Sublayer instead — got
`FWP_E_WRONG_SESSION` (`0x8032000C`). That sublayer is scoped to Proton's own
private WFP session and can't be modified from an independent script. A
real, intentional access boundary, not a bug.

## Step 6 — raw WFP filter, attempt 2 (failed, for a subtler reason)

Registered our own sublayer (`FwpmSubLayerAdd0`) with weight `500` —
*lower* than Proton's `1001`, on the assumption that lower means evaluated
first. Still blocked, identical error.

This is where we stopped guessing and went to Microsoft's actual
documentation rather than a third blind attempt. Two pages settled it:

- [Filter Arbitration](https://learn.microsoft.com/en-us/windows/win32/fwp/filter-arbitration) —
  the authoritative rulebook. Key facts:
  - **Higher-weight sublayers are evaluated first**, not lower (our weight
    500 was backwards — needed to be *above* 1001, not below).
  - A plain filter **block is "hard" by default** — cannot be overridden by
    anything evaluated afterward.
  - A plain filter **permit is "soft" by default** — a block encountered
    later still overrides it, no matter how high its weight number.
  - There's a specific flag for this exact scenario:
    `FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT` turns a permit into a "hard
    permit," which an ordinary filter block cannot override (only a callout
    *veto* could, and Proton's block is a plain filter, not a callout).
  - This is literally the same mechanism Proton's own client uses
    internally — their "permit OpenVPN server" and "permit app to bypass
    tunnel" filters carry this exact flag, which is how their own exceptions
    survive their own block.
- [FWPM_FILTER0 reference](https://learn.microsoft.com/en-us/windows/win32/api/fwpmtypes/ns-fwpmtypes-fwpm_filter0) —
  confirmed filter weight semantics too ("higher-numbered weights have
  higher priorities and will be evaluated before lower-weighted filters").

## Step 7 — the actual fix

Two changes from attempt 2:

1. Sublayer weight raised from `500` to `5000` (above Proton's `1001`, so
   ours is evaluated first).
2. Filter flags include `FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT` (hard permit).

First attempt at this hit `FWP_E_INVALID_FLAGS` (`0x8032001E`) — the initial
guess at the flag's numeric value (`0x00000010`) was wrong; that bit is
actually `FWPM_FILTER_FLAG_PERMIT_IF_CALLOUT_UNREGISTERED`, which only
applies to callout actions. Looked up the real value
(`0x00000008`) and re-ran.

Result: connected cleanly, no error. LAN access worked while the VPN tunnel
stayed up.

## Step 8 — verifying it was actually safe

Before calling it done:

- **No leak check**: with the VPN connected and the filter active, an
  external IP check (`api.ipify.org`) showed a Proton exit IP, not the
  router's real WAN IP — confirmed via `whois` (Proton's IP registered to a
  residential-proxy block in Switzerland; the router's real IP is a US
  address). Non-LAN traffic still tunnels correctly.
- **Kill switch still works**: disconnected Proton entirely and confirmed
  the external IP reverted to the real ISP address (expected — kill switch
  protects an active-but-dropped tunnel, not a deliberate disconnect).
  Reconnected and re-verified LAN access + no leak together.

## Step 9 — persistence

The filter/sublayer as built don't survive a reboot or a Windows Filtering
Engine service restart — they're not marked persistent. Added an explicit
`-Persistent` switch to the script (defaulting to off, so day-to-day testing
stays non-destructive) which sets `FWPM_SUBLAYER_FLAG_PERSISTENT` and
`FWPM_FILTER_FLAG_PERSISTENT` on the sublayer *and* filter together — setting
persistence on only one of the pair throws `FWP_E_LIFETIME_MISMATCH`, a
known WFP gotcha (a persistent filter can't reference a non-persistent
sublayer).

## Summary of wrong turns, for next time

| Attempt | Why it failed |
|---|---|
| Route-table fix | Never was a routing problem — LAN route was already intact |
| `New-NetFirewallRule` outbound Allow | Windows silently skips compiling a real filter when default policy is already Allow |
| Own filter in `FWPM_SUBLAYER_UNIVERSAL`, big weight | Right layer, wrong sublayer priority (32768 < ... actually higher than 1001, but permit was soft, so it lost to Proton's later hard block anyway) |
| Filter directly in Proton's own sublayer | `FWP_E_WRONG_SESSION` — can't touch another session's dynamic sublayer |
| Own sublayer, weight 500 (< 1001), soft permit | Weight direction was backwards (lower isn't higher priority), and still a soft permit regardless |
| Own sublayer, weight 5000 (> 1001), hard permit | **This worked** |
