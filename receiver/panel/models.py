"""GhostSIP models. Configuration is a singleton row edited in the admin;
Handset rows generate Asterisk's pjsip.conf; Event is the persisted activity
log; InjectedCall is the cross-process dedup guard; KnownAddress backs the
new-device-address alert."""

from __future__ import annotations

import secrets

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone


def generate_secret() -> str:
    """32-char URL-safe secret (192 bits) — used for SIP and webhook secrets."""
    return secrets.token_urlsafe(24)


endpoint_validator = RegexValidator(
    r"^[A-Za-z0-9_-]{1,40}$", "Letters, digits, - and _ only (max 40)."
)


def _no_linebreaks(value: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValidationError("Line breaks are not allowed here.")


class Configuration(models.Model):
    """Singleton. Everything the admin panel manages except handsets."""

    # --- Webhook (from VoIPstudio) ---
    webhook_username = models.CharField(
        max_length=100, blank=True, validators=[_no_linebreaks],
        help_text="Must match the Basic Auth embedded in the VoIPstudio webhook URL: "
                  "https://USER:PASS@your-domain/webhook",
    )
    webhook_password = models.CharField(
        max_length=100, blank=True, default=generate_secret, validators=[_no_linebreaks]
    )

    # --- SIP listener ---
    sip_port = models.PositiveIntegerField(
        default=5560, validators=[MinValueValidator(1024), MaxValueValidator(65535)],
        help_text="UDP port the phones register to. Non-standard on purpose (scanner noise). "
                  "Must match each phone's server port and the firewall rule; a change needs "
                  "a stack restart.",
    )

    # --- Ghost-call behaviour ---
    ring_seconds = models.PositiveIntegerField(
        default=4, validators=[MinValueValidator(1), MaxValueValidator(20)],
        help_text="How long a ghost call rings before Asterisk cancels it.",
    )
    caller_name_prefix = models.CharField(
        max_length=60, blank=True,
        help_text="Shown before the number in the missed-call entry so ghost calls match "
                  "VoIPstudio's ring-group-name prepend (e.g. TechRescue). Callback always "
                  "dials the bare number.",
    )
    alert_info = models.CharField(
        max_length=100, blank=True, validators=[_no_linebreaks], verbose_name="Alert-Info header",
        help_text="Optional value mapped to a silent ring class on the VVX. Empty = off.",
    )
    include_user_context = models.BooleanField(
        default=False, verbose_name="Inject for direct calls too",
        help_text="Also inject for missed direct (User-context) calls, not just ring-group.",
    )
    debug_log_payloads = models.BooleanField(
        default=False, verbose_name="Debug: log full payloads",
        help_text="Commissioning aid (test A): records raw webhook JSON including caller "
                  "numbers. Turn OFF in normal use — numbers are otherwise masked.",
    )

    # --- Trunk-back seat ---
    trunk_server = models.CharField(
        max_length=200, default="sip.voipstudio.com", validators=[_no_linebreaks]
    )
    trunk_username = models.CharField(max_length=100, blank=True, validators=[_no_linebreaks])
    trunk_password = models.CharField(max_length=100, blank=True, validators=[_no_linebreaks])

    # --- Pushover ---
    pushover_enabled = models.BooleanField(default=False)
    pushover_user_key = models.CharField(max_length=100, blank=True, validators=[_no_linebreaks])
    pushover_api_token = models.CharField(max_length=100, blank=True, validators=[_no_linebreaks])
    alert_on_errors = models.BooleanField(
        default=False, help_text="Normal-priority push when an ERROR is recorded "
                                 "(rate-limited to one per 15 minutes).",
    )
    alert_on_new_registration = models.BooleanField(
        default=True, verbose_name="Alert on new device address",
        help_text="Push when a phone's credential authenticates from an address it has "
                  "never used before. Routine re-registrations stay silent.",
    )

    # --- Lockdown (state owned by the admin buttons, not this form) ---
    lockdown_active = models.BooleanField(default=False, editable=False)
    lockdown_auto_enabled = models.BooleanField(
        default=False, verbose_name="Auto-lockdown",
        help_text="Engage lockdown automatically when a credential authenticates from a "
                  "never-seen address. Arm AFTER first rollout. Never triggered by failed "
                  "attempts (that would let outsiders switch callbacks off).",
    )
    lockdown_suspend_until = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "configuration"
        verbose_name_plural = "configuration"

    def __str__(self) -> str:
        return "GhostSIP configuration"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "Configuration":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def clean(self) -> None:
        if self.sip_port in (8088, 8100):
            raise ValidationError({"sip_port": "Clashes with an internal service port."})
        if any(c in self.caller_name_prefix for c in '"\\\r\n'):
            raise ValidationError(
                {"caller_name_prefix": "Quotes, backslashes and line breaks are not allowed "
                                       "(this goes inside a SIP header)."}
            )
        for field in ("trunk_username",):
            if any(c in getattr(self, field) for c in "[]@ "):
                raise ValidationError(
                    {field: "Contains characters not usable in pjsip.conf."}
                )

    def lockdown_suspended(self) -> bool:
        return bool(self.lockdown_suspend_until and self.lockdown_suspend_until > timezone.now())


class Handset(models.Model):
    """One VVX. The endpoint name doubles as the SIP username on the phone's
    GhostSIP line; the password is auto-generated on add."""

    name = models.CharField(max_length=100, help_text="Free text, e.g. 'Front desk'.")
    endpoint = models.CharField(
        max_length=40, unique=True, validators=[endpoint_validator],
        help_text="PJSIP endpoint name = the SIP username on the phone (e.g. phone1).",
    )
    sip_password = models.CharField(
        max_length=100, default=generate_secret,
        help_text="Auto-generated. Copy into the phone's GhostSIP line.",
    )

    class Meta:
        ordering = ["endpoint"]

    def __str__(self) -> str:
        return f"{self.endpoint} ({self.name})"

    def clean(self) -> None:
        if any(c in self.sip_password for c in "\r\n[]"):
            raise ValidationError(
                {"sip_password": "Contains characters not usable in pjsip.conf."}
            )


class Event(models.Model):
    """Persisted activity log — replaces the old in-memory ring buffer.
    Searchable and filterable in the admin; pruned by the watcher."""

    INFO, WARNING, ERROR = "info", "warning", "error"
    LEVELS = [(INFO, "Info"), (WARNING, "Warning"), (ERROR, "Error")]
    KINDS = [
        ("webhook", "Webhook"),
        ("injection", "Injection"),
        ("security", "Security"),
        ("lockdown", "Lockdown"),
        ("system", "System"),
    ]

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(max_length=10, choices=LEVELS, db_index=True)
    kind = models.CharField(max_length=12, choices=KINDS, db_index=True)
    message = models.TextField()

    class Meta:
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"[{self.level}] {self.message[:80]}"

    @classmethod
    def log(cls, level: str, kind: str, message: str) -> "Event":
        """Record an event, echo it to stdout for `docker compose logs`, and
        (for errors) offer it to the optional rate-limited Pushover alert."""
        print(f"{timezone.now():%Y-%m-%d %H:%M:%S} {level.upper()} [{kind}] {message}", flush=True)
        event = cls.objects.create(level=level, kind=kind, message=message)
        if level == cls.ERROR:
            from panel.services import alerts  # local import avoids a cycle

            alerts.notify_error(Configuration.load(), message)
        return event


class InjectedCall(models.Model):
    """Dedup guard: one ghost injection per root_call_id, enforced by the
    database so it holds across gunicorn workers (the old in-memory dict
    could not). Rows are pruned after a day by the watcher."""

    root_call_id = models.CharField(max_length=64, unique=True)
    created = models.DateTimeField(auto_now_add=True)


class KnownAddress(models.Model):
    """Source addresses each endpoint has successfully authenticated from —
    backs the new-device-address alert and the auto-lockdown trigger."""

    endpoint = models.CharField(max_length=100)
    ip = models.GenericIPAddressField()
    first_seen = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["endpoint", "ip"], name="unique_endpoint_ip")
        ]
        verbose_name_plural = "known addresses"

    def __str__(self) -> str:
        return f"{self.endpoint} @ {self.ip}"
