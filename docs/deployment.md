# GhostSIP — Install Guide

Complete instructions for taking GhostSIP from nothing to a working system.
Follow it top to bottom on a fresh server; nothing assumes prior Docker or
Linux knowledge beyond copy-pasting commands. Each stage ends with a check —
don't move on until the check passes.

**The shape of what you're building:** one small VPS runs a Docker Compose
stack — a `ghostsip` container (Asterisk + the Django admin/webhook app
together) and a `caddy` container (automatic HTTPS). The public internet can
reach exactly three things: SIP on port 5560, the VoIPstudio webhook, and a
bare health check. The admin is reached only through an SSH tunnel.

**Install checklist:**

- [ ] 0 — Accounts and prerequisites gathered
- [ ] 1 — VPS created, first login, OS updated
- [ ] 2 — SSH keys set up, password login disabled
- [ ] 3 — Docker installed
- [ ] 4 — GhostSIP cloned, `.env` written
- [ ] 5 — Firewall configured
- [ ] 6 — Stack built and started; public URL and admin tunnel verified
- [ ] 7 — fail2ban watching Asterisk
- [ ] 8 — Everything configured in the admin
- [ ] 9 — Phones configured and registered
- [ ] 10 — VoIPstudio webhook created
- [ ] 11 — Acceptance tests (A/B/C) run

---

## 0. What you need before starting

Gather these first; every later stage assumes them.

1. **A VPS account** — DigitalOcean, Vultr, Linode/Akamai, Mythic Beasts or
   similar. Spec when creating the server:
   - **Ubuntu 24.04 LTS** (the OS this guide and the stack are tested on)
   - **2 GB RAM, 1–2 vCPU, 20 GB disk** (~£5–6/mo tier)
   - **London region** (callback audio relays through this box; keep the
     round-trip short)
   - **A dedicated IPv4 address** (standard on the providers above — avoid
     any "IPv6-only" or "NAT VPS" budget tier)
   - Turn on the provider's **automated snapshot/backup** add-on (~£1/mo).
2. **A DNS name for the webhook** — a subdomain of a domain you own, e.g.
   `ghostsip.yourdomain.co.uk`. You'll create one `A` record pointing it at
   the VPS IP (stage 1), at whatever service manages that domain's DNS —
   your registrar's control panel, or cPanel → Zone Editor if the domain is
   on Krystal/shared hosting. **This is NOT set in the VPS panel** and has
   nothing to do with the long auto-generated hostname the VPS came with;
   that hostname is just a label for the IP and should not be used here
   (it ties you to the provider and can hit shared certificate limits).

   > **DNS propagation takes time.** A brand-new record, or one on a domain
   > whose nameservers you've just changed, can take anywhere from a few
   > minutes to a few hours (occasionally up to 24–48 h) to be visible
   > everywhere. Create the record as early as possible — ideally right
   > after stage 1 when you know the IP — so it has propagated by the time
   > you reach stage 6. Caddy cannot fetch the TLS certificate until the
   > name resolves to the VPS from the public internet. Check progress with
   > `nslookup ghostsip.yourdomain.co.uk` (from your PC) or
   > [dnschecker.org](https://dnschecker.org); when it returns the VPS IP,
   > you're good.
3. **A dedicated VoIPstudio "trunk-back" seat** — an ordinary extra user/seat
   on the VoIPstudio account whose **SIP username, password and registrar**
   you can read in the dashboard (the same details you used to configure the
   VVXes manually). While in the dashboard, set **international call
   barring** on that seat — it's one of the anti-fraud layers.
4. **A Pushover account** (optional but recommended, ~one-off $5 mobile
   licence) — note your **User Key**, and create an *Application* called
   GhostSIP to get an **API Token**.
5. **Access to the GhostSIP repo** (`github.com/ken515151/ghostSIP`) —
   currently public, so the clone in stage 4 needs nothing. If it's ever
   made private again, create a GitHub **fine-grained personal access
   token** with read-only access to just this repo and clone with
   `https://USER:TOKEN@github.com/...` instead.

## 1. Create the VPS and log in

Create the server per the spec above, note its IP, and create the DNS record
now so it has time to propagate.

From your Windows machine (PowerShell — OpenSSH is built into Windows 10/11):

```powershell
ssh root@YOUR_VPS_IP
```

Accept the host-key prompt, log in with the password/key the provider gave
you, then bring the OS current:

```bash
apt update && apt upgrade -y
timedatectl set-timezone Europe/London
reboot
```

**Check:** after a minute, `ssh root@YOUR_VPS_IP` works again and
`date` shows UK time.

## 2. SSH keys (do this before anything else)

The SSH login is GhostSIP's real security boundary — the admin panel sits
behind it — so make it key-only now.

On your Windows PC (PowerShell):

```powershell
ssh-keygen -t ed25519        # accept defaults; a passphrase is sensible
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@YOUR_VPS_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

Confirm key login works — **in a new window, before locking the door**:

```powershell
ssh root@YOUR_VPS_IP         # should log in with NO password prompt
```

Only once that works, disable password login on the VPS:

```bash
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

**Check:** `ssh root@YOUR_VPS_IP` still works from your PC; from anywhere
without your key it's refused.

> **Locked out later?** If you ever lose this key, you are not stuck — the
> provider's web console is a separate door that still works. See
> *Recovering access if you lose your SSH key* near the end of this guide.
> Consider enrolling a second device's key now so it never comes to that.

## 3. Install Docker

Docker's official repository (Ubuntu's own packaging is older):

```bash
apt install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

**Check:** `docker --version` and `docker compose version` both print
versions.

## 4. Get GhostSIP and write the bootstrap secrets

```bash
apt install -y git
git clone https://github.com/ken515151/ghostSIP.git /opt/ghostsip
cd /opt/ghostsip
cp deploy/.env.example .env
```

Generate two strong values and edit the file:

```bash
openssl rand -base64 24      # run twice — once per password below
nano .env
```

Fill in:

| Variable | Value |
|---|---|
| `GHOSTSIP_DOMAIN` | your DNS name, e.g. `ghostsip.yourdomain.co.uk` |
| `GHOSTSIP_ADMIN_PASSWORD` | first generated value — the admin login |
| `GHOSTSIP_ARI_PASSWORD` | second generated value — internal Asterisk↔app secret; never typed anywhere else |

Save (Ctrl-O, Enter, Ctrl-X), then:

```bash
chmod 600 .env
```

These are the **only** hand-edited settings in the whole system. Everything
else is configured in the admin.

> **Save these passwords somewhere safe now** — a password manager
> (Bitwarden, 1Password, KeePass) is ideal. Record: the **admin password**
> (you log in with it), the **ARI password**, and later the **VoIPstudio
> trunk seat** and **handset SIP secrets** the admin generates. Nothing here
> can be "recovered" if lost — the admin password can be reset by editing
> `.env` and restarting, but the others just have to be regenerated and
> re-entered everywhere. A moment saving them now avoids that.

> **Where `.env` lives:** the default location this guide uses is
> `/opt/ghostsip/.env` on the VPS (because you cloned the repo to
> `/opt/ghostsip`). That's the single file to keep alongside your backups
> (see *Backup and restore*), and the file to edit if you ever change the
> admin password. It is `chmod 600` and git-ignored, so it never leaves the
> server on its own.

**Check:** `cat /opt/ghostsip/.env` shows all three values filled in, no
placeholders left.

## 5. Firewall

```bash
ufw allow OpenSSH
ufw allow 80/tcp            # ACME certificate challenge + HTTPS redirect
ufw allow 443/tcp           # the webhook
ufw allow 5560/udp          # SIP — non-standard on purpose; must match the
                            # admin's "SIP port" setting (default 5560)
ufw allow 10000:20000/udp   # RTP audio (matches asterisk/rtp.conf)
ufw default deny incoming
ufw enable                  # answer y
```

The stack uses **host networking** deliberately, so Docker cannot punch
holes around ufw (the classic published-ports gotcha) — the rules above are
the complete exposure. ARI (8088) and the admin (8100) bind to loopback and
are never reachable from outside.

**Check:** `ufw status` lists exactly the rules above, `Status: active`.

## 6. Build, start, verify

```bash
cd /opt/ghostsip
docker compose up -d --build     # first build takes a few minutes
docker compose ps                # both containers "running"
docker compose logs -f           # Ctrl-C to stop watching
```

In the logs you should see, in order: database migrations applying, `admin
user created: admin`, `Asterisk Ready.`, then supervisord reporting
`asterisk`, `web` and `watcher` all `RUNNING`, and finally `Lockdown state
asserted in Asterisk: active=False`.

**Check (public side)**, from your own PC's browser:

- `https://YOUR_DOMAIN/healthz` → `{"ok": true}` with a valid padlock.
- `https://YOUR_DOMAIN/admin` → **404 — this is correct.** The public domain
  serves only the webhook; the admin is deliberately not on the internet.

> **If healthz hangs or shows a certificate error, it's almost always DNS,
> not GhostSIP.** Caddy can only get the certificate once your domain
> resolves to the VPS from the public internet (stage 0). Confirm with
> `nslookup YOUR_DOMAIN` from your PC — if it doesn't return the VPS IP yet,
> the record simply hasn't propagated; wait and re-check (it can take minutes
> to hours). The GhostSIP containers are already up and healthy regardless —
> you can watch the certificate attempts with `docker compose logs -f caddy`,
> and once DNS resolves Caddy retries on its own (or nudge it with
> `docker compose restart caddy`). Meanwhile the admin (below) works
> immediately over the SSH tunnel — it doesn't depend on DNS at all.

**Check (admin side)**, the SSH tunnel — from your PC:

```powershell
ssh -L 8100:127.0.0.1:8100 root@YOUR_VPS_IP
```

Leave that window open, browse to **http://127.0.0.1:8100/admin**, and log
in as `admin` with your `GHOSTSIP_ADMIN_PASSWORD`. You should see the
GhostSIP administration index (Configuration, Handsets, Events, Known
addresses).

Make the tunnel a double-click: save this as `ghostsip-admin.cmd` on your
desktop —

```bat
start http://127.0.0.1:8100/admin
ssh -L 8100:127.0.0.1:8100 root@YOUR_VPS_IP
```

(The browser opens, the terminal window holds the tunnel; close the window
when done.)

## 7. fail2ban — ban SIP brute-forcers at the host firewall

The container writes Asterisk's `messages` and `security` logs to
`/opt/ghostsip/data/asterisk-logs/` on the host precisely so fail2ban can
read them:

```bash
apt install -y fail2ban
cat > /etc/fail2ban/jail.d/ghostsip-asterisk.conf <<'EOF'
[asterisk]
enabled   = true
backend   = polling
logpath   = /opt/ghostsip/data/asterisk-logs/messages
            /opt/ghostsip/data/asterisk-logs/security
maxretry  = 5
findtime  = 10m
bantime   = 24h
; Escalate: each repeat offence multiplies the ban (24h → 48h → 96h …),
; capped at 4 weeks. A first mistake costs a day; a persistent scanner
; earns itself a month.
bantime.increment = true
bantime.maxtime   = 4w
; Ban on ALL ports. The stock asterisk jail assumes SIP on 5060/5061, so
; its bans would miss our port entirely (found the hard way: a "banned" IP
; kept registering on 5560). Allports also survives changing the SIP port.
banaction = %(banaction_allports)s
; Trusted phone sites — NEVER ban these. Phones share their site's public
; IP, so one mis-registering phone could otherwise lock out every phone at
; that location for the whole ban. Space-separated; keep loopback entries.
ignoreip  = 127.0.0.1/8 ::1 YOUR_SHOP_PUBLIC_IP
EOF
systemctl restart fail2ban
```

**Check:** `fail2ban-client status asterisk` shows the jail live with both
log paths.

> Escalating bans need fail2ban to remember offenders across restarts,
> which it does by default (its own SQLite db at
> `/var/lib/fail2ban/fail2ban.sqlite3`) — nothing extra to set up.

Useful later: `fail2ban-client status asterisk` lists currently banned IPs;
`fail2ban-client set asterisk unbanip SOME_IP` frees one immediately. If a
phone site's IP ever changes and its phones get banned mid-setup, that's
the release valve — then update `ignoreip` and `systemctl restart fail2ban`.

(The other anti-fraud layers are already in place by design: the dialplan
only routes UK national numbers out via the trunk seat, SIP secrets are
32-character generated values, lockdown exists, and you set provider-side
barring on the seat in stage 0. See docs/decisions.md — no phone site has a
static IP, so there is deliberately no source-IP allowlist.)

## 8. Configure everything in the admin

Through the tunnel, in the admin:

**GhostSIP → Configuration** (opens straight onto the single settings page):

1. **Webhook**: choose a username (e.g. `vswebhook`); a strong password is
   already pre-generated. Note both — they become part of the webhook URL in
   stage 10.
2. **SIP listener**: leave 5560 unless you have a reason.
3. **Ghost-call behaviour**: defaults are fine. Set **Caller name prefix**
   to the exact ring-group name VoIPstudio prepends on real calls (so ghost
   entries look identical in the phones' call logs). Leave **Debug: log full
   payloads** off until test A.
4. **Trunk-back seat**: the SIP username / password / registrar of the
   dedicated VoIPstudio seat from stage 0.
5. **Pushover alerts**: User Key + API Token from stage 0, tick **Enabled**.
   Leave *Alert on new device address* on; tick *app errors* too if you want
   them. (Leave **Auto-lockdown** OFF until the phones are rolled out —
   each phone's first registration would otherwise trip it.)
6. **Save**, then use the top-right buttons: **Reload Asterisk** (applies
   the generated config) and **Send test Pushover** (a test push should
   arrive on your phone).

**GhostSIP → Handsets → Add handset** for each VVX: a display name
(e.g. `Front desk`) and an endpoint name (e.g. `phone1` — this doubles as
the SIP username). The SIP password is auto-generated; **copy it now** for
the phone config. Save, then **Reload Asterisk** from the Configuration
page.

> **Order matters: Save + Reload Asterisk BEFORE configuring the phone.**
> Until the reload, the endpoint doesn't exist in Asterisk, so the phone's
> perfectly-correct credentials are rejected — it retries continuously,
> and those rejections look exactly like a brute-force attack to fail2ban.
> With the site's IP in `ignoreip` (stage 7) it's only log noise, but the
> tidy sequence is: add handset → Save → Reload Asterisk → then point the
> phone at the server. Same rule every time settings change: **Save writes
> the config; Reload Asterisk applies it** — nothing changes live until
> the reload.

**Check:** the trunk seat registers to VoIPstudio —

```bash
docker compose exec ghostsip asterisk -rx "pjsip show registrations"
```

should show the registration as `Registered`. **GhostSIP → Events** should
show your configuration saves and no errors.

## 9. Configure the phones

Full per-handset detail: [phones/vvx-ghostsip-line.md](../phones/vvx-ghostsip-line.md).
In short, on each VVX's web UI, add a new line on the next free registration
slot:

- **Server**: `YOUR_DOMAIN` (or the VPS IP), port **5560**, UDP
- **SIP user / Auth user**: the handset's endpoint name (e.g. `phone1`)
- **Auth password**: the generated secret from the Handsets page
- Keep the VoIPstudio line as the default outbound line.

**Check:**

```bash
docker compose exec ghostsip asterisk -rx "pjsip show endpoints"
```

Each configured phone shows a registered contact (`Avail`). Expect one
"new device address" Pushover per phone as it first registers — that's the
address-learning working. Once **all** phones are rolled out, go back to
Configuration and tick **Auto-lockdown** if you want the automatic
credential-theft response armed.

## 10. Point VoIPstudio at it

In the VoIPstudio dashboard add a **new, separate** webhook (leave the
existing Query Tracker webhook untouched):

- **URL**: `https://WEBHOOK_USER:WEBHOOK_PASS@YOUR_DOMAIN/webhook`
  — the username and password from stage 8.1 embedded in the URL
- **Event**: `call.missed`

**Check:** abandon a quick test call to the ring group; **GhostSIP →
Events** shows the webhook arriving (and, with a phone registered, an
injection).

## 11. Acceptance tests

Run [test-plan.md](test-plan.md) in order:

- **A — payload verification**: turn on *Debug: log full payloads*, make one
  abandoned and one answered call to the ring group, read the raw JSON in
  Events, confirm the expectations listed in the test plan, turn the toggle
  off.
- **B — CANCEL semantics**: capture a ghost call and confirm no
  `SIP;cause=200` (pre-verified in development — the CANCEL carried
  `Q.850;cause=0` — but confirm on real hardware, and confirm the VVX logs
  the missed call). Capture on the host:

  ```bash
  apt install -y tcpdump
  tcpdump -i any -w /tmp/ghost.pcap udp port 5560
  ```

- **C — badge/display**: ghost entries look right on the VVX next to real
  missed calls, including the caller-name prefix.

An Asterisk console any time:

```bash
docker compose exec ghostsip asterisk -rvvv    # 'exit' leaves it running
```

---

## Day-2 operations

| Task | How |
|---|---|
| Watch activity | Admin → **Events** (filterable/searchable), or `docker compose logs -f ghostsip` |
| Restart the stack | `cd /opt/ghostsip && docker compose restart` |
| Update GhostSIP | `cd /opt/ghostsip && git pull && docker compose up -d --build` — DB migrations run automatically at start |
| Update Caddy | `docker compose pull caddy && docker compose up -d` |
| OS security updates | `apt update && apt upgrade -y`, or `apt install unattended-upgrades` once and forget |
| Change the admin password | edit `.env`, then `docker compose restart ghostsip` (the container re-applies it) |
| Rotate the trunk seat password | change it in the VoIPstudio dashboard, paste into Configuration, Save, Reload Asterisk |
| Lockdown | Configuration page top-right: **Engage/Lift lockdown**, **Suspend auto-lockdown 1 h** (for planned new-phone setup) |
| Settings safety net | Configuration page top-right: **Export settings** downloads all hand-entered config + handsets (incl. secrets — store like a password); **Import settings** faithfully restores such a file. Export before fiddling. |

## Backup and restore

**What to back up** — two things, tiny:

1. `/opt/ghostsip/data/ghostsip/` — the SQLite database (all settings,
   handsets, events), the generated `pjsip.conf`, and the Django secret key
2. `/opt/ghostsip/.env`

Copy them off the VPS periodically (from your PC:
`scp -r root@VPS:/opt/ghostsip/data/ghostsip .` plus the `.env`), and keep
the provider's snapshots on. **Treat backups like the server** — they
contain the SIP and webhook secrets in plaintext.

**Restore / rebuild from nothing:** new VPS → stages 1–7 → copy your saved
`.env` and `data/ghostsip/` back into place → `docker compose up -d --build`.
Phones re-register on their own; VoIPstudio needs nothing (the domain
followed you via DNS).

## Recovering access if you lose your SSH key

Key-only SSH (stage 2) means a lost or wiped key would normally lock you
out — **but you are never actually locked out**, because the VPS provider's
**web console** is a completely separate door that doesn't use SSH at all.
Read this now, before you need it; once you're locked out you can't open
this file on the server.

> **The system keeps running the whole time.** SSH only gates
> *administration*. Phones keep getting missed-call injections and callbacks
> while you sort access out — so this is never an emergency, fix it at
> leisure.

**Recovery steps (Krystal panel → your VPS → Console):**

1. Open the **web console / VNC** for the VPS in the Krystal control panel
   and log in as `root`.
   - If the root password was never set or you've forgotten it, most panels
     have a **"reset root password"** button; use it, then log in. The
     console is not governed by `sshd_config`, so `PasswordAuthentication no`
     does not block it.
2. On the **new machine** you want to use, create a fresh key and copy its
   public half:
   ```powershell
   ssh-keygen -t ed25519
   type $env:USERPROFILE\.ssh\id_ed25519.pub
   ```
3. In the console, add that key and remove the lost one:
   ```bash
   nano ~/.ssh/authorized_keys
   ```
   Paste the new public key on its own line; delete the line for the lost
   key (dead weight, and a liability if the old key was stolen rather than
   just lost). Save, then:
   ```bash
   chmod 600 ~/.ssh/authorized_keys
   ```
4. From the new machine, confirm `ssh root@YOUR_VPS_IP` logs in with the new
   key.

**Make future-you's life easier — enrol a second key today.** Add a key from
a second device (another PC, or a phone with an SSH app) to
`~/.ssh/authorized_keys` now, one per line. Then losing one key isn't even a
console trip — you SSH in from the other device and add a replacement. Also
put a **passphrase** on each key: a stolen key file is then useless without
it, so "lost" and "stolen" become the same low-severity event.

**What the console can NOT recover:** the server itself. If the VPS is
destroyed, the console goes with it — that is what the off-box backup
(`data/ghostsip/` + `.env`) and the provider snapshots above are for. Keys
are cheap to reissue; the database and secrets are the irreplaceable part.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Anything at all | Admin → Configuration → **System status** (top-right button): live trunk registration, phone endpoint states, and a real TLS + DNS + webhook reachability check with certificate expiry — most questions answer themselves there. |
| No missed call appeared on the phones | Admin → **Events**: did the webhook arrive? Did injection run? Any `Originate FAILED`? Work backwards from the first missing step. |
| Webhook never arrives | `https://YOUR_DOMAIN/healthz` from outside (Caddy/DNS ok?); credentials in the VoIPstudio webhook URL match Configuration → Webhook? Events shows `Webhook auth failed` if not. |
| Phone won't register | `docker compose exec ghostsip asterisk -rx "pjsip show endpoints"`; port 5560/UDP in the phone config and ufw; endpoint name and password match the Handsets page; after handset changes, was **Reload Asterisk** pressed? |
| Phone stays "Unavailable" for a while after a rebuild | Normal, but should self-recover within ~2 min (the server caps registration to 120 s). If a phone was registered before the fix that introduced this cap, it still holds its old long lease — reboot it once (or toggle its line) so it re-registers under the new cap; every rebuild after that recovers on its own. To confirm the cap is applied: `pjsip set logger on`, watch a REGISTER in `docker compose logs -f ghostsip`, and check the `200 OK`'s `Contact:` header ends with `;expires=120` (the phone may request 3600; Asterisk granting 120 is the proof). `pjsip set logger off` when done. |
| Trunk not registered | `... "pjsip show registrations"`; seat credentials in Configuration; VoIPstudio dashboard shows the seat offline? |
| Callback doesn't ring out | Is **lockdown** engaged (Configuration page shows status)? Trunk registered? The dialplan only routes UK national numbers (0…) by design. |
| Container won't start | `docker compose logs ghostsip` — the entrypoint names any missing `.env` variable explicitly. |
| Live SIP debugging | `docker compose exec ghostsip asterisk -rvvv`, then `pjsip set logger on` (`pjsip set logger off` when done). |
| Locked out of admin (django-axes) | wait 10 minutes, or `docker compose exec ghostsip /opt/ghostsip/venv/bin/python /opt/ghostsip/receiver/manage.py axes_reset` |
| Pushover silent | Configuration → **Send test Pushover**; check Events for send errors; quiet hours only defer normal-priority pushes (brute-force alerts are high priority and bypass them). |
