"""Caller-number handling — mirrors Query Tracker's capture-proven treatment
of the same VoIPstudio feed (query-tracker/includes/phone.php)."""

from __future__ import annotations

import re


def normalise_caller(raw: object) -> str:
    """Digits only, with VoIPstudio's international UK forms rewritten to
    national ("441224..." / "0044..." -> "01224..."), so the missed-call
    entry reads like a normal UK number and dials back correctly.
    Digits-only also makes the value safe inside the SIP From header."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("0044"):
        return "0" + digits[4:]
    if digits.startswith("44") and len(digits) >= 11:
        return "0" + digits[2:]
    return digits


def mask_caller(num: str) -> str:
    """Last-5 form for logs and events — full caller numbers don't belong in
    stored logs as a steady state (same stance as Query Tracker)."""
    return ("…" + num[-5:]) if len(num) > 5 else num
