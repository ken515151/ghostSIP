# GhostSIP — one image containing Asterisk + the Django admin/webhook app,
# run together under supervisord. Co-locating them is deliberate: the app
# writes pjsip.conf and runs `asterisk -rx 'pjsip reload'` directly, with no
# cross-container plumbing.
#
# Built from the repo root:  docker compose up -d --build
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        asterisk \
        supervisor \
        util-linux \
        python3 \
        python3-venv \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Static Asterisk config. pjsip.conf is a stub that includes the file the
# app generates into the mounted /etc/ghostsip volume.
COPY asterisk/extensions.conf asterisk/http.conf asterisk/rtp.conf asterisk/logger.conf asterisk/modules.conf /etc/asterisk/
COPY deploy/pjsip-root.conf /etc/asterisk/pjsip.conf

# Defaults so the image behaves correctly even outside docker-compose
# (compose sets the same values); the generated pjsip.conf and the SQLite DB
# live in the persistent /etc/ghostsip volume.
ENV GHOSTSIP_PJSIP_PATH=/etc/ghostsip/pjsip.conf \
    GHOSTSIP_DB=/etc/ghostsip/db.sqlite3 \
    GHOSTSIP_SECRET_KEY_FILE=/etc/ghostsip/secret_key \
    GHOSTSIP_STATIC_ROOT=/opt/ghostsip/staticfiles \
    GHOSTSIP_ASTERISK_RELOAD_CMD="asterisk -rx 'pjsip reload'" \
    DJANGO_SETTINGS_MODULE=ghostsip.settings
# ari.conf is generated at container start from GHOSTSIP_ARI_PASSWORD — see
# entrypoint.sh — so no secret is baked into the image.
RUN rm -f /etc/asterisk/ari.conf

# Django app
COPY receiver/ /opt/ghostsip/receiver/
RUN python3 -m venv /opt/ghostsip/venv \
    && /opt/ghostsip/venv/bin/pip install --no-cache-dir \
        -r /opt/ghostsip/receiver/requirements.txt \
    && GHOSTSIP_DJANGO_SECRET=build /opt/ghostsip/venv/bin/python \
        /opt/ghostsip/receiver/manage.py collectstatic --noinput

COPY deploy/supervisord.conf /etc/supervisor/conf.d/ghostsip.conf
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
