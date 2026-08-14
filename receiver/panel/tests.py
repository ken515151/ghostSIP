"""GhostSIP test suite — ports every scenario proven during the FastAPI
incarnation: the QT-capture-derived webhook filtering, number normalisation,
the brute-force and new-address detectors, pjsip rendering, lockdown rules,
and model validation."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from panel.models import Configuration, Event, Handset, InjectedCall, KnownAddress
from panel.services import alerts, pjsip
from panel.services.phone import mask_caller, normalise_caller

FAIL_LINE = ('SecurityEvent="ChallengeResponseFailed",Severity="Error",Service="PJSIP",'
             'RemoteAddress="IPV4/UDP/203.0.113.9/5566",AccountID="phone1"')
AUTH_LINE = ('SecurityEvent="SuccessfulAuth",Service="PJSIP",AccountID="phone1",'
             'RemoteAddress="IPV4/UDP/81.2.3.4/5566"')


def _basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"HTTP_AUTHORIZATION": f"Basic {token}"}


def _missed(caller="441224622312", root=100, call=101, final=True, dest="in", **extra) -> dict:
    payload = {"event_name": "call.missed", "final": final, "destination": dest,
               "src": caller, "call_id": call, "root_call_id": root}
    payload.update(extra)
    return payload


class WebhookTests(TestCase):
    def setUp(self):
        self.client = Client()
        config = Configuration.load()
        config.webhook_username = "vs"
        config.webhook_password = "whsecret"
        config.save()
        Handset.objects.create(name="Front desk", endpoint="phone1", sip_password="p1")
        self.auth = _basic("vs", "whsecret")

    def post(self, payload, **kwargs):
        return self.client.post("/webhook", data=json.dumps(payload),
                                content_type="application/json", **{**self.auth, **kwargs})

    @patch("panel.views.ari.originate_ghost_call", return_value=(True, "ok"))
    def test_abandoned_call_injects_once(self, originate):
        r = self.post(_missed())
        self.assertEqual(r.json()["action"], "injected")
        originate.assert_called_once()
        self.assertEqual(originate.call_args.args[0], "phone1")
        self.assertEqual(originate.call_args.args[1], "01224622312")  # normalised

    @patch("panel.views.ari.originate_ghost_call", return_value=(True, "ok"))
    def test_per_leg_artifact_ignored(self, originate):
        r = self.post(_missed(final=False))
        self.assertEqual(r.json()["reason"], "not final (per-leg event)")
        originate.assert_not_called()

    @patch("panel.views.ari.originate_ghost_call", return_value=(True, "ok"))
    def test_duplicate_root_deduped(self, originate):
        self.post(_missed(root=200, call=201))
        r = self.post(_missed(root=200, call=202))  # second leg, same root
        self.assertEqual(r.json()["reason"], "duplicate root_call_id")
        originate.assert_called_once()

    @patch("panel.views.ari.originate_ghost_call", return_value=(True, "ok"))
    def test_outbound_and_withheld_ignored(self, originate):
        self.assertIn("destination", self.post(_missed(dest="out")).json()["reason"])
        self.assertEqual(self.post(_missed(caller="")).json()["reason"], "no caller number")
        originate.assert_not_called()

    @patch("panel.views.ari.originate_ghost_call", return_value=(True, "ok"))
    def test_context_filter(self, originate):
        r = self.post(_missed(root=300, context="User"))
        self.assertIn("context", r.json()["reason"])
        config = Configuration.load()
        config.include_user_context = True
        config.save()
        r = self.post(_missed(root=301, context="User"))
        self.assertEqual(r.json()["action"], "injected")

    def test_bad_auth_and_unparseable(self):
        r = self.client.post("/webhook", data="{}", content_type="application/json",
                             **_basic("vs", "wrong"))
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/webhook", data="not json",
                             content_type="application/json", **self.auth)
        self.assertEqual(r.status_code, 200)  # never make VoIPstudio retry-loop
        self.assertEqual(r.json()["reason"], "unparseable payload")

    def test_healthz_reveals_nothing(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})


class PhoneTests(TestCase):
    def test_normalisation(self):
        self.assertEqual(normalise_caller("441224622312"), "01224622312")
        self.assertEqual(normalise_caller("00441224622312"), "01224622312")
        self.assertEqual(normalise_caller("+441224622312"), "01224622312")
        self.assertEqual(normalise_caller("01224622312"), "01224622312")
        # header-injection attempt reduces to harmless digits
        self.assertEqual(normalise_caller('44122\r\nEvil: x"<sip:1>'), "441221")

    def test_masking(self):
        self.assertEqual(mask_caller("01224622312"), "…22312")


class DetectorTests(TestCase):
    def test_brute_force_threshold_and_window(self):
        det = alerts.BruteForceDetector(threshold=5, window=600)
        self.assertIsNone(det.feed(AUTH_LINE, now=0))  # successes don't count
        results = [det.feed(FAIL_LINE, now=i) for i in range(5)]
        self.assertTrue(all(r is None for r in results[:-1]))
        self.assertIn("5 failed SIP auth", results[-1])
        det2 = alerts.BruteForceDetector(threshold=3, window=10)
        det2.feed(FAIL_LINE, now=0)
        det2.feed(FAIL_LINE, now=1)
        self.assertIsNone(det2.feed(FAIL_LINE, now=100))  # window expired

    def test_successful_auth_parsing(self):
        self.assertEqual(alerts.parse_successful_auth(AUTH_LINE), ("phone1", "81.2.3.4"))
        self.assertIsNone(alerts.parse_successful_auth(FAIL_LINE))
        self.assertIsNone(alerts.parse_successful_auth('SecurityEvent="SuccessfulAuth" junk'))


class PjsipTests(TestCase):
    def test_render(self):
        config = Configuration.load()
        config.trunk_username = "trunk1"
        config.trunk_password = "tp"
        config.save()
        Handset.objects.create(name="A", endpoint="phone1", sip_password="s3cret")
        text = pjsip.render(Configuration.load(), Handset.objects.all())
        self.assertIn("bind=0.0.0.0:5560", text)
        self.assertIn("allow_reload=yes", text)
        self.assertIn("[phone1](vvx-endpoint)", text)
        self.assertIn("password=s3cret", text)
        self.assertIn("client_uri=sip:trunk1@sip.voipstudio.com", text)

    def test_no_trunk_section_without_username(self):
        text = pjsip.render(Configuration.load(), [])
        self.assertNotIn("voipstudio-reg", text)


class ValidationTests(TestCase):
    def test_configuration_rules(self):
        config = Configuration.load()
        config.sip_port = 8088
        with self.assertRaises(ValidationError):
            config.full_clean()
        config.sip_port = 5560
        config.caller_name_prefix = 'Tech"Rescue'
        with self.assertRaises(ValidationError):
            config.full_clean()

    def test_handset_rules(self):
        hs = Handset(name="x", endpoint="bad name]", sip_password="ok")
        with self.assertRaises(ValidationError):
            hs.full_clean()
        hs = Handset(name="x", endpoint="phone1", sip_password="bad\npw")
        with self.assertRaises(ValidationError):
            hs.full_clean()

    def test_handset_password_autogenerated(self):
        hs = Handset.objects.create(name="x", endpoint="phone9")
        self.assertGreaterEqual(len(hs.sip_password), 30)


class LockdownTests(TestCase):
    def test_suspend_window(self):
        config = Configuration.load()
        self.assertFalse(config.lockdown_suspended())
        config.lockdown_suspend_until = timezone.now() + timedelta(minutes=30)
        self.assertTrue(config.lockdown_suspended())
        config.lockdown_suspend_until = timezone.now() - timedelta(minutes=1)
        self.assertFalse(config.lockdown_suspended())

    @patch("panel.services.lockdown.ari.set_lockdown_variable", return_value=True)
    @patch("panel.services.lockdown.alerts.send", return_value=True)
    def test_set_active_persists_and_logs(self, send, set_var):
        from panel.services import lockdown as ld

        self.assertTrue(ld.set_active(True, "test", high_priority_alert=True))
        self.assertTrue(Configuration.load().lockdown_active)
        self.assertTrue(Event.objects.filter(kind="lockdown", level="warning").exists())
        ld.set_active(False, "test")
        self.assertFalse(Configuration.load().lockdown_active)


class WatcherLogicTests(TestCase):
    """New-address handling as the watcher applies it."""

    def _feed_auth(self, line: str):
        from panel.management.commands.watch_security_log import Command

        cmd = Command()
        cmd._handle_line(line, alerts.BruteForceDetector())

    @patch("panel.services.lockdown.ari.set_lockdown_variable", return_value=True)
    @patch("panel.services.alerts.send", return_value=True)
    def test_new_address_alerts_once_and_auto_lockdown(self, send, set_var):
        config = Configuration.load()
        config.lockdown_auto_enabled = True
        config.save()
        self._feed_auth(AUTH_LINE)
        self.assertTrue(KnownAddress.objects.filter(endpoint="phone1", ip="81.2.3.4").exists())
        self.assertTrue(Configuration.load().lockdown_active)  # auto-engaged
        # same address again: known, no new alert, no state change
        events_before = Event.objects.count()
        self._feed_auth(AUTH_LINE)
        self.assertEqual(Event.objects.count(), events_before)

    @patch("panel.services.lockdown.ari.set_lockdown_variable", return_value=True)
    @patch("panel.services.alerts.send", return_value=True)
    def test_suspension_blocks_auto_lockdown(self, send, set_var):
        config = Configuration.load()
        config.lockdown_auto_enabled = True
        config.lockdown_suspend_until = timezone.now() + timedelta(minutes=30)
        config.save()
        self._feed_auth(AUTH_LINE)
        self.assertFalse(Configuration.load().lockdown_active)
        # the new-address event still records
        self.assertTrue(KnownAddress.objects.filter(endpoint="phone1").exists())


class DedupHousekeepingTests(TestCase):
    def test_old_rows_pruned(self):
        from panel.management.commands.watch_security_log import Command

        stale = InjectedCall.objects.create(root_call_id="old")
        InjectedCall.objects.filter(pk=stale.pk).update(
            created=timezone.now() - timedelta(hours=48))
        InjectedCall.objects.create(root_call_id="fresh")
        Command()._housekeeping()
        self.assertFalse(InjectedCall.objects.filter(root_call_id="old").exists())
        self.assertTrue(InjectedCall.objects.filter(root_call_id="fresh").exists())
