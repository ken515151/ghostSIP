"""Read-only system status for the admin: live Asterisk state (via the same
in-container CLI the Reload button uses — a fixed whitelist of commands, no
user input) and an end-to-end check of the public webhook URL, including
proper TLS certificate validation (chain + hostname, issuer, expiry)."""

from __future__ import annotations

import datetime
import os
import socket
import ssl
import subprocess

import requests

STATUS_COMMANDS = [
    ("Trunk registration (VoIPstudio)", "pjsip show registrations"),
    ("Phone endpoints", "pjsip show endpoints"),
    ("Asterisk uptime", "core show uptime"),
]


def asterisk_status() -> list[tuple[str, str, str]]:
    """(title, command, output) for each whitelisted CLI command."""
    results = []
    for title, cmd in STATUS_COMMANDS:
        try:
            proc = subprocess.run(
                ["asterisk", "-rx", cmd], capture_output=True, text=True, timeout=10
            )
            text = (proc.stdout + proc.stderr).strip() or "(no output)"
        except (OSError, subprocess.TimeoutExpired) as exc:
            text = f"(could not run: {exc})"
        results.append((title, cmd, text))
    return results


def public_check() -> dict:
    """DNS → TLS → webhook liveness for the public domain, from inside the
    container out through the real internet-facing path (Caddy included).
    The TLS step uses ssl.create_default_context, so the certificate chain
    and hostname are genuinely verified — a self-signed or mismatched cert
    fails here exactly as it would for VoIPstudio."""
    result = {
        "domain": os.environ.get("GHOSTSIP_DOMAIN", ""),
        "dns_ip": None,
        "cert_ok": False,
        "cert_issuer": "",
        "cert_expires": None,
        "cert_days_left": None,
        "healthz_ok": False,
        "error": "",
    }
    domain = result["domain"]
    if not domain:
        result["error"] = "GHOSTSIP_DOMAIN is not set in .env"
        return result

    try:
        result["dns_ip"] = socket.gethostbyname(domain)
    except OSError as exc:
        result["error"] = f"DNS lookup failed: {exc}"
        return result

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=10) as raw:
            with ctx.wrap_socket(raw, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        issuer = dict(item[0] for item in cert.get("issuer", ()))
        result["cert_issuer"] = issuer.get("organizationName") or issuer.get("commonName", "?")
        expires = datetime.datetime.strptime(
            cert["notAfter"], "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=datetime.timezone.utc)
        result["cert_expires"] = expires
        result["cert_days_left"] = (
            expires - datetime.datetime.now(datetime.timezone.utc)
        ).days
        result["cert_ok"] = True
    except (OSError, ssl.SSLError, ValueError, KeyError) as exc:
        result["error"] = f"TLS check failed: {exc}"
        return result

    try:
        resp = requests.get(f"https://{domain}/healthz", timeout=10)
        result["healthz_ok"] = resp.status_code == 200 and resp.json().get("ok") is True
        if not result["healthz_ok"]:
            result["error"] = f"healthz returned HTTP {resp.status_code}"
    except (requests.RequestException, ValueError) as exc:
        result["error"] = f"healthz request failed: {exc}"
    return result
