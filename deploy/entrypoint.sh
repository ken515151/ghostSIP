#!/bin/bash
# GhostSIP container entrypoint: check the bootstrap env, generate the ARI
# credentials and Django secret key, prepare the persistent volume, run
# migrations + admin bootstrap, then hand over to supervisord.
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
# The app reads the same env var, so the two always match. ARI is reachable
# only on loopback (http.conf binds 127.0.0.1).
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

# Persistent volume: Django DB, generated pjsip.conf, the Django secret key.
# The app runs as the asterisk user (supervisord.conf), so it must own this.
mkdir -p /etc/ghostsip
touch /etc/ghostsip/pjsip.conf

# Django SECRET_KEY: generated once into the volume, stable across restarts.
if [[ ! -s /etc/ghostsip/secret_key ]]; then
    /opt/ghostsip/venv/bin/python - <<'PY' > /etc/ghostsip/secret_key
from django.core.management.utils import get_random_secret_key
print(get_random_secret_key())
PY
fi
chmod 600 /etc/ghostsip/secret_key
chown -R asterisk:asterisk /etc/ghostsip
chmod 644 /etc/ghostsip/pjsip.conf

# Runtime dirs Asterisk needs when started directly rather than via init.
mkdir -p /var/run/asterisk /var/log/asterisk
chown -R asterisk:asterisk /var/run/asterisk /var/log/asterisk \
    /var/lib/asterisk /var/spool/asterisk

# DB migrations + admin bootstrap, as the unprivileged app user.
cd /opt/ghostsip/receiver
run_app() { setpriv --reuid asterisk --regid asterisk --clear-groups "$@"; }
run_app /opt/ghostsip/venv/bin/python manage.py migrate --noinput
run_app /opt/ghostsip/venv/bin/python manage.py ensure_admin
# Regenerate pjsip.conf from the DB in case handsets changed while stopped.
run_app /opt/ghostsip/venv/bin/python manage.py shell -c \
    "from panel.services import pjsip; from panel.models import Configuration, Handset; pjsip.write(Configuration.load(), Handset.objects.all())"

exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
