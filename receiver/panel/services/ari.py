"""Asterisk interaction: ghost-call origination and the lockdown global via
ARI (loopback-only), plus the pjsip reload subprocess.

ARI credentials come from the bootstrap .env — the same value entrypoint.sh
writes into ari.conf, so they always match and are never typed twice."""

from __future__ import annotations

import os
import subprocess

import requests

ARI_URL = os.environ.get("GHOSTSIP_ARI_URL", "http://127.0.0.1:8088/ari")
ARI_USERNAME = os.environ.get("GHOSTSIP_ARI_USERNAME", "ghostsip")
ARI_PASSWORD = os.environ.get("GHOSTSIP_ARI_PASSWORD", "")
RELOAD_CMD = os.environ.get("GHOSTSIP_ASTERISK_RELOAD_CMD", "asterisk -rx 'pjsip reload'")

LOCKDOWN_VARIABLE = "GHOSTSIP_LOCKDOWN"


def _auth() -> tuple[str, str]:
    return (ARI_USERNAME, ARI_PASSWORD)


def originate_ghost_call(endpoint: str, caller: str, config) -> tuple[bool, str]:
    """One INVITE → ring → CANCEL toward a single handset. Lands in the
    [ghost-answered] dialplan context (which hangs up) if answered early;
    normally the timeout expires and Asterisk CANCELs with no SIP;cause=200
    (verified in test B)."""
    prefix = (config.caller_name_prefix or "").strip()
    display = f"{prefix} {caller}" if prefix else caller
    params = {
        "endpoint": f"PJSIP/{endpoint}",
        "extension": "s",
        "context": "ghost-answered",
        "priority": 1,
        "callerId": f'"{display}" <{caller}>',
        "timeout": config.ring_seconds,
    }
    body: dict = {}
    if config.alert_info:
        body["variables"] = {"PJSIP_HEADER(add,Alert-Info)": config.alert_info}
    try:
        resp = requests.post(f"{ARI_URL}/channels", params=params, json=body, auth=_auth(), timeout=10)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", "") or resp.text[:120]
            except ValueError:
                detail = resp.text[:120]
            hint = ""
            if "allocation" in detail.lower():
                # Asterisk's "Allocation failed" here almost always means the
                # endpoint has no registered contact to send the INVITE to.
                hint = (" — usually the phone is not registered right now "
                        "(check System status; common straight after a rebuild)")
            return False, f"ARI {resp.status_code}: {detail}{hint}"
        return True, "ok"
    except requests.RequestException as exc:
        return False, str(exc)


def set_lockdown_variable(active: bool) -> bool:
    """Push the lockdown state into the Asterisk global the dialplan gates on.
    Instant, no reload."""
    try:
        resp = requests.post(
            f"{ARI_URL}/asterisk/variable",
            params={"variable": LOCKDOWN_VARIABLE, "value": "1" if active else "0"},
            auth=_auth(),
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False


def reload_pjsip() -> tuple[bool, str]:
    if not RELOAD_CMD:
        return False, "reload command disabled"
    try:
        proc = subprocess.run(RELOAD_CMD, shell=True, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False, "reload timed out"
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out or ("ok" if proc.returncode == 0 else "failed")
