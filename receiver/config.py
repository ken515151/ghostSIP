"""GhostSIP configuration store.

Everything that used to live in environment variables now lives in a single
JSON file edited through the web admin panel (app.py). The only hand-set
values are the bootstrap env vars (admin login, ARI secret, domain) — see
deploy/.env.example.

Config path resolution:
  GHOSTSIP_CONFIG env var, else /etc/ghostsip/config.json.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from typing import Any

CONFIG_PATH = os.environ.get("GHOSTSIP_CONFIG", "/etc/ghostsip/config.json")

DEFAULTS: dict[str, Any] = {
    "webhook": {
        # Must match the Basic Auth embedded in the VoIPstudio webhook URL:
        #   https://USER:PASS@ghostsip.example.com/webhook
        "username": "",
        "password": "",
    },
    "ari": {
        "url": "http://127.0.0.1:8088/ari",
        "username": "ghostsip",
        "password": "",
    },
    "sip": {
        # UDP port the phones register to. Non-standard on purpose: 5060
        # attracts constant internet-wide scanning, and moving off it removes
        # ~99% of that noise. Must match each phone's server port and the
        # firewall rule (docs/deployment.md).
        "port": 5560,
    },
    "ghost": {
        "ring_seconds": 4,
        "dedup_window_seconds": 120,
        # Alert-Info value mapped to a silent ring class on the VVX. Empty = off.
        "alert_info": "",
        # Display-name prefix for the ghost caller ID, so entries match the
        # ring-group-name prepend VoIPstudio puts on real calls (e.g.
        # "TechRescue" -> log shows "TechRescue 01224..."). Cosmetic only:
        # the callback always dials the bare number. Empty = number only.
        "caller_name_prefix": "",
        # Also inject for missed direct (User-context) calls, not just queue.
        "include_user_context": False,
        # Log full webhook payloads (caller numbers included). Commissioning
        # aid for test A — leave OFF in steady state; numbers are otherwise
        # masked to their last 5 digits in the logs.
        "debug_log_payloads": False,
    },
    "trunkback": {
        "server": "sip.voipstudio.com",
        "username": "",
        "password": "",
    },
    "pushover": {
        # When enabled, repeated failed SIP registrations ALWAYS send a
        # high-priority push (the brute-force tripwire — see alerts.py).
        "enabled": False,
        "user_key": "",
        "app_token": "",
        # Optional: normal-priority push when the receiver logs an ERROR.
        "alert_on_errors": False,
    },
    # Each: {"name": free text, "endpoint": pjsip name, "password": SIP secret}
    "handsets": [],
}

_lock = threading.Lock()


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load() -> dict:
    """Load config, filling any missing keys from DEFAULTS.

    The ARI password falls back to the GHOSTSIP_ARI_PASSWORD env var when
    unset in config.json — in the Docker deployment the same env value also
    generates Asterisk's ari.conf (deploy/entrypoint.sh), so it's entered
    once in .env instead of twice."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            stored = json.load(fh)
    except FileNotFoundError:
        stored = {}
    conf = deep_merge(DEFAULTS, stored)
    if not conf["ari"]["password"]:
        conf["ari"]["password"] = os.environ.get("GHOSTSIP_ARI_PASSWORD", "")
    return conf


def save(config: dict) -> None:
    """Atomically write config to disk (0600)."""
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    tmp = f"{CONFIG_PATH}.tmp"
    with _lock:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
        os.replace(tmp, CONFIG_PATH)


def gen_secret(nbytes: int = 24) -> str:
    """URL-safe random secret for SIP/ARI/webhook credentials."""
    return secrets.token_urlsafe(nbytes)
