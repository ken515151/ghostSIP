# GhostSIP — deployment guide (Docker on a VPS)

The whole system runs as a Docker Compose stack on one small VPS: a
`ghostsip` container (Asterisk + the receiver/admin panel together) and a
`caddy` container (automatic HTTPS). Follow this top to bottom on a fresh
server; nothing here assumes prior Docker knowledge.

The old bare-metal (apt + systemd) instructions were removed when the Docker
shape was settled — they're in git history if ever wanted.

---

## 0. What you need before starting

- **VPS**: Ubuntu 24.04 LTS, 2 GB RAM, 1–2 vCPU, 20 GB disk, **dedicated
  IPv4**, London region. (~£5/mo — DigitalOcean, Vultr, Linode, Mythic
  Beasts all fine.) Turn on the provider's snapshot/backup add-on.
- **A DNS name**: create an `A` record, e.g. `ghostsip.yourdomain.co.uk` →
  the VPS IP. Caddy needs it resolving before first start to fetch the TLS
  certificate.
- **VoIPstudio trunk-back seat**: a dedicated seat whose SIP
  username/password/registrar you can see in the dashboard. While there, set
  **international call barring** on that seat — it's one of the anti-fraud
  layers (§6).

## 1. First login and basics

SSH in as root (or the user your provider created):

```bash
ssh root@YOUR_VPS_IP
apt update && apt upgrade -y
timedatectl set-timezone Europe/London
```

## 2. Install Docker

Docker's official repository (the Ubuntu-packaged docker is older):

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
docker --version    # sanity check
```

## 3. Get GhostSIP and configure the bootstrap secrets

First push this repo to GitHub if it isn't there yet (it currently has no
remote), then on the VPS:

```bash
apt install -y git
git clone https://github.com/YOUR_GITHUB_USER/ghostSIP.git /opt/ghostsip
cd /opt/ghostsip
cp deploy/.env.example .env
```

(If you'd rather not push to GitHub, `scp -r` the repo folder to
`/opt/ghostsip` instead — but the git route makes the update procedure in
Day-2 operations a one-liner.)

Generate the two passwords and put them in `.env`:

```bash
openssl rand -base64 24    # run twice — once per password
nano .env
```

Set in `.env`:

- `GHOSTSIP_DOMAIN` — your DNS name from step 0
- `GHOSTSIP_ADMIN_PASSWORD` — first generated value (the /admin login)
- `GHOSTSIP_ARI_PASSWORD` — second generated value (internal
  Asterisk↔receiver secret; you never type it anywhere else)

Then keep it root-only:

```bash
chmod 600 .env
```

These are the **only** hand-edited settings. Everything else is done in the
web admin panel.

## 4. Firewall — before starting the stack

```bash
ufw allow OpenSSH
ufw allow 80/tcp            # ACME certificate challenge + redirect
ufw allow 443/tcp           # webhook + admin panel
ufw allow 5560/udp          # SIP — non-standard on purpose; must match the
                            # panel's "SIP port" setting (default 5560)
ufw allow 10000:20000/udp   # RTP (matches asterisk/rtp.conf)
ufw default deny incoming
ufw enable
```

The compose file uses **host networking** deliberately, so Docker cannot
punch holes around ufw (the classic published-ports gotcha) — these rules
are the whole story. ARI (8088) and the receiver (8100) bind to loopback and
are never reachable from outside.

## 5. Build and start

```bash
cd /opt/ghostsip
docker compose up -d --build
docker compose ps          # both containers should be "running"
docker compose logs -f     # watch first startup; Ctrl-C to stop watching
```

First build takes a few minutes. Then verify from your own machine:

- `https://YOUR_DOMAIN/healthz` → `{"ok": true, ...}` with a valid
  certificate (Caddy fetched it automatically — if this hangs, your DNS
  record isn't resolving to the VPS yet).
- `https://YOUR_DOMAIN/admin` → log in with `admin` + your
  `GHOSTSIP_ADMIN_PASSWORD`.

## 6. fail2ban (host-side, watching Asterisk's logs)

The container writes Asterisk's `messages` and `security` logs to
`/opt/ghostsip/data/asterisk-logs/` on the host so fail2ban can ban SIP
brute-forcers at the host firewall:

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
fail2ban-client status asterisk   # jail should be live
```

The other anti-fraud layers are already in place: the dialplan only routes
UK national numbers out via the trunk seat, SIP secrets are 32-char
generated, and you set provider-side barring on the seat in step 0. (See
docs/decisions.md — no phone site has a static IP, so there's no source-IP
allowlist; this layered posture is the design.)

## 7. Configure everything in the admin panel

Open `https://YOUR_DOMAIN/admin`:

1. **Settings → Webhook**: click *Generate* for username/password. These
   become part of the VoIPstudio webhook URL in step 9.
2. **Settings → Trunk-back seat**: the SIP username/password/registrar of
   the dedicated VoIPstudio seat.
3. **Settings → Ghost-call behaviour**: defaults are fine to start. Set
   **Caller name prefix** to the ring-group name VoIPstudio prepends on real
   calls so ghost entries match.
4. **Settings → Pushover alerts** (recommended): user key + an API token
   from [pushover.net](https://pushover.net), tick Enabled. Repeated failed
   SIP registrations then send a **high-priority** push — the brute-force
   tripwire (no IP allowlist exists, so this is how you'd find out).
   Optionally tick app-error alerts too. Save, then *Send test alert*.
5. **Handsets**: one row per VVX — pick an endpoint name (e.g. `phone1`),
   click *Gen* for its SIP password.
6. **Save configuration** → then **Reload Asterisk**.
7. Check the **pjsip.conf** tab shows your handsets and the trunk
   registration, and the **Logs** tab is clean.

Confirm the trunk registered:

```bash
docker compose exec ghostsip asterisk -rx "pjsip show registrations"
```

## 8. Configure the phones

Per handset instructions: [phones/vvx-ghostsip-line.md](../phones/vvx-ghostsip-line.md).
Server address = `YOUR_DOMAIN` (or the VPS IP), port **5560** UDP (the
panel's SIP port — not 5060); credentials from the Handsets tab. Then:

```bash
docker compose exec ghostsip asterisk -rx "pjsip show endpoints"
```

Each configured phone should show a registered contact.

## 9. Point VoIPstudio at it

In the VoIPstudio dashboard add a **new** webhook (leave the Query Tracker
one untouched — spec §3):

- URL: `https://WEBHOOK_USER:WEBHOOK_PASS@YOUR_DOMAIN/webhook`
  (the Generate-d values from step 7.1)
- Event: `call.missed`

## 10. Test

Run [test-plan.md](test-plan.md) in order — A (payload verification, with
the panel's debug toggle on), B (CANCEL semantics on the wire), C (badge +
display). For test B's packet capture, run tcpdump on the **host** — host
networking means the container's SIP traffic is right there:

```bash
apt install -y tcpdump
tcpdump -i any -w /tmp/ghost.pcap udp port 5560
```

An Asterisk console when needed:

```bash
docker compose exec ghostsip asterisk -rvvv    # 'exit' leaves it running
```

---

## Day-2 operations

| Task | How |
|---|---|
| Watch logs | Admin panel **Logs** tab, or `docker compose logs -f ghostsip` |
| Restart stack | `docker compose restart` |
| Update GhostSIP | `cd /opt/ghostsip && git pull && docker compose up -d --build` |
| Update Caddy | `docker compose pull caddy && docker compose up -d` |
| OS security updates | `apt update && apt upgrade -y` (or enable `unattended-upgrades`) |
| Back up | Everything that matters is `/opt/ghostsip/data/ghostsip/` (config.json + generated pjsip.conf) plus your `.env`. Provider snapshots cover the rest. |
| Rebuild from nothing | New VPS → steps 1–6 → restore `.env` and `data/ghostsip/` → `docker compose up -d --build` → phones re-register on their own. |

## Fault-finding

1. **Admin panel Logs tab** — every webhook event, injection result and
   error, with the header dot red on any error. First stop for "no missed
   call appeared".
2. `docker compose logs ghostsip` — the same plus Asterisk's console output
   and startup errors (e.g. bad `.env`, the entrypoint says exactly which
   variable is missing).
3. `docker compose exec ghostsip asterisk -rx "pjsip show registrations"` —
   trunk-back seat up?
4. `docker compose exec ghostsip asterisk -rx "pjsip show endpoints"` —
   phones registered?
5. `docker compose exec ghostsip asterisk -rvvv` then `pjsip set logger on`
   — live SIP on the wire.
6. Webhook not arriving at all? `https://YOUR_DOMAIN/healthz` from outside,
   then check the webhook URL's embedded credentials match Settings →
   Webhook.
