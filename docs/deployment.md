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
2. **A DNS name for the webhook** — create an `A` record at your DNS host,
   e.g. `ghostsip.yourdomain.co.uk` → the VPS IP, once you know the IP
   (stage 1). Caddy can't fetch its TLS certificate until this resolves.
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

**Check:** `cat .env` shows all three values filled in, no placeholders left.

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
  (Hangs or certificate errors = the DNS record isn't resolving to the VPS
  yet; give it time and `docker compose restart caddy`.)
- `https://YOUR_DOMAIN/admin` → **404 — this is correct.** The public domain
  serves only the webhook; the admin is deliberately not on the internet.

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
enabled  = true
backend  = polling
logpath  = /opt/ghostsip/data/asterisk-logs/messages
           /opt/ghostsip/data/asterisk-logs/security
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
systemctl restart fail2ban
```

**Check:** `fail2ban-client status asterisk` shows the jail live with both
log paths.

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

## Troubleshooting

| Symptom | Where to look |
|---|---|
| No missed call appeared on the phones | Admin → **Events**: did the webhook arrive? Did injection run? Any `Originate FAILED`? Work backwards from the first missing step. |
| Webhook never arrives | `https://YOUR_DOMAIN/healthz` from outside (Caddy/DNS ok?); credentials in the VoIPstudio webhook URL match Configuration → Webhook? Events shows `Webhook auth failed` if not. |
| Phone won't register | `docker compose exec ghostsip asterisk -rx "pjsip show endpoints"`; port 5560/UDP in the phone config and ufw; endpoint name and password match the Handsets page; after handset changes, was **Reload Asterisk** pressed? |
| Trunk not registered | `... "pjsip show registrations"`; seat credentials in Configuration; VoIPstudio dashboard shows the seat offline? |
| Callback doesn't ring out | Is **lockdown** engaged (Configuration page shows status)? Trunk registered? The dialplan only routes UK national numbers (0…) by design. |
| Container won't start | `docker compose logs ghostsip` — the entrypoint names any missing `.env` variable explicitly. |
| Live SIP debugging | `docker compose exec ghostsip asterisk -rvvv`, then `pjsip set logger on` (`pjsip set logger off` when done). |
| Locked out of admin (django-axes) | wait 10 minutes, or `docker compose exec ghostsip /opt/ghostsip/venv/bin/python /opt/ghostsip/receiver/manage.py axes_reset` |
| Pushover silent | Configuration → **Send test Pushover**; check Events for send errors; quiet hours only defer normal-priority pushes (brute-force alerts are high priority and bypass them). |
