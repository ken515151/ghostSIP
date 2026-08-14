"""Lockdown: suspend the outbound callback relay — the only path on this box
that can cost money. Enforced by the GHOSTSIP_LOCKDOWN Asterisk global the
dialplan gates on (services/ari.py), state persisted on Configuration.

Triggers: the admin buttons (engage / lift / suspend-1h), or automatically
from the watcher when a credential authenticates from a never-seen address —
armed by the auto-lockdown toggle, disarmed for an hour by the suspend
button. Deliberately NEVER triggered by failed auth attempts: those are
unauthenticated noise anyone can generate."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from panel.models import Configuration, Event
from panel.services import alerts, ari

SUSPEND = timedelta(hours=1)


def set_active(active: bool, reason: str, high_priority_alert: bool = False) -> bool:
    """Persist and apply the lockdown state; record and alert. Returns
    whether Asterisk accepted the variable (state persists regardless and is
    re-asserted at watcher startup)."""
    config = Configuration.load()
    config.lockdown_active = active
    config.save()
    applied = ari.set_lockdown_variable(active)
    if active:
        Event.log(Event.WARNING, "lockdown", f"Lockdown engaged — {reason}"
                  + ("" if applied else " (Asterisk not yet applied; will re-assert)"))
        alerts.send(config, "GhostSIP: lockdown engaged",
                    f"{reason} Outbound callback relay suspended — review and lift it "
                    f"in the admin.", high_priority=high_priority_alert)
    else:
        Event.log(Event.INFO, "lockdown", f"Lockdown lifted — {reason}")
        alerts.send(config, "GhostSIP: lockdown lifted", "Outbound callback relay restored.")
    return applied


def suspend_auto() -> None:
    """Disarm the automatic trigger for 1 hour (planned new-device setup).
    New-address alerts still send; manual lockdown still works."""
    config = Configuration.load()
    config.lockdown_suspend_until = timezone.now() + SUSPEND
    config.save()
    Event.log(Event.INFO, "lockdown", "Auto-lockdown suspended for 1 hour (new-device setup).")
    alerts.send(config, "GhostSIP: auto-lockdown suspended",
                "Auto-lockdown disarmed for 1 hour (new-device setup). "
                "New-address alerts still send.")
