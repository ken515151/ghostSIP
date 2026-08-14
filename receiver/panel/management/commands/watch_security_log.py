"""Long-running watcher, run under supervisord alongside gunicorn.

Responsibilities (semantics unchanged from the original design):
  - assert the persisted lockdown state into Asterisk at startup (a restart
    clears Asterisk globals; an engaged lockdown must survive reboots);
  - tail Asterisk's security log:
      * repeated failed SIP auth  -> HIGH-priority Pushover (brute tripwire);
      * successful auth from a never-seen address -> Event + normal push,
        and (when armed and not suspended) engage auto-lockdown;
  - housekeeping: prune old Events and InjectedCall dedup rows.

Survives the log file not existing yet, rotation, and any parse surprise."""

from __future__ import annotations

import os
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from panel.models import Configuration, Event, InjectedCall, KnownAddress
from panel.services import alerts, ari, lockdown

SECURITY_LOG = os.environ.get("GHOSTSIP_SECURITY_LOG", "/var/log/asterisk/security")
POLL_SECONDS = 5
EVENT_RETENTION_DAYS = 30
DEDUP_RETENTION_HOURS = 24
HOUSEKEEPING_EVERY = 3600  # seconds


class Command(BaseCommand):
    help = "Tail the Asterisk security log; raise alerts, drive auto-lockdown, prune tables."

    def handle(self, *args, **options):
        self._assert_lockdown()
        detector = alerts.BruteForceDetector()
        pos = 0
        first_sight = True
        last_housekeeping = 0.0
        while True:
            try:
                lines, pos, first_sight = self._read_new_lines(pos, first_sight)
                for line in lines:
                    self._handle_line(line, detector)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # the watcher must outlive any surprise
                print(f"watcher hiccup: {exc}", flush=True)
            if time.monotonic() - last_housekeeping > HOUSEKEEPING_EVERY:
                last_housekeeping = time.monotonic()
                self._housekeeping()
            time.sleep(POLL_SECONDS)

    # --- pieces -------------------------------------------------------------
    def _assert_lockdown(self) -> None:
        for _ in range(60):
            config = Configuration.load()
            if ari.set_lockdown_variable(config.lockdown_active):
                Event.log(Event.INFO, "system",
                          f"Lockdown state asserted in Asterisk: active={config.lockdown_active}")
                return
            time.sleep(5)
        Event.log(Event.ERROR, "system", "Could not assert lockdown state in Asterisk after 5 min")

    def _read_new_lines(self, pos: int, first_sight: bool):
        try:
            with open(SECURITY_LOG, encoding="utf-8", errors="replace") as fh:
                size = os.fstat(fh.fileno()).st_size
                if size < pos:
                    pos = 0  # rotated/truncated — start over
                if first_sight:
                    pos = size  # don't replay history into stale alerts at boot
                fh.seek(pos)
                lines = fh.readlines()
                return lines, fh.tell(), False
        except FileNotFoundError:
            return [], pos, False  # log appears later; read from the top then

    def _handle_line(self, line: str, detector: alerts.BruteForceDetector) -> None:
        summary = detector.feed(line)
        if summary and alerts.cooldown_ok("sip-brute", alerts.ALERT_COOLDOWN):
            config = Configuration.load()
            Event.log(Event.ERROR, "security", f"SIP brute force: {summary}")
            alerts.send(config, "GhostSIP: SIP brute force", summary, high_priority=True)

        auth = alerts.parse_successful_auth(line)
        if auth:
            endpoint, ip = auth
            _, created = KnownAddress.objects.get_or_create(endpoint=endpoint, ip=ip)
            if created:
                previous = list(
                    KnownAddress.objects.filter(endpoint=endpoint)
                    .exclude(ip=ip)
                    .values_list("ip", flat=True)
                )
                news = (
                    f"SIP endpoint '{endpoint}' authenticated from new address {ip} "
                    f"(previously: {', '.join(previous) or 'none on record'}). Expected "
                    f"after an ISP IP change or a phone moving site; investigate if neither."
                )
                config = Configuration.load()
                Event.log(Event.WARNING, "security", news)
                if config.alert_on_new_registration:
                    alerts.send(config, "GhostSIP: new device address", news)
                # Auto-lockdown: a SUCCESSFUL auth from an unknown address is
                # the one trigger an attacker can't fire without already
                # holding a valid secret. Never triggered by failures.
                if (config.lockdown_auto_enabled and not config.lockdown_active
                        and not config.lockdown_suspended()):
                    lockdown.set_active(True, f"auto: {news}", high_priority_alert=True)

    def _housekeeping(self) -> None:
        Event.objects.filter(
            created__lt=timezone.now() - timedelta(days=EVENT_RETENTION_DAYS)
        ).delete()
        InjectedCall.objects.filter(
            created__lt=timezone.now() - timedelta(hours=DEDUP_RETENTION_HOURS)
        ).delete()
