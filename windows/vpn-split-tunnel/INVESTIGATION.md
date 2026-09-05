# Investigation log: split-tunneling specific sites around ProtonVPN

How we chased down why fabtcg.com and fabrary.net broke under ProtonVPN, got
fabtcg.com fixed, and found out why fabrary.net needs a different approach
entirely. Kept for next time this needs revisiting, or the same shape of
problem shows up for another site.

## Starting point

Two sites broken while connected to ProtonVPN (free tier):
`fabtcg.com` and `fabrary.net`. The ask: a whitelist so specific sites go out
the normal internet connection instead of the tunnel.

## Step 1 — reuse or new mechanism?

`../vpn-lan-access` already solves a related problem (ProtonVPN's free-tier
kill switch blocks all outbound traffic that isn't through the tunnel, no
exceptions) using a raw WFP permit filter: own sublayer weighted above
Proton's Dynamic Sublayer (1001), `FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT` to
make the permit "hard" so it beats Proton's block. See that project's own
`INVESTIGATION.md` for how that was found.

Key difference from the LAN case: the LAN subnet already had its own
on-link route more specific than Proton's `0.0.0.0/1` + `128.0.0.0/1`
split-default routes, so only the firewall needed fixing. The open internet
has no such fallback route — permitting a site's IP past the kill switch
without also giving it a more-specific route just means it still rides
Proton's default routes into the tunnel, defeating the point (the site would
still see Proton's exit IP). So this needed both a route *and* a filter,
per IP.

## Step 2 — baseline, and a red herring

Checked adapters/routes: `ProTUN` tunnel up, real default route via Wi-Fi →
`192.168.0.1`, Proton's two `/1` on-link routes present as expected.

Resolved both domains:
- `fabrary.net` → Cloudflare (`104.26.x`, `172.67.x`)
- `fabtcg.com` → AWS (`13.54.x`, `52.64.x`, `3.104.x`, ap-southeast-2)

Baseline test with the VPN still connected: raw TCP connect to every
resolved IP succeeded (so the kill switch wasn't blocking general internet
traffic — that's expected, its free-tier "block everything" behavior was
only actually a problem for LAN, which has no built-in exception). HTTP:
`fabrary.net` → 200, `fabtcg.com` → 403.

**Red herring**: the `fabtcg.com` 403 looked like a VPN-IP block, but it was
actually the site's WAF rejecting bare `curl` requests with no User-Agent —
completely unrelated to the VPN. Confirmed later (Step 4) once a browser
User-Agent was used.

User confirmed the real fabrary.net symptom: login succeeds but drops back
to the front page as logged-out; browsing a deck shows a `403` error page
built into the app itself, which explicitly names "fabrary api: not
connected" and warns about VPNs.

## Step 3 — build the generalized fix

Wrote `split-tunnel.ps1`: same WFP C# as `wfp-lan-permit.ps1`, generalized
from one fixed LAN range to a list of arbitrary IPs (renamed type `WfpSite`,
own sublayer GUID so it doesn't collide with the LAN script's), plus:

- Resolves a list of domains via `Resolve-DnsName`.
- Adds a `/32` route per IP via the real (non-VPN) default gateway,
  auto-detected as the default route whose `NextHop` isn't `0.0.0.0`
  (Proton's `/1` routes are on-link, i.e. `NextHop 0.0.0.0`; the real one
  has an actual gateway).
- Adds one WFP hard-permit filter per IP, same sublayer/weight technique.
- Tracks everything added in `split-tunnel-state.json` (IP, domain, filter
  key) so `-Remove`/`-Refresh` know exactly what to undo.

Elevation note: `Start-Process -Verb RunAs -Wait -RedirectStandardOutput`
doesn't work — `ShellExecute`-based elevation (`RunAs`) doesn't support
stdio redirection, and combining them throws a parameter-binding error
before it even gets to the UAC prompt. Had the user run it directly in
their own elevated window instead.

## Step 4 — fabtcg.com confirmed fixed, fabrary.net not

First `-Add` (fabrary.net + fabtcg.com, 6 IPs total) went in cleanly.
Verification:

- Routes confirmed correct: all IPs route via `192.168.0.1`/Wi-Fi, not the
  tunnel.
- General VPN traffic unaffected: `api.ipify.org` still showed the Proton
  exit IP.
- `fabtcg.com` with a real browser User-Agent → `200`, and its own
  `geot_rocket_country=US` geo-IP cookie confirmed the site now sees a real
  US/ISP-originated request instead of Proton's exit country. **Fixed.**
- `fabrary.net` in an actual browser: same login loop / 403 error page as
  before. **Not fixed** — the static site was never broken (it already
  loaded fine over the VPN); the problem is specifically its backend API.

## Step 5 — finding fabrary.net's real backend

The in-app error page named an API, not the site itself, so the fix needed
to target the right host. Downloaded fabrary.net's JS bundle
(`assets/index-Bz_oH4fX.js`) and grepped it for hostnames rather than
guessing. Found:

- `content.fabrary.net` — a second Cloudflare-fronted hostname (resolved to
  the same IPs already tracked).
- An AWS AppSync GraphQL endpoint:
  `42xrd23ihbd47fjvsrt27ufpfe.appsync-api.us-east-2.amazonaws.com/graphql`.
- AWS Cognito for auth: `cognito-identity.` and `cognito-idp.` (region was
  templated in the bundle, not hardcoded in the URL itself).

Region confirmed as `us-east-2` — not guessed — by finding the actual
Cognito identity pool ID (`us-east-2:e50f3ed7-32ed-4b22-a05e-10b3e7e03fe0`)
and user pool ID (`us-east-2_ZqRBXmgea`) hardcoded elsewhere in the same
bundle.

Extended `split-tunnel.ps1`'s `-Add` to merge into existing state instead of
refusing when entries already exist (it originally required `-Remove`
first), so the new hosts could be added without disturbing the working
fabtcg.com fix.

Added a separate `add-fabrary-api.ps1` wrapper for the 4 new domains rather
than one long `-Domains a,b,c,d` command — a long comma-separated argument
pasted into a narrow/bordered terminal got line-wrapped mid-hostname
(`amazonaws.com` split across two lines by the terminal itself), silently
truncating the domain into garbage that failed DNS resolution. A script
file sidesteps that entirely.

Ran it: 16 more IPs added across the 4 new hosts (`content.fabrary.net`
resolved to the same 3 already-tracked Cloudflare IPs, as expected; AppSync
→ 4 IPs in `99.84.118.0/24`; both Cognito hosts → 3 IPs each).

## Step 6 — still broken; captured a HAR to stop guessing

Login still looped identically. Rather than keep guessing at what else
might be missing, had the user reproduce the login attempt with Firefox's
Network panel (Persist Logs on) and export the whole session as a HAR file.

The HAR was 9 MB — too big to read directly, and no `jq` available on this
box. Wrote a small Node script (`parse-har.js`, in the session scratchpad,
not checked in) to load it, strip static assets, and group non-2xx/failed
requests by host + status.

Found the exact failure: two `403`s against
`42xrd23ihbd47fjvsrt27ufpfe.appsync-api.us-east-2.amazonaws.com`, but the
HAR's `serverIPAddress` for those requests was `99.84.234.42` — **not any of
the four IPs we'd whitelisted** (`99.84.118.4/12/47/112`), despite being the
exact same hostname.

## Step 7 — the AppSync host has no fixed IP set at all

Re-resolved the same AppSync hostname 5 times in a row, a few seconds apart:
consistently returned the same `99.84.118.0/24` cluster that session — but
that still didn't match what the browser had actually connected to
(`99.84.234.x`). Re-resolved again while writing this up and got a *third*,
completely unrelated range (`13.35.107.0/24`).

This isn't DNS caching or occasional staleness — it's CloudFront/Route53
steering each lookup across a large edge-IP pool, seemingly not even
consistently tied to one geographic PoP for repeated lookups from the same
resolver. A static per-IP allowlist structurally cannot keep up with this,
no matter how often `-Refresh` runs. (The two Cognito hosts, by contrast,
resolved to a small, stable set and worked fine via the same mechanism —
this problem is specific to AppSync's CloudFront front-end, not IP-based
split-tunneling in general.)

## Step 8 — evaluated options, landed on a different mechanism entirely

Two IP-based options considered and rejected/deferred:

- **Whitelist all of AWS's published CloudFront IP ranges.** Bounded and
  documented (`ip-ranges.json`), but a real blast-radius increase: any
  CloudFront-hosted site — not just fabrary.net — would ride through the
  kill switch if anything else on the machine happened to hit one of those
  IPs.
- **A local (laptop-side) name-based proxy + PAC file.** Solves the IP
  rotation problem (matches by hostname, not IP), but only protects this
  one laptop, and is real new infrastructure to build and maintain.

User's own suggestion changed the shape of the problem for the better:
**run the proxy on the LAN's Pi-hole instead of the laptop.** This turned
out to be strictly better, for reasons specific to this network:

1. The Pi-hole was never inside the VPN tunnel — ProtonVPN is a client
   installed only on the Windows laptop, not a network-wide/router-level
   VPN. The Pi-hole's own outbound traffic is just normal ISP traffic; no
   kill switch, no rotating-IP problem, nothing to work around.
2. The laptop can already reach the Pi-hole over the LAN with the VPN
   connected — `vpn-lan-access`'s existing fix already permits
   `192.168.0.0/24` outbound past the kill switch. No new firewall work
   needed for the laptop → Pi-hole leg.
3. Any device on the LAN pointed at the Pi-hole's PAC file gets the fix,
   not just this one laptop.

Wrote the plan up in `network-config/hosts/luca-pihole/MIGRATION.md`
(separate repo) rather than implementing immediately: that box is currently
running EOL Ubuntu 20.04 with Pi-hole v4.4, with an existing migration plan
to a fresh OS + fresh Pi-hole not yet executed. Installing a new listening
service (tinyproxy) on an unpatched, unsupported OS isn't worth it — the
proxy plan is queued as a step to do once that rebuild happens.

## Current state

| Site | Status | Mechanism |
|---|---|---|
| `fabtcg.com` | **Fixed** | `split-tunnel.ps1` (routes + WFP filters, laptop-only, non-persistent) |
| `fabrary.net` static content | Was never actually broken | — |
| `fabrary.net` login/API | **Still broken** | Needs the Pi-hole proxy (queued, not yet built) |

## Summary of wrong turns, for next time

| Attempt | Why it fell short |
|---|---|
| Assume the `fabtcg.com` 403 was VPN-caused | It was curl's missing User-Agent triggering the site's WAF — unrelated to the VPN. Confirmed by retesting with a browser UA. |
| Whitelist only `fabrary.net`'s own resolved IPs | The actual failure was against a separate API host (`*.appsync-api...amazonaws.com`) never referenced by the root domain's DNS at all — had to read the JS bundle to find it. |
| Whitelist the AppSync host by its resolved IPs | CloudFront-fronted; the edge IP pool is large enough that three resolutions in a row returned three different `/24`s, none matching what the browser actually connected to. `-Refresh` can't fix a target that moves faster than you can poll it. |
| Local (laptop-only) proxy as the next idea | Would have solved the IP-rotation problem, but only for one device — the Pi-hole option solves the same problem for the whole LAN with none of this laptop's kill-switch complexity, since it was never in the tunnel to begin with. |
