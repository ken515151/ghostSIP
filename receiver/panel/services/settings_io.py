"""Settings export/import: the hand-entered state (Configuration +
Handsets) as one JSON file, downloadable and restorable from the admin.

Deliberately excluded: Events, known addresses and the injection dedup
table (operational history that rebuilds itself) and live lockdown state
(owned by the lockdown buttons; only the auto-arm toggle travels).

The export contains the SIP/webhook/trunk secrets in plaintext — a restore
that can't restore credentials is useless — so the file must be guarded
like a password. It only ever crosses the SSH tunnel.

Import is a faithful restore: Configuration fields are overwritten and the
handset list is made to match the file exactly (handsets not in the file
are removed). Everything is validated with the same model rules as the
admin forms before anything is written, inside one transaction."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from panel.models import Configuration, Handset

VERSION = 1

# Explicit allowlist — never dump model internals wholesale.
CONFIG_FIELDS = [
    "webhook_username", "webhook_password",
    "sip_port",
    "ring_seconds", "caller_name_prefix", "alert_info",
    "include_user_context", "debug_log_payloads",
    "trunk_server", "trunk_username", "trunk_password",
    "pushover_enabled", "pushover_user_key", "pushover_api_token",
    "alert_on_errors", "alert_on_new_registration",
    "lockdown_auto_enabled",
]


def export_settings() -> dict:
    config = Configuration.load()
    return {
        "ghostsip_settings_version": VERSION,
        "exported_at": timezone.now().isoformat(),
        "configuration": {field: getattr(config, field) for field in CONFIG_FIELDS},
        "handsets": [
            {"name": hs.name, "endpoint": hs.endpoint, "sip_password": hs.sip_password}
            for hs in Handset.objects.all()
        ],
    }


def import_settings(data: object) -> str:
    """Validate fully, then apply atomically. Returns a summary string;
    raises ValidationError (message meant for the admin) on any problem,
    in which case nothing was changed."""
    if not isinstance(data, dict):
        raise ValidationError("Not a GhostSIP settings file (expected a JSON object).")
    if data.get("ghostsip_settings_version") != VERSION:
        raise ValidationError(
            f"Unsupported settings file version "
            f"{data.get('ghostsip_settings_version')!r} (expected {VERSION})."
        )
    conf_data = data.get("configuration")
    handset_data = data.get("handsets")
    if not isinstance(conf_data, dict) or not isinstance(handset_data, list):
        raise ValidationError("Settings file is missing configuration or handsets.")

    # --- validate everything before writing anything ---
    config = Configuration.load()
    for field in CONFIG_FIELDS:
        if field in conf_data:
            setattr(config, field, conf_data[field])
    try:
        config.full_clean()
    except ValidationError as exc:
        raise ValidationError(f"Configuration in file is invalid: {exc.messages}")

    endpoints_seen: set[str] = set()
    handsets: list[Handset] = []
    for entry in handset_data:
        if not isinstance(entry, dict) or not entry.get("endpoint"):
            raise ValidationError("A handset entry is malformed (missing endpoint).")
        endpoint = str(entry["endpoint"])
        if endpoint in endpoints_seen:
            raise ValidationError(f"Duplicate handset endpoint in file: {endpoint!r}.")
        endpoints_seen.add(endpoint)
        obj = Handset.objects.filter(endpoint=endpoint).first() or Handset(endpoint=endpoint)
        obj.name = str(entry.get("name", "")) or endpoint
        obj.sip_password = str(entry.get("sip_password", ""))
        try:
            obj.full_clean()
        except ValidationError as exc:
            raise ValidationError(f"Handset {endpoint!r} in file is invalid: {exc.messages}")
        handsets.append(obj)

    # --- apply ---
    with transaction.atomic():
        config.save()
        removed, _ = Handset.objects.exclude(endpoint__in=endpoints_seen).delete()
        for obj in handsets:
            obj.save()

    return (
        f"Settings restored: configuration applied, "
        f"{len(handsets)} handset(s) in place, {removed} removed."
    )
