# vpn-split-tunnel

Routes specific sites *around* ProtonVPN's tunnel — so they see your real ISP
IP instead of Proton's exit IP — while everything else stays tunneled as
normal. For sites that block or loop VPN traffic (anti-bot/WAF rules,
"suspicious IP" sign-in loops, geo restrictions, etc).

## The problem

Two symptoms, same root cause (the site treats Proton's exit IP as
suspicious):

- `fabtcg.com` returned `HTTP 403` on every request while connected to
  ProtonVPN.
- `fabrary.net` loaded fine, but signing in dropped you back to the front
  page as logged-out, and browsing to a deck showed `Request failed with
  status code 403` against its backend API.

## The fix

Same underlying trick as [`../vpn-lan-access`](../vpn-lan-access)'s WFP
permit filter (own sublayer weighted above ProtonVPN's Dynamic Sublayer,
`FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT` hard-permit flag so it beats Proton's
kill-switch block) — but generalized to arbitrary resolved site IPs instead
of one fixed LAN subnet, **plus** an explicit `/32` host route per IP.

The extra route matters because, unlike your LAN subnet, the open internet
has no pre-existing more-specific route to fall back on: ProtonVPN's
`0.0.0.0/1` + `128.0.0.0/1` split-default routes would otherwise still catch
that traffic and send it into the tunnel — permitting it past the kill
switch alone isn't enough, it also has to actually leave via your real
adapter or the site still sees Proton's IP.

`split-tunnel.ps1` resolves a list of domains to their current IPs and adds
both pieces (route + WFP filter) for each one, tracking what it added in
`split-tunnel-state.json` so `-Remove`/`-Refresh` can clean up precisely.

## Usage

Run from an **elevated** PowerShell window.

```powershell
# Resolve the default domain list and add routes+filters (non-persistent)
.\split-tunnel.ps1 -Add

# Resolve a specific list instead
.\split-tunnel.ps1 -Add -Domains a.com,b.com

# Add more domains on top of what's already tracked (existing entries kept)
.\split-tunnel.ps1 -Add -Domains another-site.com

# Survives reboot/BFE restart
.\split-tunnel.ps1 -Add -Persistent

# Re-resolve tracked domains: add newly-appeared IPs, drop ones that no
# longer resolve. Run this periodically for CDN-backed sites - their IPs
# rotate (see caveat below).
.\split-tunnel.ps1 -Refresh

# Show what's currently tracked
.\split-tunnel.ps1 -Status

# Remove everything this script added (routes, filters, sublayer)
.\split-tunnel.ps1 -Remove
```

`add-fabrary-api.ps1` is a thin wrapper that adds fabrary.net's actual
backend hosts (see caveat below) — kept as a separate file because the
domain list is long enough to get mangled by terminal line-wrapping if
pasted as one `-Domains` argument.

## Verified

- `fabtcg.com`: fixed. `/32` routes route via the real gateway, WFP filters
  confirmed via `netsh wfp show filters`, and the site now returns `200`
  with `geot_rocket_country=US` (its own geo-IP cookie), instead of `403`.
- `fabrary.net` static content: was never actually broken (loaded `200`
  over the VPN in testing) — included anyway since it's the same product.
- `fabrary.net` login/API: **still broken** — see caveat below. Root-caused
  via a HAR capture of a login attempt to a `403` against its AWS AppSync
  GraphQL API (`*.appsync-api.us-east-2.amazonaws.com`).
- General VPN traffic unaffected: `api.ipify.org` still shows the Proton
  exit IP throughout.

## Caveat: doesn't work for CDN-backed APIs with rotating IPs

fabrary.net's login flow calls an AWS AppSync API that sits behind
CloudFront. CloudFront has no fixed IP set — Route53 steers each DNS lookup
across a large, effectively unbounded edge pool. Confirmed by resolving the
same hostname 3 times in a row and getting 3 different `/24` ranges, none of
which matched what the browser's own request actually hit. A static IP
whitelist can't keep up with that, and refreshing more often doesn't help —
it's not "occasionally stale," the pool is just too large to pin down.

`-Refresh` is enough for hosts with a small, stable IP set (confirmed fine
for the Cognito auth endpoints used by the same site), but not for anything
CloudFront-fronted like the AppSync API.

The actual fix for that case is a name-based (not IP-based) split-tunnel
proxy on a LAN device that was never inside the VPN tunnel to begin with —
sidesteps the kill switch and the IP-rotation problem entirely, and works
for every device on the network, not just this one. See
`network-config/hosts/luca-pihole/MIGRATION.md` (separate repo) for that
plan — queued behind a pending Pi-hole OS reinstall, since the current box
is EOL and not a good place to add a new listening service.

## Scope and caveats

- Only touches outbound `ALE_AUTH_CONNECT_V4` for the tracked IPs, and adds
  routes only for those exact `/32`s. Nothing else is modified.
- `split-tunnel-state.json` is machine-local runtime state (resolved IPs,
  generated filter GUIDs) - gitignored, not meant to be committed.
- Same ProtonVPN-version caveat as `vpn-lan-access`: this relies on the
  kill switch's current WFP implementation (observed on v5.1.7). Re-check
  `../vpn-lan-access/INVESTIGATION.md`'s diagnostic steps if Proton changes
  how it's enforced.
- Fully reversible via `-Remove`.
