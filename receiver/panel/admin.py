"""GhostSIP admin. Nearly everything here is django.contrib.admin doing the
work; the custom parts are the pjsip regeneration hook on save and four
buttons (lockdown engage/lift, suspend-1h, reload Asterisk) added to the
Configuration page via get_urls + a small template override."""

from __future__ import annotations

from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils import timezone

from panel.models import Configuration, Event, Handset, KnownAddress
from panel.services import ari, lockdown, pjsip


def _regenerate_pjsip(request) -> None:
    try:
        pjsip.write(Configuration.load(), Handset.objects.all())
        messages.info(request, "pjsip.conf regenerated — press “Reload Asterisk” to apply.")
    except OSError as exc:
        messages.error(request, f"Could not write pjsip.conf: {exc}")


@admin.register(Configuration)
class ConfigurationAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Status", {"fields": ["lockdown_status"]}),
        ("Webhook (from VoIPstudio)", {"fields": ["webhook_username", "webhook_password"]}),
        ("SIP listener", {"fields": ["sip_port"]}),
        ("Ghost-call behaviour", {
            "fields": ["ring_seconds", "caller_name_prefix", "alert_info",
                        "include_user_context", "debug_log_payloads"],
        }),
        ("Trunk-back seat (VoIPstudio callback relay)", {
            "fields": ["trunk_server", "trunk_username", "trunk_password"],
        }),
        ("Pushover alerts", {
            "fields": ["pushover_enabled", "pushover_user_key", "pushover_api_token",
                        "alert_on_new_registration", "alert_on_errors"],
        }),
        ("Lockdown", {"fields": ["lockdown_auto_enabled"]}),
    ]
    readonly_fields = ["lockdown_status"]
    change_form_template = "admin/panel/configuration/change_form.html"

    @admin.display(description="Lockdown status")
    def lockdown_status(self, obj: Configuration) -> str:
        if obj.lockdown_active:
            return "ACTIVE — outbound callbacks suspended"
        state = "armed" if obj.lockdown_auto_enabled else "off"
        if obj.lockdown_suspended():
            until = timezone.localtime(obj.lockdown_suspend_until).strftime("%H:%M")
            return f"clear — auto-lockdown suspended until {until}"
        return f"clear — auto-lockdown {state}"

    # Singleton: no add/delete; the changelist goes straight to the row.
    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def changelist_view(self, request, extra_context=None):
        config = Configuration.load()
        return HttpResponseRedirect(
            reverse("admin:panel_configuration_change", args=[config.pk])
        )

    def save_model(self, request, obj, form, change) -> None:
        super().save_model(request, obj, form, change)
        Event.log(Event.INFO, "system", "Configuration saved via admin")
        _regenerate_pjsip(request)

    # --- custom buttons -----------------------------------------------------
    def get_urls(self):
        wrap = self.admin_site.admin_view
        extra = [
            path("lockdown/engage/", wrap(self.engage_view), name="panel_lockdown_engage"),
            path("lockdown/lift/", wrap(self.lift_view), name="panel_lockdown_lift"),
            path("lockdown/suspend/", wrap(self.suspend_view), name="panel_lockdown_suspend"),
            path("reload-asterisk/", wrap(self.reload_view), name="panel_reload_asterisk"),
        ]
        return extra + super().get_urls()

    def _back(self):
        return HttpResponseRedirect(
            reverse("admin:panel_configuration_change", args=[Configuration.load().pk])
        )

    def engage_view(self, request):
        if request.method == "POST":
            applied = lockdown.set_active(True, "engaged manually in the admin.")
            if applied:
                messages.warning(request, "Lockdown engaged — outbound callbacks suspended.")
            else:
                messages.error(request, "Lockdown saved but Asterisk did not accept the "
                                        "variable — it will be re-asserted; check Events.")
        return self._back()

    def lift_view(self, request):
        if request.method == "POST":
            lockdown.set_active(False, "lifted in the admin.")
            messages.success(request, "Lockdown lifted — outbound callbacks restored.")
        return self._back()

    def suspend_view(self, request):
        if request.method == "POST":
            lockdown.suspend_auto()
            messages.info(request, "Auto-lockdown suspended for 1 hour. "
                                   "New-address alerts still send.")
        return self._back()

    def reload_view(self, request):
        if request.method == "POST":
            ok, detail = ari.reload_pjsip()
            if ok:
                Event.log(Event.INFO, "system", "Asterisk PJSIP reloaded from admin")
                messages.success(request, f"Asterisk reloaded: {detail}")
            else:
                Event.log(Event.ERROR, "system", f"Asterisk reload failed: {detail}")
                messages.error(request, f"Asterisk reload failed: {detail}")
        return self._back()


@admin.register(Handset)
class HandsetAdmin(admin.ModelAdmin):
    list_display = ["endpoint", "name"]
    search_fields = ["endpoint", "name"]

    def save_model(self, request, obj, form, change) -> None:
        super().save_model(request, obj, form, change)
        Event.log(Event.INFO, "system", f"Handset {obj.endpoint!r} saved via admin")
        _regenerate_pjsip(request)

    def delete_model(self, request, obj) -> None:
        super().delete_model(request, obj)
        Event.log(Event.INFO, "system", f"Handset {obj.endpoint!r} deleted via admin")
        _regenerate_pjsip(request)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["created", "level", "kind", "message"]
    list_filter = ["level", "kind"]
    search_fields = ["message"]
    date_hierarchy = "created"
    ordering = ["-created"]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False  # read-only viewer


@admin.register(KnownAddress)
class KnownAddressAdmin(admin.ModelAdmin):
    list_display = ["endpoint", "ip", "first_seen"]
    search_fields = ["endpoint", "ip"]

    def has_add_permission(self, request) -> bool:
        return False


admin.site.site_header = "GhostSIP administration"
admin.site.site_title = "GhostSIP"
admin.site.index_title = "GhostSIP"
