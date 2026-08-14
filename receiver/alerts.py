"""Pushover alerting for GhostSIP.

Two alert sources:

  - SIP brute force (always armed once Pushover is enabled): a background
    task tails Asterisk's security log — same container, so it's just a
    file — and sends a HIGH-priority push when repeated auth/registration
    failures cross a threshold. With no source-IP allowlist on SIP
    (docs/decisions.md), this tripwire is how you find out someone is
    guessing passwords.

  - Application errors (optional toggle): any ERROR the receiver logs
    (failed originate, pjsip.conf write failure, ...) sends a
    normal-priority push, rate-limited so a repeating fault is one ping,
    not a storm.

Alerts are best-effort by design: a Pushover outage can never break call
handling, and the alerting module never alerts about itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import Counter, deque

import httpx

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
SECURITY_LOG = os.environ.get("GHOSTSIP_SECURITY_LOG", "/var/log/asterisk/security")

# Asterisk security-log events that mean "someone failed SIP auth".
FAIL_EVENTS = {"InvalidAccountID", "ChallengeResponseFailed", "InvalidPassword", "FailedACL"}
_EVENT_RE = re.compile(r'SecurityEvent="(?P<event>\w+)"')
_ADDR_RE = re.compile(r'RemoteAddress="[^"/]+/[^"/]+/(?P<ip>[^"/]+)/')

BRUTE_THRESHOLD = 5    # this many failures...
BRUTE_WINDOW = 600     # ...within this many seconds trips the alert
ALERT_COOLDOWN = 3600  # at most one brute-force alert per hour
ERROR_COOLDOWN = 900   # at most one app-error alert per 15 minutes
POLL_SECONDS = 5

log = logging.getLogger("ghostsip.alerts")

_last_sent: dict[str, float] = {}


def _cooldown_ok(key: str, seconds: float) -> bool:
    now = time.monotonic()
    if now - _last_sent.get(key, float("-inf")) < seconds:
        return False
    _last_sent[key] = now
    return True


async def send(conf: dict, title: str, message: str, priority: int = 0) -> bool:
    """Send one Pushover message. priority 1 = high (bypasses quiet hours)."""
    po = conf.get("pushover", {})
    if not (po.get("enabled") and po.get("user_key") and po.get("app_token")):
        return False
    data = {
        "token": po["app_token"],
        "user": po["user_key"],
        "title": title,
        "message": message,
        "priority": priority,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(PUSHOVER_URL, data=data)
            resp.raise_for_status()
        log.info("pushover alert sent: %s", title)
        return True
    except httpx.HTTPError as exc:
        log.error("pushover send failed: %s", exc)
        return False


async def notify_error(conf: dict, message: str) -> None:
    """Optional normal-priority push for application ERRORs, rate-limited."""
    if conf.get("pushover", {}).get("alert_on_errors") and _cooldown_ok(
        "app-error", ERROR_COOLDOWN
    ):
        await send(conf, "GhostSIP error", message, priority=0)


class BruteForceDetector:
    """Sliding-window counter over security-log lines. Pure logic, no I/O,
    so it's directly testable."""

    def __init__(self, threshold: int = BRUTE_THRESHOLD, window: float = BRUTE_WINDOW):
        self.threshold = threshold
        self.window = window
        self.failures: deque[tuple[float, str]] = deque()

    def feed(self, line: str, now: float | None = None) -> str | None:
        """Feed one log line; returns an alert summary when the threshold is
        crossed (and resets the window), else None."""
        m = _EVENT_RE.search(line)
        if not m or m.group("event") not in FAIL_EVENTS:
            return None
        now = time.monotonic() if now is None else now
        ip_match = _ADDR_RE.search(line)
        self.failures.append((now, ip_match.group("ip") if ip_match else "unknown"))
        while self.failures and now - self.failures[0][0] > self.window:
            self.failures.popleft()
        if len(self.failures) < self.threshold:
            return None
        counts = Counter(ip for _, ip in self.failures)
        top = ", ".join(f"{ip} ({n}x)" for ip, n in counts.most_common(3))
        summary = (
            f"{len(self.failures)} failed SIP auth/registration attempts in the last "
            f"{int(self.window // 60)} min. Sources: {top}. "
            f"fail2ban should be banning; check the server."
        )
        self.failures.clear()
        return summary


async def watch_security_log(load_conf) -> None:
    """Background task: tail the Asterisk security log and raise the
    HIGH-priority brute-force alert. Survives the file not existing yet,
    log rotation, and any parse surprise."""
    detector = BruteForceDetector()
    pos = 0
    first_sight = True
    while True:
        try:
            with open(SECURITY_LOG, encoding="utf-8", errors="replace") as fh:
                size = os.fstat(fh.fileno()).st_size
                if size < pos:
                    pos = 0  # rotated/truncated — start over
                if first_sight:
                    pos = size  # don't replay history into a stale alert at boot
                    first_sight = False
                fh.seek(pos)
                lines = fh.readlines()
                pos = fh.tell()
            for line in lines:
                summary = detector.feed(line)
                if summary and _cooldown_ok("sip-brute", ALERT_COOLDOWN):
                    log.warning("SIP brute force detected: %s", summary)
                    await send(
                        load_conf(), "GhostSIP: SIP brute force", summary, priority=1
                    )
        except FileNotFoundError:
            first_sight = False  # log appears later; read it from the top then
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the watcher must outlive any surprise
            log.warning("security log watcher hiccup: %s", exc)
        await asyncio.sleep(POLL_SECONDS)
