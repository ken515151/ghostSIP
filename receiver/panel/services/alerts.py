"""Pushover alerting via Apprise, plus the security-log parsers.

Semantics unchanged from the original design (docs/decisions.md):
  - HIGH priority when repeated failed SIP auth attempts cross a threshold;
  - normal priority when a credential authenticates from a new address;
  - optional normal-priority push on recorded ERRORs, rate-limited.
Alerts are best-effort: a Pushover outage never breaks call handling."""

from __future__ import annotations

import re
import time
from collections import Counter, deque

import apprise

# Asterisk security-log events that mean "someone failed SIP auth".
FAIL_EVENTS = {"InvalidAccountID", "ChallengeResponseFailed", "InvalidPassword", "FailedACL"}
_EVENT_RE = re.compile(r'SecurityEvent="(?P<event>\w+)"')
_ADDR_RE = re.compile(r'RemoteAddress="[^"/]+/[^"/]+/(?P<ip>[^"/]+)/')
_ACCOUNT_RE = re.compile(r'AccountID="(?P<acct>[^"]+)"')

BRUTE_THRESHOLD = 5    # this many failures...
BRUTE_WINDOW = 600     # ...within this many seconds trips the alert
ALERT_COOLDOWN = 3600  # at most one brute-force alert per hour
ERROR_COOLDOWN = 900   # at most one app-error alert per 15 minutes

_last_sent: dict[str, float] = {}


def cooldown_ok(key: str, seconds: float) -> bool:
    now = time.monotonic()
    if now - _last_sent.get(key, float("-inf")) < seconds:
        return False
    _last_sent[key] = now
    return True


def send(config, title: str, message: str, high_priority: bool = False) -> bool:
    """Send one Pushover message through Apprise. Returns delivery success."""
    if not (config.pushover_enabled and config.pushover_user_key and config.pushover_api_token):
        return False
    priority = "high" if high_priority else "normal"
    url = (
        f"pover://{config.pushover_user_key}@{config.pushover_api_token}"
        f"?priority={priority}"
    )
    notifier = apprise.Apprise()
    notifier.add(url)
    return bool(notifier.notify(title=title, body=message))


def notify_error(config, message: str) -> None:
    """Optional normal-priority push for recorded ERRORs, rate-limited."""
    if config.alert_on_errors and cooldown_ok("app-error", ERROR_COOLDOWN):
        send(config, "GhostSIP error", message)


class BruteForceDetector:
    """Sliding-window counter over security-log lines. Pure logic, no I/O."""

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


def parse_successful_auth(line: str) -> tuple[str, str] | None:
    """(endpoint, ip) from a SuccessfulAuth security-log line, else None."""
    m = _EVENT_RE.search(line)
    if not m or m.group("event") != "SuccessfulAuth":
        return None
    acct = _ACCOUNT_RE.search(line)
    addr = _ADDR_RE.search(line)
    if not acct or not addr:
        return None
    return acct.group("acct"), addr.group("ip")
