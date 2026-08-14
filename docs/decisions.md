# GhostSIP — "decide during build" items

The spec (v2) leaves a handful of choices to be settled while building, and
this repo picks a default for each so it runs out of the box. Each is
adjustable — here's the default, why, and what would change it.

## Payload semantics — borrowed from Query Tracker's proven captures

The spec called for a blind payload capture (test A) before writing the
receiver. Better source found: Query Tracker's `voip_webhook.php` receives
this exact VoIPstudio feed and its comments record capture-proven field
semantics (`root_call_id` grouping legs, per-leg `call.missed` artifacts,
the `final` flag, bare-44 `src` format). The receiver is built on those, and
test A shrinks to verifying the abandoned-call specifics — see
[test-plan.md](test-plan.md). Two design consequences:

- **Act only on `final: true`** — per-leg `call.missed` events fire even for
  calls that end up answered ("answered elsewhere" artifacts); injecting on
  them would create ghost missed calls for answered calls.
- **Caller numbers are normalised to UK national** (`441224…` → `01224…`),
  mirroring Query Tracker's `normalise_phone_input`, so the missed-call entry
  reads like a normal number and dials back correctly.

## Include missed *direct* calls, or only ring-group?

- **Default:** ring-group only. **Settled by the live test-A capture
  (14 Aug 2026):** VoIPstudio sends `context: "RG-<id>"` (e.g. `RG-83502`)
  for ring-group calls — not the `"Queue"` their docs claimed (wrong about
  payload fields again). The filter accepts the `RG-` prefix, `Queue` (in
  case it ever appears) or an absent field. The same capture confirmed
  `final: true`, `terminated_by: "caller"`, per-leg `call_id` vs
  `root_call_id`, bare-44 `src`, and the CLI display-name prepend format
  `"TechRescue | <number>"` (so the admin's caller-name prefix should be
  set to `TechRescue |` to match).
- **Why:** the diagnosed defect (spec §2) is specifically the
  `SIP;cause=200` sent on abandoned ring-group calls. Direct calls to a single
  extension generally log their own missed call already, so injecting there
  risks duplicates.
- **Change it:** toggle **"Inject for direct calls too"** in the admin panel
  (Settings → Ghost-call behaviour), or `include_user_context` in config.json.
  Turn it on only if test A shows direct missed calls are *also* suppressed.

## Dedup strategy

- **Default:** key on `root_call_id` (the id that groups a group-ring's legs
  — deduping on `call_id` would let each leg's event through); ignore repeats
  within a 120 s window (admin-configurable).
- **Why:** one abandoned call must yield exactly one ghost call per handset
  (spec §4.2), and a group ring fires events per leg.
- **Change it:** adjust the dedup window in the panel.

## Busy guard (skip injection if the phone is on a call)

- **Default:** **not implemented.** A ghost call rings ~4 s and is never
  answered; arriving during an active call is a brief, non-disruptive event,
  and the optional silent Alert-Info ring removes even the chirp.
- **Why:** the spec offers this as optional ("decide during build"); the VVX
  REST status check adds moving parts for little gain at this scale.
- **Change it:** if real use shows ghost calls interrupting live calls audibly,
  add a check (VVX REST call status, or a short delay-and-retry) in
  `_originate`. Left as a documented gap rather than silently skipped.

## Caller display name — matching VoIPstudio's ring-group prepend

- **Default:** empty (ghost entries show just the number).
- **Why it exists:** VoIPstudio is set to prepend the ring-group/company name
  to the caller ID on real calls, so the handsets show e.g.
  "TechRescue 01224…". Ghost entries should look identical or they'd stand
  out in the call log. This is display-name only — the webhook `src` is the
  bare number (QT-proven) and the callback dials the URI's number part, so
  nothing functional depends on it.
- **Change it:** set **Caller name prefix** in the admin panel (Settings →
  Ghost-call behaviour) to the same text VoIPstudio prepends. Check the match
  visually during test C.

## Alert-Info silent ring

- **Default:** off (empty Alert-Info). Ghost calls ring normally for ~4 s.
- **Why:** it's polish, and the exact UCS mapping needs verifying on the
  handsets' firmware (see [phones/](../phones/)).
- **Change it:** set the Alert-Info value in the panel and map it to a silent
  ring class on each VVX. **Caveat:** injecting the header via the ARI
  originate `variables` (`PJSIP_HEADER(add,…)`) needs confirming during test B;
  if it doesn't reach the wire, switch the originate to a Local channel with a
  pre-dial handler that adds the header.

## No source-IP allowlisting — layered SIP exposure instead

- **Fact established 14 Aug 2026:** no phone location (shop included, most
  likely) has a static IP, so the spec's "allowlist SIP to the shop's static
  IP" isn't available. SIP listens openly.
- **Compensating controls** (docs/deployment.md): UK-national-only
  dialplan (international premium fraud unroutable even with a stolen
  credential), VoIPstudio-side call barring on the trunk seat, generated
  24-byte SIP secrets, fail2ban, and a non-standard SIP port (5560 by
  default — decided 14 Aug 2026; GUI-configurable, kills ~99% of scanner
  noise; a port change needs the ufw rule and every phone's server port
  updated to match, then a stack restart).
- **Accepted residual risk:** a stolen phone credential could make UK-rate
  calls billed to the shop until noticed — bounded by fail2ban and the
  trunk seat's own limits.
- **Change it:** if any site gains a static IP, add a ufw allowlist rule for
  it; the rest of the layers stay.

## Alerting — Pushover

- **Default:** off until keys are entered (admin → Configuration → Pushover
  alerts). Once enabled, a **high-priority** push fires when ≥5 failed SIP
  auth/registration attempts land within 10 minutes (the watcher tails
  Asterisk's security log — same container, so it's just a file). At most
  one brute-force alert per hour. A **normal-priority** push also fires when
  an endpoint successfully authenticates from an address it has never used
  before (on by default; known addresses persist in the database, visible
  in admin) — deliberately NOT on every registration, since phones
  re-REGISTER every few minutes and that would be pure spam; the new-address
  event is the one that means "credential in use from somewhere new".
  Optional third toggle: normal-priority push when an ERROR is recorded,
  max one per 15 minutes. Delivery is via Apprise.
- **Why:** with SIP open to the internet and no source-IP allowlist, the
  brute-force tripwire is the "someone is guessing passwords" signal;
  fail2ban does the banning, Pushover does the telling-Ken.
- **Change it:** thresholds/cooldowns are constants at the top of
  receiver/panel/services/alerts.py.

## Receiver rewritten on Django (decided 14 Aug 2026)

- **Decision (Ken's):** "no more hand-rolled iffyness" — the app must stand
  on proven, maintained foundations, because it will be looked at roughly
  once a year. The FastAPI receiver with its hand-assembled auth, CSRF
  trick, JSON-config round-trip, hand-written panel UI, Pushover client and
  in-memory log buffer was replaced wholesale.
- **What it became:** Django 5.2 LTS (security-supported to 2028) +
  django.contrib.admin (the whole UI, generated from models) +
  django.contrib.auth (hashed passwords, sessions) + django-axes (login
  lockout) + Django's CSRF middleware + SQLite/ORM/migrations + Apprise
  (Pushover delivery) + gunicorn/whitenoise. Events, known device
  addresses and the injection dedup guard now persist in the database —
  the dedup is thereby also correct across gunicorn workers, which the old
  in-memory dict was not.
- **What survived unchanged:** the ~250-line domain core (webhook filter on
  the QT-proven payload semantics, ghost originate, pjsip render, security
  log watcher, lockdown), the container/Caddy/Asterisk architecture, and
  every behaviour decision in this file. All prior test scenarios were
  ported to Django TestCase (21 tests) and the full stack was re-verified
  live in Docker — including a fake SIP phone registering and receiving a
  ghost INVITE whose CANCEL carried `Q.850;cause=0`, i.e. **no
  SIP;cause=200** (pre-validating test B's wire-side condition).

## Admin panel is NOT internet-facing (decided 14 Aug 2026)

- **Decision (Ken's, from hard experience):** no home-grown login system
  gets exposed to the internet — Query Tracker went down that road once and
  hardening it after the fact took ages. The public domain serves ONLY
  `/webhook` (+ bare `/healthz`); every other path 404s at Caddy.
- **How the panel is reached instead:** SSH tunnel only
  (`ssh -L 8100:127.0.0.1:8100`, then http://127.0.0.1:8100/admin) — the
  security boundary is OpenSSH with key auth, about the most proven login
  system in existence. No VPN and no desktop environment on the VPS (both
  considered and rejected: Tailscale unwanted, xrdp adds RAM + attack
  surface for the same outcome). Phones are unaffected (they speak SIP,
  not HTTP).
- **Django's session login, CSRF middleware and django-axes lockout stay**
  as defence-in-depth behind the tunnel, but they are not the front line
  and must never become one: any future route that would publish /admin
  (or any new UI) to the public internet is wrong by default.

## Encryption status — what is enforced vs what is not (audited 14 Aug 2026)

Enforced, not merely preferred:

- **HTTPS for the webhook:** Caddy redirects HTTP→HTTPS automatically and
  only serves over TLS (1.2+); the app itself listens on loopback only, so
  there is no plaintext path from outside. Webhook credentials only ever
  cross the wire encrypted. The admin crosses the network solely inside the
  SSH tunnel's encryption.
- **Pushover:** HTTPS API.
- **ARI and the receiver:** plaintext HTTP but bound to 127.0.0.1 inside
  the container/host — never reachable externally (and never exposed in
  ufw).

NOT encrypted — known, accepted for now:

- **SIP signalling (UDP 5560):** digest authentication means passwords
  never cross the wire (and 32-char random secrets make offline cracking of
  a captured challenge infeasible), but the signalling itself — caller
  numbers, call metadata — is readable by an on-path observer.
- **RTP audio for callbacks:** cleartext G.711 across the internet between
  phone and VPS, like most SIP trunking today. An on-path (ISP-level)
  attacker could listen to a callback. Note the phones' existing VoIPstudio
  and Voipfone lines have the same property unless TLS/SRTP was enabled
  there.
- **Upgrade path if wanted:** PJSIP TLS transport + SRTP on the GhostSIP
  line (VVX supports both); cost is cert management on the transport and
  per-phone config. The VoIPstudio leg's encryption is theirs to offer.
  Worth doing if ever serving client sites; optional polish for the shop.

## Lockdown — suspend the outbound relay

- **What it is:** a panel-controlled kill switch for the outbound callback
  relay — the only path on this box that can cost money. Enforced by an
  Asterisk global (`GHOSTSIP_LOCKDOWN`) the dialplan checks per call, set
  via ARI: instant, no reload, survives restarts (state in the database,
  re-asserted at startup by the watcher). Ghost-call injection and the phones' normal
  VoIPstudio line are untouched — only tap-to-callback pauses, so a false
  positive costs convenience, not operations.
- **Triggers:** manual button; or automatic on a credential successfully
  authenticating from a never-seen address, armed by a toggle you switch on
  AFTER first rollout. **Deliberately never triggered by failed auth
  attempts** — those are unauthenticated noise anyone can generate, and
  auto-locking on them would hand outsiders a callback kill switch
  (fail2ban + the high-priority push cover that case).
- **Suspend (1 h):** the panel's suspend button disarms the automatic
  trigger for an hour for planned new-device/new-office setup. New-address
  alerts still send; manual lockdown still works; re-arms itself.
- **Change it:** suspend duration is `SUSPEND` in
  receiver/panel/services/lockdown.py.

## Reload mechanism for Asterisk

- **Default:** the panel's "Reload Asterisk" button runs
  `asterisk -rx 'pjsip reload'` — no sudo, no cross-container plumbing,
  because Asterisk and the receiver deliberately share one container
  (supervisord runs both; see deploy/). That co-location is *why* the GUI
  can write pjsip.conf and reload without a docker-socket mount or AMI.
- **Why:** handset changes regenerate pjsip.conf; PJSIP needs a reload to pick
  them up, and doing it from the UI keeps you out of the shell.
- **Change it:** override `GHOSTSIP_ASTERISK_RELOAD_CMD` in
  docker-compose.yml (empty disables the button; reload by hand with
  `docker compose exec ghostsip asterisk -rx 'pjsip reload'`).
