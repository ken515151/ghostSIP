"""Lockdown: suspend the outbound callback relay — the only path on this
box that can cost money. Missed-call injection and the phones' normal
VoIPstudio line are untouched; only tap-to-callback pauses.

Enforced by the GHOSTSIP_LOCKDOWN Asterisk global variable, which the
dialplan checks per call (asterisk/extensions.conf) — set through ARI, so
it takes effect instantly with no reload. The authoritative state lives in
config.json (surviving restarts); app.py re-asserts the variable into
Asterisk at startup.

Triggers:
  - manual, from the admin panel;
  - automatic, when a credential successfully authenticates from a
    never-seen address (alerts.py) — armed by the panel's auto-lockdown
    toggle, and temporarily disarmed by the 1-hour suspend button for
    planned new-device setup. Deliberately NEVER triggered by failed auth
    attempts: those are unauthenticated internet noise anyone can generate,
    and auto-locking on them would hand attackers a callback kill switch.
"""

from __future__ import annotations

import logging
import time

import httpx

import config as cfg

log = logging.getLogger("ghostsip.lockdown")

VARIABLE = "GHOSTSIP_LOCKDOWN"
SUSPEND_SECONDS = 3600


async def apply_state(conf: dict, log_errors: bool = True) -> bool:
    """Push conf['lockdown']['active'] into the Asterisk global. Returns
    True when Asterisk accepted it."""
    value = "1" if conf["lockdown"]["active"] else "0"
    try:
        async with httpx.AsyncClient(
            auth=(conf["ari"]["username"], conf["ari"]["password"]), timeout=10
        ) as client:
            resp = await client.post(
                f"{conf['ari']['url']}/asterisk/variable",
                params={"variable": VARIABLE, "value": value},
            )
            resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        if log_errors:
            log.error("could not set %s=%s in Asterisk: %s", VARIABLE, value, exc)
        return False


async def set_active(active: bool) -> tuple[dict, bool]:
    """Persist the new lockdown state and apply it to Asterisk."""
    conf = cfg.load()
    conf["lockdown"]["active"] = active
    cfg.save(conf)
    applied = await apply_state(conf)
    return conf, applied


def suspended(conf: dict) -> bool:
    return time.time() < conf["lockdown"].get("suspend_until", 0)


def suspend(seconds: float = SUSPEND_SECONDS) -> dict:
    """Disarm auto-lockdown for a window (planned new-device setup).
    New-address alerts still send; only the automatic trigger sleeps."""
    conf = cfg.load()
    conf["lockdown"]["suspend_until"] = time.time() + seconds
    cfg.save(conf)
    return conf
