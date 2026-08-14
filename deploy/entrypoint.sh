#!/bin/bash
# GhostSIP container entrypoint: sanity-check the bootstrap env, generate
# ari.conf from the env secret, make sure the generated-pjsip include target
# exists, then hand over to supervisord (asterisk + receiver).
set -euo pipefail

if [[ -z "${GHOSTSIP_ADMIN_PASSWORD:-}" ]]; then
    echo "FATAL: GHOSTSIP_ADMIN_PASSWORD is not set — copy deploy/.env.example to .env and fill it in." >&2
    exit 1
fi
if [[ -z "${GHOSTSIP_ARI_PASSWORD:-}" ]]; then
    echo "FATAL: GHOSTSIP_ARI_PASSWORD is not set — copy deploy/.env.example to .env and fill it in." >&2
    exit 1
fi

# ARI credentials: generated here so the secret lives in .env, not the image.
# The receiver reads the same env var (config.py fallback), so the two always
# match. ARI is reachable only on loopback (http.conf binds 127.0.0.1).
cat > /etc/asterisk/ari.conf <<EOF
[general]
enabled=yes
pretty=no

[ghostsip]
type=user
read_only=no
password=${GHOSTSIP_ARI_PASSWORD}
EOF
chmod 640 /etc/asterisk/ari.conf
chown root:asterisk /etc/asterisk/ari.conf

# The admin panel writes the real pjsip.conf here (mounted volume); Asterisk
# includes it. An empty file on first boot is fine — no endpoints until the
# panel saves, but Asterisk starts and the panel is reachable.
mkdir -p /etc/ghostsip
touch /etc/ghostsip/pjsip.conf
chmod 644 /etc/ghostsip/pjsip.conf

# Runtime dirs Asterisk needs when started directly rather than via init.
mkdir -p /var/run/asterisk /var/log/asterisk
chown -R asterisk:asterisk /var/run/asterisk /var/log/asterisk \
    /var/lib/asterisk /var/spool/asterisk

exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
